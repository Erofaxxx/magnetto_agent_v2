"""
Exploration tools — даём подагенту посмотреть живой срез данных, не только
зашитую в system-prompt схему колонок.

Пока один tool: `sample_table(table_name, n=5)`.
Scope — в замыкании: каждый подагент получает свой `sample_table`, который
разрешает смотреть ТОЛЬКО таблицы из его `schema_tables`. Это предотвращает
выход подагента за пределы своей доменной зоны.

Фильтрация:
  - если в таблице есть `report_date` → `WHERE report_date = (SELECT max(report_date) ...)`
  - если есть `snapshot_date` → аналогично по snapshot_date
  - если есть `date` (транзакционная) → `WHERE date < today()` (сегодняшний день может быть неполным)
  - иначе без фильтра, просто LIMIT

Truncation:
  - строковые поля > 200 символов — обрезаются с '…'
  - итоговый markdown cap по длине ~4000 chars (чтобы не раздувать tool_result)
"""
from __future__ import annotations

from typing import Iterable

from langchain_core.tools import tool

from .schema_cache import get_schema_cache


_MAX_N = 20
_CELL_MAX_CHARS = 200
_RESULT_MAX_CHARS = 4000


def _pick_date_filter(table_name: str) -> tuple[str, str]:
    """
    Вернёт (where_clause, order_clause) на основе известной схемы таблицы.
    Пустые строки если колонки даты не найдены.
    """
    cache = get_schema_cache()
    cols = cache.get(table_name) or []
    col_names = {c.get("name") for c in cols if c.get("name")}

    # Snapshot-like (приоритет report_date над snapshot_date — у нас чаще так)
    if "report_date" in col_names:
        return (
            f"WHERE report_date = (SELECT max(report_date) FROM magnetto.{table_name})",
            "ORDER BY report_date DESC",
        )
    if "snapshot_date" in col_names:
        return (
            f"WHERE snapshot_date = (SELECT max(snapshot_date) FROM magnetto.{table_name})",
            "",
        )
    # Transactional
    if "date" in col_names:
        return ("WHERE date < today()", "ORDER BY date DESC")
    return ("", "")


def _truncate_cell(v) -> str:
    s = "NULL" if v is None else str(v)
    if len(s) > _CELL_MAX_CHARS:
        return s[: _CELL_MAX_CHARS - 1] + "…"
    return s


def make_sample_table_tool(allowed_tables: Iterable[str]):
    """
    Factory: возвращает @tool `sample_table` с baked-in allowed_tables.

    Подагент получает инструмент, которым может смотреть только свои таблицы.
    """
    allowed = set(allowed_tables)

    @tool
    def sample_table(table_name: str, n: int = 5) -> str:
        """
        Получить первые N строк из таблицы ClickHouse, чтобы увидеть реальные данные.

        Когда использовать:
        - Узнать какие значения бывают в enum/LowCardinality колонках
          (cabinet_name, traffic_source, status, zone_status, …).
        - Проверить формат Array-поля (goalsID, channels_path, semantic_tags).
        - Поймать NULL / пустые строки, сюрпризы в кейсе/пробелах.
        - Убедиться что данные выглядят ожидаемо, ПЕРЕД тем как писать сложный SQL.

        Возвращает Markdown-таблицу с N строками (max 20). Автоматически
        фильтрует по последнему report_date / snapshot_date или WHERE date < today()
        чтобы не схватить неполный текущий день.

        Args:
            table_name: точное имя таблицы из ТВОЕГО schema_tables (см. system prompt).
            n: сколько строк вернуть, 1..20, по умолчанию 5.
        """
        if table_name not in allowed:
            return (
                f"⛔ sample_table: таблица '{table_name}' не входит в твой schema_tables. "
                f"Разрешены: {sorted(allowed) if allowed else '(пусто)'}. "
                "Если нужно посмотреть другую — эскалируй главному агенту, он "
                "переключит маршрут на подходящего subagent'а."
            )

        n = max(1, min(int(n), _MAX_N))
        where, order = _pick_date_filter(table_name)
        sql = f"SELECT * FROM magnetto.{table_name} {where} {order} LIMIT {n}".strip()
        # Collapse double spaces from empty where/order
        sql = " ".join(sql.split())

        try:
            from tools import _get_ch_client  # lazy to avoid import cycles
            ch = _get_ch_client()
            result = ch.execute_query(sql)
            if not result.get("success"):
                return f"⚠ sample_table: ClickHouse error — {result.get('error', 'unknown')}. SQL: `{sql}`"

            import pandas as pd
            df = pd.read_parquet(result["parquet_path"])

            if df.empty:
                return f"sample_table({table_name}, n={n}): 0 строк с фильтром `{where or 'без фильтра'}`. Попробуй другую таблицу или без фильтра."

            # Truncate long cells
            df_display = df.astype(object).map(_truncate_cell) if hasattr(df, "map") \
                else df.astype(object).applymap(_truncate_cell)

            try:
                md = df_display.to_markdown(index=False)
            except Exception:
                # markdown может упасть если установки нет — fallback на простой формат
                header = " | ".join(df_display.columns)
                sep = " | ".join(["---"] * len(df_display.columns))
                body = "\n".join(" | ".join(str(v) for v in row) for row in df_display.values)
                md = f"| {header} |\n| {sep} |\n{body}"

            if len(md) > _RESULT_MAX_CHARS:
                md = md[: _RESULT_MAX_CHARS] + f"\n\n… (truncated; всего вернулось {len(df)} строк × {len(df.columns)} колонок)"

            return f"sample of `{table_name}` ({len(df)} rows, фильтр: `{where or 'none'}`):\n\n{md}"
        except Exception as exc:  # pragma: no cover
            return f"⚠ sample_table failed: {exc}"

    return sample_table
