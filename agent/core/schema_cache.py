"""
Singleton in-process cache для схем таблиц ClickHouse.

Используется:
  - specialized subagents (direct-optimizer, scoring-intelligence) — при старте
    рендерят свой schema_section из этого кэша
  - generalist subagent — schema_tables: ["*"], получает все таблицы
    на лету из tables, переданных main agent'ом

Обновление: при рестарте процесса (для прод это достаточно — systemctl restart
analytics-agent при изменениях). Можно добавить ручной refresh через API,
но для MVP — только startup load.

Детерминированный рендер: таблицы в schema_section всегда сортируются по имени,
столбцы — в естественном порядке БД (position). Это обеспечивает cache hit
для Anthropic prompt caching.
"""
from __future__ import annotations

import threading
from typing import Iterable


class SchemaCache:
    """In-process кэш схем таблиц ClickHouse."""

    _instance: "SchemaCache | None" = None
    _init_lock = threading.Lock()

    def __new__(cls) -> "SchemaCache":
        if cls._instance is None:
            with cls._init_lock:
                if cls._instance is None:
                    inst = super().__new__(cls)
                    inst._tables = {}       # name -> list[{'name': str, 'type': str}]
                    inst._loaded = False
                    inst._lock = threading.Lock()
                    cls._instance = inst
        return cls._instance

    # ─── Loading ─────────────────────────────────────────────────────────────

    def load(self, ch_client=None) -> None:
        """
        Загрузить схему всех таблиц базы (один раз при старте процесса).

        Args:
            ch_client: клиент ClickHouseClient. Если None — импортируется
                       из tools._get_ch_client().
        """
        with self._lock:
            if self._loaded:
                return

            if ch_client is None:
                try:
                    from tools import _get_ch_client  # type: ignore
                    ch_client = _get_ch_client()
                except Exception as exc:
                    raise RuntimeError(
                        f"SchemaCache.load: could not get ClickHouse client: {exc}"
                    ) from exc

            tables = ch_client.list_tables()
            for t in tables:
                name = t.get("table")
                cols = t.get("columns", [])
                if name and cols:
                    # normalize: list of {name, type}
                    norm = []
                    for c in cols:
                        if isinstance(c, dict):
                            norm.append({"name": c.get("name"), "type": c.get("type", "")})
                        else:
                            norm.append({"name": str(c), "type": ""})
                    self._tables[name] = norm

            self._loaded = True
            print(f"✅ SchemaCache loaded: {len(self._tables)} tables")

    def is_loaded(self) -> bool:
        return self._loaded

    def reload(self, ch_client=None) -> None:
        """Force reload (при обновлении схемы без рестарта)."""
        with self._lock:
            self._tables.clear()
            self._loaded = False
        self.load(ch_client=ch_client)

    # ─── Access ──────────────────────────────────────────────────────────────

    def get(self, table_name: str) -> list[dict] | None:
        """Вернуть столбцы таблицы или None если нет."""
        return self._tables.get(table_name)

    def all_table_names(self) -> list[str]:
        return sorted(self._tables.keys())

    def render_schema_section(self, table_names: Iterable[str]) -> str:
        """
        Детерминированно отрендерить секцию схемы для subagent system prompt.

        Таблицы сортируются по имени (для стабильного cache-control),
        столбцы — в естественном порядке position (как из БД).

        Args:
            table_names: имена таблиц для включения в секцию.

        Returns:
            Markdown-строка со схемой:
              **table_name**: col1 Type1, col2 Type2, ...
        """
        # Filter and sort deterministically
        wanted = sorted(set(name for name in table_names if name in self._tables))
        if not wanted:
            return "Схема таблиц недоступна."

        lines = []
        for name in wanted:
            cols = self._tables.get(name, [])
            parts = [f"{c['name']} {c['type']}" for c in cols if c.get("name")]
            lines.append(f"**{name}** ({len(cols)} cols): " + ", ".join(parts))
        return "\n\n".join(lines)

    def render_table_reference(self, table_name: str) -> str:
        """Отрендерить полную таблицу в markdown для одной таблицы (детально)."""
        cols = self._tables.get(table_name)
        if not cols:
            return f"⚠ Таблица `{table_name}` не найдена в SchemaCache."
        lines = [f"## `{table_name}` ({len(cols)} колонок)\n"]
        lines.append("| # | name | type |")
        lines.append("|---|------|------|")
        for i, c in enumerate(cols, 1):
            lines.append(f"| {i} | `{c['name']}` | `{c.get('type', '')}` |")
        return "\n".join(lines)


# ─── Compact data_map renderer ───────────────────────────────────────────────

def render_data_map_compact(data_map_path) -> str:
    """
    Парсит `data_map.md` и возвращает компактный каталог: имя_таблицы +
    первая строка описания. Используется в system prompt'е generalist'а
    как замена полного data_map.md (5K → ~1K токенов).

    Формат входа (паттерн из существующего data_map.md):
        ## Section heading
        - **`table_name`** — описание таблицы. Может содержать ⚠ маркеры
          и переносы строк.
          Skills: `clickhouse-basics`, ...   ← эта строка отсекается

    Формат выхода (детерминированный, для cache stability):
        - `table_name`: описание (1 строка, до 220 chars)

    Группировка по разделам (## headings) сохраняется. Skills-строки и
    markdown-форматирование убираются.

    Args:
        data_map_path: pathlib.Path или str с полным data_map.md

    Returns:
        Markdown-строка компактного каталога. Если файл не найден —
        возвращает сообщение об ошибке (не падает, чтобы не ломать subagent).
    """
    from pathlib import Path
    import re

    path = Path(data_map_path) if not isinstance(data_map_path, Path) else data_map_path
    if not path.exists():
        return f"⚠ data_map не найден по пути: {path}"

    text = path.read_text(encoding="utf-8")
    lines = text.splitlines()

    # Pattern для table-bullet: `- **\`table_name\`** — description...`
    table_re = re.compile(r"^\s*-\s+\*\*`([a-zA-Z_][\w]*)`\*\*\s*[—–-]\s*(.*)$")
    # Section heading: `## Section name`
    section_re = re.compile(r"^##\s+(.+)$")

    out_lines: list[str] = []
    current_section: str | None = None
    section_emitted: set[str] = set()

    for raw in lines:
        # Section heading
        m_sec = section_re.match(raw)
        if m_sec:
            current_section = m_sec.group(1).strip()
            continue
        # Table bullet
        m_tab = table_re.match(raw)
        if m_tab:
            tname = m_tab.group(1)
            desc = m_tab.group(2).strip()
            # Strip markdown emphasis markers (** _ `)
            desc = re.sub(r"`([^`]+)`", r"\1", desc)
            desc = re.sub(r"\*\*([^*]+)\*\*", r"\1", desc)
            # Truncate to one logical line
            desc = desc.split("\n")[0].strip()
            if len(desc) > 220:
                desc = desc[:217] + "..."
            # Emit section header once
            if current_section and current_section not in section_emitted:
                out_lines.append(f"\n### {current_section}")
                section_emitted.add(current_section)
            out_lines.append(f"- `{tname}`: {desc}")

    if not out_lines:
        return "⚠ data_map пустой или не распознан парсером."

    return "\n".join(out_lines).strip()


# ─── Convenience ─────────────────────────────────────────────────────────────

def get_schema_cache() -> SchemaCache:
    """Get the process-wide singleton SchemaCache."""
    cache = SchemaCache()
    if not cache.is_loaded():
        cache.load()
    return cache
