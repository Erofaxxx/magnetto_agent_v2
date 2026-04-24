"""
build_agent(session_id, client_id) — фабрика главного deepagents агента.

Собирает:
  - Main agent с tools: clickhouse_query (редко), python_analysis (post-process),
    list_tables (резерв), think_tool, delegate_to_generalist
  - Memory: AGENTS.md + data_map.md (всегда в system prompt через MemoryMiddleware)
  - Skills: clients/<id>/skills/ + shared_skills/ (progressive disclosure)
  - Subagents: direct-optimizer + scoring-intelligence (из SUBAGENT.md)
  - Backend: session-scoped CompositeBackend (/parquet/, /plots/, /memories/)
  - Middleware: CachingMiddleware + BudgetMiddleware
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

from .caching_middleware import CachingMiddleware
from .budget_middleware import BudgetMiddleware
from .delegate_to_generalist import make_delegate_to_generalist_tool
from .dynamic_context_middleware import DynamicContextMiddleware
from .enforcement_middleware import HardcodeDetector, RoutingEnforcer
from .schema_cache import get_schema_cache
from .session_backend import make_backend_factory
from .subagent_loader import load_subagents
from .tools import clickhouse_query, list_tables, python_analysis, think_tool


# ─── Config (from env, with fallbacks mirroring legacy config.py) ───────────

_CLIENTS_ROOT = Path(__file__).resolve().parent.parent / "clients"
_DB_PATH = os.environ.get("DB_PATH") or str(
    Path(__file__).resolve().parent.parent / "chat_history.db"
)
_OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY", "")
_MODEL = os.environ.get("MODEL", "anthropic/claude-sonnet-4.6")
_MAX_TOKENS = int(os.environ.get("MAX_TOKENS", "8192"))
_MAX_ITERATIONS = int(os.environ.get("MAX_AGENT_ITERATIONS", "30"))

_OPENROUTER_HEADERS = {
    "HTTP-Referer": "https://server.asktab.ru",
    "X-Title": "Magnetto Analytics Agent (deepagents)",
}


def _build_model(model_name: str) -> ChatOpenAI:
    """
    ChatOpenAI configured for OpenRouter with Anthropic provider pinning.
    Pinning is critical — without it OpenRouter round-robins across Anthropic
    edges, and each edge has its own prompt cache → cache miss every time.
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
        }
    return ChatOpenAI(**kwargs)


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

    # ── Tools for main agent (plus delegate_to_generalist closure) ──────
    tool_list_subagent = [clickhouse_query, python_analysis, think_tool]
    delegate_tool = make_delegate_to_generalist_tool(
        client_dir=client_dir,
        default_model=llm,
        tools_fn=lambda: tool_list_subagent,
        # ORDER: DynamicContext first (outermost) → today+VAT запечатаны В system
        # prompt ДО cache_control, поэтому сидят внутри кэша. Один cache-miss
        # в сутки в полночь МСК, дальше cache read.
        middleware=[DynamicContextMiddleware(), CachingMiddleware()],
    )
    main_tools = [
        think_tool,
        clickhouse_query,        # rare: COUNT / single-fact / post-processing support
        python_analysis,         # post-processing of parquet returned by subagents
        list_tables,             # fallback only
        delegate_tool,
    ]

    # ── Subagents (specialized) ─────────────────────────────────────────
    # subagent_loader returns dicts with model strings; convert to ChatOpenAI
    subagent_specs = load_subagents(
        client_dir=client_dir,
        default_model=llm,
        tools=tool_list_subagent,
    )
    # Replace model strings with model instances (pin Anthropic provider).
    #
    # Middleware order (outermost → innermost): DynamicContext + Caching.
    # DynamicContext FIRST так, чтобы today+VAT упаковались В system prompt
    # ДО того, как CachingMiddleware поставит cache_control. Подагент видит
    # actual дату как часть своего system prompt, и этот блок кэшируется
    # вместе с остальной стабильной частью (schema + skills).
    for spec in subagent_specs:
        mdl = spec.get("model")
        if isinstance(mdl, str):
            spec["model"] = _build_model(mdl)
        existing_mw = list(spec.get("middleware") or [])
        # Strip any pre-existing copies to enforce correct order
        existing_mw = [
            m for m in existing_mw
            if not isinstance(m, (DynamicContextMiddleware, CachingMiddleware))
        ]
        spec["middleware"] = [DynamicContextMiddleware(), CachingMiddleware()] + existing_mw

    # ── Checkpointer (per-process single conn) ──────────────────────────
    conn = sqlite3.connect(_DB_PATH, check_same_thread=False)
    conn.execute("PRAGMA busy_timeout = 5000")
    checkpointer = SqliteSaver(conn)

    # ── Assemble deep agent ──────────────────────────────────────────────
    agent = create_deep_agent(
        model=llm,
        tools=main_tools,
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
            # 1) DynamicContext FIRST — добавляет today+VAT в system prompt,
            #    ДО того как Caching поставит cache_control. Блок становится
            #    жёсткой частью системной инструкции и лежит ВНУТРИ кэша.
            #    Cache miss 1 раз в сутки при смене даты — терпимо.
            # 2) Caching — ставит cache_control на: последний блок system
            #    (включая today+VAT), последний ToolMessage (граница истории),
            #    последний HumanMessage (свежий пользовательский ввод).
            #    Всё это кэшируется на 5 мин Anthropic ephemeral TTL.
            # 3-4) Enforcement — перехватывают сложные clickhouse_query/hardcode
            #    на wrap_tool_call, к model_call не относятся.
            DynamicContextMiddleware(),
            CachingMiddleware(),
            BudgetMiddleware(max_iterations=_MAX_ITERATIONS),
            RoutingEnforcer(),       # blocks complex clickhouse_query without delegation
            HardcodeDetector(),      # blocks pd.DataFrame({...: [lits]}) patterns
        ],
        checkpointer=checkpointer,
    )

    _AGENT_CACHE[cache_key] = agent
    print(
        f"✅ deepagents main agent ready | client: {client_id} | model: {model_name} | "
        f"subagents: {len(subagent_specs)} | iter_limit: {_MAX_ITERATIONS}"
    )
    return agent
