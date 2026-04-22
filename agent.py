"""
LangGraph-based ClickHouse Analytics Agent — with dynamic Skills routing.

Architecture:
  - LLM    : Claude Sonnet 4.6 via OpenRouter (ChatOpenAI adapter)
  - Router : Claude Haiku 4.5 — classifies query → loads relevant skill .md files
  - Graph  : StateGraph (router_node → agent_node ⇄ tools_node)
  - Memory : SqliteSaver checkpointer — persists full conversation per session_id
  - Tools  : list_tables (fallback), clickhouse_query, python_analysis

Skills system:
  The router runs before every user turn and selects which skill instruction
  files to inject into the system prompt.  This keeps the base prompt at
  ~1 500 tokens while adding only the relevant domain instructions (~500–800
  tokens per skill) for the current query.

  New skills: add a .md file + one entry in skills/_registry.py — no code change.

Context optimisations (in _build_messages, a per-instance closure):
  1. Static schema embedded in system prompt at startup — no list_tables round-trip.
  2. Sliding window: only last MAX_HISTORY_TURNS human turns kept in context.
  3. Turn summarisation: each previous turn's internal tool-call chain (AIMessage+
     tool_calls + ToolMessages) is replaced by a compact SQL/row-count AIMessage.
     The final agent answer (shown to user) is preserved verbatim.
     Tool chain: ~830 tokens → ~50 tokens; final answer kept as-is.
  4. Intra-turn ToolMessage compression: within the current request, a
     clickhouse_query ToolMessage is compressed (col_stats stripped, only
     row_count/columns/parquet_path kept) as soon as any AIMessage follows it —
     i.e. the LLM already consumed col_stats once, so it is not needed again.
     python_analysis ToolMessages are compressed only when a later python_analysis
     call follows (retry scenario).
  5. Prompt caching: system prompt + last history message marked with cache_control
     (Anthropic via OpenRouter) — ~68 % input-token savings across tool calls.
"""

import json
import time
import sqlite3
from copy import copy
from typing import Annotated, Optional, TypedDict

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_openai import ChatOpenAI
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, StateGraph
from langgraph.graph.message import add_messages
from langgraph.prebuilt import ToolNode

from config import (
    ALLOWED_MODELS,
    DB_PATH,
    MAX_AGENT_ITERATIONS,
    MAX_HISTORY_TURNS,
    MAX_TOKENS,
    MODEL,
    MODEL_PROVIDER,
    OPENROUTER_API_KEY,
    ROUTER_MODEL,
    TEMP_DIR,
    TEMP_FILE_TTL_SECONDS,
)
from message_utils import (
    build_schema_block,
    compress_tool_message,
    group_into_turns,
    summarize_previous_turn,
)
from tools import TOOLS
import router as skill_router
from skills._registry import SKILLS, load_skill_instructions

# ─── Agent state ──────────────────────────────────────────────────────────────

class AgentState(TypedDict):
    messages: Annotated[list, add_messages]  # reducer: накопление сообщений
    active_skills: list[str]                  # имена активных skills
    skill_instructions: str                   # объединённые инструкции skills


# ─── Context compression helpers ──────────────────────────────────────────────
# Shared implementations live in message_utils.py.
# Local aliases keep call-sites unchanged.
_compress_tool_message = compress_tool_message
_group_into_turns = group_into_turns
_summarize_previous_turn = summarize_previous_turn
_build_schema_block = build_schema_block


# ─── Core system prompt ────────────────────────────────────────────────────────
# Компактная база (~1 500 токенов). Детальные правила SQL/Python/визуализации
# и доменные инструкции загружаются динамически через {skill_section}.

