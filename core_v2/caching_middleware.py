"""
CachingMiddleware — ставит `cache_control: ephemeral` на стабильные блоки
system prompt и messages, чтобы Anthropic prompt caching через OpenRouter
работал оптимально.

Модель кэша (до 4 breakpoints у Anthropic):
  1. System prompt (стабильный: AGENTS.md + data_map.md + skills index) — всегда
  2. Последний ToolMessage / AIMessage старше last-user — «история до текущего turn»
  3. Последний HumanMessage — граница между кэшем и новым вводом пользователя

Правила:
  - Работает только если модель — Anthropic (детектится по имени).
  - Для не-Anthropic (DeepSeek, OpenAI) не делает ничего.
  - Не модифицирует сам текст, только оборачивает content в блочный формат
    с cache_control.

Использование:
  create_deep_agent(
      model=...,
      middleware=[CachingMiddleware()],
      ...
  )
"""
from __future__ import annotations

from copy import copy
from typing import Any

from langchain.agents.middleware.types import AgentMiddleware, ModelRequest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage


def _is_anthropic(model: Any) -> bool:
    """Detect Anthropic-backed model (direct ChatAnthropic or OpenRouter wrapper)."""
    # Direct Anthropic wrapper
    cls = type(model).__name__
    if "Anthropic" in cls:
        return True
    # OpenRouter / OpenAI-compatible wrappers — inspect model name
    for attr in ("model_name", "model", "model_id"):
        val = getattr(model, attr, None)
        if isinstance(val, str) and "claude" in val.lower():
            return True
    # extra_body pinning check
    extra = getattr(model, "extra_body", None) or {}
    if isinstance(extra, dict):
        provider = extra.get("provider", {})
        if isinstance(provider, dict):
            order = provider.get("order") or []
            if any("anthropic" in str(p).lower() for p in order):
                return True
    return False


def _attach_cache_control(content: Any) -> list[dict]:
    """
    Normalize `content` to list-of-blocks and attach cache_control ephemeral
    to the last block.  Returns a fresh list (does not mutate input).
    """
    if isinstance(content, str):
        if not content:
            return content  # type: ignore[return-value]
        return [{
            "type": "text",
            "text": content,
            "cache_control": {"type": "ephemeral"},
        }]
    if isinstance(content, list):
        if not content:
            return content
        new = [dict(b) if isinstance(b, dict) else b for b in content]
        last = new[-1]
        if isinstance(last, dict):
            last = dict(last)
            last["cache_control"] = {"type": "ephemeral"}
            new[-1] = last
        return new
    return content


def _clone_message_with_content(msg, new_content):
    """Create a copy of msg with replaced content (pydantic-safe)."""
    try:
        return msg.model_copy(update={"content": new_content})
    except Exception:
        out = copy(msg)
        out.content = new_content
        return out


class CachingMiddleware(AgentMiddleware):
    """
    Attach `cache_control: ephemeral` to:
      1. System prompt (last block).
      2. Last ToolMessage among messages (so tool-call chain is cached).
      3. Last HumanMessage (boundary between cached history and fresh input).
    """

    def wrap_model_call(self, request: ModelRequest, handler):
        if not _is_anthropic(request.model):
            return handler(request)

        # ── 1. System prompt breakpoint ────────────────────────────────────
        if request.system_message is not None:
            content = request.system_message.content
            new_content = _attach_cache_control(content)
            if new_content is not content:
                request.system_message = _clone_message_with_content(
                    request.system_message, new_content
                )

        # ── 2+3. Messages: last ToolMessage + last HumanMessage ────────────
        msgs = list(request.messages)
        last_tool_idx = next(
            (i for i in range(len(msgs) - 1, -1, -1) if isinstance(msgs[i], ToolMessage)),
            None,
        )
        last_human_idx = next(
            (i for i in range(len(msgs) - 1, -1, -1) if isinstance(msgs[i], HumanMessage)),
            None,
        )

        # Avoid double-marking a single message (if last human is also the last message
        # and there's no tool before it, the system+1 breakpoint is enough).
        marked: set[int] = set()
        if last_tool_idx is not None and last_tool_idx != last_human_idx:
            tm = msgs[last_tool_idx]
            new_c = _attach_cache_control(tm.content)
            if new_c is not tm.content:
                msgs[last_tool_idx] = _clone_message_with_content(tm, new_c)
                marked.add(last_tool_idx)
        if last_human_idx is not None and last_human_idx not in marked:
            hm = msgs[last_human_idx]
            new_c = _attach_cache_control(hm.content)
            if new_c is not hm.content:
                msgs[last_human_idx] = _clone_message_with_content(hm, new_c)

        request.messages = msgs
        return handler(request)

    async def awrap_model_call(self, request: ModelRequest, handler):
        if not _is_anthropic(request.model):
            return await handler(request)

        if request.system_message is not None:
            content = request.system_message.content
            new_content = _attach_cache_control(content)
            if new_content is not content:
                request.system_message = _clone_message_with_content(
                    request.system_message, new_content
                )

        msgs = list(request.messages)
        last_tool_idx = next(
            (i for i in range(len(msgs) - 1, -1, -1) if isinstance(msgs[i], ToolMessage)),
            None,
        )
        last_human_idx = next(
            (i for i in range(len(msgs) - 1, -1, -1) if isinstance(msgs[i], HumanMessage)),
            None,
        )
        marked: set[int] = set()
        if last_tool_idx is not None and last_tool_idx != last_human_idx:
            tm = msgs[last_tool_idx]
            new_c = _attach_cache_control(tm.content)
            if new_c is not tm.content:
                msgs[last_tool_idx] = _clone_message_with_content(tm, new_c)
                marked.add(last_tool_idx)
        if last_human_idx is not None and last_human_idx not in marked:
            hm = msgs[last_human_idx]
            new_c = _attach_cache_control(hm.content)
            if new_c is not hm.content:
                msgs[last_human_idx] = _clone_message_with_content(hm, new_c)

        request.messages = msgs
        return await handler(request)
