"""
BudgetMiddleware — глобальный счётчик tool-итераций (включая subagents).

Цель: ограничить суммарное число вызовов инструментов на одну пользовательскую
задачу (main + все вложенные task() вызовы).

Mechanism:
  - Per-thread счётчик в модульной переменной (thread_id → int).
  - before_model: если счётчик уже >= лимита — добавляем в state
    HumanMessage с "⛔ ЛИМИТ ИСЧЕРПАН", запрещаем новые tool_calls.
  - after_model: видим сколько tool_calls планирует модель, инкрементим
    будущий расход (чтобы вовремя предупредить).

Мягкие предупреждения через инъекцию комментария в system_message'а:
  - осталось > 10 → ничего
  - остолось 6..10 → "⚡ 6 итераций" (легкий hint)
  - осталось 3..5 → "⚠ Мало итераций, объединяй запросы"
  - осталось 1..2 → "🚨 Почти исчерпан — последняя возможность"
  - осталось 0   → "⛔ ЛИМИТ. Дай финальный ответ без инструментов"

Счётчик инкрементится по ToolMessages в state["messages"] — так учитываются
И main-итерации, И subagent-итерации (subagent в deepagents ходит как обычный
tool, его внутренние вызовы видны как ToolMessages после task-tool возвращения).

Актуально: 30 total по умолчанию (конфигурируется через env).
"""
from __future__ import annotations

import os
import threading
from typing import Any

from langchain.agents.middleware.types import AgentMiddleware, ModelRequest
from langchain_core.messages import SystemMessage, ToolMessage


_DEFAULT_BUDGET = int(os.environ.get("MAX_AGENT_ITERATIONS", "30"))


def _count_tool_calls(state: dict) -> int:
    """Count ToolMessages since last HumanMessage (current user turn)."""
    messages = state.get("messages") or []
    # find last human index
    from langchain_core.messages import HumanMessage
    last_h = -1
    for i, m in enumerate(messages):
        if isinstance(m, HumanMessage):
            last_h = i
    if last_h < 0:
        return 0
    return sum(1 for m in messages[last_h:] if isinstance(m, ToolMessage))


def _append_budget_notice(request: ModelRequest, used: int, budget: int) -> None:
    """Inject a short budget notice as a transient SystemMessage at the END of
    the conversation (suffix), NOT as an append to the global system_message.

    Why: appending to system_message changes the cached prefix on every call
    (the «N/M итераций» counter increments each turn). Anthropic auto-cache
    treats this as a fresh prefix → cache miss EVERY iteration once the
    threshold kicks in (was burning ~$0.30/iter from iter 10 onwards on long
    sub runs).

    Adding a fresh SystemMessage at the very end keeps messages 0..N-1 byte-
    identical to the previous turn → those still hit cache. Only the new tail
    is uncached, which is correct.
    """
    remaining = budget - used
    if remaining > 10:
        return
    if remaining <= 0:
        note = (
            f"[⛔ ЛИМИТ ИСЧЕРПАН ({used}/{budget}). "
            "Немедленно дай финальный ответ на основе уже собранных данных. "
            "НЕ вызывай инструменты.]"
        )
    elif remaining <= 2:
        note = (
            f"[🚨 Почти исчерпан ({used}/{budget}). Осталось {remaining} вызовов. "
            "Используй только если критически необходимо. После — финальный ответ.]"
        )
    elif remaining <= 5:
        note = (
            f"[⚠ Мало итераций ({used}/{budget}). Осталось {remaining}. "
            "Объединяй оставшиеся запросы через WITH/CTE, не дроби на шаги.]"
        )
    else:  # 6..10
        note = f"[⚡ {used}/{budget} итераций использовано, осталось {remaining}.]"

    # Append a fresh SystemMessage to request.messages (suffix). This message is
    # NOT cached on subsequent turns (it's regenerated each call with new
    # numbers), but the prefix before it stays stable → cache hit on the
    # heavy part.
    if request.messages is None:
        request.messages = []
    request.messages = list(request.messages) + [SystemMessage(content=note)]


class BudgetMiddleware(AgentMiddleware):
    """
    Attach a budget notice to the system prompt when close to the iteration limit.
    """

    def __init__(self, max_iterations: int = _DEFAULT_BUDGET) -> None:
        super().__init__()
        self.max_iterations = max_iterations

    def wrap_model_call(self, request: ModelRequest, handler):
        used = _count_tool_calls(request.state) if request.state else 0
        _append_budget_notice(request, used, self.max_iterations)
        return handler(request)

    async def awrap_model_call(self, request: ModelRequest, handler):
        used = _count_tool_calls(request.state) if request.state else 0
        _append_budget_notice(request, used, self.max_iterations)
        return await handler(request)
