"""
CachingMiddleware — ставит `cache_control: ephemeral` на стабильные блоки
system prompt и messages, чтобы Anthropic prompt caching работал на полную
глубину tool-chain'а.

Anthropic разрешает **до 4 cache_control маркеров в одном запросе**.
Используем все 4, чтобы получить rolling incremental cache внутри turn'а:

  1. System prompt (последний блок). Самая стабильная часть — AGENTS.md +
     data_map.md + skills index + SUBAGENT.md body + schema_section + блок
     «Сегодня + НДС». Меняется только в полночь (новая дата), всё остальное
     время byte-stable → кэш живёт.

  2. Последний HumanMessage. Граница «история до текущего turn». В пределах
     одного turn'а не сдвигается.

  3. Предпоследний ToolMessage (если есть). Ключевая точка для rolling
     window: на шаге N+1 он уже был «последним ToolMessage» на шаге N,
     Anthropic записал кэш до этой позиции на шаге N, и на шаге N+1
     Anthropic находит hit до этой позиции через prefix match.

  4. Последний ToolMessage. Новый «конец известного мира» — кэш пишется
     только на дельту между (3) и (4) — это обычно один свежий tool_result.

Итог: каждый следующий model_call читает из кэша всё до предпоследнего
ToolMessage, пишет только последний tool_result. Без этой схемы (до фикса)
на каждом шаге переписывался ВЕСЬ накопленный tool-chain, потому что
маркер был только на самом свежем tool_result, а Anthropic проверяет hits
только на позициях текущих маркеров.

Правила:
  - Работает только если модель — Anthropic (детектится по имени).
  - Для не-Anthropic (DeepSeek, OpenAI) не делает ничего.
  - Не модифицирует сам текст, только оборачивает content в блочный формат
    с cache_control (метаданные, не участвуют в хэше кэш-ключа).

Использование:
  create_deep_agent(
      model=...,
      middleware=[CachingMiddleware()],
      ...
  )
"""
from __future__ import annotations

from copy import copy
from typing import Any

from langchain.agents.middleware.types import AgentMiddleware, ModelRequest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage


# Anthropic hard limit — не более 4 cache_control маркеров в одном запросе.
_MAX_BREAKPOINTS = 4


def _is_anthropic(model: Any) -> bool:
    """Detect Anthropic-backed model (direct ChatAnthropic or OpenRouter wrapper)."""
    # Direct Anthropic wrapper
    cls = type(model).__name__
    if "Anthropic" in cls:
        return True
    # OpenRouter / OpenAI-compatible wrappers — inspect model name
    for attr in ("model_name", "model", "model_id"):
        val = getattr(model, attr, None)
        if isinstance(val, str) and "claude" in val.lower():
            return True
    # extra_body pinning check
    extra = getattr(model, "extra_body", None) or {}
    if isinstance(extra, dict):
        provider = extra.get("provider", {})
        if isinstance(provider, dict):
            order = provider.get("order") or []
            if any("anthropic" in str(p).lower() for p in order):
                return True
    return False


def _attach_cache_control(content: Any) -> list[dict]:
    """
    Normalize `content` to list-of-blocks and attach cache_control ephemeral
    to the last block.  Returns a fresh list (does not mutate input).
    """
    if isinstance(content, str):
        if not content:
            return content  # type: ignore[return-value]
        return [{
            "type": "text",
            "text": content,
            "cache_control": {"type": "ephemeral"},
        }]
    if isinstance(content, list):
        if not content:
            return content
        new = [dict(b) if isinstance(b, dict) else b for b in content]
        last = new[-1]
        if isinstance(last, dict):
            last = dict(last)
            last["cache_control"] = {"type": "ephemeral"}
            new[-1] = last
        return new
    return content


def _clone_message_with_content(msg, new_content):
    """Create a copy of msg with replaced content (pydantic-safe)."""
    try:
        return msg.model_copy(update={"content": new_content})
    except Exception:
        out = copy(msg)
        out.content = new_content
        return out


def _apply_breakpoints(request: ModelRequest) -> None:
    """
    Shared logic for sync/async wrap_model_call. Mutates `request` in-place:
      - attaches cache_control to the last block of system_message (breakpoint #1)
      - attaches cache_control to up to TWO last ToolMessage positions
        (breakpoints #2 and #3 — rolling window for tool-chain)
      - attaches cache_control to the last HumanMessage (breakpoint #4)

    Total ≤ 4, within Anthropic's hard limit.
    """
    # ── 1. System prompt breakpoint ───────────────────────────────────────
    if request.system_message is not None:
        content = request.system_message.content
        new_content = _attach_cache_control(content)
        if new_content is not content:
            request.system_message = _clone_message_with_content(
                request.system_message, new_content
            )

    # ── 2-4. Messages: last HumanMessage + last TWO ToolMessage'ей ────────
    msgs = list(request.messages)

    # Все индексы ToolMessage в порядке появления
    tool_indices = [i for i, m in enumerate(msgs) if isinstance(m, ToolMessage)]

    # Последний HumanMessage
    last_human_idx = next(
        (i for i in range(len(msgs) - 1, -1, -1) if isinstance(msgs[i], HumanMessage)),
        None,
    )

    # Соберём позиции для маркеров (ставим на 2 последних ToolMessage +
    # последний HumanMessage). Это максимум 3 маркера в messages + 1 system
    # = 4, укладываемся в Anthropic лимит.
    positions_to_mark: list[int] = []
    positions_to_mark.extend(tool_indices[-2:])           # последние 2 ToolMessage
    if last_human_idx is not None:
        positions_to_mark.append(last_human_idx)

    # Дедуплицируем (на всякий — теоретически последний human не может быть
    # tool, но защищаемся) и применяем маркеры.
    marked: set[int] = set()
    for idx in positions_to_mark:
        if idx in marked:
            continue
        msg = msgs[idx]
        new_c = _attach_cache_control(msg.content)
        if new_c is not msg.content:
            msgs[idx] = _clone_message_with_content(msg, new_c)
            marked.add(idx)

    request.messages = msgs


class CachingMiddleware(AgentMiddleware):
    """
    Attach `cache_control: ephemeral` strategically on up to 4 positions:
      1. System prompt (last block)
      2. Previous-to-last ToolMessage  (rolling-window anchor)
      3. Last ToolMessage              (current tool-chain tip)
      4. Last HumanMessage             (turn boundary)

    With (2) in place, each successive model_call in a tool-chain reads from
    cache up to the previous ToolMessage and writes only the delta (one new
    tool_result). Before this change only (3) was marked, so Anthropic could
    only find cache-hit up to HumanMessage — causing the whole accumulated
    tool-chain to be re-written on every step.
    """

    def wrap_model_call(self, request: ModelRequest, handler):
        if not _is_anthropic(request.model):
            return handler(request)
        _apply_breakpoints(request)
        return handler(request)

    async def awrap_model_call(self, request: ModelRequest, handler):
        if not _is_anthropic(request.model):
            return await handler(request)
        _apply_breakpoints(request)
        return await handler(request)
