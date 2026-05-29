"""
build_agent(session_id, client_id) — фабрика главного deepagents агента.

Собирает:
  - Main agent: thin orchestrator. Tools: think_tool, python_analysis (для
    post-processing parquet), sample_table (discovery 5 строк), describe_table.
    `task` tool появляется автоматически от deepagents SubAgentMiddleware и
    позволяет делегировать одному из 4 подагентов.
    БЕЗ clickhouse_query — любой полноценный SQL только через task().
  - Memory: AGENTS.md + data_map.md (всегда в system prompt через MemoryMiddleware)
  - Skills: clients/<id>/skills/ + shared_skills/ (progressive disclosure)
  - Subagents (declarative из SUBAGENT.md): direct-optimizer, scoring-intelligence,
    command-center, generalist (≈ замена бывшего custom delegate_to_generalist).
  - Backend: session-scoped CompositeBackend (/parquet/, /plots/, /memories/)
  - Middleware: DynamicContext, Caching (logging only — auto-cache настроен в
    extra_body модели), Tool/ModelCallLimit budget, HardcodeDetector, ToolExclusion.
  - Checkpointer: SqliteSaver (как в старом агенте, для истории диалогов)
"""
from __future__ import annotations

import os
import sqlite3
from pathlib import Path
from typing import Optional

from deepagents import create_deep_agent
from langchain_openai import ChatOpenAI
from langgraph.checkpoint.sqlite import SqliteSaver
from langchain.agents.middleware import ModelCallLimitMiddleware, ToolCallLimitMiddleware

from .caching_middleware import CachingMiddleware
from .dynamic_context_middleware import DynamicContextMiddleware
from .enforcement_middleware import HardcodeDetector
from .exploration_tools import make_describe_table_tool, make_sample_table_tool
from .main_response_models import MainFinalAnswer
from .schema_cache import get_schema_cache
from .session_backend import make_backend_factory
from .subagent_loader import load_subagents
from .tool_exclusion_middleware import ToolExclusionMiddleware
from .tools import clickhouse_query, list_tables, python_analysis, think_tool


# ─── Config (from env, with fallbacks mirroring legacy config.py) ───────────

_CLIENTS_ROOT = Path(__file__).resolve().parent.parent / "clients"
_DB_PATH = os.environ.get("DB_PATH") or str(
    Path(__file__).resolve().parent.parent / "chat_history.db"
)
_OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY", "")
_MODEL = os.environ.get("MODEL", "anthropic/claude-sonnet-4.6")
_MAX_TOKENS = int(os.environ.get("MAX_TOKENS", "16384"))
_MAX_ITERATIONS = int(os.environ.get("MAX_AGENT_ITERATIONS", "30"))
_MAX_SUBAGENT_ITERATIONS = int(os.environ.get("MAX_SUBAGENT_ITERATIONS", "20"))

_OPENROUTER_HEADERS = {
    "HTTP-Referer": "https://server.asktab.ru",
    "X-Title": "Magnetto Analytics Agent (deepagents)",
}


def _build_model(model_name: str) -> ChatOpenAI:
    """
    ChatOpenAI configured for OpenRouter with Anthropic provider pinning.
    Pinning is critical — без него OpenRouter round-robins across Anthropic
    edges, and each edge has its own prompt cache → cache miss every time.

    Кэширование — top-level `cache_control` в extra_body (OpenRouter automatic
    caching). Anthropic сам двигает breakpoint в конец сообщений на каждом
    запросе, так что растущая tool-цепочка инкрементально расширяет кэш без
    ручной расстановки маркеров. TTL 5 минут (1.25× write, 0.1× read).

    Important: top-level cache_control обходит баг langchain_openai
    (`_sanitize_chat_completions_content` срезает cache_control с ToolMessage
    content blocks). Top-level поле не трогается санитайзером.
    """
    if not _OPENROUTER_API_KEY:
        raise RuntimeError("OPENROUTER_API_KEY is not set")

    kwargs: dict = dict(
        model=model_name,
        api_key=_OPENROUTER_API_KEY,
        base_url="https://openrouter.ai/api/v1",
        max_tokens=_MAX_TOKENS,
        default_headers=_OPENROUTER_HEADERS,
    )
    if model_name.startswith("anthropic/"):
        kwargs["extra_body"] = {
            "provider": {
                "order": ["Anthropic"],
                "allow_fallbacks": False,
            },
            "cache_control": {"type": "ephemeral"},
        }
    return ChatOpenAI(**kwargs)


