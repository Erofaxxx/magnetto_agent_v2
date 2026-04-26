"""
FinalAnswerCapMiddleware — режет max_tokens main'а когда он собирается
генерить финальный ответ после делегирования(ий) подагенту.

ЗАЧЕМ
─────
Финальный ответ пользователю собирается программно в `api_adapter.
_extract_final_text` из summaries подагентов + main'овского комментария.
Если main решит переписать sub'овский summary своими словами — это лишние
дорогие output токены без новой информации (сейчас 1000-1500 токенов
переписи на каждом запросе с делегированием).

Cap гарантирует что main физически не может выкатить больше чем
«комментарий поверх». Anthropic при достижении max_tokens просто truncate'ит
без retry — никакие токены не тратятся впустую.

ЛОГИКА CAP
──────────
- Применяется ТОЛЬКО когда main сделал хотя бы одно делегирование в
  текущем turn'е И последнее сообщение в request — task ToolMessage
  (то есть main вот-вот будет писать ответ после получения результата).
- Не применяется если последнее сообщение — другой ToolMessage (например
  python_analysis), потому что main может писать synthesis по результатам
  пост-обработки и нужно больше токенов.
- Cap = N * PER_SUBAGENT_CAP, где N — количество task ToolMessage в
  текущем turn'е. Multi-subagent синтез получает пропорционально больше.

DEFAULT
───────
PER_SUBAGENT_CAP = 800 токенов на одно делегирование.
- 1 task → 800 tok.   На комментарий типа «следующий шаг», intro/outro.
- 2 task → 1600 tok.  На синтез и сравнение результатов.
- 3 task → 2400 tok.  На сложный multi-source разбор.

DISABLE
───────
FINAL_ANSWER_CAP=0 в env → middleware no-op.
FINAL_ANSWER_CAP=N    → переопределить per-subagent cap.

WORST CASE
──────────
Main сгенерил >cap токенов → Anthropic truncate'ит на cap'е. Main'овский
комментарий обрывается на полуслове, но программная композиция всё равно
вставит sub.summary целиком — пользователь получит полный ответ,
комментарий main'а будет неполным. Не катастрофа, индикатор что cap мал.
"""
from __future__ import annotations

import os

from langchain.agents.middleware.types import AgentMiddleware, ModelRequest
from langchain_core.messages import HumanMessage, ToolMessage


_PER_SUBAGENT_CAP = int(os.environ.get("FINAL_ANSWER_CAP", "800"))


class FinalAnswerCapMiddleware(AgentMiddleware):
    """См. модульный docstring."""

    def wrap_model_call(self, request: ModelRequest, handler):
        cap = self._compute_cap(request)
        if cap is not None:
            request = self._apply_cap(request, cap)
        return handler(request)

    async def awrap_model_call(self, request: ModelRequest, handler):
        cap = self._compute_cap(request)
        if cap is not None:
            request = self._apply_cap(request, cap)
        return await handler(request)

    @staticmethod
    def _apply_cap(request: ModelRequest, cap: int) -> ModelRequest:
        new_settings = {**(request.model_settings or {}), "max_tokens": cap}
        return request.override(model_settings=new_settings)

    def _compute_cap(self, request: ModelRequest) -> int | None:
        """
        Возвращает max_tokens cap или None (без cap'а).

        Cap включается только если ВСЕ условия:
          1. _PER_SUBAGENT_CAP > 0 (не отключено через env)
          2. это main agent (в request.tools есть 'task')
          3. последнее сообщение — task ToolMessage (main pre-final answer)

        n_task = число task ToolMessage'ей в текущем turn'е (от последнего
        HumanMessage до конца). Cap = n_task * _PER_SUBAGENT_CAP.
        """
        if _PER_SUBAGENT_CAP <= 0:
            return None

        if not self._is_main(request):
            return None

        msgs = request.messages or []
        if not msgs:
            return None

        last = msgs[-1]
        if not (
            isinstance(last, ToolMessage)
            and (getattr(last, "name", "") or "") == "task"
        ):
            # Cap применяется только когда main собирается отвечать после task.
            # Если последнее — другой tool (python_analysis, sample_table) —
            # main делает доп. работу, нужно больше токенов, не cap'им.
            return None

        last_human_idx = self._find_last_human_idx(msgs)
        n_task = sum(
            1
            for m in msgs[last_human_idx:]
            if isinstance(m, ToolMessage) and (getattr(m, "name", "") or "") == "task"
        )
        return max(1, n_task) * _PER_SUBAGENT_CAP

    @staticmethod
    def _is_main(request: ModelRequest) -> bool:
        try:
            tool_names = {getattr(t, "name", "") for t in (request.tools or [])}
            return "task" in tool_names
        except Exception:
            return False

    @staticmethod
    def _find_last_human_idx(msgs: list) -> int:
        for i in range(len(msgs) - 1, -1, -1):
            if isinstance(msgs[i], HumanMessage):
                return i
        return 0
