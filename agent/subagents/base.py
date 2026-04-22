"""
BaseSubAgent — reusable base class for specialised sub-agents.

Mirrors the main AnalyticsAgent architecture:
  - StateGraph: agent ⟷ tools (no router — domain knowledge always loaded)
  - Tools: clickhouse_query, python_analysis, list_tables (same as main agent)
  - 4-layer message compression (sliding window, turn summarisation,
    intra-turn compression, iteration counter)
  - Prompt caching for Anthropic (cache_control blocks)
  - Stateless execution (no memory between calls)

Sub-agents are invoked by the main agent as regular LangGraph tools.
Each call creates a fresh graph invocation with no prior history.
"""

import json
from copy import copy
from pathlib import Path
from typing import Annotated, TypedDict

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_openai import ChatOpenAI
from langgraph.graph import END, StateGraph
from langgraph.graph.message import add_messages
from langgraph.prebuilt import ToolNode

from config import ALLOWED_MODELS, MAX_TOKENS, MODEL, MODEL_PROVIDER, OPENROUTER_API_KEY
from message_utils import (
    build_schema_block,
    compress_tool_message,
    group_into_turns,
    summarize_previous_turn,
)
from tools import clickhouse_query, list_tables, python_analysis

# ─── Sub-agent tools: same as main agent ──────────────────────────────────────
_SUBAGENT_TOOLS = [list_tables, clickhouse_query, python_analysis]

# ─── Default limits ───────────────────────────────────────────────────────────
_DEFAULT_MAX_ITERATIONS = 10  # Lower than main agent (15) — focused tasks


# ─── State ────────────────────────────────────────────────────────────────────

class SubAgentState(TypedDict):
    messages: Annotated[list, add_messages]


# ─── LLM factory (shared with main agent) ────────────────────────────────────

_OPENROUTER_HEADERS = {
    "HTTP-Referer": "https://server.asktab.ru",
    "X-Title": "ClickHouse Analytics SubAgent",
}


def _create_subagent_llm(model: str, provider: str) -> ChatOpenAI:
    """Return a ChatOpenAI client for a sub-agent (mirrors agent._create_llm)."""
    kwargs: dict = dict(
        model=model,
        api_key=OPENROUTER_API_KEY,
        base_url="https://openrouter.ai/api/v1",
        max_tokens=MAX_TOKENS,
        default_headers=_OPENROUTER_HEADERS,
    )
    if provider == "anthropic":
        kwargs["extra_body"] = {
            "provider": {
                "order": ["Anthropic"],
                "allow_fallbacks": False,
            },
        }
    return ChatOpenAI(**kwargs)


