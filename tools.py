"""
LangGraph tool definitions for the Analytics Agent.

Three tools:
  1. list_tables      — discover ClickHouse schema
  2. clickhouse_query — run SELECT → save Parquet → return preview + path
  3. python_analysis  — exec Python with df loaded from Parquet, capture plots
"""

import json
import threading
from typing import Optional

# Hard cap on tool result size sent to the LLM — last-resort safety net.
# Root causes (huge col_stats samples, df.to_string()) are fixed upstream;
# this cap only triggers if something unexpected slips through.
# 50 000 chars ≈ 12 500 tokens. Raised from 20 000 to avoid cutting off
# legitimate stdout/result output that the agent needs to see in full.
_MAX_RESULT_CHARS = 50_000


def _cap_result(text: str) -> str:
    """Truncate tool result to _MAX_RESULT_CHARS if needed."""
    if len(text) <= _MAX_RESULT_CHARS:
        return text
    half = _MAX_RESULT_CHARS // 2
    return (
        text[:half]
        + f"\n… [result truncated: {len(text)} chars total, showing first and last {half}] …\n"
        + text[-half:]
    )

from langchain_core.tools import tool

# ─── Lazy singleton (ClickHouse only) ─────────────────────────────────────────
# Created on first use so config is loaded before connecting.
_ch_client = None

# Serialise ClickHouse access: the client uses a single connection that does
# not support concurrent queries. The lock prevents "Attempt to execute
# concurrent queries within the same session" errors when the agent issues
# two clickhouse_query tool-calls in the same LLM turn.
_ch_lock = threading.Lock()


def _get_ch_client():
    global _ch_client
    if _ch_client is None:
        from clickhouse_client import ClickHouseClient
        _ch_client = ClickHouseClient()
    return _ch_client


# ─── Tool 1: list_tables ──────────────────────────────────────────────────────
@tool
def list_tables() -> str:
    """
    Get the list of ALL tables in the ClickHouse database with their column names.

    NOTE: The schema (with column types) is already embedded in your system prompt —
    do NOT call this at the start of a session. Use it only if a table seems missing
    or the embedded schema appears incomplete.

    Returns: JSON array of objects like:
      [{"table": "visits", "columns": [{"name": "date", "type": "Date"}, ...]}, ...]
    """
    try:
        tables = _get_ch_client().list_tables()
        return json.dumps(tables, ensure_ascii=False)
    except Exception as exc:
        return json.dumps({"error": str(exc)})


# ─── Tool 2: clickhouse_query ─────────────────────────────────────────────────
@tool
def clickhouse_query(sql: str) -> str:
    """
    Execute a SELECT query against ClickHouse.

    Returns JSON with fields:
      - row_count: total rows returned
      - columns: list of column names
      - col_stats: per-column stats (type, min/max for numeric/datetime; unique+sample for strings)
      - parquet_path: path to saved Parquet file — pass this to python_analysis
      - cached: true if result was served from cache without hitting ClickHouse

    Rules: SELECT only; always include LIMIT; use WITH/CTE to join multiple tables in one query.

    Args:
        sql: ClickHouse SELECT statement.
    """
    try:
        with _ch_lock:
            result = _get_ch_client().execute_query(sql)
        return _cap_result(json.dumps(result, ensure_ascii=False, default=str))
    except Exception as exc:
        return json.dumps({"success": False, "error": str(exc)})


# ─── Tool 3: python_analysis ──────────────────────────────────────────────────
@tool(response_format="content_and_artifact")
def python_analysis(code: str, parquet_path: str) -> tuple[str, list[str]]:
    """
    Execute Python to analyze data from a ClickHouse query result.

    `df` (pandas DataFrame) is pre-loaded with all transformations applied
    (Array columns converted to lists, types coerced).
    Available: df, pd, np, plt, sns, result=None, df_info (column type map).

    To load additional datasets use pd.read_parquet(parquet_path) — the same
    type coercions (numeric, datetime, Array→list) are applied automatically.

    Rules:
    1. Set `result` to a Markdown string (shown to the user).
    2. Use print() for intermediate logging (e.g. diagnostics, row counts).
    3. All open matplotlib figures are auto-captured as PNG after your code runs.
       NEVER call plt.close() — it destroys the figure before capture → blank.
    4. Label charts in Russian; format numbers with thousands separators.
    5. Handle missing data and outliers before calculations: check for nulls,
       filter extreme outliers with quantile, never print raw Array columns
       (they can contain thousands of elements — use [:5] or len() instead).

    Args:
        code: Python code. `df` is already loaded from parquet_path.
        parquet_path: Returned by clickhouse_query.
    """
    try:
        from python_sandbox import PythonSandbox
        result = PythonSandbox().execute(code=code, parquet_path=parquet_path)
        plots: list[str] = result.pop("plots", [])
        # plots_count lets the LLM know how many charts were delivered to the
        # user — visible in the compressed ToolMessage on retry calls, so the
        # agent can skip rebuilding visualisations that are already shown.
        result["plots_count"] = len(plots)
        content = _cap_result(json.dumps(result, ensure_ascii=False, default=str))
        return content, plots
    except Exception as exc:
        import traceback as tb
        full_tb = f"{exc}\n{tb.format_exc()}"
        content = json.dumps({
            "success": False,
            "output": "",
            "result": None,
            "error": full_tb[-1500:] if len(full_tb) > 1500 else full_tb,
        })
        return content, []


# ─── Sub-agent tools ─────────────────────────────────────────────────────────
from tools_subagents import ask_direct_optimizer, ask_scoring_agent

# ─── Exported list ────────────────────────────────────────────────────────────
TOOLS = [list_tables, clickhouse_query, python_analysis, ask_direct_optimizer, ask_scoring_agent]
