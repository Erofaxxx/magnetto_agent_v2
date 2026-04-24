"""
DynamicContextMiddleware — дописывает в конец system-prompt две строки:
сегодняшнюю дату (Europe/Moscow) и ставку НДС. Блок идёт БЕЗ cache_control,
поэтому живёт только текущий запрос и не ломает Anthropic prompt caching.

Ставить в middleware-списке ПОСЛЕ CachingMiddleware (innermost).
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
        f"\nСегодня: {today}. Любой относительный период без года "
        f"(«за март», «в этом месяце», «на прошлой неделе») — это {year} год.\n"
        f"НДС в РФ: 22% (cost в витринах Директа хранится БЕЗ НДС; с НДС = cost × 1.22).\n"
    )


def _clone(msg, new_content):
    try:
        return msg.model_copy(update={"content": new_content})
    except Exception:
        out = copy(msg)
        out.content = new_content
        return out


class DynamicContextMiddleware(AgentMiddleware):
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
