"""
Deep Agents v2 core — фабрики, middleware, инструменты.

Architecture:
  - agent_factory.build_agent(session_id, client_id) → CompiledStateGraph
  - Main agent: карта данных + skills index, без clickhouse_query для доменов
  - Specialized subagents: direct-optimizer, scoring-intelligence (фикс. schema+skills)
  - Generalist subagent: создаётся on-demand через delegate_to_generalist tool
  - Session-scoped filesystem: /parquet/, /plots/, /memories/
  - Prompt caching: 3 breakpoints через CachingMiddleware
  - Iteration budget: 30 total через BudgetMiddleware
"""

__all__ = ["build_agent"]


def build_agent(*args, **kwargs):
    from .agent_factory import build_agent as _build
    return _build(*args, **kwargs)
