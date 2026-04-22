"""
SegmentBuilderAgent — специализированный агент для создания сегментов аудитории.

Отличия от AnalyticsAgent:
  - Другой системный промпт: интервьюер-конструктор, не аналитик
  - Другой набор инструментов: clickhouse_query + save_segment (без python_analysis)
  - Схема БД подгружается при старте из ClickHouse (как в AnalyticsAgent)
    и кэшируется в экземпляре агента — устойчива к изменениям схемы после перезапуска
  - Компрессия токенов (4 слоя):
      1. Суммаризация предыдущих ходов → ~8 токенов вместо ~600 на ход
      2. Сжатие COUNT-результатов clickhouse_query → {"count": N} вместо ~500 токенов
      3. Промпт-кэширование (Anthropic) → ~68% экономии на повторных вызовах
      4. Компактный системный промпт (~900 токенов с живой схемой вместо ~2500)
  - Нет роутера skills (всегда один режим)
  - thread_id в SQLite имеет префикс "seg_" → не пересекается с аналитическими сессиями
"""

import json
import sqlite3
from copy import copy
from typing import Annotated, Optional, TypedDict

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_openai import ChatOpenAI
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, StateGraph
from langgraph.graph.message import add_messages
from langgraph.prebuilt import ToolNode

from config import ALLOWED_MODELS, DB_PATH, MAX_TOKENS, MODEL, MODEL_PROVIDER, OPENROUTER_API_KEY
from segment_store import _SHARED_OWNER
from tools import clickhouse_query
from tools_segmentation import _current_owner, save_segment

# ─── Tools ─────────────────────────────────────────────────────────────────────
SEGMENT_TOOLS = [clickhouse_query, save_segment]

# ─── Max history turns for segment sessions ────────────────────────────────────
# Сессии короткие (5–10 ходов), хранить все — достаточно
_MAX_SEG_TURNS = 8

# ─── System prompt template ────────────────────────────────────────────────────
# {schema_section} заполняется динамически при старте (живая схема из ClickHouse)

_SYSTEM_PROMPT_TEMPLATE = """\
Ты — специалист по сегментации аудитории. Твоя единственная задача — помочь маркетологу \
создать точный, проверенный сегмент пользователей и сохранить его.

## Схема базы данных

{schema_section}

## Алгоритм работы

### Шаг 1 — Собери информацию (вопросы по одному-два за раз)

Обязательно уточни:
1. **Имя сегмента** — как маркетолог хочет его называть?
2. **Цель** — для чего сегмент: ретаргетинг, отчёт, атрибуция?
3. **Временное окно** — последние N дней / конкретный период / когорта / всё время?
4. **Кто входит** — купившие или нет? Сколько визитов? Какое устройство? Из каких источников?
5. **Что делали** — смотрели конкретные категории? Достигали целей Метрики?
6. **Geography** — конкретные города или все?

Задавай вопросы по 1–2 за раз. Если ответ очевиден из контекста — не спрашивай.

### Шаг 2 — Сформируй SQL и проверь

После сбора информации:
1. Выбери основную таблицу (dm_client_profile для большинства случаев)
2. Напиши COUNT-запрос: `SELECT count() AS cnt FROM ... WHERE ...`
3. Вызови `clickhouse_query` с этим SQL
4. Покажи маркетологу: размер сегмента + SQL

### Шаг 3 — Покажи итоговое определение и попроси подтверждение

```
**Сегмент: [Название]**
Подход: [тип]
Период: [описание]
Условия:
  - [условие 1]
  - [условие 2]
Размер: ~[N] пользователей

[SQL для материализации — SELECT DISTINCT client_id]

Сохранить сегмент?
```

### Шаг 4 — Сохрани после явного "Да"

Только после явного подтверждения вызови `save_segment` с полным JSON-объектом.
SQL в поле `sql_query` должен возвращать `client_id` (не COUNT).

## Правила генерации SQL

### Выбор таблицы
- RFM, воронка, когорта, канал → `dm_client_profile`
- Мультиканальность, длина пути → `dm_conversion_paths`
- Покупки конкретных товаров → `dm_purchases`
- Просмотры категорий, цели Метрики → `visits`

### Временное окно
```sql
-- rolling N дней (для dm_client_profile)
WHERE days_since_last_visit <= {{N}}

-- rolling по дате визита (visits, dm_client_journey)
WHERE date >= today() - INTERVAL {{N}} DAY

-- fixed период
WHERE date BETWEEN '{{from}}' AND '{{to}}'

-- когорта по первому визиту
WHERE toYYYYMM(first_visit_date) = {{YYYYMM}}
```

### Финальный SQL (возвращает client_id)
```sql
SELECT DISTINCT client_id
FROM dm_client_profile
WHERE {{условия}}
LIMIT 500000
```

## Ограничения
- Никогда не сохраняй без явного подтверждения пользователя
- Никогда не сохраняй без проверенного COUNT-запроса
- Для канальной сегментации уточни: first_touch, last_touch или any_touch
- Максимум без LIMIT — 500K строк

## Стиль
- Вопросы задавай коротко и по-русски
- Не объясняй техническую реализацию, говори о бизнес-смысле
- После сохранения: "Сегмент сохранён. Теперь вы можете: ..."
"""


