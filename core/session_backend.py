"""
Runtime-aware CompositeBackend factory — per-session виртуальная ФС для deepagents.

Structure:
  /parquet/<hash>.parquet    → физически: TEMP_DIR/sessions/<session_id>/parquet/*
  /plots/<name>.png          → физически: TEMP_DIR/sessions/<session_id>/plots/*
  /plots/index.md            → reference graph index (auto-maintained)
  /memories/*.md             → заметки между turn'ами
  /skills/<name>/SKILL.md    → client skills (read-only, shared between sessions)
  /shared_skills/<name>/SKILL.md → общие skills (read-only)
  /AGENTS.md                 → identity
  /data_map.md               → карта данных

Backend callable signature (deepagents 0.5.3):
    backend: Callable[[ToolRuntime], BackendProtocol]

Мы получаем `session_id` из state или config через ToolRuntime и строим
правильный FilesystemBackend на `TEMP_DIR/sessions/<session_id>/...`.
"""
from __future__ import annotations

from pathlib import Path
from typing import Callable

try:
    from config import TEMP_DIR as _TEMP_DIR  # type: ignore
except Exception:
    _TEMP_DIR = Path(__file__).resolve().parent.parent / "temp_data"
_TEMP_DIR = Path(_TEMP_DIR)

from .session_context import get_current_session, make_session_context


# Root containing AGENTS.md, data_map.md, skills/, shared_skills/, subagents/
_CLIENTS_ROOT = Path(__file__).resolve().parent.parent / "clients"


def make_backend_factory(client_id: str = "magnetto") -> Callable:
    """
    Return a callable that, given a ToolRuntime, builds a CompositeBackend
    with per-session parquet/plots/memories and shared read-only skills/docs.

    deepagents 0.5.3 accepts `backend=Callable[[ToolRuntime], Backend]`,
    invoked on every tool call. We read session_id from the runtime config's
    `thread_id` (LangGraph convention) or fall back to ContextVar.
    """
    from deepagents.backends import (
        CompositeBackend,
        FilesystemBackend,
        StateBackend,
    )

    client_dir = _CLIENTS_ROOT / client_id
    if not client_dir.exists():
        raise ValueError(f"Client dir not found: {client_dir}")

    def _build(runtime) -> CompositeBackend:
        # Resolve session_id
        session_id = _extract_session_id(runtime)
        session_root = _TEMP_DIR / "sessions" / session_id
        session_root.mkdir(parents=True, exist_ok=True)
        (session_root / "parquet").mkdir(exist_ok=True)
        (session_root / "plots").mkdir(exist_ok=True)
        (session_root / "memories").mkdir(exist_ok=True)

        routes: dict = {
            # Per-session filesystems (RW)
            "/parquet/":  FilesystemBackend(root_dir=str(session_root / "parquet"),  virtual_mode=True),
            "/plots/":    FilesystemBackend(root_dir=str(session_root / "plots"),    virtual_mode=True),
            "/memories/": FilesystemBackend(root_dir=str(session_root / "memories"), virtual_mode=True),

            # Client-wide read-only resources (same for all sessions of same client)
            "/skills/":        FilesystemBackend(root_dir=str(client_dir / "skills"),        virtual_mode=True),
            "/shared_skills/": FilesystemBackend(root_dir=str(client_dir / "shared_skills"), virtual_mode=True),
            "/subagents/":     FilesystemBackend(root_dir=str(client_dir / "subagents"),     virtual_mode=True),
        }
        # Top-level files (AGENTS.md, data_map.md) live under / — use client_dir as default.
        default = FilesystemBackend(root_dir=str(client_dir), virtual_mode=True)

        return CompositeBackend(default=default, routes=routes)

    return _build


def _extract_session_id(runtime) -> str:
    """
    Pull session_id from ToolRuntime.

    deepagents/LangGraph ToolRuntime exposes:
      - runtime.config → RunnableConfig with {'configurable': {'thread_id': ...}}
      - runtime.context (optional) — our custom context

    We try: context.session_id → config.thread_id → ContextVar → fallback.
    """
    # 1. Explicit context (if Context schema set in create_deep_agent)
    ctx = getattr(runtime, "context", None)
    if ctx is not None:
        sid = getattr(ctx, "session_id", None)
        if sid:
            return str(sid)
        if isinstance(ctx, dict) and ctx.get("session_id"):
            return str(ctx["session_id"])

    # 2. LangGraph config thread_id
    config = getattr(runtime, "config", None)
    if config is not None:
        cfg = (
            config.get("configurable", {})
            if isinstance(config, dict)
            else getattr(config, "configurable", {}) or {}
        )
        tid = cfg.get("thread_id") if isinstance(cfg, dict) else None
        if tid:
            return str(tid)

    # 3. ContextVar (set by API server before invoke)
    sc = get_current_session()
    if sc is not None:
        return sc.session_id

    # 4. Fallback — shared scratch namespace
    return "__shared__"
