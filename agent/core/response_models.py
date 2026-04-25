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
        description=(
            "Markdown-ответ главному агенту. Это то что main увидит и почти "
            "дословно покажет пользователю.\n\n"
            "Длина — ровно та, которую требует задача. Короткий вопрос → "
            "короткий ответ. Глубокий аналитический вопрос → подробный "
            "многосекционный отчёт с раскладкой по срезам, таблицами с "
            "десятками строк, методологическими комментариями, контекстом, "
            "выводами. НЕ сокращай ради краткости. Многостраничный аналитический "
            "ответ — нормально и желательно если того требует задача.\n\n"
            "Включай: все ключевые цифры, ВСЕ строки таблицы которая отвечает "
            "на вопрос, методологию (как считал), допущения, противоречия в "
            "данных, рекомендации/инсайты по месту."
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