# ─── State ──────────────────────────────────────────────────────────────────────

class SegmentAgentState(TypedDict):
    messages: Annotated[list, add_messages]


# ─── Compression helpers ────────────────────────────────────────────────────────

def _extract_count_from_tool_result(content: str) -> Optional[int]:
    """
    Если clickhouse_query вернул результат COUNT-запроса (одна строка, одна колонка),
    извлечь числовое значение. Возвращает None если это не COUNT-запрос.
    """
    try:
        data = json.loads(content)
        cols = data.get("columns") or []
        stats = data.get("col_stats") or {}
        rc = data.get("row_count")
        # COUNT-запрос → одна строка, одна колонка (cnt/count/c)
        if rc == 1 and len(cols) == 1:
            col_name = cols[0]
            col_stat = stats.get(col_name) or {}
            # min == max для COUNT (все строки одинаковы)
            mn = col_stat.get("min")
            mx = col_stat.get("max")
            if mn is not None and mn == mx:
                return int(mn)
    except Exception:
        pass
    return None


def _compress_seg_tool_message(msg: ToolMessage) -> ToolMessage:
    """
    Сжать ToolMessage из clickhouse_query для предыдущих ходов.
    COUNT-запросы → {"count": N}
    Обычные запросы → row_count + columns (без col_stats, без parquet_path)
    """
    tool_name = getattr(msg, "name", "") or ""
    if tool_name != "clickhouse_query":
        return msg

    try:
        data = json.loads(msg.content)
        count = _extract_count_from_tool_result(msg.content)
        if count is not None:
            new_content = json.dumps({"count": count})
        else:
            new_content = json.dumps(
                {
                    "row_count": data.get("row_count"),
                    "columns": data.get("columns"),
                },
                ensure_ascii=False,
            )

        try:
            return msg.model_copy(update={"content": new_content})
        except Exception:
            new_msg = copy(msg)
            new_msg.content = new_content
            return new_msg
    except Exception:
        return msg


def _group_into_turns(messages: list) -> list[list]:
    """Разбить плоский список сообщений на ходы (каждый начинается с HumanMessage)."""
    turns: list[list] = []
    current: list = []
    for msg in messages:
        if isinstance(msg, HumanMessage):
            if current:
                turns.append(current)
            current = [msg]
        else:
            current.append(msg)
    if current:
        turns.append(current)
    return turns


