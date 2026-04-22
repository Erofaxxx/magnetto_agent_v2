"""
DirectOptimizerAgent — подагент оптимизации Яндекс Директа.

Специализируется на таблицах:
  - bad_keywords  — рейтинг ключевых фраз (zone_status, bid_zone, goal_score)
  - bad_placements — рейтинг площадок РСЯ
  - bad_queries   — рейтинг поисковых запросов (is_chronic, автотаргетинг)
  - dm_direct_performance — статистика Директа по кампаниям

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
    _SKILLS_DIR / "direct_keywords_placements.md",
    _SKILLS_DIR / "direct_queries.md",
    _SKILLS_DIR / "direct_performance.md",
]

_SYSTEM_PROMPT = """Ты — аналитик оптимизации Яндекс Директа. Работаешь с ClickHouse-базой компании Magnetto (девелопер недвижимости).

Твоя задача — анализировать эффективность рекламных кампаний:
- Находить неэффективные ключевые слова, площадки и поисковые запросы
- Формировать отчёты по Директу (расходы, лиды, CRM, ROAS)
- Сравнивать SEARCH и РСЯ
- Давать рекомендации по оптимизации бюджета

## Схема базы данных

{schema_section}

## Принципы работы

- Отвечай конкретно: цифры, таблицы, выводы
- При анализе zone_status учитывай контекст (брендовые фразы, сезонность, нишевые площадки)
- Всегда фильтруй `WHERE date < today()` для dm_direct_performance (данные за сегодня неполные)
- Используй `nullIf(..., 0)` в знаменателях дробей
- Числа с разделителями тысяч: 1 234 567
- Язык — русский, Markdown

## Доменные инструкции

{skill_section}"""


class DirectOptimizerAgent(BaseSubAgent):
    """Sub-agent for Yandex Direct campaign optimisation."""

    _SCHEMA_TABLES = ["bad_keywords", "bad_placements", "bad_queries", "dm_direct_performance"]

    def __init__(self, model: str = MODEL) -> None:
        skill_text = self._load_skill_files(_SKILL_FILES)
        prompt = _SYSTEM_PROMPT.replace("{skill_section}", skill_text)
        super().__init__(system_prompt=prompt, max_iterations=10, model=model, schema_tables=self._SCHEMA_TABLES)


# ─── Singleton cache ──────────────────────────────────────────────────────────
_agents: dict[str, DirectOptimizerAgent] = {}


def get_direct_optimizer(model: Optional[str] = None) -> DirectOptimizerAgent:
    """Return (or create) a cached DirectOptimizerAgent instance."""
    key = model or MODEL
    if key not in _agents:
        _agents[key] = DirectOptimizerAgent(model=key)
    return _agents[key]
