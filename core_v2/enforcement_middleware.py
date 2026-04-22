"""
EnforcementMiddleware — принудительное маршрутизирование через subagents.

Три механизма:

1. RoutingEnforcer
   Блокирует `clickhouse_query` с GROUP BY / JOIN / WITH / ARRAY JOIN, если
   в этом turn'е ещё не было вызова task() или delegate_to_generalist().
   Возвращает ToolMessage с инструкцией: делегируй + список таблиц.

2. HardcodeDetector
   Блокирует `python_analysis` с паттернами pd.DataFrame({'x': [lit, lit, lit, ...]}).
   Эти паттерны — главная причина искажённых данных (T8). Если python_analysis
   написан "по памяти" из предыдущего ToolMessage — агент должен использовать `df`
   или pd.read_parquet(path).

3. MultiSkillRequirement
   При вызове delegate_to_generalist с skills=[только один] и сложным SQL
   требует 2+ skills (или xорошее обоснование).

Middleware работают в `wrap_tool_call` — LLM получает ToolMessage с объяснением
и должен исправиться, вместо реального выполнения ошибочного вызова.
"""
from __future__ import annotations

import json
import re
from typing import Any

from langchain.agents.middleware.types import AgentMiddleware
from langchain_core.messages import AIMessage, ToolMessage


# ─── Helpers ────────────────────────────────────────────────────────────────

_COMPLEX_SQL_PATTERNS = (
    r"\bGROUP\s+BY\b",
    r"\bJOIN\b",
    r"\bWITH\s+\w+\s+AS\b",      # CTE
    r"\bARRAY\s+JOIN\b",
    r"\bUNION\s+(ALL|DISTINCT)?\b",
    r"\bWINDOW\b",
    r"\bOVER\s*\(",               # window functions
)
_SIMPLE_ALLOWED_PATTERNS = (
    # SELECT count() ... no GROUP BY
    # SELECT MIN/MAX(col) ...
    # SELECT DISTINCT col ... LIMIT N
    # DESCRIBE TABLE ...
    # SELECT * FROM x LIMIT N (preview — also разрешено для проверки схемы)
    # Эти паттерны ОК если нет complex patterns выше
)


def _sql_is_complex(sql: str) -> bool:
    """Return True if SQL contains GROUP BY / JOIN / CTE / UNION / ARRAY JOIN."""
    if not sql:
        return False
    for p in _COMPLEX_SQL_PATTERNS:
        if re.search(p, sql, re.IGNORECASE):
            return True
    return False


def _sql_is_trivially_simple(sql: str) -> bool:
    """
    Whitelist for self-service SQL. True only if:
    - COUNT(*) / COUNT() без GROUP BY
    - MIN/MAX одной колонки
    - DISTINCT с LIMIT <= 100
    - SELECT * FROM x LIMIT N (preview без агрегации)
    - DESCRIBE / SHOW TABLES / SHOW COLUMNS
    """
    if not sql:
        return True
    s = sql.strip().upper()

    # DESCRIBE / SHOW *
    if s.startswith(("DESCRIBE ", "DESC ", "SHOW ")):
        return True

    # No complex patterns, что точно simple
    if _sql_is_complex(sql):
        return False

    # SELECT COUNT() / SELECT min/max(...) — single aggregate
    if re.match(r"^\s*SELECT\s+(COUNT\s*\(\s*(\*|\w+)?\s*\)|MIN\s*\(|MAX\s*\()",
                sql, re.IGNORECASE):
        return True

    # SELECT DISTINCT col FROM ... LIMIT N (N<=100)
    m = re.search(r"LIMIT\s+(\d+)", sql, re.IGNORECASE)
    if re.search(r"^\s*SELECT\s+DISTINCT\s", sql, re.IGNORECASE):
        if m and int(m.group(1)) <= 100:
            return True

    # SELECT * FROM X LIMIT N — preview
    if re.search(r"^\s*SELECT\s+\*\s+FROM\s+\w+\s*(WHERE\s+[^()]*)?\s*LIMIT\s+\d+\s*$",
                 sql, re.IGNORECASE):
        return True

    return False


def _turn_had_delegation(messages: list) -> bool:
    """Check if current turn (since last HumanMessage) already had task / delegate_to_generalist."""
    from langchain_core.messages import HumanMessage
    last_human_idx = -1
    for i, m in enumerate(messages):
        if isinstance(m, HumanMessage):
            last_human_idx = i
    if last_human_idx < 0:
        return False

    for m in messages[last_human_idx:]:
        if not isinstance(m, AIMessage):
            continue
        for tc in (getattr(m, "tool_calls", []) or []):
            name = tc.get("name", "")
            if name in ("task", "delegate_to_generalist"):
                return True
    return False


# ─── Routing Enforcer ────────────────────────────────────────────────────────