def _summarize_seg_turn(turn_msgs: list) -> list:
    """
    Сжать предыдущий ход сегментатора:
      COUNT-запрос → "SQL: SELECT count()… | COUNT=N"
      Финальный текст агента → сохраняется вербатим (вопросы к пользователю важны)
    """
    human_msg: Optional[HumanMessage] = None
    sql_snippet = ""
    count_result: Optional[int] = None
    final_ai_msg: Optional[AIMessage] = None

    for msg in turn_msgs:
        if isinstance(msg, HumanMessage):
            human_msg = msg

        elif isinstance(msg, AIMessage):
            for tc in getattr(msg, "tool_calls", []):
                if tc.get("name") == "clickhouse_query" and not sql_snippet:
                    sql = (tc.get("args") or {}).get("sql", "")
                    if sql:
                        sql_snippet = sql[:100] + ("…" if len(sql) > 100 else "")

            if not getattr(msg, "tool_calls", None):
                content = msg.content
                has_text = (isinstance(content, str) and content.strip()) or (
                    isinstance(content, list)
                    and any(
                        isinstance(b, dict) and b.get("type") == "text" and b.get("text", "").strip()
                        for b in content
                    )
                )
                if has_text:
                    final_ai_msg = msg

        elif isinstance(msg, ToolMessage):
            if (getattr(msg, "name", "") or "") == "clickhouse_query" and count_result is None:
                count_result = _extract_count_from_tool_result(msg.content)

    result: list = []
    if human_msg is not None:
        result.append(human_msg)

    tool_parts: list[str] = []
    if sql_snippet:
        tool_parts.append(f"SQL: {sql_snippet}")
    if count_result is not None:
        tool_parts.append(f"COUNT={count_result}")
    if tool_parts:
        result.append(AIMessage(content=" | ".join(tool_parts)))

    if final_ai_msg is not None:
        result.append(final_ai_msg)

    return result if result else []


# ─── Agent class ────────────────────────────────────────────────────────────────

