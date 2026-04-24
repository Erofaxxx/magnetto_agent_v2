"""
ToolExclusionMiddleware — вырезает указанные tools из request до model call.

Нужно чтобы убрать у main-агента tools, которые приходят от встроенного
deepagents FilesystemMiddleware (`ls`, `glob`, `grep`, ...), но которые
нам не нужны и только провоцируют fallback-поведение (main пытается
искать data_map.md через glob хотя он уже в system prompt).

Реализовано через фильтрацию `request.tools` в `wrap_model_call`. Сами
tools по-прежнему зарегистрированы в графе, но модель их не видит —
значит не может вызвать. Это эквивалент deepagents приватного
_ToolExclusionMiddleware, но без импорта из приватного API.
"""
from __future__ import annotations

from typing import Any

from langchain.agents.middleware.types import AgentMiddleware, ModelRequest


def _tool_name(tool: Any) -> str | None:
    if isinstance(tool, dict):
        name = tool.get("name")
        return name if isinstance(name, str) else None
    name = getattr(tool, "name", None)
    return name if isinstance(name, str) else None


class ToolExclusionMiddleware(AgentMiddleware):
    """Filter out tool definitions before model sees them."""

    def __init__(self, excluded: set[str] | frozenset[str]) -> None:
        self._excluded = frozenset(excluded)

    def _filter(self, request: ModelRequest) -> ModelRequest:
        if not self._excluded:
            return request
        tools = list(getattr(request, "tools", None) or [])
        filtered = [t for t in tools if _tool_name(t) not in self._excluded]
        if len(filtered) != len(tools):
            # Prefer .override if available, else mutate in place.
            override = getattr(request, "override", None)
            if callable(override):
                return override(tools=filtered)
            request.tools = filtered
        return request

    def wrap_model_call(self, request: ModelRequest, handler):
        return handler(self._filter(request))

    async def awrap_model_call(self, request: ModelRequest, handler):
        return await handler(self._filter(request))
