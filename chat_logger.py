"""
ChatLogger — passive observer for agent runs.

Writes full-fidelity event logs to `agent_logs` table in the SAME SQLite DB
as SqliteSaver, but in a completely separate table.

Key design constraints:
  - Agent knows NOTHING about this module.
  - Logger is called AFTER agent.analyze() returns, from api_server.py.
  - Any exception in logger is caught silently — never propagates to agent.
  - Uses WAL mode to coexist safely with SqliteSaver writes.

Table: agent_logs
  id            INTEGER PRIMARY KEY AUTOINCREMENT
  session_id    TEXT     — LangGraph thread_id
  turn_index    INTEGER  — 1-based, = number of HumanMessages seen so far
  seq           INTEGER  — ordering within a turn (0, 1, 2, ...)
  event_type    TEXT     — 'human' | 'ai_thinking' | 'tool_call' | 'tool_result' | 'ai_answer'
  tool_name     TEXT     — 'clickhouse_query' | 'python_analysis' | 'list_tables' | NULL
  tool_call_id  TEXT     — links tool_call ↔ tool_result pair
  content       TEXT     — full content (SQL, code, JSON result, text)
  token_est     INTEGER  — rough token estimate (len/4) for cost analysis
  duration_ms   INTEGER  — NULL (reserved for future per-call timing)
  created_at    TEXT     — ISO-8601 UTC timestamp
"""

import json
import sqlite3
import threading
from datetime import datetime, timezone
from typing import Optional

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage


# ─── Module-level singleton ───────────────────────────────────────────────────

_logger: Optional["ChatLogger"] = None
_logger_lock = threading.Lock()


def get_logger(db_path: str) -> "ChatLogger":
    global _logger
    if _logger is None:
        with _logger_lock:
            if _logger is None:
                _logger = ChatLogger(db_path)
    return _logger


# ─── Main class ───────────────────────────────────────────────────────────────

