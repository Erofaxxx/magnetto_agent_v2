"""
EnforcementMiddleware — страховочные middleware на wrap_tool_call.

Сейчас один механизм: `HardcodeDetector`.

  Блокирует `python_analysis` с паттернами pd.DataFrame({'x': [lit, lit, lit, ...]}).
  Эти паттерны — главная причина искажённых данных (T8). Если python_analysis
  написан "по памяти" из предыдущего ToolMessage — агент должен использовать `df`
  или pd.read_parquet(path).

Прежний `RoutingEnforcer` удалён: main-agent больше не имеет доступа к
`clickhouse_query` (физическое ограничение через список tools в agent_factory),
блокировать нечего.

Middleware работают в `wrap_tool_call` — LLM получает ToolMessage с объяснением
и должен исправиться, вместо реального выполнения ошибочного вызова.
"""
from __future__ import annotations

import re

from langchain.agents.middleware.types import AgentMiddleware
from langchain_core.messages import ToolMessage


# ─── Hardcode Detector ───────────────────────────────────────────────────────

# Detect pd.DataFrame({'col': [lit, lit, ...], ...}) with >=4 literal values
# OR dict({'col': [lit, lit, lit, lit], ...}).
_HARDCODE_PATTERNS = [
    # pd.DataFrame({ ... 'col': [1, 2, 3, 4, ...] ... })
    re.compile(
        r"pd\.DataFrame\s*\(\s*\{[\s\S]*?\[\s*"
        r"(?:[+-]?\d+\.?\d*|'[^']*'|\"[^\"]*\")"
        r"(?:\s*,\s*(?:[+-]?\d+\.?\d*|'[^']*'|\"[^\"]*\")){3,}"
        r"[\s\S]*?\}\s*\)",
        re.MULTILINE,
    ),
    # dict-style: data = {'visits': [18340, 19804, 17095, ...]}
    re.compile(
        r"=\s*\{[\s\S]*?:\s*\[\s*"
        r"(?:[+-]?\d+\.?\d*|'[^']*'|\"[^\"]*\")"
        r"(?:\s*,\s*(?:[+-]?\d+\.?\d*|'[^']*'|\"[^\"]*\")){3,}"
        r"[\s\S]*?\}",
        re.MULTILINE,
    ),
]

_HARDCODE_REDIRECT_MESSAGE = """\
⛔ БЛОКИРОВКА: в твоём python_analysis коде обнаружен ХАРДКОД данных
(pd.DataFrame({{'col': [lit, lit, lit, ...]}} или dict с длинным списком литералов).

ПОЧЕМУ: когда ты копируешь значения из предыдущего ToolMessage в код, LLM-память
искажает числа. Пример (T8): ты написал `credits: [99, 18, 8, 2, 2]`, а в parquet
было `[99, 16, 9, 3, 2]` — НЕТ совпадений по 4 из 5 значений. Маркетолог получает
ложный отчёт.

ЧТО ДЕЛАТЬ:
1. Используй `df` — он УЖЕ загружен из переданного parquet_path.
2. Если нужны данные из другого SQL — либо новый clickhouse_query + parquet_path в
   следующий python_analysis, либо `pd.read_parquet('/parquet/<hash>.parquet')`
   в текущем коде.
3. Если нужно объединить данные — пиши SQL с UNION ALL или JOIN через delegation,
   не строй DataFrame из литералов.

Твой код (первые 500 символов):
```python
{code_preview}
```

Перепиши без хардкода. Работай с df или читай parquet по пути."""


class HardcodeDetector(AgentMiddleware):
    """Blocks python_analysis when code contains pd.DataFrame with literal lists."""

    def _check(self, request):
        tool_name = getattr(request, "tool", None)
        tool_name = getattr(tool_name, "name", None) if tool_name else None
        if tool_name != "python_analysis":
            return None

        args = getattr(request, "tool_call", {}).get("args", {}) if hasattr(request, "tool_call") else {}
        if not args:
            args = getattr(request, "args", {}) or {}
        code = args.get("code", "") if isinstance(args, dict) else ""

        for pat in _HARDCODE_PATTERNS:
            if pat.search(code):
                tool_call_id = (
                    args.get("tool_call_id")
                    or getattr(request, "tool_call_id", None)
                    or (getattr(request, "tool_call", {}) or {}).get("id", "")
                )
                msg = _HARDCODE_REDIRECT_MESSAGE.format(
                    code_preview=code[:500] + ("..." if len(code) > 500 else "")
                )
                return ToolMessage(content=msg, tool_call_id=tool_call_id or "", name="python_analysis")
        return None

    def wrap_tool_call(self, request, handler):
        blocked = self._check(request)
        if blocked is not None:
            return blocked
        return handler(request)

    async def awrap_tool_call(self, request, handler):
        blocked = self._check(request)
        if blocked is not None:
            return blocked
        return await handler(request)