def _budget_middleware(soft_tool_calls: int, hard_model_calls: int) -> list:
    """
    Cache-safe бюджет итераций (заменяет старый prompt-инжектящий BudgetMiddleware).

    - ToolCallLimitMiddleware(exit_behavior="continue"): когда исчерпаны
      soft_tool_calls tool-вызовов, дальнейшие блокируются статичным сообщением,
      и модель пишет финальный ответ из уже собранного.
    - ModelCallLimitMiddleware(exit_behavior="end"): жёсткий backstop, гарантирует
      завершение. deepagents ставит recursion_limit=9999 на main и НЕ пробрасывает
      config в подагентов (bug #1698), поэтому лимит обязан жить в middleware.

    Оба считают счётчики в STATE и не дописывают эскалирующее предупреждение в
    хвост сообщений каждый ход, поэтому Anthropic prompt-cache префикс растёт до
    конца цикла (старый BudgetMiddleware замораживал кэш после ~10 итераций).
    """
    return [
        ToolCallLimitMiddleware(run_limit=soft_tool_calls, exit_behavior="continue"),
        ModelCallLimitMiddleware(run_limit=hard_model_calls, exit_behavior="end"),
    ]


# ─── Singleton cache: one agent per (client_id, model) ────────────────────

_AGENT_CACHE: dict[tuple, "object"] = {}


