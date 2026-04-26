"""
Adapter: runs the deepagents-based agent with the same return shape as the
legacy AnalyticsAgent.analyze(), so api_server.py only needs a flag switch.

Return dict:
    {
      "success":     bool,
      "session_id":  str,
      "text_output": str,        # final markdown answer
      "plots":       list[str],  # base64 PNG data URIs (for UI inline display)
      "plot_urls":   list[str],  # virtual paths in /plots/ for referencing
      "parquet_paths": list[str],
      "tool_calls":  list[dict],
      "error":       str | None,
      "_messages":   list,       # for observability logger
    }
"""
from __future__ import annotations

import json
import traceback as tb
from typing import Any, Optional

from langchain_core.messages import (
    AIMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)

from .agent_factory import build_agent
from .session_context import make_session_context, set_current_session


def analyze_deepagents(
    query: str,
    session_id: str,
    model: Optional[str] = None,
    client_id: str = "magnetto",
) -> dict[str, Any]:
    """Drop-in replacement for AnalyticsAgent.analyze()."""

    try:
        agent = build_agent(client_id=client_id, model=model)
    except Exception as exc:
        return {
            "success": False,
            "session_id": session_id,
            "text_output": "",
            "plots": [],
            "plot_urls": [],
            "parquet_paths": [],
            "tool_calls": [],
            "error": f"agent build failed: {exc}",
            "traceback": tb.format_exc(),
            "_messages": [],
        }

    # ── Set up per-session context (parquet/plots dirs) ──────────────────
    sess_ctx = make_session_context(session_id=session_id, client_id=client_id)

    # LangGraph thread_id == session_id — guarantees separate memory per chat
    config = {"configurable": {"thread_id": session_id}}

    try:
        with set_current_session(sess_ctx):
            result = agent.invoke(
                {"messages": [HumanMessage(content=query)]},
                config=config,
            )

        messages: list = result.get("messages", []) if isinstance(result, dict) else []
        # MainFinalAnswer instance из response_format — главный источник
        # main'овского текста (Pydantic-капнут до 600 chars).
        structured_response = result.get("structured_response") if isinstance(result, dict) else None

        text_output = _extract_final_text(messages, structured_response)
        plots_b64 = _extract_plots(messages)
        plot_urls = _extract_plot_urls(messages)
        parquet_paths = _extract_parquet_paths(messages)
        tool_calls = _extract_tool_calls(messages)

        return {
            "success": True,
            "session_id": session_id,
            "text_output": text_output,
            "plots": plots_b64,
            "plot_urls": plot_urls,
            "parquet_paths": parquet_paths,
            "tool_calls": tool_calls,
            "error": None,
            "_messages": messages,
        }

    except Exception as exc:
        # Salvage state on error for logger
        _err_msgs = []
        try:
            snapshot = agent.get_state(config)
            _err_msgs = list(snapshot.values.get("messages", []))
        except Exception:
            pass
        return {
            "success": False,
            "session_id": session_id,
            "text_output": "",
            "plots": [],
            "plot_urls": [],
            "parquet_paths": [],
            "tool_calls": [],
            "error": str(exc),
            "traceback": tb.format_exc(),
            "_messages": _err_msgs,
        }


# ─── Extractors ─────────────────────────────────────────────────────────────

def _extract_final_text(messages: list, structured_response=None) -> str:
    """
    Финал для пользователя:
      = sub.summary(s) (программная композиция) + main.text (из MainFinalAnswer).

    Источник main'овского текста — `structured_response` (MainFinalAnswer
    instance), а НЕ AIMessage. Pydantic max_length=600 на поле .text
    структурно гарантирует что main не выкатит переписку sub'овского ответа.

    Если sub_summaries пустой (main отвечал сам без task делегирования) —
    main.text и есть финал (он же ответ, до 600 chars).

    Fallback: если structured_response отсутствует (старая версия модели,
    отказ structured-output) — берём текст последнего AIMessage как раньше.
    """
    last_human_idx = _find_last_human_idx(messages)
    turn_msgs = messages[last_human_idx:]

    sub_summaries: list[str] = []
    for msg in turn_msgs:
        if isinstance(msg, ToolMessage) and (getattr(msg, "name", "") or "") == "task":
            s = _extract_summary_from_subagent_result(msg.content)
            if s:
                sub_summaries.append(s)

    main_text = _extract_main_text_from_structured(structured_response)
    if not main_text:
        # Fallback — структурированный ответ отсутствует, берём AIMessage.
        main_text = _extract_main_ai_text(messages)

    if not sub_summaries:
        return main_text

    parts = list(sub_summaries)
    if main_text:
        parts.append(main_text)
    return "\n\n---\n\n".join(parts)