class ChatLogger:
    """
    Thread-safe append-only event log.
    Called from api_server._run_agent_job() after agent.analyze() returns.
    """

    def __init__(self, db_path: str) -> None:
        # Separate connection from SqliteSaver's connection.
        # WAL mode allows concurrent readers + one writer safely.
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.execute("PRAGMA busy_timeout = 5000")  # wait up to 5s on lock
        self._lock = threading.Lock()
        self._init_schema()
        print(f"✅ ChatLogger ready | db: {db_path}")

    # ── Schema ────────────────────────────────────────────────────────────────

    def _init_schema(self) -> None:
        with self._lock:
            self._conn.executescript("""
                CREATE TABLE IF NOT EXISTS agent_logs (
                    id           INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id   TEXT    NOT NULL,
                    turn_index   INTEGER NOT NULL,
                    seq          INTEGER NOT NULL,
                    event_type   TEXT    NOT NULL,
                    tool_name    TEXT,
                    tool_call_id TEXT,
                    content      TEXT,
                    token_est    INTEGER,
                    duration_ms  INTEGER,
                    created_at   TEXT    NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_logs_session
                    ON agent_logs(session_id, turn_index, seq);
                CREATE INDEX IF NOT EXISTS idx_logs_created
                    ON agent_logs(created_at);
            """)
            self._conn.commit()

    # ── Public API ────────────────────────────────────────────────────────────

    def log_turn(self, session_id: str, messages: list, started_at: str) -> None:
        """
        Extract and persist all events from the current turn.

        Called with the full message list returned by graph.invoke().
        Finds the boundary of the current turn (last HumanMessage) and
        logs only new messages from that point forward.

        Completely safe to call — all exceptions are swallowed.
        """
        try:
            self._log_turn_unsafe(session_id, messages, started_at)
        except Exception as exc:
            # Never let logger errors reach the caller (agent result is already returned)
            print(f"[ChatLogger] WARNING: log_turn failed silently: {exc}")

    def log_router(
        self,
        session_id: str,
        turn_index: int,
        active_skills: list[str],
        query_preview: str,
        created_at: str,
    ) -> None:
        """
        Log the router classification result for a single turn.

        Stored as event_type='router_result', tool_name=None.
        content: JSON with active_skills list and query preview.
        Completely safe — all exceptions are swallowed.
        """
        try:
            content = json.dumps(
                {"active_skills": active_skills, "query": query_preview[:200]},
                ensure_ascii=False,
            )
            with self._lock:
                self._conn.execute(
                    """INSERT INTO agent_logs
                       (session_id, turn_index, seq, event_type, tool_name,
                        tool_call_id, content, token_est, duration_ms, created_at)
                       VALUES (?,?,?,?,?,?,?,?,?,?)""",
                    (session_id, turn_index, -1, "router_result", None,
                     None, content, len(content) // 4, None, created_at),
                )
                self._conn.commit()
        except Exception as exc:
            print(f"[ChatLogger] WARNING: log_router failed silently: {exc}")

    def get_session_logs(self, session_id: str) -> list[dict]:
        """Return all events for a session, ordered by turn and seq."""
        cur = self._conn.execute(
            """SELECT id, turn_index, seq, event_type, tool_name,
                      tool_call_id, content, token_est, created_at
               FROM agent_logs
               WHERE session_id = ?
               ORDER BY turn_index, seq, id""",
            (session_id,),
        )
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]

    def get_sessions(self) -> list[dict]:
        """Summary list of all sessions — for a sessions browser."""
        cur = self._conn.execute(
            """SELECT
                   session_id,
                   MAX(turn_index)                                        AS turns,
                   SUM(token_est)                                         AS total_tokens_est,
                   COUNT(*) FILTER (WHERE event_type = 'tool_call')      AS tool_calls,
                   MIN(created_at)                                        AS started_at,
                   MAX(created_at)                                        AS last_active
               FROM agent_logs
               GROUP BY session_id
               ORDER BY last_active DESC"""
        )
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]

    def get_turn(self, session_id: str, turn_index: int) -> list[dict]:
        """All events for one specific turn."""
        cur = self._conn.execute(
            """SELECT id, seq, event_type, tool_name, tool_call_id,
                      content, token_est, created_at
               FROM agent_logs
               WHERE session_id = ? AND turn_index = ?
               ORDER BY seq, id""",
            (session_id, turn_index),
        )
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]

    def get_stats(self) -> dict:
        """Aggregate stats across all sessions — useful for optimization analysis."""
        cur = self._conn.execute("""
            SELECT
                COUNT(DISTINCT session_id)                                 AS sessions,
                COUNT(*) FILTER (WHERE event_type = 'human')               AS human_turns,
                COUNT(*) FILTER (WHERE event_type = 'tool_call')           AS tool_calls_total,
                COUNT(*) FILTER (WHERE tool_name = 'clickhouse_query')     AS ch_queries,
                COUNT(*) FILTER (WHERE tool_name = 'python_analysis')      AS py_analyses,
                COUNT(*) FILTER (WHERE tool_name = 'list_tables')          AS list_tables_calls,
                AVG(token_est) FILTER (WHERE event_type = 'tool_result'
                    AND tool_name = 'clickhouse_query')                    AS avg_ch_result_tokens,
                AVG(token_est) FILTER (WHERE event_type = 'tool_result'
                    AND tool_name = 'python_analysis')                     AS avg_py_result_tokens,
                SUM(token_est)                                             AS total_tokens_est
            FROM agent_logs
        """)
        cols = [d[0] for d in cur.description]
        row = cur.fetchone()
        return dict(zip(cols, row))

    # ── Internal ──────────────────────────────────────────────────────────────

    def _log_turn_unsafe(self, session_id: str, messages: list, started_at: str) -> None:
        # Find start of current turn (last HumanMessage index)
        last_human_idx = -1
        for i, msg in enumerate(messages):
            if isinstance(msg, HumanMessage):
                last_human_idx = i
        if last_human_idx < 0:
            return

        # turn_index = count of all HumanMessages up to and including this one
        turn_index = sum(
            1 for m in messages[:last_human_idx + 1]
            if isinstance(m, HumanMessage)
        )

        with self._lock:
            # Idempotency: skip if this turn is already logged
            cur = self._conn.execute(
                "SELECT COUNT(*) FROM agent_logs WHERE session_id=? AND turn_index=?",
                (session_id, turn_index),
            )
            if cur.fetchone()[0] > 0:
                return

            now = started_at  # timestamp from when the turn started (turn_timestamp)
            rows = []
            seq = 0

            for msg in messages[last_human_idx:]:

                if isinstance(msg, HumanMessage):
                    text = _to_text(msg.content)
                    rows.append(_row(session_id, turn_index, seq, "human",
                                    None, None, text, now))
                    seq += 1

                elif isinstance(msg, AIMessage):
                    # Tool calls (agent "thinking" — requests to tools)
                    for tc in getattr(msg, "tool_calls", []) or []:
                        args = tc.get("args", {})
                        content = json.dumps(args, ensure_ascii=False)
                        rows.append(_row(session_id, turn_index, seq,
                                         "tool_call",
                                         tc.get("name", ""),
                                         tc.get("id", ""),
                                         content, now))
                        seq += 1

                    # Text content
                    text = _to_text(msg.content)
                    if text and not getattr(msg, "tool_calls", None):
                        # No tool_calls = this is the final answer
                        rows.append(_row(session_id, turn_index, seq,
                                         "ai_answer", None, None, text, now))
                        seq += 1
                    elif text and getattr(msg, "tool_calls", None):
                        # Has tool_calls AND text = thinking out loud before tool use
                        rows.append(_row(session_id, turn_index, seq,
                                         "ai_thinking", None, None, text, now))
                        seq += 1

                elif isinstance(msg, ToolMessage):
                    tool_name = getattr(msg, "name", "") or ""
                    tc_id = getattr(msg, "tool_call_id", None) or ""
                    content = _sanitize_tool_result(tool_name, msg.content)
                    rows.append(_row(session_id, turn_index, seq,
                                     "tool_result", tool_name, tc_id,
                                     content, now))
                    seq += 1

            if rows:
                self._conn.executemany(
                    """INSERT INTO agent_logs
                       (session_id, turn_index, seq, event_type, tool_name,
                        tool_call_id, content, token_est, duration_ms, created_at)
                       VALUES (?,?,?,?,?,?,?,?,?,?)""",
                    rows,
                )
                self._conn.commit()


# ── Helper functions ──────────────────────────────────────────────────────────

def _row(session_id, turn_index, seq, event_type,
         tool_name, tool_call_id, content, created_at):
    token_est = len(content) // 4 if content else 0
    return (session_id, turn_index, seq, event_type,
            tool_name, tool_call_id, content,
            token_est, None, created_at)


def _to_text(content) -> str:
    """Extract plain text from str or Anthropic content-block list."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = [
            b.get("text", "")
            for b in content
            if isinstance(b, dict) and b.get("type") == "text"
        ]
        return "\n".join(p for p in parts if p)
    return str(content) if content else ""


_MAX_TOOL_RESULT_SIZE = 50_000  # Cap at 50KB — enough for any real SQL result metadata


def _sanitize_tool_result(tool_name: str, content: str) -> str:
    """
    Store full tool results for analysis.
    Only cap extremely large outputs to avoid bloating the DB.
    Base64 plots are in ToolMessage.artifact (not content), so no issue.
    """
    if not content:
        return content
    if len(content) > _MAX_TOOL_RESULT_SIZE:
        return content[:_MAX_TOOL_RESULT_SIZE] + "\n… [truncated at 50KB]"
    return content
