"""
CachingMiddleware — логирует Anthropic prompt-cache usage stats.

После перехода на OpenRouter automatic caching (top-level `cache_control`
в `ChatOpenAI(extra_body=...)`, см. agent_factory._build_model) ручная
расстановка cache_control маркеров на блоки сообщений больше не нужна:
Anthropic сам двигает breakpoint в конец истории на каждом запросе и
кэширует префикс инкрементально. TTL 5 минут (default).

Этот middleware остаётся как pure logger:
  - извлекает usage_metadata из ответа модели
  - печатает [cache <agent>] tools=N humans=M | in=X read=Y write=Z uncached=W
  - различает main / sub / generalist по составу tools
  - вывод идёт в stdout → journalctl -u analytics-agent

Disable via env: CACHE_LOG=0.

Использование:
  create_deep_agent(
      model=...,                 # ChatOpenAI с extra_body.cache_control
      middleware=[CachingMiddleware()],
      ...
  )
"""
from __future__ import annotations

import os
from typing import Any

from langchain.agents.middleware.types import AgentMiddleware, ModelRequest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage


# Логировать usage cache statistics в stdout (→ journalctl -u analytics-agent)
# Включено по умолчанию, отключить: CACHE_LOG=0
_CACHE_LOG = os.environ.get("CACHE_LOG", "1") != "0"


def _unwrap_ai_message(response):
    """
    LangChain middleware wrap_model_call sees response as ModelResponse
    (dataclass с .result: list[BaseMessage]) или ExtendedModelResponse
    (wraps ModelResponse). usage живёт на последнем AIMessage внутри .result.
    Unwrap iteratively.
    """
    from langchain_core.messages import AIMessage as _AIMessage

    node = response
    for _ in range(4):  # safety bound
        if node is None:
            return None
        # ExtendedModelResponse.model_response → ModelResponse
        if hasattr(node, "model_response") and node.model_response is not None:
            node = node.model_response
            continue
        # ModelResponse.result → list[BaseMessage]
        if hasattr(node, "result"):
            result = getattr(node, "result") or []
            for m in reversed(result):
                if isinstance(m, _AIMessage):
                    return m
            return None
        if isinstance(node, _AIMessage):
            return node
        if isinstance(node, dict):
            res = node.get("result")
            if isinstance(res, list):
                for m in reversed(res):
                    if isinstance(m, _AIMessage):
                        return m
            return None
        return None
    return None


def _extract_cache_stats(response) -> dict:
    """
    Pull cache usage out of the AIMessage inside response. Tries both
    canonical locations:
      - msg.usage_metadata.input_token_details.{cache_read, cache_creation}
      - msg.response_metadata.token_usage.prompt_tokens_details.{cached_tokens, cache_write_tokens}
    Returns normalized dict with keys: in, out, read, write.
    """
    msg = _unwrap_ai_message(response)
    if msg is None:
        return {}

    out: dict = {}
    um = getattr(msg, "usage_metadata", None) or {}
    if isinstance(um, dict):
        itd = um.get("input_token_details") or {}
        if um.get("input_tokens") is not None:
            out["in"] = um.get("input_tokens")
        if um.get("output_tokens") is not None:
            out["out"] = um.get("output_tokens")
        if itd.get("cache_read") is not None:
            out["read"] = itd.get("cache_read")
        if itd.get("cache_creation") is not None:
            out["write"] = itd.get("cache_creation")
    rm = getattr(msg, "response_metadata", None) or {}
    if isinstance(rm, dict):
        tu = rm.get("token_usage") or {}
        ptd = tu.get("prompt_tokens_details") or {}
        if "read" not in out and ptd.get("cached_tokens") is not None:
            out["read"] = ptd.get("cached_tokens")
        if "write" not in out and ptd.get("cache_write_tokens") is not None:
            out["write"] = ptd.get("cache_write_tokens")
        if "in" not in out and tu.get("prompt_tokens") is not None:
            out["in"] = tu.get("prompt_tokens")
        if "out" not in out and tu.get("completion_tokens") is not None:
            out["out"] = tu.get("completion_tokens")
    return out


def _short_session_id() -> str:
    """
    Достаём session_id из ContextVar (выставляется api_server'ом перед
    invoke). Возвращаем короткую форму (первые 8 символов) — этого хватает
    чтобы grep'ить логи одной сессии без переполнения строки.
    Если нет контекста (например, тест) — возвращаем '-'.
    """
    try:
        from .session_context import get_current_session
        ctx = get_current_session()
        if ctx is None:
            return "-"
        sid = getattr(ctx, "session_id", None) or ""
        return sid[:8] if sid else "-"
    except Exception:
        return "-"


def _log_cache(agent_name: str, request: ModelRequest, response) -> None:
    if not _CACHE_LOG:
        return
    stats = _extract_cache_stats(response)
    if not stats:
        # Тихо: если usage недоступен — просто ничего не пишем
        return
    n_tools = sum(1 for m in request.messages if isinstance(m, ToolMessage))
    n_humans = sum(1 for m in request.messages if isinstance(m, HumanMessage))
    in_tok = stats.get("in")
    out_tok = stats.get("out")
    read = int(stats.get("read") or 0)
    write = int(stats.get("write") or 0)
    if isinstance(in_tok, int):
        uncached = max(0, in_tok - read - write)
    else:
        uncached = "?"
    sid = _short_session_id()
    print(
        f"[cache session={sid} agent={agent_name}] "
        f"tools={n_tools} humans={n_humans} | "
        f"in={in_tok if in_tok is not None else '?'} "
        f"read={read} write={write} uncached={uncached} | "
        f"out={out_tok if out_tok is not None else '?'}",
        flush=True,
    )


def _agent_name_from_request(request: ModelRequest) -> str:
    """
    Best-effort label: main / sub / agent.

    В ModelRequest нет имени напрямую — определяем по составу tools:
      - 'task' в tools → главный агент (deepagents auto-injects task tool
        только в main)
      - 'sample_table' / 'describe_table' / 'clickhouse_query' → подагент
      - иначе → 'agent'
    """
    try:
        tool_names = {getattr(t, "name", "") for t in (request.tools or [])}
        if "task" in tool_names:
            return "main"
        if "clickhouse_query" in tool_names:
            return "sub"
        if "sample_table" in tool_names or "describe_table" in tool_names:
            return "sub"
        return "agent"
    except Exception:
        return "agent"


class CachingMiddleware(AgentMiddleware):
    """
    Pure-logging middleware: печатает usage stats после каждого model call.

    Кэшированием управляет OpenRouter automatic caching, настроенный через
    `ChatOpenAI(extra_body={"cache_control": {"type": "ephemeral"}})` в
    `agent_factory._build_model`. Anthropic auto-advances cache breakpoint
    в конец сообщений на каждом запросе.
    """

    def wrap_model_call(self, request: ModelRequest, handler):
        response = handler(request)
        _log_cache(_agent_name_from_request(request), request, response)
        return response

    async def awrap_model_call(self, request: ModelRequest, handler):
        response = await handler(request)
        _log_cache(_agent_name_from_request(request), request, response)
        return response
