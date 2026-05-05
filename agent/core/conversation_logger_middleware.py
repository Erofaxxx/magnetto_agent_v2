"""
ConversationLoggerMiddleware — пишет каждый шаг диалога в chat_logger БД
в реальном времени, связывая события подагента с родительским task() из main.

Зачем:
  Раньше chat_logger.log_turn() писал только сообщения главного агента —
  всё, что происходило ВНУТРИ подагента (его SQL, его ход мыслей, его
  ToolMessage'ы), оставалось невидимым: main видит только финальный
  SubagentResult.summary, а его и логировали.

  Этот middleware подключается отдельно к main и к каждому подагенту.
  При вызове `task` tool из main мы запоминаем tool_call_id в ContextVar.
  Подагент работает в той же coroutine — его middleware читает ContextVar
  и подписывает им свои события. В БД появляется иерархия:

      main:tool_call(task, id=ABC)
        sub:ai_thinking(parent_tool_call_id=ABC)
        sub:tool_call(clickhouse_query, parent_tool_call_id=ABC)
        sub:tool_result(clickhouse_query, parent_tool_call_id=ABC)
        sub:ai_answer(parent_tool_call_id=ABC)
      main:tool_result(task, id=ABC)

Также ловит блоки extended thinking (если включены) — записывает их как
event_type='ai_thinking_extended', чтобы видны были рассуждения, которые
обычный _to_text() игнорирует.

Disable via env: CONVERSATION_LOG=0.
"""
from __future__ import annotations

import contextvars
import json
import os
from datetime import datetime, timezone
from typing import Optional

from langchain.agents.middleware.types import AgentMiddleware, ModelRequest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage


_LOG_ENABLED = os.environ.get("CONVERSATION_LOG", "1") != "0"

# Связь sub→main: main wrap_tool_call("task") выставляет id, подагент читает.
_CURRENT_PARENT_TC_ID: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar(
    "current_parent_tc_id", default=None
)
# Имя текущего подагента (из args.subagent_type) — попадёт в agent_role.
_CURRENT_SUBAGENT_NAME: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar(
    "current_subagent_name", default=None
)

_MAX_CONTENT = 50_000


def _trim(s: str) -> str:
    if not s:
        return s
    if len(s) > _MAX_CONTENT:
        return s[:_MAX_CONTENT] + "\n… [truncated at 50KB]"
    return s


def _to_text(content) -> str:
    """Extract plain-text portion of an AIMessage content (str or block list)."""
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


def _extract_thinking_blocks(content) -> list[str]:
    """Pull `type: thinking` blocks from Anthropic extended-thinking content."""
    if not isinstance(content, list):
        return []
    out: list[str] = []
    for blk in content:
        if not isinstance(blk, dict):
            continue
        if blk.get("type") == "thinking":
            txt = blk.get("thinking") or blk.get("text") or ""
            if txt:
                out.append(txt)
    return out


def _get_logger():
    try:
        from chat_logger import get_logger
        from config import DB_PATH
        return get_logger(DB_PATH)
    except Exception as exc:
        # Logger init failure must never break the agent
        print(f"[ConversationLog] logger unavailable: {exc}")
        return None


def _session_id() -> str:
    """Read session_id from ContextVar set by api_server before invoke()."""
    try:
        from .session_context import get_current_session
        ctx = get_current_session()
        sid = getattr(ctx, "session_id", None) if ctx else None
        return sid or "unknown"
    except Exception:
        return "unknown"


def _turn_index(messages: list) -> int:
    """1-based turn = number of HumanMessages seen so far."""
    if not messages:
        return 0
    return sum(1 for m in messages if isinstance(m, HumanMessage))


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _agent_role() -> str:
    """
    Determine current agent role:
      - sub:<name> if we are inside a task() call (ContextVar set)
      - main otherwise
    """
    parent = _CURRENT_PARENT_TC_ID.get()
    if not parent:
        return "main"
    name = _CURRENT_SUBAGENT_NAME.get() or "unknown"
    return f"sub:{name}"


