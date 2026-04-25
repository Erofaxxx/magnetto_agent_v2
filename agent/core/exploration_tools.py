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
        Discovery-tool: получить N строк из таблицы ClickHouse, чтобы УВИДЕТЬ
        реальные данные перед тем как принимать решения.

        ## ЗОВИ ЭТОТ TOOL когда:

        - В вопросе пользователя упоминается значение, которого ты не знаешь.
          Пример: "покажи кампании в audit-magnetto-tab2" — что такое
          'audit-magnetto-tab2'? Возьми sample_table таблицы со столбцом
          cabinet_name (например dm_direct_performance) — увидишь, что это
          один из значений колонки cabinet_name. После этого сразу делегируй
          с правильным фильтром, без гаданий типа "это файл / utm / вкладка".

        - Не уверен какие литералы бывают в LowCardinality / enum-колонке
          (cabinet_name, traffic_source, status, state, zone_status, health,
          ad_type, и т.д.).

        - Хочешь убедиться что Array-поле (goalsID, channels_path,
          semantic_tags, priority_goal_ids) выглядит как ты думаешь — перед
          тем как писать ARRAY JOIN или hasAny.

        - Видишь странные числа в ответе подагента и хочешь спот-чек самой
          выборки.

        ## НЕ ЗОВИ когда:

        - Уже знаешь ответ из data_map.md или предыдущего turn'а.
        - Нужны агрегаты/группировки/большой SELECT — это работа подагента,
          делегируй через task(). sample_table — это
          ТОЛЬКО подсмотр 5 строк, не анализ.

        ## Что возвращает

        Markdown-таблица с N строками (max 20). Автофильтр по последнему
        report_date / snapshot_date или WHERE date < today() — чтобы не
        схватить неполный текущий день. Длинные строки усекаются до 200
        символов, общий результат cap ~4KB.

        Args:
            table_name: точное имя таблицы (без префикса 'magnetto.').
            n: сколько строк вернуть, 1..20, по умолчанию 5.
        """
        if table_name not in allowed:
            return (
                f"⛔ sample_table: таблица '{table_name}' недоступна в этом scope. "
                f"Доступны: {sorted(allowed) if allowed else '(пусто)'}. "
                "Если ты подагент — эскалируй главному агенту, чтобы он "
                "переключил маршрут на подходящего subagent'а."
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


def make_describe_table_tool(allowed_tables: Iterable[str]):
    """
    Factory: возвращает @tool `describe_table` с baked-in allowed_tables.

    Дешёвый discovery-tool: возвращает полную схему ОДНОЙ таблицы (имена
    колонок + типы) без обращения к ClickHouse — всё из in-process SchemaCache.
    Используется подагентом ДО написания SQL чтобы не гадать имена колонок.

    Аналог `Read` в Claude Code: дешевле чем list_tables (который тащит ВСЕ
    схемы), точнее чем sample_table (который ещё и SELECT делает).
    """
    allowed = set(allowed_tables)

    @tool
    def describe_table(table_name: str) -> str:
        """
        Полная схема таблицы ClickHouse: список колонок с типами в порядке
        position. Используй ДО написания SQL когда нужно увидеть точные
        имена и типы колонок (особенно для JOIN — типы должны совпадать).

        Дешёвый tool: данные из in-process SchemaCache, без обращения к CH.
        НЕ показывает данные (для этого sample_table). НЕ делает SELECT.

        Args:
            table_name: точное имя таблицы (без префикса 'magnetto.').

        Returns:
            Markdown с таблицей `# | name | type` или ⛔ если таблица не в scope.
        """
        if table_name not in allowed:
            return (
                f"⛔ describe_table: таблица '{table_name}' недоступна в этом scope. "
                f"Доступны: {sorted(allowed) if allowed else '(пусто)'}."
            )
        cache = get_schema_cache()
        return cache.render_table_reference(table_name)

    return describe_table
