"""
DynamicContextMiddleware — дописывает в system-prompt блок «Сегодня + НДС»
(Europe/Moscow). Блок кладётся ПЕРЕД тем, как CachingMiddleware расставит
cache_control, поэтому он попадает ВНУТРЬ кэша.

Стоимость: один cache miss в сутки (при смене даты), дальше cache read.
Плюс: блок становится жёсткой частью system prompt — модель обязана его
учитывать, а не использовать training-data дату.

Ставить в middleware-списке ДО CachingMiddleware (outermost / первым).
"""
from __future__ import annotations

from copy import copy
from datetime import datetime

try:
    from zoneinfo import ZoneInfo
    _TZ = ZoneInfo("Europe/Moscow")
except Exception:
    _TZ = None

from langchain.agents.middleware.types import AgentMiddleware, ModelRequest


def _dynamic_block() -> str:
    now = datetime.now(_TZ) if _TZ else datetime.utcnow()
    today = now.date().isoformat()
    year = now.year
    return (
        "\n\n---\n"
        "## Актуальный контекст даты и налога (жёсткая инструкция)\n\n"
        f"- **Сегодня: {today}** (Europe/Moscow).\n"
        f"- Любой относительный период без явного года («за март», «в этом месяце», "
        f"«на прошлой неделе», «вчера», «за квартал») — это **{year} год**. "
        "НЕ используй даты из training-data, используй ТОЛЬКО дату выше как единственный "
        "источник истины о «сейчас».\n"
        "- **НДС в РФ: 22%** (действует с 1 января 2026).\n"
        "    - `cost` в витринах Яндекс.Директа хранится **БЕЗ НДС**.\n"
        "    - «расход с НДС» = `cost × 1.22`; «расход без НДС» = `cost` как есть.\n"
        "    - Если в вопросе НДС не упомянут — отдавай как `cost` (без НДС), но одной "
        "строкой упомяни что это без НДС.\n"
    )


def _clone(msg, new_content):
    try:
        return msg.model_copy(update={"content": new_content})
    except Exception:
        out = copy(msg)
        out.content = new_content
        return out


class DynamicContextMiddleware(AgentMiddleware):
    """
    Append today+VAT block to the END of system prompt content.

    Place FIRST in the middleware list so CachingMiddleware (second) sees
    the modified content and includes the block under cache_control.
    """

    def wrap_model_call(self, request: ModelRequest, handler):
        sm = request.system_message
        if sm is not None:
            block = {"type": "text", "text": _dynamic_block()}
            content = sm.content
            if isinstance(content, str):
                new_content = [{"type": "text", "text": content}, block]
            elif isinstance(content, list):
                new_content = list(content) + [block]
            else:
                new_content = content
            if new_content is not content:
                request.system_message = _clone(sm, new_content)
        return handler(request)
