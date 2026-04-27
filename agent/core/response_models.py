"""
Pydantic-модели структурированного ответа подагентов.

Используется через `response_format` в SUBAGENT.md frontmatter:
    ---
    response_format: response_models.SubagentResult
    ---

deepagents оборачивает agent через `with_structured_output(SubagentResult)`,
JSON-сериализует финальный ответ и отдаёт main'у одним ToolMessage. Это
радикально уменьшает токены в контексте main: вместо 1.5-3K токенов
свободного текста — 200-500 токенов структуры.

Поля выбраны так, чтобы main мог:
  - показать пользователю `summary` (готовый markdown)
  - дать дальнейшие задачи на parquet_paths (через python_analysis)
  - сослаться на plot_urls в финальном ответе
  - понять что подагент трогал (used_tables/used_skills) для аудита
  - явно увидеть warnings (data quality)

Всё кроме `summary` опционально — модель не должна выдумывать поля если
их нет.
"""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class SubagentResult(BaseModel):
    """Стандартный структурированный результат любого подагента."""

    summary: str = Field(
        max_length=20000,
        description=(
            "Markdown-ответ главному агенту. Это то что main увидит и почти "
            "дословно покажет пользователю.\n\n"
            "ЖЁСТКИЙ ЛИМИТ: 20000 символов (~5000 токенов). Pydantic физически "
            "обрежет всё, что длиннее. Поэтому: для большой выборки данных "
            "(сотни строк, детальные срезы) — выгружай в parquet и кладёшь "
            "путь(и) в `parquet_paths`; в `summary` оставляй агрегированную "
            "сводку + топ-N значимых строк + комментарии. НЕ дублируй данные "
            "из parquet'а в markdown.\n\n"
            "Включай в summary: ключевые цифры, агрегированные срезы (per-канал, "
            "per-кампания), методологию, противоречия, инсайты. Не дублируй "
            "то, что уже есть в parquet'е."
        )
    )
    parquet_paths: list[str] = Field(
        default_factory=list,
        description=(
            "Виртуальные пути parquet-файлов с детальными данными "
            "(`/parquet/<hash>.parquet`). Main может потом передать их в "
            "python_analysis для дополнительной обработки."
        ),
    )
    plot_urls: list[str] = Field(
        default_factory=list,
        description=(
            "Ссылки на построенные графики в виде `/plots/<file>.png`. "
            "Main вставит их в финальный ответ пользователю."
        ),
    )
    used_tables: list[str] = Field(
        default_factory=list,
        description="Имена таблиц ClickHouse, к которым обращался subagent (для аудита).",
    )
    used_skills: list[str] = Field(
        default_factory=list,
        description="Названия SKILL.md, тело которых читал subagent.",
    )
    warnings: list[str] = Field(
        default_factory=list,
        description=(
            "⚠ Замечания о качестве данных: малая выборка, NULL в важных "
            "колонках, устаревший snapshot, методологические ограничения."
        ),
    )