class BaseSubAgent:
    """
    Reusable base for specialised sub-agents.

    Subclasses must set ``self.system_prompt`` before calling ``super().__init__()``,
    or pass it to the constructor.  Typical pattern::

        class MySubAgent(BaseSubAgent):
            def __init__(self):
                prompt = "You are a …"
                super().__init__(system_prompt=prompt)
    """

    def __init__(
        self,
        system_prompt: str,
        max_iterations: int = _DEFAULT_MAX_ITERATIONS,
        model: str = MODEL,
        schema_tables: list[str] | None = None,
    ) -> None:
        if not OPENROUTER_API_KEY:
            raise ValueError("OPENROUTER_API_KEY is not set in .env")

        provider = ALLOWED_MODELS.get(model, MODEL_PROVIDER)
        is_anthropic = provider == "anthropic"
        self._max_iterations = max_iterations

        # ── LLM ──────────────────────────────────────────────────────────
        self.llm = _create_subagent_llm(model, provider)

        # ── Schema at startup (filtered to relevant tables only) ─────────
        self.schema_section = self._fetch_schema_section(schema_tables)

        # ── Finalise system prompt ───────────────────────────────────────
        self._system_prompt = system_prompt.format(
            schema_section=self.schema_section,
        )

        # ── Message builder closure (mirrors agent._build_messages) ──────
        max_iterations = self._max_iterations
        system_prompt_text = self._system_prompt

        def _build_messages(state: SubAgentState) -> list:
            messages = state.get("messages", [])

            # 1. Sliding window — keep last 3 turns (sub-agents are short-lived)
            human_indices = [
                i for i, m in enumerate(messages) if isinstance(m, HumanMessage)
            ]
            if len(human_indices) > 3:
                cutoff = human_indices[-3]
                messages = messages[cutoff:]

            # Locate current-turn boundary
            current_turn_start = 0
            for i, msg in enumerate(messages):
                if isinstance(msg, HumanMessage):
                    current_turn_start = i

            # 2. Summarise previous turns
            prev_turns = group_into_turns(messages[:current_turn_start])
            compressed_prev: list = []
            for turn in prev_turns:
                compressed_prev.extend(summarize_previous_turn(turn))

            # 3. Intra-turn ToolMessage compression
            current_msgs = messages[current_turn_start:]
            ai_positions: set[int] = {
                i for i, m in enumerate(current_msgs) if isinstance(m, AIMessage)
            }
            py_positions: set[int] = {
                i for i, m in enumerate(current_msgs)
                if isinstance(m, ToolMessage)
                and (getattr(m, "name", "") or "") == "python_analysis"
            }

            compressed_current: list = []
            for i, msg in enumerate(current_msgs):
                name = (getattr(msg, "name", "") or "") if isinstance(msg, ToolMessage) else ""
                if isinstance(msg, ToolMessage) and name == "clickhouse_query":
                    already_seen = any(j > i for j in ai_positions)
                    if already_seen:
                        compressed_current.append(compress_tool_message(msg))
                    else:
                        compressed_current.append(msg)
                elif isinstance(msg, ToolMessage) and name == "python_analysis" and any(j > i for j in py_positions):
                    compressed_current.append(compress_tool_message(msg))
                else:
                    compressed_current.append(msg)

            # 3b. Iteration counter
            tool_uses_so_far = sum(
                1 for m in compressed_current if isinstance(m, ToolMessage)
            )
            remaining = max_iterations - tool_uses_so_far
            if compressed_current and isinstance(compressed_current[0], HumanMessage):
                first = compressed_current[0]
                old_content = first.content if isinstance(first.content, str) else ""
                if is_anthropic:
                    content_blocks: list = [
                        {
                            "type": "text",
                            "text": old_content,
                            "cache_control": {"type": "ephemeral"},
                        }
                    ]
                    if tool_uses_so_far > 0:
                        counter = f"[⚡ Итерации: {tool_uses_so_far}/{max_iterations}, осталось: {remaining}]"
                        if remaining <= 0:
                            counter += (
                                " ⛔ ЛИМИТ ИСЧЕРПАН. Немедленно дай финальный ответ на основе уже собранных данных."
                                " НЕ вызывай инструменты."
                            )
                        elif remaining == 1:
                            counter += (
                                " 🚨 Остался 1 вызов инструмента."
                                " Используй последний вызов только если критически необходим."
                            )
                        elif remaining <= 3:
                            counter += (
                                " ⚠️ Мало итераций. Если данных достаточно — отвечай сейчас."
                            )
                        content_blocks.append({"type": "text", "text": counter})
                    try:
                        compressed_current[0] = first.model_copy(
                            update={"content": content_blocks}
                        )
                    except Exception:
                        new_first = copy(first)
                        new_first.content = content_blocks
                        compressed_current[0] = new_first
                elif tool_uses_so_far > 0:
                    counter = f"\n[⚡ Итерации: {tool_uses_so_far}/{max_iterations}, осталось: {remaining}]"
                    if remaining <= 0:
                        counter += (
                            " ⛔ ЛИМИТ ИСЧЕРПАН. Немедленно дай финальный ответ."
                            " НЕ вызывай инструменты."
                        )
                    elif remaining == 1:
                        counter += " 🚨 Остался 1 вызов инструмента."
                    elif remaining <= 3:
                        counter += " ⚠️ Мало итераций."
                    try:
                        compressed_current[0] = first.model_copy(
                            update={"content": old_content + counter}
                        )
                    except Exception:
                        new_first = copy(first)
                        new_first.content = old_content + counter
                        compressed_current[0] = new_first

            # 4. History cache breakpoint (Anthropic only)
            if is_anthropic and compressed_prev:
                last_hist_msg = compressed_prev[-1]
                content = last_hist_msg.content
                if isinstance(content, str) and content:
                    new_content = [
                        {
                            "type": "text",
                            "text": content,
                            "cache_control": {"type": "ephemeral"},
                        }
                    ]
                elif isinstance(content, list) and content:
                    new_content = list(content)
                    last_block = dict(new_content[-1])
                    last_block["cache_control"] = {"type": "ephemeral"}
                    new_content[-1] = last_block
                else:
                    new_content = None
                if new_content is not None:
                    try:
                        compressed_prev[-1] = last_hist_msg.model_copy(
                            update={"content": new_content}
                        )
                    except Exception:
                        new_msg = copy(last_hist_msg)
                        new_msg.content = new_content
                        compressed_prev[-1] = new_msg

            # 5. System prompt with cache_control
            if is_anthropic:
                sys_msg = SystemMessage(content=[
                    {
                        "type": "text",
                        "text": system_prompt_text,
                        "cache_control": {"type": "ephemeral"},
                    }
                ])
            else:
                sys_msg = SystemMessage(content=system_prompt_text)

            return [sys_msg] + compressed_prev + compressed_current

        # ── Graph: agent ⟷ tools ─────────────────────────────────────────
        tools = _SUBAGENT_TOOLS
        llm_with_tools = self.llm.bind_tools(tools)

        def agent_node(state: SubAgentState) -> dict:
            msgs = _build_messages(state)
            response = llm_with_tools.invoke(msgs)
            return {"messages": [response]}

        def force_summary_node(state: SubAgentState) -> dict:
            msgs = _build_messages(state)
            summary_request = HumanMessage(content=(
                "⛔ Лимит итераций исчерпан. Дай структурированный финальный ответ:\n\n"
                "1. **Что сделано** — какие данные собраны\n"
                "2. **Что не успел** — какие шаги остались\n"
                "3. **Что исследовать дальше**\n\n"
                "Отвечай только текстом. Не вызывай инструменты."
            ))
            response = self.llm.invoke(msgs + [summary_request])
            return {"messages": [response]}

        def should_continue(state: SubAgentState) -> str:
            last = state["messages"][-1]

            # Hard stop by iteration budget
            msgs = state["messages"]
            human_indices = [i for i, m in enumerate(msgs) if isinstance(m, HumanMessage)]
            current_turn_start = human_indices[-1] if human_indices else 0
            current_tool_uses = sum(
                1 for m in msgs[current_turn_start:]
                if isinstance(m, ToolMessage)
            )
            if current_tool_uses >= max_iterations:
                return "force_summary"

            if hasattr(last, "tool_calls") and last.tool_calls:
                return "tools"
            return END

        graph = StateGraph(SubAgentState)
        graph.add_node("agent", agent_node)
        graph.add_node("tools", ToolNode(tools))
        graph.add_node("force_summary", force_summary_node)
        graph.set_entry_point("agent")
        graph.add_conditional_edges(
            "agent",
            should_continue,
            {"tools": "tools", END: END, "force_summary": "force_summary"},
        )
        graph.add_edge("tools", "agent")
        graph.add_edge("force_summary", END)
        # No checkpointer — stateless, fresh context each call
        self.graph = graph.compile()

        print(
            f"✅ {self.__class__.__name__} ready | model: {model} | "
            f"max_iter: {max_iterations}"
        )

    # ─── Schema fetch ─────────────────────────────────────────────────────────

    @staticmethod
    def _fetch_schema_section(table_names: list[str] | None = None) -> str:
        """Fetch DB schema at startup, optionally filtered to specific tables."""
        try:
            from tools import _get_ch_client
            tables = _get_ch_client().list_tables()
            if table_names:
                tables = [t for t in tables if t.get("table") in table_names]
            schema_block = build_schema_block(tables)
            return (
                "Схема таблиц (загружена при старте агента):\n\n"
                + schema_block
            )
        except Exception as exc:
            print(f"⚠️  SubAgent: could not fetch schema: {exc}")
            return (
                "Схема недоступна при старте. "
                "Используй инструмент `list_tables` чтобы получить список таблиц."
            )

    # ─── Load skill files ─────────────────────────────────────────────────────

    @staticmethod
    def _load_skill_files(skill_paths: list[Path]) -> str:
        """Load and concatenate skill MD files for embedding in system prompt."""
        parts: list[str] = []
        for path in skill_paths:
            try:
                content = path.read_text(encoding="utf-8").strip()
                if content:
                    parts.append(content)
            except Exception as exc:
                print(f"⚠️  Could not load sub-agent skill from {path}: {exc}")
        return "\n\n---\n\n".join(parts)

    # ─── Public API ───────────────────────────────────────────────────────────

    def run(self, query: str) -> dict:
        """
        Execute a single stateless query and return structured result.

        Returns:
            {
              "success":     bool,
              "text_output": str,     # Final Markdown answer
              "plots":       list,    # base64 PNG data URIs
              "tool_calls":  list,    # Compact log of tool invocations
              "error":       str | None,
            }
        """
        config = {
            # 1 agent + N cycles × 2 + force_summary(1) + buffer(3)
            "recursion_limit": 1 + self._max_iterations * 2 + 1 + 3,
        }

        try:
            result = self.graph.invoke(
                {"messages": [HumanMessage(content=query)]},
                config=config,
            )
            messages: list = result.get("messages", [])
            text_output = self._extract_final_text(messages)
            plots = self._extract_plots(messages)
            tool_calls = self._extract_tool_calls(messages)

            return {
                "success": True,
                "text_output": text_output,
                "plots": plots,
                "tool_calls": tool_calls,
                "error": None,
            }
        except Exception as exc:
            import traceback as tb
            return {
                "success": False,
                "text_output": "",
                "plots": [],
                "tool_calls": [],
                "error": str(exc),
                "traceback": tb.format_exc(),
            }

    # ─── Extractors (mirrors AnalyticsAgent) ──────────────────────────────────

    @staticmethod
    def _extract_final_text(messages: list) -> str:
        for msg in reversed(messages):
            if not isinstance(msg, AIMessage):
                continue
            content = msg.content
            if isinstance(content, str) and content.strip():
                return content
            if isinstance(content, list):
                parts = [
                    block["text"]
                    for block in content
                    if isinstance(block, dict) and block.get("type") == "text"
                ]
                text = "\n".join(parts).strip()
                if text:
                    return text
        return ""

    @staticmethod
    def _extract_plots(messages: list) -> list[str]:
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

    @staticmethod
    def _extract_tool_calls(messages: list) -> list[dict]:
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
                args = tc.get("args", {})
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
                            if not data.get("success"):
                                entry["error"] = data.get("error", "")
                    except Exception:
                        entry["output_raw"] = str(tm.content)[:500]
                tool_calls.append(entry)
        return tool_calls