def build_agent(
    client_id: str = "magnetto",
    model: Optional[str] = None,
) -> object:
    """
    Build (or return cached) CompiledStateGraph for a given client.

    Session-awareness is achieved via:
      - checkpointer (SqliteSaver) keyed by thread_id
      - backend factory reading session_id from runtime config
      - ContextVar set by api_server before invoke()

    Args:
        client_id: directory name under clients/ (default: "magnetto")
        model:     optional model override (otherwise uses env MODEL)

    Returns:
        Compiled deepagents agent ready for .invoke({"messages": [...]}).
    """
    model_name = model or _MODEL
    cache_key = (client_id, model_name)
    if cache_key in _AGENT_CACHE:
        return _AGENT_CACHE[cache_key]

    client_dir = _CLIENTS_ROOT / client_id
    if not client_dir.exists():
        raise ValueError(f"Unknown client: {client_dir}")

    # ── Warm up schema cache once (subagents and delegate_to_generalist need it) ──
    schema_cache = get_schema_cache()
    if not schema_cache.is_loaded():
        schema_cache.load()

    # ── Model ────────────────────────────────────────────────────────────
    llm = _build_model(model_name)

    # ── Session-scoped backend factory ───────────────────────────────────
    backend_factory = make_backend_factory(client_id=client_id)

    # ── Tools for subagents (per-subagent scope) ────────────────────────
    # Фабрика: для каждого subagent собираем список tools с персональным
    # scope (sample_table + describe_table работают только с allowed_tables).
    # Это не даёт подагенту заглядывать в таблицы чужой доменной зоны.
    # Для generalist'а subagent_loader разворачивает schema_tables: ["*"]
    # в полный список — он получает доступ ко всем таблицам.
    def _make_subagent_tools(schema_tables: list[str]) -> list:
        scope = schema_tables or []
        return [
            clickhouse_query,
            python_analysis,
            think_tool,
            make_sample_table_tool(allowed_tables=scope),
            make_describe_table_tool(allowed_tables=scope),
        ]

    # Main agent: thin orchestrator. БЕЗ clickhouse_query, БЕЗ
    # delegate_to_generalist (заменён на generalist subagent через task()).
    #
    # Tools у main:
    # - think_tool — дисциплина мышления перед делегированием
    # - python_analysis — post-processing parquet, возвращённого подагентом
    #   (когорты, мерж двух parquet, доп. графики; SQL не пишет)
    # - sample_table — discovery 5 строк по любой таблице (для уточнения
    #   значения cabinet_name/traffic_source/etc. перед формулированием task)
    # - describe_table — посмотреть схему таблицы перед формулированием task
    #
    # `task` tool появляется автоматически от deepagents SubAgentMiddleware
    # с описанием всех зарегистрированных подагентов (direct-optimizer,
    # scoring-intelligence, command-center, generalist).
    all_tables = schema_cache.all_table_names()
    main_tools = [
        think_tool,
        python_analysis,         # post-processing of parquet returned by subagents
        make_sample_table_tool(allowed_tables=all_tables),
        make_describe_table_tool(allowed_tables=all_tables),
    ]

    # ── Subagents (declarative из SUBAGENT.md) ───────────────────────────
    # subagent_loader сканирует clients/<id>/subagents/*/SUBAGENT.md,
    # парсит frontmatter (name, description, model, schema_tables,
    # response_format, extra_skills_paths), рендерит {schema_section} и
    # {data_map_compact} placeholders, резолвит response_format в Pydantic
    # класс. tools — callable, loader вызовет её с (расширенным из ["*"])
    # списком schema_tables каждого subagent'а.
    subagent_specs = load_subagents(
        client_dir=client_dir,
        default_model=llm,
        tools=_make_subagent_tools,
    )

    # ── Override deepagents' default `general-purpose` subagent ─────────
    # deepagents v0.5.3 auto-injects a built-in `general-purpose` subagent
    # IFF no spec with that name is supplied (see deepagents/graph.py:547):
    #     if not any(s["name"] == "general-purpose" for s in inline_subagents):
    #         inline_subagents.insert(0, general_purpose_spec)
    # The built-in inherits the MAIN agent's tools, which means it has NO
    # `clickhouse_query` (we intentionally don't give it to main, see
    # `_make_main_tools` above). When the LLM guesses `subagent_type=
    # "general-purpose"` by habit, it lands in that broken default and
    # honestly reports "I don't have clickhouse_query, only sample_table" —
    # exactly the failure we observed in session 2ea277a3 on 2026-05-21.
    #
    # Fix: register an alias spec named `general-purpose` that mirrors our
    # `generalist` (full-scope ClickHouse access via wildcard schema_tables).
    # deepagents sees the name is taken and skips its default injection.
    # As a bonus, model fat-fingering "general-purpose" now lands in the
    # working agent instead of a broken one.
    _gen = next((s for s in subagent_specs if s.get("name") == "generalist"), None)
    if _gen is not None and not any(s.get("name") == "general-purpose" for s in subagent_specs):
        # Shallow copy is fine: tools/system_prompt/model are immutable from
        # our perspective; middleware list is replaced (not mutated) below.
        alias = {**_gen, "name": "general-purpose"}
        subagent_specs.insert(0, alias)
    elif _gen is None:
        print(
            "⚠ agent_factory: no 'generalist' subagent found in client_dir — "
            "deepagents will inject its default 'general-purpose' (no "
            "clickhouse_query). Add a 'generalist' SUBAGENT.md or rename one "
            "to 'general-purpose' to silence the default."
        )

    # Replace model strings with model instances (pin Anthropic provider +
    # auto-cache в extra_body — см. _build_model).
    #
    # DynamicContext FIRST: today+VAT попадают в system prompt ДО вызова
    # модели и автоматически оказываются в auto-кэше Anthropic.
    # CachingMiddleware — только лог usage stats.
    for spec in subagent_specs:
        mdl = spec.get("model")
        if isinstance(mdl, str):
            spec["model"] = _build_model(mdl)
        existing_mw = list(spec.get("middleware") or [])
        # Strip any pre-existing copies to enforce correct order
        existing_mw = [
            m for m in existing_mw
            if not isinstance(m, (DynamicContextMiddleware, CachingMiddleware, ToolCallLimitMiddleware, ModelCallLimitMiddleware))
        ]
        # ВАЖНО: бюджет итераций у каждого подагента — без него subagent может
        # уйти в бесконечный retry-цикл (например, при невалидном structured-output
        # из-за обрезания на max_tokens). Уже было: 351 итерация = $17 за один запрос.
        spec["middleware"] = [
            DynamicContextMiddleware(),
            CachingMiddleware(),
            # cache-safe: Tool/ModelCallLimit считают в state, не рвут prompt-кэш;
            # config в subagent.invoke не пробрасывается (deepagents #1698) → лимит
            # обязан жить в middleware, не в recursion_limit.
            *_budget_middleware(
                soft_tool_calls=_MAX_SUBAGENT_ITERATIONS,
                hard_model_calls=_MAX_SUBAGENT_ITERATIONS + 5,
            ),
        ] + existing_mw

    # ── Checkpointer (per-process single conn) ──────────────────────────
    conn = sqlite3.connect(_DB_PATH, check_same_thread=False)
    conn.execute("PRAGMA busy_timeout = 5000")
    checkpointer = SqliteSaver(conn)

    # ── Assemble deep agent ──────────────────────────────────────────────
    agent = create_deep_agent(
        model=llm,
        tools=main_tools,
        # response_format=MainFinalAnswer — структурно ограничивает финальный
        # текст main'а до 600 chars (Pydantic max_length). Main физически
        # не может выкатить переписку sub'овского ответа. api_adapter
        # извлекает structured_response.text и склеивает с sub.summary.
        response_format=MainFinalAnswer,
        memory=[
            str(client_dir / "AGENTS.md"),
            str(client_dir / "data_map.md"),
        ],
        skills=[
            str(client_dir / "skills"),
            str(client_dir / "shared_skills"),
        ],
        subagents=subagent_specs,
        backend=backend_factory,
        middleware=[
            # ORDER (outermost → innermost):
            # 1) DynamicContext — добавляет today+VAT в system prompt. Auto-кэш
            #    Anthropic (см. _build_model.extra_body.cache_control) включает
            #    этот блок в кэшируемый префикс. Cache miss 1 раз в сутки при
            #    смене даты — терпимо.
            # 2) Caching — purely logging now. Печатает usage stats
            #    (read/write/uncached) после каждого model call в journalctl.
            #    Расстановка cache_control больше не нужна — top-level
            #    cache_control в extra_body модели делает это автоматически
            #    и обходит баг langchain_openai с санитайзингом ToolMessage.
            # 3) HardcodeDetector — ловит pd.DataFrame({...: [литералы]}) в
            #    python_analysis, на wrap_tool_call.
            # RoutingEnforcer убран: main физически не имеет clickhouse_query,
            # больше нечего блокировать.
            DynamicContextMiddleware(),
            CachingMiddleware(),
            # Cache-safe бюджет итераций (см. _budget_middleware): не инжектит
            # эскалирующее предупреждение в промпт каждый ход → prompt-кэш растёт
            # до конца цикла. Заменяет старый BudgetMiddleware.
            *_budget_middleware(
                soft_tool_calls=_MAX_ITERATIONS,
                hard_model_calls=_MAX_ITERATIONS + 5,
            ),
            HardcodeDetector(),      # blocks pd.DataFrame({...: [lits]}) patterns
            # Убираем у main ненужные tools от встроенной FilesystemMiddleware.
            # glob/grep/ls провоцировали fallback-поведение (main искал
            # data_map.md как внешний файл, хотя он уже в system prompt).
            # read_file / write_file / edit_file оставляем — они нужны для
            # /memories/ и /plots/ (хотя редко используются).
            ToolExclusionMiddleware(excluded={"glob", "grep", "ls"}),
        ],
        checkpointer=checkpointer,
    )

    _AGENT_CACHE[cache_key] = agent
    print(
        f"✅ deepagents main agent ready | client: {client_id} | model: {model_name} | "
        f"subagents: {len(subagent_specs)} | iter_limit: {_MAX_ITERATIONS}"
    )
    return agent
