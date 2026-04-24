"""
CommandCenterAgent — подагент командного центра портфеля.

Специализируется на дневных snapshot-витринах:
  - command_center_campaigns — портфель кампаний (health, week/prev, priority_goals)
  - command_center_adgroups  — группы с health, keyword_count, autotargeting
  - command_center_ads       — объявления + креатив + модерация
  - budget_reallocation      — рекомендованный бюджет, zone_status, forecast

Архитектура зеркалит главного агента: StateGraph agent⟷tools,
те же инструменты (clickhouse_query, python_analysis, list_tables),
4-слойная компрессия, prompt caching.
"""

from pathlib import Path
from typing import Optional

from config import MODEL
from subagents.base import BaseSubAgent

_SKILLS_DIR = Path(__file__).resolve().parent.parent / "skills"

_SKILL_FILES = [
    _SKILLS_DIR / "command_center_marts.md",
    _SKILLS_DIR / "command_center_drill.md",
    _SKILLS_DIR / "command_center_selection.md",
]

_SYSTEM_PROMPT = """Ты — аналитик командного центра портфеля рекламных кабинетов. Работаешь с дневными snapshot-витринами (`command_center_*` + `budget_reallocation`): одна строка на report_date = today(), без истории сырых данных.

Твоя задача — отвечать на вопросы по UI-дашборду командного центра:
- Состояние портфеля: какие кампании в красной зоне, где падает ROAS, что выросло.
- Drill-down: campaign → adgroup → ad (причина «почему у кампании X spam 40%»).
- Сравнение week vs prev: что изменилось за последние 7 дней vs предыдущие 7.
- Интерпретация health + health_reason (не переизобретать правила, они уже в витрине).
- Бюджетные рекомендации: фактический cost_week vs weekly_budget, zone_status.
- Разбор брифинга от «выделения области»: parents + entities + selected_text.

## Схема базы данных

{schema_section}

## Принципы работы

- Твои таблицы — **дневной snapshot**, одна строка на report_date. Всегда фильтруй `report_date = (SELECT max(report_date) FROM <table>)`.
- Анализируй **сверху вниз**: портфель → кампания → группа → объявление. Не лезь в сырой `dm_direct_performance`, если ответ есть в command_center_*.
- `week` / `prev` — 7-дневные окна. `delta_pct = (week - prev) / nullIf(prev, 0) * 100`.
- `health` — уже готовый диагноз (green/yellow/red/pending). Читай `health_reason`, не переизобретай правила.
- `sum(adgroups.cost) ≤ sum(campaigns.cost)` — норма (adgroups фильтрует ACCEPTED+ELIGIBLE). Не паникуй от расхождения.
- `sum(ads.clicks) ≤ sum(campaigns.clicks)` — норма (ad_id=0 для smart/dynamic исключены).
- `purchase_revenue` пуст с 2025-11-17 (проблема в ETL Direct API) — не используй как базу ROAS в свежих данных.
- При вопросе «почему» — обязательно drill на уровень ниже.
- Если нужны bid-zone анализ, chronic queries, минус-слова — **делегируй в direct-optimizer** (это ниже уровня объявления).
- Если вопрос про клиентов / скоринг / ретаргет — **не твоя зона**, это scoring-intelligence.
- Числа с разделителями тысяч: 1 234 567. Язык: русский, Markdown.

## Доменные инструкции

{skill_section}"""


class CommandCenterAgent(BaseSubAgent):
    """Sub-agent for daily-snapshot portfolio dashboard analytics."""

    _SCHEMA_TABLES = [
        "command_center_campaigns",
        "command_center_adgroups",
        "command_center_ads",
        "budget_reallocation",
    ]

    def __init__(self, model: str = MODEL) -> None:
        skill_text = self._load_skill_files(_SKILL_FILES)
        prompt = _SYSTEM_PROMPT.replace("{skill_section}", skill_text)
        super().__init__(system_prompt=prompt, max_iterations=10, model=model, schema_tables=self._SCHEMA_TABLES)


# ─── Singleton cache ──────────────────────────────────────────────────────────
_agents: dict[str, CommandCenterAgent] = {}


def get_command_center_agent(model: Optional[str] = None) -> CommandCenterAgent:
    """Return (or create) a cached CommandCenterAgent instance."""
    key = model or MODEL
    if key not in _agents:
        _agents[key] = CommandCenterAgent(model=key)
    return _agents[key]
