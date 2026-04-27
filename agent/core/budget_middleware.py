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
    # Only fire on hard-limit thresholds. The previous soft hints (⚡ at
    # remaining≤10 and ⚠ at remaining≤5) were appended to the message stream
    # every iteration, with a different counter each time. Auto-cache (Anthropic
    # via OpenRouter `cache_control: ephemeral`) places its breakpoint on the
    # last message — so the changing counter text in the trailing position
    # invalidated the cached prefix on EVERY iteration past iter 10. Net effect
    # on a 20-iter sub: ~10 cache misses × ~$0.20 = ~$2 wasted per session.
    #
    # Now the notice fires only when the model needs to actually adjust its
    # behavior (slow down at 🚨, stop tools at ⛔). That's at most 2 cache
    # breakpoint shifts per session — and in the typical case where the sub
    # finishes in <18 iters, zero shifts.
    if remaining > 2:
        return
    # Notice text is **static within a level** (no dynamic counter). Anthropic
    # auto-cache hashes the request prefix up to its breakpoint; if the trailing
    # notice text is identical between turns, the hash is identical → cache HIT.
    # The previous dynamic "осталось 2 → 1" wording invalidated cache every iter.
    # Now: 1 miss when 🚨 first appears, then HIT for any subsequent 🚨 turn.
    # The level transition 🚨 → ⛔ still causes 1 miss (different text). In the
    # typical sub run that finishes well below the limit, NEITHER notice fires
    # → cache works through the whole session.
    if remaining <= 0:
        note = (
            "[⛔ ЛИМИТ ИСЧЕРПАН. Дай финальный ответ на основе уже собранных "
            "данных. НЕ вызывай инструменты.]"
        )
    else:  # remaining 1..2
        note = (
            "[🚨 Почти исчерпан лимит итераций. Используй tool только если "
            "критически необходимо. После — финальный ответ.]"
        )

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