def _extract_main_text_from_structured(structured_response) -> str:
    """
    structured_response — это MainFinalAnswer pydantic instance (или dict
    в edge cases). Извлекаем поле .text.
    """
    if structured_response is None:
        return ""
    try:
        # Pydantic v2 instance
        if hasattr(structured_response, "text"):
            t = structured_response.text or ""
            return t.strip()
        # Dict-shaped fallback
        if isinstance(structured_response, dict):
            t = structured_response.get("text") or ""
            return t.strip()
    except Exception:
        pass
    return ""


def _find_last_human_idx(messages: list) -> int:
    for i in range(len(messages) - 1, -1, -1):
        if isinstance(messages[i], HumanMessage):
            return i
    return 0


def _extract_main_ai_text(messages: list) -> str:
    """
    Текст последнего AIMessage в истории. Аналог старого _extract_final_text.
    Используется для извлечения main'овского комментария (после делегирования)
    или прямого ответа main'а (когда без делегирования).
    """
    for msg in reversed(messages):
        if not isinstance(msg, AIMessage):
            continue
        content = msg.content
        if isinstance(content, str) and content.strip():
            return content.strip()
        if isinstance(content, list):
            parts = [
                b["text"] for b in content
                if isinstance(b, dict) and b.get("type") == "text" and b.get("text")
            ]
            txt = "\n".join(parts).strip()
            if txt:
                return txt
    return ""


def _extract_summary_from_subagent_result(content) -> str:
    """
    Извлекаем summary из task ToolMessage:
      - Если subagent с response_format=SubagentResult — content это JSON,
        берём поле "summary".
      - Если subagent без response_format — content это уже markdown,
        отдаём как есть.
      - Если структура неожиданная — возвращаем пустую строку (не ломаем
        композицию остальных).
    """
    if not content:
        return ""
    # ToolMessage.content может быть str или list[dict]
    text: str
    if isinstance(content, str):
        text = content
    elif isinstance(content, list):
        # Соберём text-блоки
        parts = []
        for b in content:
            if isinstance(b, dict) and b.get("type") == "text" and b.get("text"):
                parts.append(b["text"])
            elif isinstance(b, str):
                parts.append(b)
        text = "\n".join(parts)
    else:
        return ""

    if not text.strip():
        return ""

    # Пробуем распарсить как JSON (response_format=SubagentResult случай)
    stripped = text.strip()
    if stripped.startswith("{"):
        try:
            data = json.loads(stripped)
            if isinstance(data, dict):
                summary = data.get("summary")
                if isinstance(summary, str) and summary.strip():
                    return summary.strip()
                # Если summary нет — возможно subagent вернул что-то нестандартное.
                # Не дропаем, отдаём весь JSON-текст.
                return stripped
        except json.JSONDecodeError:
            pass

    # Не JSON — это free-form markdown от subagent без response_format.
    return stripped


def _extract_plots(messages: list) -> list[str]:
    """Base64 plots from CURRENT turn (after last HumanMessage)."""
    last_human_idx = -1
    for i, msg in enumerate(messages):
        if isinstance(msg, HumanMessage):
            last_human_idx = i
    if last_human_idx < 0:
        return []
    plots: list[str] = []
    for msg in messages[last_human_idx:]:
        if not isinstance(msg, ToolMessage):
            continue
        if (getattr(msg, "name", "") or "") != "python_analysis":
            continue
        artifact = getattr(msg, "artifact", None)
        if isinstance(artifact, list):
            plots.extend(artifact)
    return plots