def _unwrap_response_messages(response) -> list:
    """
    wrap_model_call sees the raw model response. The fresh AIMessage(s)
    produced by this call live at .result (ModelResponse) or
    .model_response.result (ExtendedModelResponse). Walk down a few levels
    defensively in case the wrapper changes shape.
    """
    node = response
    for _ in range(4):
        if node is None:
            return []
        if hasattr(node, "model_response") and node.model_response is not None:
            node = node.model_response
            continue
        if hasattr(node, "result"):
            return list(getattr(node, "result") or [])
        if isinstance(node, dict):
            res = node.get("result")
            if isinstance(res, list):
                return list(res)
            return []
        if isinstance(node, AIMessage):
            return [node]
        return []
    return []


class ConversationLoggerMiddleware(AgentMiddleware):
    """
    Real-time event logger. Stateless — safe to share one instance across
    main and all subagents (the only place we'd get conflicting state is
    ContextVar, which is per-coroutine and properly scoped).
    """

    # ─── Model call ────────────────────────────────────────────────────────

    def wrap_model_call(self, request: ModelRequest, handler):
        if not _LOG_ENABLED:
            return handler(request)
        response = handler(request)
        try:
            self._log_response(request, response)
        except Exception as exc:
            print(f"[ConversationLog] wrap_model_call write failed: {exc}")
        return response

    async def awrap_model_call(self, request: ModelRequest, handler):
        if not _LOG_ENABLED:
            return await handler(request)
        response = await handler(request)
        try:
            self._log_response(request, response)
        except Exception as exc:
            print(f"[ConversationLog] awrap_model_call write failed: {exc}")
        return response

    def _log_response(self, request: ModelRequest, response) -> None:
        logger = _get_logger()
        if logger is None:
            return

        sid = _session_id()
        msgs = list(request.messages or [])
        turn_idx = _turn_index(msgs)
        role = _agent_role()
        parent = _CURRENT_PARENT_TC_ID.get()

        for ai_msg in _unwrap_response_messages(response):
            if not isinstance(ai_msg, AIMessage):
                continue
            text = _to_text(ai_msg.content)
            tool_calls = list(getattr(ai_msg, "tool_calls", []) or [])

            # Extended thinking blocks (claude opus/sonnet with thinking enabled)
            for thought in _extract_thinking_blocks(ai_msg.content):
                logger.log_event(
                    session_id=sid,
                    turn_index=turn_idx,
                    event_type="ai_thinking_extended",
                    agent_role=role,
                    tool_name=None,
                    tool_call_id=None,
                    parent_tool_call_id=parent,
                    content=_trim(thought),
                    created_at=_now(),
                )

            # Plain text portion: either preamble before tool_calls, or final answer
            if text and tool_calls:
                logger.log_event(
                    session_id=sid,
                    turn_index=turn_idx,
                    event_type="ai_thinking",
                    agent_role=role,
                    tool_name=None,
                    tool_call_id=None,
                    parent_tool_call_id=parent,
                    content=_trim(text),
                    created_at=_now(),
                )
            elif text and not tool_calls:
                logger.log_event(
                    session_id=sid,
                    turn_index=turn_idx,
                    event_type="ai_answer",
                    agent_role=role,
                    tool_name=None,
                    tool_call_id=None,
                    parent_tool_call_id=parent,
                    content=_trim(text),
                    created_at=_now(),
                )

    # ─── Tool call ─────────────────────────────────────────────────────────

    def wrap_tool_call(self, request, handler):
        if not _LOG_ENABLED:
            return handler(request)
        return self._wrap_tool_call_sync(request, handler, is_async=False)

    async def awrap_tool_call(self, request, handler):
        if not _LOG_ENABLED:
            return await handler(request)

        logger = _get_logger()
        meta = self._tool_meta(request)
        if logger is not None:
            self._log_tool_call(logger, meta)

        token_parent, token_name = self._enter_task_context(meta)
        try:
            response = await handler(request)
        finally:
            self._exit_task_context(token_parent, token_name)

        if logger is not None:
            self._log_tool_result(logger, meta, response)
        return response

    def _wrap_tool_call_sync(self, request, handler, *, is_async: bool):
        logger = _get_logger()
        meta = self._tool_meta(request)
        if logger is not None:
            self._log_tool_call(logger, meta)

        token_parent, token_name = self._enter_task_context(meta)
        try:
            response = handler(request)
        finally:
            self._exit_task_context(token_parent, token_name)

        if logger is not None:
            self._log_tool_result(logger, meta, response)
        return response

    # ─── Helpers ───────────────────────────────────────────────────────────

    @staticmethod
    def _tool_meta(request) -> dict:
        """Best-effort extraction of (tool_name, tool_call_id, args, state)."""
        tool = getattr(request, "tool", None)
        tool_name = getattr(tool, "name", None) if tool else None
        if not tool_name:
            tool_name = getattr(request, "tool_name", None) or ""

        tool_call = getattr(request, "tool_call", None)
        if isinstance(tool_call, dict):
            tc_id = tool_call.get("id") or ""
            args = tool_call.get("args") or {}
        else:
            tc_id = (
                getattr(request, "tool_call_id", None)
                or (getattr(tool_call, "id", None) if tool_call else None)
                or ""
            )
            args = (
                getattr(tool_call, "args", None) if tool_call else None
            ) or getattr(request, "args", None) or {}

        state = getattr(request, "state", None) or {}
        if isinstance(state, dict):
            state_msgs = state.get("messages") or []
        else:
            state_msgs = getattr(state, "messages", []) or []

        return {
            "tool_name": tool_name or "",
            "tool_call_id": tc_id,
            "args": args if isinstance(args, dict) else {},
            "state_messages": state_msgs,
        }

    def _log_tool_call(self, logger, meta: dict) -> None:
        try:
            content = json.dumps(meta["args"], ensure_ascii=False, default=str)
        except Exception:
            content = str(meta["args"])
        logger.log_event(
            session_id=_session_id(),
            turn_index=_turn_index(meta["state_messages"]),
            event_type="tool_call",
            agent_role=_agent_role(),
            tool_name=meta["tool_name"],
            tool_call_id=meta["tool_call_id"],
            parent_tool_call_id=_CURRENT_PARENT_TC_ID.get(),
            content=_trim(content),
            created_at=_now(),
        )

    def _log_tool_result(self, logger, meta: dict, response) -> None:
        try:
            if isinstance(response, ToolMessage):
                tm_content = _to_text(response.content)
            else:
                tm_content = _to_text(getattr(response, "content", "") or "")
        except Exception:
            tm_content = str(response)
        logger.log_event(
            session_id=_session_id(),
            turn_index=_turn_index(meta["state_messages"]),
            event_type="tool_result",
            agent_role=_agent_role(),
            tool_name=meta["tool_name"],
            tool_call_id=meta["tool_call_id"],
            parent_tool_call_id=_CURRENT_PARENT_TC_ID.get(),
            content=_trim(tm_content),
            created_at=_now(),
        )

    @staticmethod
    def _enter_task_context(meta: dict):
        """
        If this is a `task` tool call, mark its tool_call_id as the parent for
        any subagent events emitted inside handler(). The subagent_type from
        args becomes the agent_role suffix (sub:<name>).
        """
        if meta["tool_name"] != "task":
            return (None, None)
        sub_name = ""
        args = meta.get("args") or {}
        if isinstance(args, dict):
            sub_name = (
                args.get("subagent_type")
                or args.get("subagent")
                or args.get("name")
                or ""
            )
        token_parent = _CURRENT_PARENT_TC_ID.set(meta["tool_call_id"] or "")
        token_name = _CURRENT_SUBAGENT_NAME.set(sub_name or "unknown")
        return (token_parent, token_name)

    @staticmethod
    def _exit_task_context(token_parent, token_name) -> None:
        if token_name is not None:
            _CURRENT_SUBAGENT_NAME.reset(token_name)
        if token_parent is not None:
            _CURRENT_PARENT_TC_ID.reset(token_parent)