_SYSTEM_PROMPT_CORE = """Ты — лучший в мире аналитик рекламных данных. Работаешь с ClickHouse-базой компании.
Твоя задача — отвечать на вопросы маркетолога по данным: трафик, покупки, кампании, поведение клиентов.

Стиль работы: ты внутри рабочего процесса — маркетолог работает с данными каждый день, задаёт много вопросов подряд, возвращается к предыдущим темам, уточняет. Ты часть этого потока, не разовый отчёт. Отвечай коротко и по делу — как коллега, который уже в контексте.

## Схема базы данных

{schema_section}

## Принцип работы

Ты ведёшь расследование, а не отвечаешь на изолированные вопросы. Держи нить:
* Помни что уже выяснили в этой сессии — не повторяй, опирайся
* Если данные противоречат здравому смыслу — скажи первым, не жди вопроса
* После ответа — одной строкой назови следующий логичный шаг. Говори "следующий шаг: X", не спрашивай "хочешь посмотреть?"
* Не принимай данные за истину без проверки: аномалия, малая выборка, методология фильтрации — всё под сомнением пока не объяснено

## Рабочий процесс

### 1. Понять запрос — определи тип:
- **Факт** ("сколько", "покажи", "топ") → одна цифра или таблица, без выводов
- **Анализ** ("почему", "сравни", "есть ли разница") → данные + 1–2 инсайта
- **Интерпретация** ("это норма?", "хорошо или плохо?") → одна витрина + маркетинговая логика
- **Drill-down** ("разбери", "детализируй") → до первого запроса определи структуру финального ответа
- **Уточнение** к предыдущему → сначала проверь, можно ли ответить из уже выгруженных данных

### 1.5. Оценить объём

Лимит итераций инструментов — оцени объём до первого вызова:
- Если укладываешься → выполняй полностью
- Если не укладываешься → раздели на логически завершённые части. Каждая часть самодостаточна: законченная таблица, законченный вывод.
  Начни: "Задача большая, разобью на N частей. Сейчас — часть 1: [что делаю]."
  В конце: "⏭ Часть [X] из [N]: [что будет дальше]"

### 2. Схема таблиц

Схема базы данных уже в промпте выше — НЕ вызывай list_tables.
Используй list_tables только если схема кажется неполной или таблица не найдена.

### 3. Выгрузить данные → 4. Проанализировать → 5. Сформировать ответ

(Детальные правила для этих шагов — в активных skills ниже, если загружены)

Формат финального ответа:
* Факт → прямой ответ первым предложением. Таблица если нужна. Всё.
* Анализ → данные + максимум 2 инсайта. Без раздела "Ключевые выводы" если инсайт уже в таблице
* Интерпретация → вывод + маркетинговая логика. Без домыслов о бизнесе
* Drill-down → полная детализация, гипотезы, объяснение аномалий

Рекомендации — только при трёх условиях: данные есть, канал виден в данных, есть CR или spend.
Если вопрос требует данных которых нет в витринах — скажи прямо: "Для этого нужен Директ. Сейчас недоступен."

## Справочник значений полей

### deviceCategory — тип устройства
1 — десктоп, 2 — мобильные телефоны, 3 — планшеты, 4 — TV
При фильтрации/группировке — расшифровывай цифры в читаемые названия.

## Расхождение визитов между витринами — норма

dm_traffic_performance считает ВСЕ визиты, включая анонимные (clientID = 0).
dm_client_journey / dm_client_profile / dm_ml_features — только clientID > 0.
Разница = анонимные сессии. Это архитектурное решение, не ошибка данных.

# Товарная аналитика

⚠️ Никогда не используй ARRAY JOIN на таблице visits для товарных данных — только витрины dm_orders, dm_purchases, dm_products.
dm_orders (1 строка = 1 заказ) → dm_purchases (1 строка = 1 позиция) → dm_products (1 строка = 1 товар, агрегат за всё время). Связь: dm_purchases.order_id = dm_orders.order_id.
Для рейтингов и топов — dm_products (без JOIN). Для детализации по позициям — dm_purchases JOIN dm_orders. Для выручки заказов и атрибуции (first/last touch) — dm_orders.
dm_purchases не содержит utm/device/city — они только в dm_orders.

## Стиль ответа
* Markdown: заголовки ##/###, таблицы, **жирный** для ключевых цифр
* Эмодзи — только ⚠️ для предупреждений. Больше нигде
* Числа с разделителями тысяч: 1 234 567
* Язык — русский
* Конкретика: цифры, динамика, сравнение — без воды
* Каждое слово в выводе должно нести смысл. Никаких итоговых блоков с эмодзи, повторов, обобщений ради обобщений.
{skill_section}"""


