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
from langchain_core.messages import HumanMessage, ToolMessage


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
    """Inject a budget notice as a fresh HumanMessage at the END of the
    conversation. The text is **static within a level** so the cached prefix
    is preserved across iterations.

    Why HumanMessage and not SystemMessage:
        ChatOpenAI (our transport) sends OpenAI-format messages to OpenRouter,
        which translates to Anthropic format. ALL `SystemMessage` instances in
        the messages array — anywhere in the conversation — get **merged into
        Anthropic's single `system` field** at translation time. The system
        field is part of the cached prefix; any modification to it invalidates
        the entire cache.

        That's why our previous attempt to append a SystemMessage at the tail
        still broke the cache: even though we put it in the messages list,
        OpenRouter folded it back into system, and the system text changed
        each turn (different notice).

        HumanMessage stays as `role="user"` in both OpenAI and Anthropic
        formats — it's just another conversation message. Adding one at the
        tail behaves identically to a normal tool/assistant turn ending: the
        prefix [system, m0..m_{n-1}] is unchanged → cache hit on the bulk.

    Why static text per level:
        Even with HumanMessage, if the text varies between iterations within
        a level (e.g. "осталось 2" → "осталось 1"), the cached prefix from
        the previous iter doesn't match the new one (notice text differs).
        Static text per level → prefix matches → cache hit at iter N+1.
        Worst case: 1 miss per level transition (no-notice → ⚡ → ⚠ → 🚨 → ⛔).
    """
    remaining = budget - used
    if remaining > 10:
        return  # silent, no cache impact
    if remaining <= 0:
        note = (
            "[SYSTEM REMINDER: лимит итераций ИСЧЕРПАН. Дай финальный ответ "
            "на основе уже собранных данных. НЕ вызывай инструменты.]"
        )
    elif remaining <= 2:
        note = (
            "[SYSTEM REMINDER: 🚨 почти исчерпан лимит итераций. Используй tool "
            "только если критически необходимо. После — финальный ответ.]"
        )
    elif remaining <= 5:
        note = (
            "[SYSTEM REMINDER: ⚠ мало итераций осталось. Объединяй оставшиеся "
            "запросы через WITH/CTE, не дроби на шаги.]"
        )
    else:  # remaining 6..10
        note = (
            "[SYSTEM REMINDER: ⚡ лимит итераций приближается. Избегай дробных "
            "шагов, готовься переходить к финалу.]"
        )

    if request.messages is None:
        request.messages = []
    request.messages = list(request.messages) + [HumanMessage(content=note)]
    print(f"[BudgetMiddleware] used={used}/{budget} remaining={remaining} → notice appended")


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
