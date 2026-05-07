"""
BudgetMiddleware — глобальный счётчик tool-итераций (включая subagents).

Цель: ограничить суммарное число вызовов инструментов на одну пользовательскую
задачу (main + все вложенные task() вызовы).

Mechanism:
  - Счётчик ToolMessages в state["messages"] начиная с последнего HumanMessage
    (current user turn) — так учитываются и main-итерации, и subagent-итерации
    (subagent в deepagents ходит как обычный tool, его внутренние вызовы видны
    как ToolMessages после возврата task-tool).
  - wrap_model_call (предкол): подмешиваем в request HumanMessage-уведомление
    о приближающемся лимите. Текст статичен в пределах "уровня" (>10 / 6..10 /
    3..5 / 1..2 / 0), чтобы не ломать prefix-cache.
  - wrap_model_call (постхук): когда лимит исчерпан, физически вырезаем
    `tool_calls` из ответа модели. Без этого hard-stop модель может проигнорить
    мягкое предупреждение и продолжить вызывать инструменты до GraphRecursionError.

Мягкие предупреждения через инъекцию HumanMessage:
  - осталось > 10 → ничего
  - осталось 6..10 → "⚡ 6 итераций" (легкий hint)
  - осталось 3..5 → "⚠ Мало итераций, объединяй запросы"
  - осталось 1..2 → "🚨 Почти исчерпан — последняя возможность"
  - осталось 0   → "⛔ ЛИМИТ" + tool_calls стрипаются из ответа модели физически.

Актуально: 30 total по умолчанию (конфигурируется через env).
"""
from __future__ import annotations

import os
import threading
from typing import Any

from langchain.agents.middleware.types import AgentMiddleware, ModelRequest
from langchain_core.messages import HumanMessage, ToolMessage


_DEFAULT_BUDGET = int(os.environ.get("MAX_AGENT_ITERATIONS", "30"))


# Tool messages emitted by deepagents' structured-response machinery don't
# represent real iteration cost — they're the LLM's way of returning the
# final answer. Counting them inflates `used` by 1 at exactly the moment we
# can least afford it (the strip step then deletes the structured output).
_NON_BUDGET_TOOL_NAMES = frozenset({
    "MainFinalAnswer",
    "SubagentResult",
})


def _count_tool_calls(state: dict) -> int:
    """Count ToolMessages since last HumanMessage (current user turn),
    excluding structured-response sentinels that don't count toward budget."""
    messages = state.get("messages") or []
    # find last human index
    from langchain_core.messages import HumanMessage
    last_h = -1
    for i, m in enumerate(messages):
        if isinstance(m, HumanMessage):
            last_h = i
    if last_h < 0:
        return 0
    return sum(
        1 for m in messages[last_h:]
        if isinstance(m, ToolMessage)
        and getattr(m, "name", None) not in _NON_BUDGET_TOOL_NAMES
    )


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


def _strip_tool_calls_if_exhausted(response: Any, used: int, budget: int) -> Any:
    """If budget is fully spent, remove pending tool_calls from the model
    response so the agent loop terminates with whatever the model produced
    instead of looping until LangGraph's recursion_limit blows up.

    Handles ALL shapes that langchain agents may hand to wrap_model_call:
      - `ModelResponse(result=list[BaseMessage], structured_response=...)`
        — the canonical shape in current langchain.agents.factory; this was
        the case the previous implementation missed entirely.
      - bare `AIMessage` (older transports)
      - `dict` with `"messages"` or `"result"` key (state-shaped responses)
    """
    if used < budget:
        return response

    _BUDGET_EXHAUSTED_NOTICE = (
        "⛔ Лимит итераций исчерпан. Ниже — сводка по уже собранным данным."
    )

    def _clear_msg(msg):
        """Clear tool_calls on an individual message (best-effort across the
        AIMessage / pydantic / langchain-core variations).

        If the message ends up with no `content` either (model planned only
        tool calls, no text), inject a placeholder so the user sees a
        definitive "limit reached" string instead of an empty bubble.
        """
        had_calls = False
        # Direct attribute (AIMessage in langchain-core)
        try:
            if getattr(msg, "tool_calls", None):
                msg.tool_calls = []
                had_calls = True
        except Exception:
            pass
        # additional_kwargs.tool_calls (OpenAI transport)
        try:
            ak = getattr(msg, "additional_kwargs", None)
            if isinstance(ak, dict) and "tool_calls" in ak:
                ak.pop("tool_calls", None)
                had_calls = True
        except Exception:
            pass
        # If we stripped calls AND the message has no displayable text,
        # backfill content so api_adapter._extract_final_text returns
        # something user-visible.
        if had_calls:
            try:
                content = getattr(msg, "content", None)
                empty = (
                    content is None
                    or (isinstance(content, str) and not content.strip())
                    or (isinstance(content, list) and not any(
                        isinstance(b, dict) and (b.get("text") or "").strip()
                        for b in content
                    ))
                )
                if empty:
                    msg.content = _BUDGET_EXHAUSTED_NOTICE
            except Exception:
                pass
        return msg

    def _clear_list(messages):
        return [_clear_msg(m) for m in messages]

    # --- ModelResponse (pydantic) -----------------------------------------
    # ModelResponse exposes `result: list[BaseMessage]`; clear inside.
    if hasattr(response, "result") and isinstance(getattr(response, "result"), list):
        new_result = _clear_list(response.result)
        # Prefer pydantic's immutable update so we return a fresh object.
        try:
            return response.model_copy(update={"result": new_result})
        except Exception:
            try:
                response.result = new_result
            except Exception:
                pass
            return response

    # --- dict shapes ------------------------------------------------------
    if isinstance(response, dict):
        for key in ("result", "messages"):
            if key in response and isinstance(response[key], list):
                response[key] = _clear_list(response[key])
                return response
        return response

    # --- bare message -----------------------------------------------------
    return _clear_msg(response)


class BudgetMiddleware(AgentMiddleware):
    """
    Attach a budget notice to the system prompt when close to the iteration limit
    and hard-stop new tool_calls once the budget is exhausted.
    """

    def __init__(self, max_iterations: int = _DEFAULT_BUDGET) -> None:
        super().__init__()
        self.max_iterations = max_iterations

    def wrap_model_call(self, request: ModelRequest, handler):
        used = _count_tool_calls(request.state) if request.state else 0
        _append_budget_notice(request, used, self.max_iterations)
        response = handler(request)
        return _strip_tool_calls_if_exhausted(response, used, self.max_iterations)

    async def awrap_model_call(self, request: ModelRequest, handler):
        used = _count_tool_calls(request.state) if request.state else 0
        _append_budget_notice(request, used, self.max_iterations)
        response = await handler(request)
        return _strip_tool_calls_if_exhausted(response, used, self.max_iterations)
