"""
Session-scoped runtime context + ContextVar для sandbox / ClickHouse tool.

Во время одного invoke() агента ставится session_id (через ContextVar),
так что вложенные инструменты и subagent-вызовы могут:
  - знать в какую папку сохранять parquet (TEMP_DIR/sessions/<session_id>/parquet/)
  - сохранять графики с уникальными именами в /plots/ папке этой сессии
  - вести /plots/index.md с описаниями графиков

Это отделено от AgentState / BaseBackend чтобы проще пробросить в низкоуровневые
tools (clickhouse_query / python_analysis), которые исторически работают
без state-объекта.
"""
from __future__ import annotations

from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path

# Default TEMP_DIR (читаем config.TEMP_DIR при старте, но здесь fallback)
try:
    from config import TEMP_DIR as _TEMP_DIR  # type: ignore
except Exception:
    _TEMP_DIR = Path(__file__).resolve().parent.parent / "temp_data"
_TEMP_DIR = Path(_TEMP_DIR)
_TEMP_DIR.mkdir(parents=True, exist_ok=True)


@dataclass(frozen=True)
class SessionContext:
    """Контекст для одной сессии (thread_id в терминах LangGraph)."""
    session_id: str
    client_id: str
    temp_root: Path  # TEMP_DIR/sessions/<session_id>/

    @property
    def parquet_dir(self) -> Path:
        p = self.temp_root / "parquet"
        p.mkdir(parents=True, exist_ok=True)
        return p

    @property
    def plots_dir(self) -> Path:
        p = self.temp_root / "plots"
        p.mkdir(parents=True, exist_ok=True)
        return p

    @property
    def memories_dir(self) -> Path:
        p = self.temp_root / "memories"
        p.mkdir(parents=True, exist_ok=True)
        return p

    @property
    def plots_index(self) -> Path:
        """Path to /plots/index.md — auto-maintained log of created charts."""
        return self.plots_dir / "index.md"


# ─── ContextVar ──────────────────────────────────────────────────────────────

_CURRENT: ContextVar[SessionContext | None] = ContextVar("magnetto_session", default=None)


def make_session_context(session_id: str, client_id: str = "magnetto") -> SessionContext:
    """Build a SessionContext with standard path layout under TEMP_DIR."""
    temp_root = _TEMP_DIR / "sessions" / session_id
    temp_root.mkdir(parents=True, exist_ok=True)
    return SessionContext(
        session_id=session_id,
        client_id=client_id,
        temp_root=temp_root,
    )


def set_current_session(ctx: SessionContext):
    """
    Install session context for the duration of a with-block.

    Usage:
        with set_current_session(ctx):
            agent.invoke(...)
    """
    token = _CURRENT.set(ctx)

    class _Restore:
        def __enter__(self_inner): return ctx
        def __exit__(self_inner, *a): _CURRENT.reset(token)

    return _Restore()


def get_current_session() -> SessionContext | None:
    """Get active session context or None if not set."""
    return _CURRENT.get()


def current_parquet_dir() -> Path:
    """Where clickhouse_query should save parquet."""
    ctx = _CURRENT.get()
    if ctx is None:
        # Fallback — legacy behaviour (shared TEMP_DIR/parquet)
        p = _TEMP_DIR / "parquet_shared"
        p.mkdir(parents=True, exist_ok=True)
        return p
    return ctx.parquet_dir


def current_plots_dir() -> Path:
    """Where python_analysis should save plot PNGs."""
    ctx = _CURRENT.get()
    if ctx is None:
        p = _TEMP_DIR / "plots_shared"
        p.mkdir(parents=True, exist_ok=True)
        return p
    return ctx.plots_dir