_ROUTING_REDIRECT_MESSAGE = """\
⛔ БЛОКИРОВКА: этот clickhouse_query содержит {patterns_found} — сложные запросы
ОБЯЗАНЫ идти через делегирование.

ПОЧЕМУ: subagent (specialized или generalist) получает в свой system prompt
доменные skills и полную схему таблиц. Когда ты пишешь сложный SQL сам, у тебя
НЕТ skill-контекста — ответ может быть технически корректным, но методологически
слабым (как было в T8 с атрибуцией: данные исказились при переносе в Python).

ЧТО ДЕЛАТЬ:
1. Определи тип запроса:
   - Яндекс Директ (bad_keywords, bad_placements, bad_queries, campaigns_settings,
     dm_direct_performance) → task(subagent_type="direct-optimizer", description=...)
   - Скоринг / воронка / брифинг (dm_active_clients_scoring, dm_step_goal_impact,
     dm_funnel_velocity, dm_path_templates, report_daily_briefing) →
     task(subagent_type="scoring-intelligence", description=...)
   - Всё остальное → delegate_to_generalist(task=..., tables=[...], skills=[...])

2. Для delegate_to_generalist ОБЯЗАТЕЛЬНО укажи в skills минимум 2 скилла:
   - один обязательный: "clickhouse-basics"
   - один или больше доменных из: attribution, cohort-analysis, campaign-analysis,
     segmentation, anomaly-detection, goals-reference, weekly-report

Твой исходный SQL был:
```sql
{sql_preview}
```

Перепиши как tool call task(...) или delegate_to_generalist(...)."""


class RoutingEnforcer(AgentMiddleware):
    """Forces complex SQL through delegation, skipping trivially simple queries."""

    def wrap_tool_call(self, request, handler):
        tool_name = getattr(request, "tool", None)
        tool_name = getattr(tool_name, "name", None) if tool_name else None

        if tool_name != "clickhouse_query":
            return handler(request)

        # Extract sql arg
        args = getattr(request, "tool_call", {}).get("args", {}) if hasattr(request, "tool_call") else {}
        if not args:
            # Try alternative structure
            args = getattr(request, "args", {}) or {}
        sql = args.get("sql", "") if isinstance(args, dict) else ""

        # Allow trivially simple
        if _sql_is_trivially_simple(sql):
            return handler(request)

        # Only enforce if complex AND no delegation yet in this turn
        if not _sql_is_complex(sql):
            return handler(request)

        state = getattr(request, "state", None)
        messages = (state or {}).get("messages", []) if isinstance(state, dict) else []
        if _turn_had_delegation(messages):
            # Already delegated in this turn — allow post-processing SQL
            return handler(request)

        # Block: return ToolMessage with redirect
        patterns = [p for p in ("GROUP BY", "JOIN", "WITH", "ARRAY JOIN", "UNION", "WINDOW")
                    if re.search(rf"\b{p.replace(' ', r'\\s+')}\b", sql, re.IGNORECASE)]
        msg = _ROUTING_REDIRECT_MESSAGE.format(
            patterns_found=", ".join(patterns),
            sql_preview=sql[:500] + ("..." if len(sql) > 500 else ""),
        )

        tool_call_id = (
            args.get("tool_call_id")
            or getattr(request, "tool_call_id", None)
            or (getattr(request, "tool_call", {}) or {}).get("id", "")
        )

        return ToolMessage(content=msg, tool_call_id=tool_call_id or "", name="clickhouse_query")

    async def awrap_tool_call(self, request, handler):
        tool_name = getattr(request, "tool", None)
        tool_name = getattr(tool_name, "name", None) if tool_name else None
        if tool_name != "clickhouse_query":
            return await handler(request)

        args = getattr(request, "tool_call", {}).get("args", {}) if hasattr(request, "tool_call") else {}
        if not args:
            args = getattr(request, "args", {}) or {}
        sql = args.get("sql", "") if isinstance(args, dict) else ""

        if _sql_is_trivially_simple(sql):
            return await handler(request)
        if not _sql_is_complex(sql):
            return await handler(request)

        state = getattr(request, "state", None)
        messages = (state or {}).get("messages", []) if isinstance(state, dict) else []
        if _turn_had_delegation(messages):
            return await handler(request)

        patterns = [p for p in ("GROUP BY", "JOIN", "WITH", "ARRAY JOIN", "UNION", "WINDOW")
                    if re.search(rf"\b{p.replace(' ', r'\\s+')}\b", sql, re.IGNORECASE)]
        msg = _ROUTING_REDIRECT_MESSAGE.format(
            patterns_found=", ".join(patterns),
            sql_preview=sql[:500] + ("..." if len(sql) > 500 else ""),
        )

        tool_call_id = (
            args.get("tool_call_id")
            or getattr(request, "tool_call_id", None)
            or (getattr(request, "tool_call", {}) or {}).get("id", "")
        )
        return ToolMessage(content=msg, tool_call_id=tool_call_id or "", name="clickhouse_query")


# ─── Hardcode Detector ───────────────────────────────────────────────────────

# Detect pd.DataFrame({'col': [lit, lit, ...], ...}) with >=4 literal values
# OR dict({'col': [lit, lit, lit, lit], ...}) что то
# Heuristic: find pd.DataFrame({ и проверяем что внутри много literal-чисел подряд.
_HARDCODE_PATTERNS = [
    # pd.DataFrame({ ... 'col': [1, 2, 3, 4, ...] ... })
    # Ловим pd.DataFrame(...) где есть список из 4+ numeric/string литералов
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
2. Если нужны данные из другого SQL — либо новый clickhouse_query + парket_path в
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


# ─── Combined export ────────────────────────────────────────────────────────

def build_enforcement_middleware() -> list:
    """Return list of enforcement middleware instances in the correct order."""
    return [RoutingEnforcer(), HardcodeDetector()]