# ─── LLM factory ──────────────────────────────────────────────────────────────

_OPENROUTER_HEADERS = {
    "HTTP-Referer": "https://server.asktab.ru",
    "X-Title": "ClickHouse Analytics Agent",
}


def _create_llm(model: str, provider: str) -> ChatOpenAI:
    """
    Return a ChatOpenAI client pointed at OpenRouter for the given model/provider.

    OpenRouter exposes a single OpenAI-compatible endpoint for all providers.
    Prompt caching for Claude models is supported: OpenRouter forwards
    cache_control blocks to Anthropic transparently.
    Provider pinning prevents round-robin across Anthropic instances
    (different instances don't share the prompt cache).
    """
    kwargs: dict = dict(
        model=model,
        api_key=OPENROUTER_API_KEY,
        base_url="https://openrouter.ai/api/v1",
        max_tokens=MAX_TOKENS,
        default_headers=_OPENROUTER_HEADERS,
    )
    if provider == "anthropic":
        kwargs["extra_body"] = {
            "provider": {
                "order": ["Anthropic"],
                "allow_fallbacks": False,
            },
        }
    return ChatOpenAI(**kwargs)


class AnalyticsAgent:
    """
    Wraps LangGraph StateGraph agent with:
      - Claude Sonnet 4.6 (prompt caching) or DeepSeek, both via OpenRouter
      - Claude Haiku 4.5 router for dynamic skill selection
      - SqliteSaver for session memory
      - Dynamic system prompt: core (~1 500 tokens) + skill instructions on demand
      - Per-request context optimisation: sliding window + message compression
    """

    def __init__(self, model: str = MODEL) -> None:
        if not OPENROUTER_API_KEY:
            raise ValueError(
                "OPENROUTER_API_KEY is not set in .env. "
                "Get your key at https://openrouter.ai"
            )

        # ── Resolve provider from model name ─────────────────────────────
        provider = ALLOWED_MODELS.get(model, MODEL_PROVIDER)

        # ── LLM via OpenRouter ────────────────────────────────────────────
        self.llm = _create_llm(model, provider)

        # ── SqliteSaver checkpointer ──────────────────────────────────────
        conn = sqlite3.connect(str(DB_PATH), check_same_thread=False)
        conn.execute("PRAGMA busy_timeout = 5000")  # wait up to 5s on lock
        self.memory = SqliteSaver(conn)

        # ── Fetch schema once at startup ──────────────────────────────────
        self.schema_section = self._fetch_schema_section()

        # ── Provider flag for prompt caching ─────────────────────────────
        is_anthropic = provider == "anthropic"

        # ── Message builder closure ───────────────────────────────────────
        # Runs before every LLM call inside agent_node.
        # Applies five layered optimisations (see module docstring).
        #
        # Key change vs. the old create_react_agent approach:
        #   system prompt is built dynamically from state["skill_instructions"]
        #   so the router's skill selection is reflected in every LLM call
        #   within the current turn.

        def _build_messages(state: AgentState) -> list:
            messages = state.get("messages", [])
            skill_instructions = state.get("skill_instructions", "")

            # ── Dynamic system prompt (core + active skills) ───────────────
            system_prompt = self._build_system_prompt(skill_instructions)

            # ── 1. Sliding window ──────────────────────────────────────────
            human_indices = [
                i for i, m in enumerate(messages) if isinstance(m, HumanMessage)
            ]
            if len(human_indices) > MAX_HISTORY_TURNS:
                cutoff = human_indices[-MAX_HISTORY_TURNS]
                messages = messages[cutoff:]

            # ── Locate current-turn boundary ──────────────────────────────
            current_turn_start = 0
            for i, msg in enumerate(messages):
                if isinstance(msg, HumanMessage):
                    current_turn_start = i

            # ── 2. Summarise previous turns ────────────────────────────────
            prev_turns = _group_into_turns(messages[:current_turn_start])
            compressed_prev: list = []
            for turn in prev_turns:
                compressed_prev.extend(_summarize_previous_turn(turn))

            # ── 3. Intra-turn ToolMessage compression ──────────────────────
            current_msgs = messages[current_turn_start:]
            # Positions where an AIMessage follows — used to detect that the
            # LLM already consumed a ToolMessage at least once.
            ai_positions: set[int] = {
                i
                for i, m in enumerate(current_msgs)
                if isinstance(m, AIMessage)
            }
            py_positions: set[int] = {
                i
                for i, m in enumerate(current_msgs)
                if isinstance(m, ToolMessage)
                and (getattr(m, "name", "") or "") == "python_analysis"
            }
            # Parallel clickhouse_query results: keep col_stats for ALL of them.
            # Previously only the first kept col_stats; this caused the agent to be
            # "blind" about schemas of parallel query results and resort to extra
            # python_analysis calls (or pd.read_parquet()) just to inspect types.
            # The col_stats overhead (~200–400 tok per result) is worth the reduction
            # in exploratory tool calls. Results are compressed normally once an
            # AIMessage follows them (the already_seen path below handles this).
            compress_parallel_ch: set[int] = set()

            compressed_current: list = []
            for i, msg in enumerate(current_msgs):
                name = (getattr(msg, "name", "") or "") if isinstance(msg, ToolMessage) else ""
                if isinstance(msg, ToolMessage) and name == "clickhouse_query":
                    already_seen = any(j > i for j in ai_positions)
                    if already_seen or i in compress_parallel_ch:
                        # LLM already saw col_stats, or parallel duplicate — strip col_stats
                        compressed_current.append(_compress_tool_message(msg))
                    else:
                        compressed_current.append(msg)
                elif isinstance(msg, ToolMessage) and name == "python_analysis" and any(j > i for j in py_positions):
                    compressed_current.append(_compress_tool_message(msg))
                else:
                    compressed_current.append(msg)

            # ── 3b. Iteration counter + HumanMessage cache breakpoint ──────
            tool_uses_so_far = sum(
                1 for m in compressed_current if isinstance(m, ToolMessage)
            )
            remaining = MAX_AGENT_ITERATIONS - tool_uses_so_far
            if compressed_current and isinstance(compressed_current[0], HumanMessage):
                first = compressed_current[0]
                old_content = first.content if isinstance(first.content, str) else ""
                if is_anthropic:
                    content_blocks: list = [
                        {
                            "type": "text",
                            "text": old_content,
                            "cache_control": {"type": "ephemeral"},
                        }
                    ]
                    if tool_uses_so_far > 0:
                        counter = f"[⚡ Итерации: {tool_uses_so_far}/{MAX_AGENT_ITERATIONS}, осталось: {remaining}]"
                        if remaining <= 0:
                            counter += (
                                " ⛔ ЛИМИТ ИСЧЕРПАН. Немедленно дай финальный ответ на основе уже собранных данных."
                                " НЕ вызывай инструменты. Используй только то, что уже есть в контексте."
                            )
                        elif remaining == 1:
                            counter += (
                                " 🚨 Остался 1 вызов инструмента. После него ты ОБЯЗАН дать финальный ответ."
                                " Используй последний вызов только если он критически необходим."
                            )
                        elif remaining <= 3:
                            counter += (
                                " ⚠️ Мало итераций. Если данных достаточно — отвечай сейчас."
                                " Объединяй оставшиеся запросы в один через WITH/CTE."
                            )
                        content_blocks.append({"type": "text", "text": counter})
                    try:
                        compressed_current[0] = first.model_copy(
                            update={"content": content_blocks}
                        )
                    except Exception:
                        new_first = copy(first)
                        new_first.content = content_blocks
                        compressed_current[0] = new_first
                elif tool_uses_so_far > 0:
                    counter = f"\n[⚡ Итерации: {tool_uses_so_far}/{MAX_AGENT_ITERATIONS}, осталось: {remaining}]"
                    if remaining <= 0:
                        counter += (
                            " ⛔ ЛИМИТ ИСЧЕРПАН. Немедленно дай финальный ответ на основе уже собранных данных."
                            " НЕ вызывай инструменты. Используй только то, что уже есть в контексте."
                        )
                    elif remaining == 1:
                        counter += (
                            " 🚨 Остался 1 вызов инструмента. После него ты ОБЯЗАН дать финальный ответ."
                            " Используй последний вызов только если он критически необходим."
                        )
                    elif remaining <= 3:
                        counter += (
                            " ⚠️ Мало итераций. Если данных достаточно — отвечай сейчас."
                            " Объединяй оставшиеся запросы в один через WITH/CTE."
                        )
                    try:
                        compressed_current[0] = first.model_copy(
                            update={"content": old_content + counter}
                        )
                    except Exception:
                        new_first = copy(first)
                        new_first.content = old_content + counter
                        compressed_current[0] = new_first

            # ── 4. History cache breakpoint (Anthropic only) ──────────────
            if is_anthropic and compressed_prev:
                last_hist_msg = compressed_prev[-1]
                content = last_hist_msg.content
                if isinstance(content, str) and content:
                    new_content: list | None = [
                        {
                            "type": "text",
                            "text": content,
                            "cache_control": {"type": "ephemeral"},
                        }
                    ]
                elif isinstance(content, list) and content:
                    new_content = list(content)
                    last_block = dict(new_content[-1])
                    last_block["cache_control"] = {"type": "ephemeral"}
                    new_content[-1] = last_block
                else:
                    new_content = None
                if new_content is not None:
                    try:
                        compressed_prev[-1] = last_hist_msg.model_copy(
                            update={"content": new_content}
                        )
                    except Exception:
                        new_msg = copy(last_hist_msg)
                        new_msg.content = new_content
                        compressed_prev[-1] = new_msg

            # ── 5. System prompt with cache_control (Anthropic) ───────────
            if is_anthropic:
                system_msg = SystemMessage(content=[
                    {
                        "type": "text",
                        "text": system_prompt,
                        "cache_control": {"type": "ephemeral"},
                    }
                ])
            else:
                system_msg = SystemMessage(content=system_prompt)

            return [system_msg] + compressed_prev + compressed_current

        # ── StateGraph with router → agent ⇄ tools ────────────────────────
        tool_node = ToolNode(TOOLS)

        def agent_node(state: AgentState) -> dict:
            messages = _build_messages(state)
            response = self.llm.bind_tools(TOOLS).invoke(messages)
            return {"messages": [response]}

        def force_summary_node(state: AgentState) -> dict:
            """Вызывается когда лимит итераций исчерпан.

            Делает один финальный вызов LLM *без инструментов*, чтобы получить
            структурированное резюме: что сделано, что не успел, что исследовать дальше.
            """
            messages = _build_messages(state)
            summary_request = HumanMessage(content=(
                "⛔ Лимит итераций исчерпан. Дай структурированный финальный ответ:\n\n"
                "1. **Что сделано** — какие данные собраны, какие запросы выполнены, "
                "какие результаты получены\n"
                "2. **Что не успел** — какие шаги плана остались невыполненными\n"
                "3. **Что исследовать дальше** — конкретные вопросы для следующего запроса\n\n"
                "Отвечай только текстом. Не вызывай инструменты."
            ))
            # self.llm без .bind_tools() — LLM не знает о инструментах,
            # tool_calls физически невозможны
            response = self.llm.invoke(messages + [summary_request])
            return {"messages": [response]}

        def should_continue(state: AgentState) -> str:
            last = state["messages"][-1]

            # ── Жёсткий стоп по бюджету итераций ──────────────────────────
            # Считаем ToolMessages начиная с последнего HumanMessage (текущий ход)
            messages = state["messages"]
            human_indices = [i for i, m in enumerate(messages) if isinstance(m, HumanMessage)]
            current_turn_start = human_indices[-1] if human_indices else 0
            current_tool_uses = sum(
                1 for m in messages[current_turn_start:]
                if isinstance(m, ToolMessage)
            )
            if current_tool_uses >= MAX_AGENT_ITERATIONS:
                # Лимит исчерпан — направляем в force_summary вместо тихого END.
                # Там LLM вызывается без инструментов и подводит структурированный итог.
                return "force_summary"
            # ──────────────────────────────────────────────────────────────

            if hasattr(last, "tool_calls") and last.tool_calls:
                return "tools"
            return END

        graph = StateGraph(AgentState)
        graph.add_node("router", self._router_node)
        graph.add_node("agent", agent_node)
        graph.add_node("tools", tool_node)
        graph.add_node("force_summary", force_summary_node)
        graph.set_entry_point("router")
        graph.add_edge("router", "agent")
        graph.add_conditional_edges(
            "agent",
            should_continue,
            {"tools": "tools", END: END, "force_summary": "force_summary"},
        )
        graph.add_edge("tools", "agent")
        graph.add_edge("force_summary", END)
        self.graph = graph.compile(checkpointer=self.memory)

        caching_info = "prompt caching ON" if is_anthropic else "no prompt caching"
        print(
            f"✅ AnalyticsAgent ready | provider: {provider} | model: {model} | "
            f"router: {ROUTER_MODEL} | {caching_info} | "
            f"skills: {len(SKILLS)} | db: {DB_PATH}"
        )

    # ─── Schema fetch ──────────────────────────────────────────────────────────

    def _fetch_schema_section(self) -> str:
        """Fetch DB schema once at startup and return formatted section string."""
        try:
            from tools import _get_ch_client
            tables = _get_ch_client().list_tables()
            schema_block = _build_schema_block(tables)
            print(f"✅ Schema loaded: {len(tables)} table(s) embedded in system prompt")
            return (
                "Схема таблиц (статичная, загружена при старте агента):\n\n"
                + schema_block
            )
        except Exception as exc:
            print(f"⚠️  Could not fetch schema at startup: {exc}")
            return (
                "Схема недоступна при старте. "
                "Используй инструмент `list_tables` чтобы получить список таблиц."
            )

    # ─── Dynamic system prompt ─────────────────────────────────────────────────

    def _build_system_prompt(self, skill_instructions: str = "") -> str:
        """
        Build the system prompt from the core template + active skill instructions.

        Args:
            skill_instructions: Combined text from loaded skill .md files.
                                 Empty string if no skills are active.
        """
        if skill_instructions:
            skill_section = "\n\n---\n\n## Активные инструкции (Skills)\n\n" + skill_instructions
        else:
            skill_section = ""
        return _SYSTEM_PROMPT_CORE.format(
            schema_section=self.schema_section,
            skill_section=skill_section,
        )

    # ─── Router node ───────────────────────────────────────────────────────────

    def _router_node(self, state: AgentState) -> dict:
        """
        Classify the latest user query and load relevant skill instructions.

        Runs once per user turn (at graph entry point) before agent_node.
        Updates active_skills and skill_instructions in the state.

        To handle context-dependent replies ("да", "продолжи", "ещё раз"),
        we pass selected prior messages to the router so it can infer intent.

        Selection rules (applied independently to user and assistant messages):
          - user messages   : 1st ever + last 3, full content (no truncation)
          - assistant messages: 1st ever + last 3, truncated in router
        """
        messages = state.get("messages", [])
        last_human = next(
            (m for m in reversed(messages) if isinstance(m, HumanMessage)),
            None,
        )
        if last_human is None:
            return {"active_skills": [], "skill_instructions": ""}

        query_text = last_human.content
        if isinstance(query_text, list):
            # Content block format (e.g. from multimodal messages)
            query_text = " ".join(
                b.get("text", "")
                for b in query_text
                if isinstance(b, dict) and b.get("type") == "text"
            )

        # ── Build routing context from full history ────────────────────────
        # Find all prior human/assistant messages before the current turn.
        last_human_idx = next(
            (i for i in range(len(messages) - 1, -1, -1)
             if isinstance(messages[i], HumanMessage)),
            None,
        )
        context: list[dict] = []
        if last_human_idx and last_human_idx > 0:
            prior = messages[:last_human_idx]

            # Collect with original position so we can merge back in order.
            human_msgs: list[tuple[int, str, str]] = []  # (idx, role, text)
            ai_msgs:    list[tuple[int, str, str]] = []

            for i, m in enumerate(prior):
                if isinstance(m, HumanMessage):
                    text = m.content if isinstance(m.content, str) else " ".join(
                        b.get("text", "") for b in m.content
                        if isinstance(b, dict) and b.get("type") == "text"
                    )
                    human_msgs.append((i, "user", text))
                elif isinstance(m, AIMessage) and not m.tool_calls:
                    # Only final answers, skip intermediate tool-call steps
                    text = m.content if isinstance(m.content, str) else ""
                    if text:
                        ai_msgs.append((i, "assistant", text))

            def _first_and_last3(lst: list) -> list:
                if len(lst) <= 4:
                    return lst
                seen: set[int] = {lst[0][0]}
                result = [lst[0]]
                for item in lst[-3:]:
                    if item[0] not in seen:
                        result.append(item)
                        seen.add(item[0])
                return result

            selected = sorted(
                _first_and_last3(human_msgs) + _first_and_last3(ai_msgs),
                key=lambda x: x[0],
            )
            context = [{"role": role, "content": text} for _, role, text in selected]

        active = skill_router.classify(query_text, context=context or None)
        instructions = load_skill_instructions(active)

        return {"active_skills": active, "skill_instructions": instructions}

    # ─── Public API ───────────────────────────────────────────────────────────

    def analyze(self, user_query: str, session_id: str) -> dict:
        """
        Process a user analytics query for a given session.

        Args:
            user_query: The user's question or request.
            session_id: Unique session identifier (= LangGraph thread_id).
                        Reuse the same session_id across requests to maintain context.

        Returns:
            {
              "success":    bool,
              "session_id": str,
              "text_output": str,         # Final Markdown text from the agent
              "plots":      list[str],    # base64 PNG data URIs from python_analysis
              "tool_calls": list[dict],   # Log of tool invocations
              "error":      str | None,
            }
        """
        config = {
            "configurable": {"thread_id": session_id},
            # router(1) + first_agent(1) + N cycles × 2 + запас(5) + force_summary(1)
            "recursion_limit": 2 + MAX_AGENT_ITERATIONS * 2 + 5 + 1,
        }

        try:
            result = self.graph.invoke(
                {"messages": [HumanMessage(content=user_query)]},
                config=config,
            )

            messages: list = result.get("messages", [])

            text_output = self._extract_final_text(messages)
            plots = self._extract_plots(messages)
            tool_calls = self._extract_tool_calls(messages)

            return {
                "success": True,
                "session_id": session_id,
                "text_output": text_output,
                "plots": plots,
                "tool_calls": tool_calls,
                "error": None,
                "_messages": messages,       # for passive observability logger only
                "_active_skills": result.get("active_skills", []),  # for router logging
            }

        except Exception as exc:
            import traceback as tb
            # Try to salvage accumulated messages for the logger even on error
            _err_msgs = []
            try:
                snapshot = self.graph.get_state(config)
                _err_msgs = list(snapshot.values.get("messages", []))
            except Exception:
                pass
            return {
                "success": False,
                "session_id": session_id,
                "text_output": "",
                "plots": [],
                "tool_calls": [],
                "error": str(exc),
                "traceback": tb.format_exc(),
                "_messages": _err_msgs,
            }

    def get_session_info(self, session_id: str) -> dict:
        """Return basic metadata about a session."""
        try:
            config = {"configurable": {"thread_id": session_id}}
            state = self.graph.get_state(config)
            msgs = state.values.get("messages", []) if state and state.values else []
            user_msgs = sum(1 for m in msgs if isinstance(m, HumanMessage))
            return {
                "session_id": session_id,
                "total_messages": len(msgs),
                "user_turns": user_msgs,
                "has_history": user_msgs > 0,
            }
        except Exception:
            return {
                "session_id": session_id,
                "total_messages": 0,
                "user_turns": 0,
                "has_history": False,
            }

    def cleanup_temp_files(self) -> int:
        """Delete Parquet files older than TEMP_FILE_TTL_SECONDS. Returns count deleted."""
        cutoff = time.time() - TEMP_FILE_TTL_SECONDS
        deleted = 0
        for f in TEMP_DIR.glob("*.parquet"):
            try:
                if f.stat().st_mtime < cutoff:
                    f.unlink()
                    deleted += 1
            except OSError:
                pass
        if deleted:
            print(f"🗑️  Deleted {deleted} expired parquet file(s)")
        return deleted

    # ─── Private helpers ──────────────────────────────────────────────────────

    def _extract_final_text(self, messages: list) -> str:
        """Return content of the last AIMessage that has non-empty text."""
        for msg in reversed(messages):
            if not isinstance(msg, AIMessage):
                continue
            content = msg.content
            if isinstance(content, str) and content.strip():
                return content
            if isinstance(content, list):
                parts = [
                    block["text"]
                    for block in content
                    if isinstance(block, dict) and block.get("type") == "text"
                ]
                text = "\n".join(parts).strip()
                if text:
                    return text
        return ""

    def _extract_plots(self, messages: list) -> list[str]:
        """
        Extract base64 PNG plots from python_analysis ToolMessages
        that belong to the CURRENT agent run (after the last HumanMessage).
        """
        last_human_idx = -1
        for i, msg in enumerate(messages):
            if isinstance(msg, HumanMessage):
                last_human_idx = i

        if last_human_idx < 0:
            return []

        plots: list[str] = []
        for msg in messages[last_human_idx:]:
            if not isinstance(msg, ToolMessage):
                continue
            if (getattr(msg, "name", "") or "") != "python_analysis":
                continue
            artifact = getattr(msg, "artifact", None)
            if isinstance(artifact, list):
                plots.extend(artifact)

        return plots

    def _extract_tool_calls(self, messages: list) -> list[dict]:
        """
        Extract a compact log of tool calls made during the current run.

        Each entry includes:
          - tool: tool name
          - input: args (SQL up to 2000 chars, other strings up to 500)
          - success: bool from ToolMessage (if available)
          - row_count / cached: for clickhouse_query
          - error: for failed calls
        """
        last_human_idx = -1
        for i, msg in enumerate(messages):
            if isinstance(msg, HumanMessage):
                last_human_idx = i

        if last_human_idx < 0:
            return []

        tool_results: dict[str, ToolMessage] = {}
        for msg in messages[last_human_idx:]:
            if isinstance(msg, ToolMessage):
                tc_id = getattr(msg, "tool_call_id", None)
                if tc_id:
                    tool_results[tc_id] = msg

        tool_calls: list[dict] = []
        for msg in messages[last_human_idx:]:
            if not isinstance(msg, AIMessage):
                continue
            for tc in getattr(msg, "tool_calls", []):
                name = tc.get("name", "")
                args = tc.get("args", {})
                tc_id = tc.get("id", "")
                compact_args = {
                    k: (
                        v[:2000] + "…" if k == "sql" and isinstance(v, str) and len(v) > 2000
                        else v[:500] + "…" if isinstance(v, str) and len(v) > 500
                        else v
                    )
                    for k, v in args.items()
                }
                entry: dict = {"tool": name, "input": compact_args}

                tm = tool_results.get(tc_id)
                if tm is not None:
                    try:
                        data = json.loads(tm.content)
                        entry["success"] = data.get("success")
                        if name == "clickhouse_query":
                            entry["row_count"] = data.get("row_count")
                            entry["cached"] = data.get("cached")
                            if not data.get("success"):
                                entry["error"] = data.get("error", "")
                        elif name == "python_analysis":
                            if not data.get("success"):
                                entry["error"] = data.get("error", "")
                    except Exception:
                        entry["output_raw"] = str(tm.content)[:500]

                tool_calls.append(entry)

        return tool_calls


# ─── Per-model singleton cache ────────────────────────────────────────────────
_agents: dict[str, AnalyticsAgent] = {}


def get_agent(model: Optional[str] = None) -> AnalyticsAgent:
    """Return (or create) a cached AnalyticsAgent instance for the given model."""
    key = model or MODEL
    if key not in _agents:
        _agents[key] = AnalyticsAgent(model=key)
    return _agents[key]
