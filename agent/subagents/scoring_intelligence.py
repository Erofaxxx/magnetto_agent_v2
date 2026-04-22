"""
ScoringIntelligenceAgent — подагент скоринга клиентов и аналитики путей.

Специализируется на таблицах:
  - dm_active_clients_scoring — скоринг клиентов (hot/warm/cold, рекомендации)
  - dm_step_goal_impact      — lift-анализ целей по шагам визитов
  - dm_funnel_velocity        — скорость воронки по когортам
  - dm_path_templates         — паттерны каналов и стоимость конверсии

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
    _SKILLS_DIR / "scoring_clients.md",
    _SKILLS_DIR / "scoring_step_impact.md",
    _SKILLS_DIR / "scoring_funnel_paths.md",
]

_SYSTEM_PROMPT = """Ты — аналитик скоринга клиентов и конверсионных путей. Работаешь с ClickHouse-базой компании Magnetto (девелопер недвижимости, цикл сделки ~70 дней, конверсия ~0.09%).

Твоя задача — анализировать:
- Скоринг клиентов: кто горячий, кого ретаргетить, когда и чем
- Lift-анализ целей: какие действия на каком шаге увеличивают конверсию
- Скорость воронки: как быстро когорты проходят визит→лид→CRM→оплата
- Паттерны каналов: какие цепочки каналов конвертят и за сколько

## Схема базы данных

{schema_section}

## Принципы работы

- Для dm_funnel_velocity фильтруй `cohort_age_days >= 60` при анализе конверсии (молодые когорты не дозрели)
- Для dm_path_templates фильтруй `snapshot_date = (SELECT max(snapshot_date) ...)`
- Для dm_step_goal_impact исключай CRM-тавтологии (332069613, 332069614) и мусор (402733217, 405315077, 405315078, 407450615, 541504123) из рекомендаций
- При расшифровке скора клиента используй JOIN dm_client_journey × dm_step_goal_impact
- Organic в пути клиента — критический фактор конверсии; реклама работает как подогрев
- Числа с разделителями тысяч: 1 234 567
- Язык — русский, Markdown

## Доменные инструкции

{skill_section}"""


class ScoringIntelligenceAgent(BaseSubAgent):
    """Sub-agent for client scoring and conversion path analytics."""

    _SCHEMA_TABLES = ["dm_active_clients_scoring", "dm_step_goal_impact", "dm_funnel_velocity", "dm_path_templates"]

    def __init__(self, model: str = MODEL) -> None:
        skill_text = self._load_skill_files(_SKILL_FILES)
        prompt = _SYSTEM_PROMPT.replace("{skill_section}", skill_text)
        super().__init__(system_prompt=prompt, max_iterations=10, model=model, schema_tables=self._SCHEMA_TABLES)


# ─── Singleton cache ──────────────────────────────────────────────────────────
_agents: dict[str, ScoringIntelligenceAgent] = {}


def get_scoring_agent(model: Optional[str] = None) -> ScoringIntelligenceAgent:
    """Return (or create) a cached ScoringIntelligenceAgent instance."""
    key = model or MODEL
    if key not in _agents:
        _agents[key] = ScoringIntelligenceAgent(model=key)
    return _agents[key]
