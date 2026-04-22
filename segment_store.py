"""
SegmentStore — thread-safe SQLite CRUD для именованных сегментов аудитории.

Изоляция по пользователям: каждый сегмент принадлежит owner (значение X-User-Id).
Пользователь видит и может удалять только свои сегменты.
Если X-User-Id не передан — используется owner="__shared__" (обратная совместимость).

При переходе на RAG — только этот модуль меняется, агент не трогается.
"""

import json
import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from typing import Optional

from config import DB_PATH

_SHARED_OWNER = "__shared__"


class SegmentStore:
    """Thread-safe CRUD для сегментов аудитории с изоляцией по owner."""

    def __init__(self, db_path: str = DB_PATH) -> None:
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        self._init_schema()

    def _init_schema(self) -> None:
        with self._lock:
            # Создать таблицу если не существует
            self._conn.executescript("""
                CREATE TABLE IF NOT EXISTS segments (
                    segment_id      TEXT PRIMARY KEY,
                    name            TEXT NOT NULL,
                    description     TEXT,
                    approach        TEXT,
                    period_json     TEXT,
                    conditions_json TEXT,
                    primary_table   TEXT,
                    join_tables_json TEXT,
                    sql_query       TEXT,
                    last_count      INTEGER,
                    last_materialized TEXT,
                    used_in_json    TEXT,
                    created_at      TEXT NOT NULL,
                    updated_at      TEXT NOT NULL
                );
            """)
            # Добавить колонку owner если её нет (миграция существующих данных)
            try:
                self._conn.execute(
                    "ALTER TABLE segments ADD COLUMN owner TEXT NOT NULL DEFAULT '__shared__'"
                )
                self._conn.commit()
            except sqlite3.OperationalError:
                pass  # колонка уже существует

            # Пересоздать уникальный индекс: (name, owner) вместо просто name
            self._conn.executescript("""
                DROP INDEX IF EXISTS idx_segments_name;
                CREATE UNIQUE INDEX IF NOT EXISTS idx_segments_name_owner
                    ON segments(name, owner);
                CREATE INDEX IF NOT EXISTS idx_segments_owner
                    ON segments(owner);
            """)
            self._conn.commit()

    def save(self, segment: dict, owner: str = _SHARED_OWNER) -> dict:
        """Сохранить или обновить сегмент. Возвращает сохранённый объект."""
        now = datetime.now(timezone.utc).date().isoformat()
        seg_id = segment.get("segment_id") or f"seg_{uuid.uuid4().hex[:8]}"

        with self._lock:
            self._conn.execute(
                """
                INSERT INTO segments (
                    segment_id, name, description, approach,
                    period_json, conditions_json, primary_table,
                    join_tables_json, sql_query, last_count,
                    last_materialized, used_in_json, owner, created_at, updated_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(segment_id) DO UPDATE SET
                    name             = excluded.name,
                    description      = excluded.description,
                    approach         = excluded.approach,
                    period_json      = excluded.period_json,
                    conditions_json  = excluded.conditions_json,
                    primary_table    = excluded.primary_table,
                    join_tables_json = excluded.join_tables_json,
                    sql_query        = excluded.sql_query,
                    last_count       = excluded.last_count,
                    last_materialized= excluded.last_materialized,
                    used_in_json     = excluded.used_in_json,
                    updated_at       = excluded.updated_at
                """,
                (
                    seg_id,
                    segment["name"],
                    segment.get("description", ""),
                    segment.get("approach", ""),
                    json.dumps(segment.get("period", {}), ensure_ascii=False),
                    json.dumps(segment.get("conditions", {}), ensure_ascii=False),
                    segment.get("primary_table", ""),
                    json.dumps(segment.get("join_tables", []), ensure_ascii=False),
                    segment.get("sql_query", ""),
                    segment.get("last_count"),
                    segment.get("last_materialized", now),
                    json.dumps(segment.get("used_in", []), ensure_ascii=False),
                    owner,
                    segment.get("created_at", now),
                    now,
                ),
            )
            self._conn.commit()

        segment["segment_id"] = seg_id
        segment["owner"] = owner
        segment["updated_at"] = now
        if "created_at" not in segment:
            segment["created_at"] = now
        return segment

    def get_by_name(self, name: str, owner: str = _SHARED_OWNER) -> Optional[dict]:
        """Найти сегмент по имени в пределах owner (регистронезависимо)."""
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM segments WHERE lower(name) = lower(?) AND owner = ?",
                (name, owner),
            ).fetchone()
        return self._row_to_dict(row) if row else None

    def get_by_id(self, segment_id: str, owner: str = _SHARED_OWNER) -> Optional[dict]:
        """Вернуть сегмент только если он принадлежит owner."""
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM segments WHERE segment_id = ? AND owner = ?",
                (segment_id, owner),
            ).fetchone()
        return self._row_to_dict(row) if row else None

    def list_all(self, owner: str = _SHARED_OWNER) -> list[dict]:
        """Список сегментов owner, отсортированный по дате обновления."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM segments WHERE owner = ? ORDER BY updated_at DESC",
                (owner,),
            ).fetchall()
        return [self._row_to_dict(r) for r in rows]

    def delete(self, segment_id: str, owner: str = _SHARED_OWNER) -> bool:
        """Удалить сегмент. Возвращает False если сегмент не найден или не принадлежит owner."""
        with self._lock:
            cur = self._conn.execute(
                "DELETE FROM segments WHERE segment_id = ? AND owner = ?",
                (segment_id, owner),
            )
            self._conn.commit()
        return cur.rowcount > 0

    def _row_to_dict(self, row: sqlite3.Row) -> dict:
        d = dict(row)
        mapping = {
            "period_json":      "period",
            "conditions_json":  "conditions",
            "join_tables_json": "join_tables",
            "used_in_json":     "used_in",
        }
        for col_json, col_name in mapping.items():
            raw = d.pop(col_json, None)
            try:
                d[col_name] = json.loads(raw or "{}")
            except Exception:
                d[col_name] = {} if col_name not in ("join_tables", "used_in") else []
        return d


# ─── Singleton ─────────────────────────────────────────────────────────────────
_store: Optional[SegmentStore] = None
_store_lock = threading.Lock()


def get_segment_store() -> SegmentStore:
    global _store
    if _store is None:
        with _store_lock:
            if _store is None:
                _store = SegmentStore()
    return _store
