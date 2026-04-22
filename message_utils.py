"""
Shared message compression utilities for AnalyticsAgent and sub-agents.

Extracted from agent.py to avoid code duplication across main agent
and sub-agents (DirectOptimizerAgent, ScoringIntelligenceAgent, etc.).
"""

import json
from copy import copy

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage


# ─── ToolMessage compression ─────────────────────────────────────────────────

def compress_tool_message(msg: ToolMessage) -> ToolMessage:
    """
    Replace a ToolMessage's content with a compact version.

    Called only for ToolMessages from PREVIOUS turns so the LLM receives
    minimal but sufficient information about past tool results.

    Compression strategy per tool:
      list_tables    → keep table+column names, drop types   (~60–70% smaller)
      clickhouse_query → keep metadata only, drop preview rows (~40–50% smaller)
      python_analysis  → keep result summary only, drop stdout  (~50–80% smaller)
    """
    tool_name = getattr(msg, "name", "") or ""
    content = msg.content

    try:
        if tool_name == "list_tables":
            data = json.loads(content)
            compact = []
            for t in data:
                cols = t.get("columns", [])
                if cols and isinstance(cols[0], dict):
                    col_names = [c["name"] for c in cols]
                else:
                    col_names = cols
                compact.append({"table": t["table"], "columns": col_names})
            new_content = json.dumps(compact, ensure_ascii=False)

        elif tool_name == "clickhouse_query":
            data = json.loads(content)
            new_content = json.dumps(
                {
                    "success": data.get("success"),
                    "cached": data.get("cached"),
                    "row_count": data.get("row_count"),
                    "columns": data.get("columns"),
                    "parquet_path": data.get("parquet_path"),
                },
                ensure_ascii=False,
            )

        elif tool_name == "python_analysis":
            data = json.loads(content)
            result_text = data.get("result") or ""
            plots_count = data.get("plots_count", 0)
            compressed: dict = {
                "success": data.get("success"),
                "result": result_text[:500] + ("…" if len(result_text) > 500 else ""),
            }
            # Preserve plots_count so the agent knows charts were already
            # delivered to the user — prevents rebuilding visualisations on
            # fix/retry calls and avoids duplicate graphs.
            if plots_count:
                compressed["plots_delivered"] = plots_count
            new_content = json.dumps(compressed, ensure_ascii=False)

        else:
            return msg

    except Exception:
        return msg

    try:
        return msg.model_copy(update={"content": new_content})
    except Exception:
        new_msg = copy(msg)
        new_msg.content = new_content
        return new_msg


# ─── Turn grouping ───────────────────────────────────────────────────────────

def group_into_turns(messages: list) -> list[list]:
    """Split a flat message list into per-turn sublists (each starting with HumanMessage)."""
    turns: list[list] = []
    current: list = []
    for msg in messages:
        if isinstance(msg, HumanMessage):
            if current:
                turns.append(current)
            current = [msg]
        else:
            current.append(msg)
    if current:
        turns.append(current)
    return turns


# ─── Turn summarisation ──────────────────────────────────────────────────────

def summarize_previous_turn(turn_msgs: list) -> list:
    """
    Compress a previous turn's internal tool-call chain while keeping the
    final agent answer intact.

    Returns one of:
      [HumanMessage, AIMessage(tool_summary), AIMessage(final_answer)]  — tools used
      [HumanMessage, AIMessage(final_answer)]                           — no tools
      [HumanMessage]                                                    — no answer yet
    """
    human_msg: HumanMessage | None = None
    sql_snippet = ""
    row_info = ""
    final_ai_msg: AIMessage | None = None

    for msg in turn_msgs:
        if isinstance(msg, HumanMessage):
            human_msg = msg

        elif isinstance(msg, AIMessage):
            for tc in getattr(msg, "tool_calls", []):
                if tc.get("name") == "clickhouse_query" and not sql_snippet:
                    sql = (tc.get("args") or {}).get("sql", "")
                    if sql:
                        sql_snippet = sql[:120] + ("…" if len(sql) > 120 else "")

            if not getattr(msg, "tool_calls", None):
                content = msg.content
                has_text = (isinstance(content, str) and content.strip()) or (
                    isinstance(content, list)
                    and any(
                        isinstance(b, dict)
                        and b.get("type") == "text"
                        and b.get("text", "").strip()
                        for b in content
                    )
                )
                if has_text:
                    final_ai_msg = msg

        elif isinstance(msg, ToolMessage):
            try:
                data = json.loads(msg.content)
                if (getattr(msg, "name", "") or "") == "clickhouse_query" and not row_info:
                    rc = data.get("row_count")
                    cols = data.get("columns") or []
                    row_info = f"{rc} rows" if rc is not None else ""
                    if cols:
                        row_info += f", cols: {', '.join(str(c) for c in cols[:6])}"
            except Exception:
                pass

    result: list = []
    if human_msg is not None:
        result.append(human_msg)

    tool_parts: list[str] = []
    if sql_snippet:
        tool_parts.append(f"SQL: {sql_snippet}")
    if row_info:
        tool_parts.append(row_info)
    if tool_parts:
        result.append(AIMessage(content=" | ".join(tool_parts)))

    if final_ai_msg is not None:
        result.append(final_ai_msg)

    return result if result else [AIMessage(content="—")]


# ─── Schema formatting ───────────────────────────────────────────────────────

def build_schema_block(tables: list[dict]) -> str:
    """
    Format a list of {table, columns} dicts into a compact schema section
    for embedding in the system prompt.
    """
    lines = []
    for t in tables:
        cols = t.get("columns", [])
        if cols and isinstance(cols[0], dict):
            if "type" in cols[0]:
                col_parts = [f"{c['name']} {c['type']}" for c in cols]
            else:
                col_parts = [c["name"] for c in cols]
        else:
            col_parts = [str(c) for c in cols]
        lines.append(f"**{t['table']}**: {', '.join(col_parts)}")
    return "\n".join(lines)
