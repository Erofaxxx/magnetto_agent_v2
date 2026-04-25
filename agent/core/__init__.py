"""
Deep Agents core — фабрики, middleware, инструменты.

Architecture:
  - agent_factory.build_agent(session_id, client_id) → CompiledStateGraph
  - Main agent (thin orchestrator): карта данных + skills index, БЕЗ
    clickhouse_query. Все запросы через task() в подагенты.
  - Subagents (declarative из SUBAGENT.md): direct-optimizer,
    scoring-intelligence, command-center (scoped schema+skills),
    generalist (доступ ко всем таблицам через discovery tools).
  - Session-scoped filesystem: /parquet/, /plots/, /memories/
  - Prompt caching: OpenRouter automatic via top-level cache_control в
    extra_body. CachingMiddleware — pure logger usage stats.
  - Iteration budget: 30 total через BudgetMiddleware
"""

__all__ = ["build_agent"]


def build_agent(*args, **kwargs):
    from .agent_factory import build_agent as _build
    return _build(*args, **kwargs)