def _extract_plot_urls(messages: list) -> list[str]:
    """Virtual /plots/<filename>.png URLs from CURRENT turn for frontend reference."""
    last_human_idx = -1
    for i, msg in enumerate(messages):
        if isinstance(msg, HumanMessage):
            last_human_idx = i
    if last_human_idx < 0:
        return []
    urls: list[str] = []
    for msg in messages[last_human_idx:]:
        if not isinstance(msg, ToolMessage):
            continue
        if (getattr(msg, "name", "") or "") != "python_analysis":
            continue
        try:
            data = json.loads(msg.content)
            urls.extend(data.get("plot_urls", []) or [])
        except Exception:
            pass
    return urls


def _extract_parquet_paths(messages: list) -> list[str]:
    """Physical parquet paths from CURRENT turn."""
    last_human_idx = -1
    for i, msg in enumerate(messages):
        if isinstance(msg, HumanMessage):
            last_human_idx = i
    if last_human_idx < 0:
        return []
    paths: list[str] = []
    for msg in messages[last_human_idx:]:
        if not isinstance(msg, ToolMessage):
            continue
        if (getattr(msg, "name", "") or "") != "clickhouse_query":
            continue
        try:
            data = json.loads(msg.content)
            if data.get("parquet_path"):
                paths.append(data["parquet_path"])
        except Exception:
            pass
    return paths


def _extract_tool_calls(messages: list) -> list[dict]:
    """Compact log of tool calls from CURRENT turn."""
    last_human_idx = -1
    for i, msg in enumerate(messages):
        if isinstance(msg, HumanMessage):
            last_human_idx = i
    if last_human_idx < 0:
        return []

    tool_results: dict[str, ToolMessage] = {}
    for msg in messages[last_human_idx:]:
        if isinstance(msg, ToolMessage):
            tc_id = getattr(msg, "tool_call_id", None)
            if tc_id:
                tool_results[tc_id] = msg

    tool_calls: list[dict] = []
    for msg in messages[last_human_idx:]:
        if not isinstance(msg, AIMessage):
            continue
        for tc in getattr(msg, "tool_calls", []):
            name = tc.get("name", "")
            args = tc.get("args", {}) or {}
            tc_id = tc.get("id", "")
            compact_args = {
                k: (
                    v[:2000] + "…" if k == "sql" and isinstance(v, str) and len(v) > 2000
                    else v[:500] + "…" if isinstance(v, str) and len(v) > 500
                    else v
                )
                for k, v in args.items()
            }
            entry: dict = {"tool": name, "input": compact_args}
            tm = tool_results.get(tc_id)
            if tm is not None:
                try:
                    data = json.loads(tm.content)
                    entry["success"] = data.get("success")
                    if name == "clickhouse_query":
                        entry["row_count"] = data.get("row_count")
                        entry["cached"] = data.get("cached")
                        if not data.get("success"):
                            entry["error"] = data.get("error", "")
                    elif name == "python_analysis":
                        entry["plots_count"] = data.get("plots_count")
                        if not data.get("success"):
                            entry["error"] = data.get("error", "")
                    elif name == "task":
                        # deepagents standard subagent delegation. Subagent name
                        # лежит в args (subagent_type/name); tool_calls_count
                        # subagent передаёт в JSON-сериализованном response_format
                        # (если задан) либо в free-form output.
                        entry["subagent"] = (
                            data.get("subagent")
                            or data.get("name")
                            or args.get("subagent_type")
                            or args.get("name")
                        )
                        if isinstance(data, dict) and "used_tables" in data:
                            entry["used_tables"] = data.get("used_tables")
                            entry["used_skills"] = data.get("used_skills")
                except Exception:
                    entry["output_raw"] = str(tm.content)[:500]
            tool_calls.append(entry)

    return tool_calls