class SegmentBuilderAgent:
    """
    Специализированный агент для создания и сохранения сегментов аудитории.

    Схема БД загружается при инициализации (как в AnalyticsAgent) и встраивается
    в системный промпт — агент адаптируется к изменениям схемы при перезапуске.
    """

    def __init__(self, model: str = MODEL) -> None:
        if not OPENROUTER_API_KEY:
            raise ValueError("OPENROUTER_API_KEY is not set")

        # ── Resolve provider from model name ─────────────────────────────
        provider = ALLOWED_MODELS.get(model, MODEL_PROVIDER)

        # ── LLM (тот же endpoint, что и основной агент) ───────────────────
        kwargs: dict = dict(
            model=model,
            api_key=OPENROUTER_API_KEY,
            base_url="https://openrouter.ai/api/v1",
            max_tokens=MAX_TOKENS,
            default_headers={
                "HTTP-Referer": "https://server.asktab.ru",
                "X-Title": "ClickHouse Segment Builder",
            },
        )
        if provider == "anthropic":
            kwargs["extra_body"] = {
                "provider": {
                    "order": ["Anthropic"],
                    "allow_fallbacks": False,
                }
            }
        self.llm = ChatOpenAI(**kwargs)

        # ── SqliteSaver (тот же DB_PATH, отдельный connection) ────────────
        conn = sqlite3.connect(str(DB_PATH), check_same_thread=False)
        self.memory = SqliteSaver(conn)

        # ── Динамическая загрузка схемы ───────────────────────────────────
        # Схема загружается один раз при старте и кэшируется.
        # При изменении схемы — перезапустить сервер.
        self.schema_section = self._fetch_schema_section()

        # ── Системный промпт с живой схемой ──────────────────────────────
        self._system_prompt = _SYSTEM_PROMPT_TEMPLATE.format(
            schema_section=self.schema_section,
        )

        # ── Флаг провайдера для промпт-кэширования ─────────────────────
        is_anthropic = provider == "anthropic"

        # ── Граф ──────────────────────────────────────────────────────────
        llm_with_tools = self.llm.bind_tools(SEGMENT_TOOLS)

        def _build_messages(state: SegmentAgentState) -> list:
            """
            Строит оптимизированный список сообщений для LLM.

            Слои компрессии:
              1. Sliding window — последние _MAX_SEG_TURNS ходов
              2. Суммаризация предыдущих ходов → ~8 токенов на ход
              3. Сжатие COUNT-результатов текущего хода → {"count": N}
              4. Промпт-кэширование (Anthropic only)
            """
            messages = state.get("messages", [])

            # ── 1. Sliding window ─────────────────────────────────────────
            human_indices = [i for i, m in enumerate(messages) if isinstance(m, HumanMessage)]
            if len(human_indices) > _MAX_SEG_TURNS:
                cutoff = human_indices[-_MAX_SEG_TURNS]
                messages = messages[cutoff:]

            # ── Граница текущего хода ─────────────────────────────────────
            current_turn_start = 0
            for i, msg in enumerate(messages):
                if isinstance(msg, HumanMessage):
                    current_turn_start = i

            # ── 2. Суммаризация предыдущих ходов ─────────────────────────
            prev_turns = _group_into_turns(messages[:current_turn_start])
            compressed_prev: list = []
            for turn in prev_turns:
                compressed_prev.extend(_summarize_seg_turn(turn))

            # ── 3. Сжатие tool results в текущем ходу ────────────────────
            # Если агент уже вызвал clickhouse_query и смотрит на результат,
            # а затем делает ещё один вызов — предыдущий COUNT уже учтён,
            # сжимаем его до {"count": N}.
            current_msgs = messages[current_turn_start:]
            tool_positions = [
                i for i, m in enumerate(current_msgs)
                if isinstance(m, ToolMessage)
                and (getattr(m, "name", "") or "") == "clickhouse_query"
            ]
            compressed_current: list = []
            for i, msg in enumerate(current_msgs):
                # Сжимаем tool result если после него есть ещё tool calls
                if (
                    isinstance(msg, ToolMessage)
                    and (getattr(msg, "name", "") or "") == "clickhouse_query"
                    and any(j > i for j in tool_positions)
                ):
                    compressed_current.append(_compress_seg_tool_message(msg))
                else:
                    compressed_current.append(msg)

            # ── 4. Промпт-кэширование (Anthropic only) ───────────────────
            # Системный промпт с живой схемой — кэшируем.
            # Последнее сообщение истории — кэшируем как второй breakpoint.
            if is_anthropic and compressed_prev:
                last_hist = compressed_prev[-1]
                content = last_hist.content
                if isinstance(content, str) and content:
                    new_content: list = [
                        {"type": "text", "text": content, "cache_control": {"type": "ephemeral"}}
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
                        compressed_prev[-1] = last_hist.model_copy(update={"content": new_content})
                    except Exception:
                        new_msg = copy(last_hist)
                        new_msg.content = new_content
                        compressed_prev[-1] = new_msg

            # ── Сборка финального списка ──────────────────────────────────
            sys_msg = SystemMessage(content=self._system_prompt)
            if is_anthropic:
                # Системный промпт с cache_control
                sys_msg = SystemMessage(
                    content=[
                        {
                            "type": "text",
                            "text": self._system_prompt,
                            "cache_control": {"type": "ephemeral"},
                        }
                    ]
                )

            return [sys_msg] + compressed_prev + compressed_current

        def agent_node(state: SegmentAgentState) -> dict:
            msgs = _build_messages(state)
            response = llm_with_tools.invoke(msgs)
            return {"messages": [response]}

        def should_continue(state: SegmentAgentState) -> str:
            last = state["messages"][-1]
            if hasattr(last, "tool_calls") and last.tool_calls:
                return "tools"
            return END

        graph = StateGraph(SegmentAgentState)
        graph.add_node("agent", agent_node)
        graph.add_node("tools", ToolNode(SEGMENT_TOOLS))
        graph.set_entry_point("agent")
        graph.add_conditional_edges("agent", should_continue, {"tools": "tools", END: END})
        graph.add_edge("tools", "agent")
        self.graph = graph.compile(checkpointer=self.memory)

        print(f"✅ SegmentBuilderAgent ready | provider: {provider} | model: {model} | schema: {self.schema_section[:60]}…")

    # ─── Schema fetch ───────────────────────────────────────────────────────────

    def _fetch_schema_section(self) -> str:
        """
        Загрузить схему БД из ClickHouse и вернуть отформатированную секцию.

        Повторяет подход из AnalyticsAgent._fetch_schema_section() — схема кэшируется
        в экземпляре и обновляется при перезапуске сервера.
        """
        try:
            from tools import _get_ch_client
            tables = _get_ch_client().list_tables()
            lines = []
            for t in tables:
                cols = t.get("columns", [])
                if cols and isinstance(cols[0], dict):
                    col_parts = [f"{c['name']} {c.get('type', '')}" for c in cols]
                else:
                    col_parts = [str(c) for c in cols]
                lines.append(f"**{t['table']}**: {', '.join(col_parts)}")
            schema_block = "\n".join(lines)
            print(f"✅ SegmentBuilder: schema loaded ({len(tables)} tables)")
            return "Схема таблиц (загружена при старте агента):\n\n" + schema_block
        except Exception as exc:
            print(f"⚠️  SegmentBuilder: could not fetch schema: {exc}")
            return (
                "Схема недоступна при старте. "
                "Используй знания о таблицах dm_client_profile, dm_client_journey, "
                "dm_conversion_paths, dm_purchases, visits из документации."
            )

    # ─── Public API ─────────────────────────────────────────────────────────────

    def chat(self, user_message: str, session_id: str, owner: str = _SHARED_OWNER) -> dict:
        """
        Один ход в диалоге сегментации.

        session_id хранится на фронтенде и передаётся при каждом запросе.
        thread_id в LangGraph = "seg_{session_id}" (не пересекается с аналитикой).
        owner — значение X-User-Id из API-запроса; изолирует сегменты по пользователям.
        """
        config = {
            "configurable": {"thread_id": f"seg_{session_id}"},
            # first_agent(1) + N cycles × 2 + запас(5)
            "recursion_limit": 2 + _MAX_SEG_TURNS * 2 + 5,  # = 23
        }
        # Устанавливаем owner в ContextVar перед вызовом графа.
        # save_segment tool читает его оттуда — LLM не может его подменить.
        token = _current_owner.set(owner)
        try:
            result = self.graph.invoke(
                {"messages": [HumanMessage(content=user_message)]},
                config=config,
            )
            messages: list = result.get("messages", [])
            text_output = self._extract_final_text(messages)
            segment_saved = any(
                isinstance(msg, ToolMessage)
                and (getattr(msg, "name", "") or "") == "save_segment"
                and '"success": true' in (msg.content or "")
                for msg in messages
            )
            return {
                "success": True,
                "session_id": session_id,
                "text_output": text_output,
                "segment_saved": segment_saved,
                "error": None,
            }
        except Exception as exc:
            import traceback as tb
            return {
                "success": False,
                "session_id": session_id,
                "text_output": "",
                "segment_saved": False,
                "error": str(exc),
                "traceback": tb.format_exc(),
            }
        finally:
            _current_owner.reset(token)

    def get_session_history(self, session_id: str) -> list[dict]:
        """Вернуть историю диалога в виде [{role, content}] для фронтенда."""
        try:
            config = {"configurable": {"thread_id": f"seg_{session_id}"}}
            state = self.graph.get_state(config)
            msgs = state.values.get("messages", []) if state and state.values else []
            history = []
            for msg in msgs:
                if isinstance(msg, HumanMessage):
                    history.append({
                        "role": "user",
                        "content": msg.content if isinstance(msg.content, str) else str(msg.content),
                    })
                elif isinstance(msg, AIMessage) and not getattr(msg, "tool_calls", None):
                    content = msg.content
                    if isinstance(content, str) and content.strip():
                        history.append({"role": "assistant", "content": content})
                    elif isinstance(content, list):
                        text = "\n".join(
                            b.get("text", "") for b in content
                            if isinstance(b, dict) and b.get("type") == "text"
                        ).strip()
                        if text:
                            history.append({"role": "assistant", "content": text})
            return history
        except Exception:
            return []

    @staticmethod
    def _extract_final_text(messages: list) -> str:
        for msg in reversed(messages):
            if isinstance(msg, AIMessage):
                content = msg.content
                if isinstance(content, str) and content.strip():
                    return content
                if isinstance(content, list):
                    parts = [
                        b.get("text", "") for b in content
                        if isinstance(b, dict) and b.get("type") == "text"
                    ]
                    text = "\n".join(parts).strip()
                    if text:
                        return text
        return ""


# ─── Per-model singleton cache ────────────────────────────────────────────────
_segment_agents: dict[str, SegmentBuilderAgent] = {}


def get_segment_agent(model: Optional[str] = None) -> SegmentBuilderAgent:
    """Return (or create) a cached SegmentBuilderAgent instance for the given model."""
    key = model or MODEL
    if key not in _segment_agents:
        _segment_agents[key] = SegmentBuilderAgent(model=key)
    return _segment_agents[key]
