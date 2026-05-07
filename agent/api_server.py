"""
FastAPI server for ClickHouse Analytics Agent.
Endpoints:
  GET  /                              health check
  GET  /health                        health check (for monitoring)
  GET  /api/info                      service info
  POST /api/session/new               create a new conversation session
  GET  /api/session/{session_id}      get session metadata
  POST /api/analyze                   submit query → returns job_id immediately
  GET  /api/job/{job_id}              poll job status / get result
  GET  /api/chat-stats                database statistics
Architecture change: async job queue.
  - POST /api/analyze starts the agent in background, returns job_id instantly.
  - GET  /api/job/{job_id} returns status: "pending" | "running" | "done" | "error"
  - Results are kept in memory for 2 hours (JOB_TTL_SECONDS).
  - Client reconnecting after disconnect can still fetch the result.
"""
import asyncio
import decimal as _decimal
import hmac
import json
import math as _math
import re as _re_mod
import threading as _threading
import uuid
from contextlib import asynccontextmanager
from datetime import date as _date, datetime, timezone
_datetime = datetime  # alias for _serialize_value
from typing import Optional, Literal
import uvicorn
from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
# config.py loads .env exactly once on import; importing it here is enough.
from config import ALLOWED_MODELS, HOST, MODEL, PORT, SERVER_URL


# ─── Session-id validation ────────────────────────────────────────────────────
# uuid4 strings are alphanumerics + dashes; we accept the same shape generically
# so legacy ids still work but reject anything path-traversal-shaped before
# concatenating into a filesystem path.
# `\A...\Z` instead of `^...$` so a trailing `\n` doesn't sneak through
# (Python's `$` matches *before* a final newline; `\Z` is hard end-of-string).
_SESSION_ID_RE = _re_mod.compile(r"\A[A-Za-z0-9_-]{8,64}\Z")


def _validate_session_id(session_id: str) -> str:
    if not _SESSION_ID_RE.match(session_id or ""):
        raise HTTPException(status_code=400, detail="Invalid session_id")
    # Block reserved internal sentinels (`__shared__`, `__anon__`) which are
    # alphanumeric+underscore and would otherwise pass the regex — a hostile
    # client could claim ownership and break the shared/anon slot for others.
    if session_id in _RESERVED_SESSION_IDS:
        raise HTTPException(status_code=400, detail="Invalid session_id")
    return session_id


_X_USER_ID_RE = _re_mod.compile(r"\A[A-Za-z0-9_.@-]{1,128}\Z")
_SEGMENT_ID_RE = _re_mod.compile(r"\Aseg_[A-Za-z0-9]{4,32}\Z")
# job_id is uuid4 (str(uuid.uuid4())); regex is a superset that also covers
# any legacy-style job_id that might still be in flight after a deploy.
_JOB_ID_RE = _re_mod.compile(r"\A[A-Za-z0-9-]{8,64}\Z")


def _validate_job_id(job_id: str) -> str:
    if not _JOB_ID_RE.match(job_id or ""):
        raise HTTPException(status_code=400, detail="Invalid job_id")
    return job_id


def _validate_segment_id(segment_id: str) -> str:
    """Reject malformed segment_id BEFORE it hits SQLite.

    SegmentStore generates ids as `seg_<uuid4-hex[:8]>`; the regex above is
    a superset of the spawn pattern so we don't break legacy ids while still
    bounding length and character set.
    """
    if not _SEGMENT_ID_RE.match(segment_id or ""):
        raise HTTPException(status_code=400, detail="Invalid segment_id")
    return segment_id


def _validate_x_user_id(x_user_id: Optional[str]) -> Optional[str]:
    """Reject malformed X-User-Id values BEFORE they hit segment_store SQLite.

    None is allowed (anonymous user falls back to _SHARED_OWNER); any non-empty
    value must match the regex above so a hostile client can't push a 1MB header
    or path-traversal-shaped id into the owner column / cache keys.
    """
    if x_user_id is None:
        return None
    if not _X_USER_ID_RE.match(x_user_id):
        raise HTTPException(status_code=400, detail="Invalid X-User-Id")
    return x_user_id


# ─── Strong-ref set for fire-and-forget tasks ────────────────────────────────
# asyncio.create_task only keeps a weak reference to the task; without an
# external strong ref the loop may garbage-collect a still-running task
# (see https://docs.python.org/3/library/asyncio-task.html#asyncio.create_task).
# We add each job task here and self-remove on completion.
_running_tasks: set[asyncio.Task] = set()


# ─── ChatLogger background writer ────────────────────────────────────────────
# Earlier code spawned 2 daemon threads per analyze() call. Under load that
# was an unbounded thread fan-out and an inconsistent shutdown experience —
# daemon=True kills mid-INSERT, daemon=False blocks SIGTERM. The writer
# below consumes a single Queue from a single long-lived thread, so the
# work is bounded and a clean shutdown can drain via a sentinel.
import queue as _queue_mod
_log_queue: "_queue_mod.Queue[Optional[dict]]" = _queue_mod.Queue(maxsize=10_000)
_log_writer_thread: Optional[_threading.Thread] = None


def _enqueue_log(item: dict) -> None:
    """Best-effort enqueue. Drops the item if the queue is saturated rather
    than blocking the request thread — observability must never throttle the
    user-facing path."""
    try:
        _log_queue.put_nowait(item)
    except _queue_mod.Full:
        print("[ChatLogger] queue full, dropping log item")


def _log_writer_loop() -> None:
    """Single consumer of `_log_queue`. None is the shutdown sentinel."""
    from chat_logger import get_logger
    from config import DB_PATH
    logger = get_logger(DB_PATH)
    while True:
        item = _log_queue.get()
        try:
            if item is None:
                return
            kind = item.get("kind")
            if kind == "turn":
                logger.log_turn(item["session_id"], item["msgs"], item["started_at"])
            elif kind == "router":
                logger.log_router(
                    item["session_id"],
                    item["turn_index"],
                    item["active_skills"],
                    item["query"],
                    item["started_at"],
                )
        except Exception as exc:
            print(f"[ChatLogger] write error (item={item.get('kind')}): {exc}")
        finally:
            _log_queue.task_done()


# ─── Analytics-session ownership (in-memory) ─────────────────────────────────
# Mirrors segment_store.session_owned_by but for the main analytics flow.
# Stored in-process: analytics jobs themselves live in _jobs (also in-memory),
# so persisting ownership across restarts is unnecessary — when the process
# dies, the corresponding sessions are gone too.
# Each entry is (owner, last_touched_ts) so cleanup_loop can drop bindings
# for sessions that haven't seen a request in SESSION_OWNER_TTL_SECONDS.
SESSION_OWNER_TTL_SECONDS = int(_os.environ.get("SESSION_OWNER_TTL_SECONDS", "86400"))
_session_owners: dict[str, tuple[str, float]] = {}
_session_owners_lock = _threading.Lock()
_ANON_OWNER = "__anon__"
# Owner-shaped strings reserved for internal sentinels — reject them as
# session_ids so a hostile X-User-Id can't grab the shared/anon slot.
_RESERVED_SESSION_IDS = frozenset({"__shared__", "__anon__"})


def _record_analytics_session_owner(session_id: str, owner: str) -> None:
    """First-call-wins binding of a session_id to an owner. Idempotent for
    repeat calls from the same owner; silently skipped for a different owner
    (the original binding stays — equivalent to INSERT OR IGNORE)."""
    import time
    now = time.time()
    with _session_owners_lock:
        existing = _session_owners.get(session_id)
        if existing is None:
            _session_owners[session_id] = (owner, now)
        elif existing[0] == owner:
            # Refresh TTL on each access by the legitimate owner.
            _session_owners[session_id] = (owner, now)


def _analytics_session_owned_by(session_id: str, owner: str) -> bool:
    """True iff this owner created the session, OR the session has no recorded
    owner yet AND the caller is anonymous (legacy/no-auth deployments)."""
    import time
    with _session_owners_lock:
        record = _session_owners.get(session_id)
        if record is None:
            return owner == _ANON_OWNER
        recorded_owner, _ = record
        if recorded_owner == owner:
            # Touch on read so the entry stays alive while in active use.
            _session_owners[session_id] = (owner, time.time())
            return True
        return False


def _expire_session_owners(now_ts: float) -> int:
    """Drop entries that haven't been touched within SESSION_OWNER_TTL_SECONDS.

    Called from _cleanup_loop. Returns count removed (for logging).
    """
    cutoff = now_ts - SESSION_OWNER_TTL_SECONDS
    with _session_owners_lock:
        expired = [sid for sid, (_o, ts) in _session_owners.items() if ts < cutoff]
        for sid in expired:
            _session_owners.pop(sid, None)
    return len(expired)


def _require_session_access(session_id: str, x_user_id: Optional[str]) -> None:
    """Raise 404 if `x_user_id` does not own `session_id`. Treat absent
    X-User-Id as the anonymous owner so single-tenant deployments still work
    without per-user headers — but as soon as one client sends X-User-Id, that
    same id must show up to access the same session.
    """
    owner = x_user_id or _ANON_OWNER
    if not _analytics_session_owned_by(session_id, owner):
        # Mask presence: same 404 whether the session is owned by someone else
        # or doesn't exist at all.
        raise HTTPException(status_code=404, detail="Session not found")


def _ch_query_locked(ch, sql: str):
    """Acquire `_ch_lock` and run a single ClickHouse query.

    The lock MUST be held inside the worker thread, not around the awaitable:
    `with _ch_lock: await asyncio.to_thread(...)` would block the event loop
    for the whole query duration because acquiring a non-reentrant
    threading.Lock from the loop thread parks every other coroutine. Holding
    the lock inside the thread keeps the loop free.
    """
    from tools import _ch_lock
    with _ch_lock:
        return ch.execute_query(sql)


# ─── Debug-endpoint authorization ─────────────────────────────────────────────
# Per-deployment opt-in: set DEBUG_TOKEN in env to enable /debug/* endpoints.
# Without the env var the endpoints respond 404 (effectively disabled),
# which matches the principle of least exposure for prod.
_DEBUG_TOKEN_ENV_NAME = "DEBUG_TOKEN"


def _require_debug_auth(x_debug_token: Optional[str] = Header(default=None, alias="X-Debug-Token")) -> None:
    """FastAPI dependency: gates /debug/* behind a constant-time-compared token."""
    import os as _os_local
    expected = (_os_local.environ.get(_DEBUG_TOKEN_ENV_NAME) or "").strip()
    if not expected:
        # Disabled in this deployment — pretend the route doesn't exist.
        raise HTTPException(status_code=404, detail="Not Found")
    if not x_debug_token or not hmac.compare_digest(expected, x_debug_token):
        raise HTTPException(status_code=401, detail="Unauthorized")

# ─── deepagents switch ────────────────────────────────────────────────────
# When USE_DEEPAGENTS=1, requests are routed through core.api_adapter
# (new deepagents-based agent). Otherwise — legacy agent.AnalyticsAgent.
import os as _os
_USE_DEEPAGENTS = _os.environ.get("USE_DEEPAGENTS", "0") in ("1", "true", "True", "yes")


# ─── Lifespan ─────────────────────────────────────────────────────────────────
# FastAPI deprecated @app.on_event("startup"/"shutdown") in favour of an
# async context manager. The lifespan replaces both events and lets us
# cleanly cancel the cleanup task on shutdown.
@asynccontextmanager
async def _lifespan(app: FastAPI):
    if _USE_DEEPAGENTS:
        from core.agent_factory import build_agent
        await asyncio.to_thread(build_agent, "magnetto", MODEL)
        print("✅ deepagents ready (USE_DEEPAGENTS=1)")
    else:
        from agent import get_agent
        get_agent()
    cleanup_task = asyncio.create_task(_cleanup_loop())
    # Start the chat-logger writer thread now so /api/analyze can immediately
    # enqueue without checking. Sentinel is pushed in finally below.
    global _log_writer_thread
    _log_writer_thread = _threading.Thread(
        target=_log_writer_loop, daemon=True, name="chat-log-writer"
    )
    _log_writer_thread.start()
    print(f"✅ ClickHouse Analytics Agent API started | {SERVER_URL}")
    try:
        yield
    finally:
        # Cancel the periodic cleanup loop and any in-flight job tasks so
        # the process can exit promptly on SIGTERM. asyncio.wait_for inside
        # _run_agent_job will see CancelledError and bail; the underlying
        # thread keeps running until it finishes naturally (Python can't
        # cancel threads), but the event loop is no longer blocked on it.
        cleanup_task.cancel()
        in_flight = list(_running_tasks)
        for t in in_flight:
            t.cancel()
        # Await everything to let CancelledError propagate cleanly. Without
        # this the lifespan returns before pending cancellations finish, and
        # uvicorn destroys the loop while tasks are still in cancellation.
        await asyncio.gather(cleanup_task, *in_flight, return_exceptions=True)
        # Flush the log queue: push the sentinel and give the writer up to
        # 5 seconds to drain. Daemon=True still bounds shutdown if it hangs.
        try:
            _log_queue.put_nowait(None)
        except _queue_mod.Full:
            pass
        if _log_writer_thread is not None:
            _log_writer_thread.join(timeout=5.0)


# ─── App ──────────────────────────────────────────────────────────────────────
app = FastAPI(
    title="ClickHouse Analytics Agent API",
    description=(
        "AI-powered advertising analytics agent. "
        "Queries ClickHouse, analyzes data with Python, returns charts & tables."
    ),
    version="2.0.0",
    docs_url="/docs",
    redoc_url="/redoc",
    lifespan=_lifespan,
)
# ─── CORS ─────────────────────────────────────────────────────────────────────
# CORS spec: при allow_credentials=True нельзя использовать "*" в allow_origins.
# Явный список оригинов задаётся через ENV CORS_ALLOWED_ORIGINS (запятые),
# например: "https://server.asktab.ru,https://app.asktab.ru".
# Если переменная пуста — credentials отключаются и используется wildcard
# (read-only публичный режим).
_cors_origins_env = (_os.environ.get("CORS_ALLOWED_ORIGINS") or "").strip()
if _cors_origins_env:
    _cors_origins = [o.strip() for o in _cors_origins_env.split(",") if o.strip()]
    _cors_credentials = True
else:
    _cors_origins = ["*"]
    _cors_credentials = False
# Explicit headers list keeps spec-compliant behaviour when credentials=True
# (some browsers reject `*` headers in that mode and Starlette would silently
# narrow it anyway). These are the only request headers we actually consume.
_CORS_HEADERS = [
    "Content-Type",
    "Authorization",
    "X-User-Id",
    "X-Debug-Token",
]
app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
    allow_credentials=_cors_credentials,
    allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
    allow_headers=_CORS_HEADERS,
)
# ─── Job store ────────────────────────────────────────────────────────────────
# job_id → JobRecord dict
# Хранится в памяти; при рестарте сервера задачи теряются (это приемлемо).
# WARNING: this implies a single-worker uvicorn deployment — multiple workers
# will each have their own dict, so a job_id created on worker A is invisible
# on worker B. Keep `--workers 1`, or move the store to Redis/SQLite later.
JOB_TTL_SECONDS = 7200  # 2 часа
JOB_TIMEOUT_SECONDS = int(_os.environ.get("JOB_TIMEOUT_SECONDS", "900"))  # 15 min default
MAX_QUERY_LENGTH = int(_os.environ.get("MAX_QUERY_LENGTH", "32000"))
JobStatus = Literal["pending", "running", "done", "error"]
_jobs: dict[str, dict] = {}
_jobs_lock = _threading.Lock()


def _new_job(session_id: str, query: str, model: Optional[str] = None) -> str:
    job_id = str(uuid.uuid4())
    with _jobs_lock:
        _jobs[job_id] = {
            "job_id": job_id,
            "session_id": session_id,
            "query": query,
            "model": model,   # None → default model
            "status": "pending",
            "created_at": datetime.now(timezone.utc).isoformat(),
            "started_at": None,
            "finished_at": None,
            "result": None,   # AnalyzeResponse dict when done
            "error": None,
        }
    return job_id


def _set_running(job_id: str) -> None:
    # No-op when the job has already been pruned by cleanup_loop — avoids
    # KeyError racing against TTL eviction.
    with _jobs_lock:
        job = _jobs.get(job_id)
        if job is None:
            return
        job["status"] = "running"
        job["started_at"] = datetime.now(timezone.utc).isoformat()


def _set_done(job_id: str, result: dict) -> None:
    with _jobs_lock:
        job = _jobs.get(job_id)
        if job is None:
            return
        job["status"] = "done"
        job["finished_at"] = datetime.now(timezone.utc).isoformat()
        job["result"] = result


def _set_error(job_id: str, error: str) -> None:
    with _jobs_lock:
        job = _jobs.get(job_id)
        if job is None:
            return
        job["status"] = "error"
        job["finished_at"] = datetime.now(timezone.utc).isoformat()
        job["error"] = error
# ─── Request / Response models ────────────────────────────────────────────────
class AnalyzeRequest(BaseModel):
    # Length is bounded so a malicious or buggy client can't push a 10 MB
    # query into the LLM context (and into Anthropic prompt cache).
    query: str = Field(min_length=1, max_length=MAX_QUERY_LENGTH)
    session_id: Optional[str] = None
    model: Optional[str] = None  # None → default model from config
class SubmitResponse(BaseModel):
    """Returned immediately after POST /api/analyze."""
    job_id: str
    session_id: str
    status: str   # always "pending"
    message: str
class JobStatusResponse(BaseModel):
    """Returned by GET /api/job/{job_id}."""
    job_id: str
    session_id: str
    status: JobStatus
    created_at: str
    started_at: Optional[str]
    finished_at: Optional[str]
    # Present only when status == "done"
    success: Optional[bool] = None
    text_output: Optional[str] = None
    plots: Optional[list[str]] = None
    plot_urls: Optional[list[str]] = None
    parquet_paths: Optional[list[str]] = None
    tool_calls: Optional[list[dict]] = None
    error: Optional[str] = None
# ─── Background worker ────────────────────────────────────────────────────────
async def _run_agent_job(job_id: str) -> None:
    """Run the agent in a thread pool and store the result in _jobs.

    `asyncio.wait_for(JOB_TIMEOUT_SECONDS)` releases the awaiting coroutine
    on timeout and lets the client poll a definitive `error` status. Note
    that Python cannot cancel a running thread, so the underlying agent
    keeps executing on the default executor until it returns naturally —
    the slot is occupied for at most `JOB_TIMEOUT_SECONDS + actual runtime`.
    A misbehaving LLM/ClickHouse stall therefore still consumes one
    executor slot until completion; a dedicated bounded ThreadPoolExecutor
    plus `cancel_futures=True` would be the next step if that becomes a
    bottleneck under load.
    """
    with _jobs_lock:
        job = _jobs.get(job_id)
    if not job:
        return
    _set_running(job_id)
    started_at = datetime.now(timezone.utc).isoformat()
    try:
        if _USE_DEEPAGENTS:
            from core.api_adapter import analyze_deepagents
            result = await asyncio.wait_for(
                asyncio.to_thread(
                    analyze_deepagents,
                    query=job["query"],
                    session_id=job["session_id"],
                    model=job.get("model"),
                ),
                timeout=JOB_TIMEOUT_SECONDS,
            )
        else:
            from agent import get_agent
            agent = get_agent(job.get("model"))
            result = await asyncio.wait_for(
                asyncio.to_thread(
                    agent.analyze,
                    user_query=job["query"],
                    session_id=job["session_id"],
                ),
                timeout=JOB_TIMEOUT_SECONDS,
            )
        _set_done(job_id, result)

        # ── Passive observability logging ──────────────────────────────────
        # Agent is already done and result is stored. We push the work onto
        # a single background queue (see _log_queue + _log_writer_loop) so
        # we don't spawn 2 new threads per request — under load the daemon
        # fan-out pattern was killed mid-write on shutdown and lost log rows.
        try:
            from langchain_core.messages import HumanMessage as _HM
            msgs = result.get("_messages", [])
            active_skills = result.get("_active_skills", [])
            turn_index = sum(1 for m in msgs if isinstance(m, _HM))
            _enqueue_log(
                {
                    "kind": "turn",
                    "session_id": job["session_id"],
                    "msgs": msgs,
                    "started_at": started_at,
                }
            )
            _enqueue_log(
                {
                    "kind": "router",
                    "session_id": job["session_id"],
                    "turn_index": turn_index,
                    "active_skills": active_skills,
                    "query": job.get("query", ""),
                    "started_at": started_at,
                }
            )
        except Exception as log_exc:
            print(f"[ChatLogger] enqueue error (non-fatal): {log_exc}")

    except asyncio.TimeoutError:
        _set_error(job_id, f"Job exceeded {JOB_TIMEOUT_SECONDS}s timeout")
        print(f"[job:{job_id}] TIMEOUT after {JOB_TIMEOUT_SECONDS}s")
    except Exception as exc:
        _set_error(job_id, str(exc))
        print(f"[job:{job_id}] ERROR: {exc}")
# ─── Session-files cleanup (deepagents) ──────────────────────────────────────
def _cleanup_session_files() -> int:
    """
    Remove parquet/plot files in sessions/<id>/ older than TEMP_FILE_TTL_SECONDS,
    then rmdir() any sub-directory and the session dir itself if it became empty.
    Without the rmdir step empty directories accumulate per-session over the
    server's lifetime, slowly eating inodes and making `iterdir()` linearly
    slower across cleanup cycles.

    Returns the number of files removed.
    """
    import time
    from config import TEMP_DIR, TEMP_FILE_TTL_SECONDS
    sessions_root = TEMP_DIR / "sessions"
    if not sessions_root.exists():
        return 0
    cutoff = time.time() - TEMP_FILE_TTL_SECONDS
    removed = 0
    for sid_dir in sessions_root.iterdir():
        if not sid_dir.is_dir():
            continue
        for sub in ("parquet", "plots"):
            d = sid_dir / sub
            if not d.exists():
                continue
            for f in d.iterdir():
                if not f.is_file():
                    continue
                try:
                    if f.stat().st_mtime < cutoff:
                        f.unlink(missing_ok=True)
                        removed += 1
                except OSError:
                    pass
            # Drop the sub-dir if it is now empty (rmdir fails for non-empty).
            try:
                d.rmdir()
            except OSError:
                pass
        # Drop the session dir itself if everything inside expired.
        try:
            sid_dir.rmdir()
        except OSError:
            pass
    return removed


# ─── Cleanup loop ─────────────────────────────────────────────────────────────
async def _cleanup_loop() -> None:
    """Remove expired jobs and parquet files every 30 minutes.

    A transient ClickHouse / FS error inside one iteration must not kill the
    loop — the whole body sits inside try/except so the loop survives until
    cancelled by lifespan shutdown.
    """
    while True:
        try:
            await asyncio.sleep(1800)
            now = datetime.now(timezone.utc).timestamp()
            # Clean expired jobs (snapshot under lock so we don't iterate
            # a dict that another coroutine is mutating).
            with _jobs_lock:
                expired = [
                    jid for jid, j in _jobs.items()
                    if j["status"] in ("done", "error")
                    and j["finished_at"]
                    and (now - datetime.fromisoformat(j["finished_at"]).timestamp()) > JOB_TTL_SECONDS
                ]
                for jid in expired:
                    _jobs.pop(jid, None)
            if expired:
                print(f"[cleanup] Removed {len(expired)} expired job(s)")
            # Clean legacy TEMP_DIR/*.parquet — only relevant when we actually
            # run on the legacy AnalyticsAgent. In deepagents mode all parquet
            # writes go through sessions/<id>/parquet/, so dragging the whole
            # legacy agent (LLM client, schema cache, SqliteSaver) into memory
            # just to glob a directory was wasteful.
            if not _USE_DEEPAGENTS:
                try:
                    from agent import get_agent
                    n = await asyncio.to_thread(get_agent().cleanup_temp_files)
                    if n:
                        print(f"[cleanup] Removed {n} expired parquet file(s)")
                except Exception as exc:
                    print(f"[cleanup] Parquet cleanup error: {exc}")
            # Clean per-session deepagents files (sessions/<id>/parquet|plots).
            if _USE_DEEPAGENTS:
                try:
                    n = await asyncio.to_thread(_cleanup_session_files)
                    if n:
                        print(f"[cleanup] Removed {n} expired session file(s)")
                except Exception as exc:
                    print(f"[cleanup] Session cleanup error: {exc}")
            # Drop stale ownership records — in-memory dict would otherwise
            # grow unbounded over server uptime (one entry per session, ~150B).
            try:
                n_o = _expire_session_owners(now)
                if n_o:
                    print(f"[cleanup] Dropped {n_o} expired session owner(s)")
            except Exception as exc:
                print(f"[cleanup] Session-owner cleanup error: {exc}")
        except asyncio.CancelledError:
            # Propagate cancellation so lifespan can await us cleanly.
            raise
        except Exception as exc:
            # Any other error — log and keep the loop alive.
            print(f"[cleanup] Unexpected error: {exc}")


# ─── Session files endpoint (deepagents only) ─────────────────────────────────
# Permits frontend to fetch a plot PNG / parquet file from this session's dir
# using the virtual path (/plots/..., /parquet/...). Only when USE_DEEPAGENTS=1.

@app.get("/api/session/{session_id}/file", summary="Download a file from session directory")
async def get_session_file(
    session_id: str,
    path: str,
    x_user_id: Optional[str] = Header(default=None, alias="X-User-Id"),
):
    """
    Serve a file from the session's virtual filesystem.
    Path must start with /plots/ or /parquet/ (prevents arbitrary FS access).

    Example: GET /api/session/abc/file?path=/plots/2026-04-20_roas.png
    """
    from fastapi.responses import FileResponse
    if not _USE_DEEPAGENTS:
        raise HTTPException(status_code=400, detail="Files endpoint only available with USE_DEEPAGENTS=1")
    _validate_session_id(session_id)
    _validate_x_user_id(x_user_id)
    _require_session_access(session_id, x_user_id)
    if not path.startswith(("/plots/", "/parquet/", "/memories/")):
        raise HTTPException(status_code=400, detail="Path must start with /plots/, /parquet/, or /memories/")
    import re
    if ".." in path or re.search(r"[/\\]\.[/\\]", path):
        raise HTTPException(status_code=400, detail="Invalid path")

    from config import TEMP_DIR
    import pathlib
    # Strip leading slash and prepend session root.  Resolve symlinks and assert
    # the result still sits under TEMP_DIR/sessions/<id>/ — defence in depth on
    # top of the prefix/dot-segment checks above.
    rel = path.lstrip("/")
    session_root = (pathlib.Path(TEMP_DIR) / "sessions" / session_id).resolve()
    full = (session_root / rel).resolve()
    try:
        full.relative_to(session_root)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid path")
    if not full.exists() or not full.is_file():
        raise HTTPException(status_code=404, detail=f"File not found: {path}")
    return FileResponse(str(full))


@app.get("/api/session/{session_id}/parquet", summary="Read a session parquet as paginated JSON")
async def get_session_parquet(
    session_id: str,
    path: str,
    offset: int = 0,
    limit: int = 100,
    x_user_id: Optional[str] = Header(default=None, alias="X-User-Id"),
):
    """
    Read a parquet file from the session's virtual filesystem and return
    a paginated JSON view. Designed for the chat UI: fetches a slice of
    rows + column dtypes + total row count, all in one shot.

    Path must start with /parquet/ (other dirs are not supported here).
    `limit` is capped at 1000 to avoid pulling the whole table over HTTP.

    Example:
      GET /api/session/abc/parquet?path=/parquet/x.parquet&offset=0&limit=100
    """
    if not _USE_DEEPAGENTS:
        raise HTTPException(status_code=400, detail="Parquet endpoint only available with USE_DEEPAGENTS=1")
    _validate_session_id(session_id)
    _validate_x_user_id(x_user_id)
    _require_session_access(session_id, x_user_id)
    if not path.startswith("/parquet/"):
        raise HTTPException(status_code=400, detail="Path must start with /parquet/")
    import re as _re
    if ".." in path or _re.search(r"[/\\]\.[/\\]", path):
        raise HTTPException(status_code=400, detail="Invalid path")
    if limit < 1:
        limit = 1
    if limit > 1000:
        limit = 1000
    if offset < 0:
        offset = 0

    from config import TEMP_DIR
    import pathlib
    rel = path.lstrip("/")
    session_root = (pathlib.Path(TEMP_DIR) / "sessions" / session_id).resolve()
    full = (session_root / rel).resolve()
    try:
        full.relative_to(session_root)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid path")
    if not full.exists() or not full.is_file():
        raise HTTPException(status_code=404, detail=f"Parquet not found: {path}")

    try:
        import pandas as _pd
        df = _pd.read_parquet(str(full))
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Failed to read parquet: {exc}")

    total_rows = int(len(df))
    total_cols = int(len(df.columns))
    columns = [
        {"name": str(c), "dtype": str(df[c].dtype)}
        for c in df.columns
    ]

    # Slice + JSON-safe coercion (NaN→None, datetimes→isoformat, numpy→native)
    sliced = df.iloc[offset : offset + limit]
    rows = json.loads(sliced.to_json(orient="records", date_format="iso", default_handler=str))

    return {
        "path": path,
        "session_id": session_id,
        "total_rows": total_rows,
        "total_cols": total_cols,
        "offset": offset,
        "limit": limit,
        "returned_rows": len(rows),
        "columns": columns,
        "rows": rows,
    }


@app.get("/api/session/{session_id}/files", summary="List files in session directory")
async def list_session_files(
    session_id: str,
    x_user_id: Optional[str] = Header(default=None, alias="X-User-Id"),
):
    """List all files (plots + parquet + memories) created in this session."""
    if not _USE_DEEPAGENTS:
        raise HTTPException(status_code=400, detail="Files endpoint only available with USE_DEEPAGENTS=1")
    _validate_session_id(session_id)
    _validate_x_user_id(x_user_id)
    _require_session_access(session_id, x_user_id)
    from config import TEMP_DIR
    import pathlib
    session_root = pathlib.Path(TEMP_DIR) / "sessions" / session_id
    if not session_root.exists():
        return {"session_id": session_id, "files": []}
    out: list[dict] = []
    for sub in ("plots", "parquet", "memories"):
        d = session_root / sub
        if not d.exists():
            continue
        for f in sorted(d.glob("*")):
            if f.is_file():
                st = f.stat()
                out.append({
                    "path": f"/{sub}/{f.name}",
                    "size": st.st_size,
                    "mtime": datetime.fromtimestamp(st.st_mtime, timezone.utc).isoformat(),
                })
    return {"session_id": session_id, "files": out}
# ─── Health / Info ─────────────────────────────────────────────────────────────
@app.get("/", summary="Health check")
async def root():
    return {"status": "online", "service": "ClickHouse Analytics Agent", "version": "2.0.0"}
@app.get("/health", summary="Health check for uptime monitors")
async def health():
    return {"status": "ok", "timestamp": datetime.now(timezone.utc).isoformat()}
@app.get("/api/info", summary="Service features")
async def info():
    return {
        "service": "ClickHouse Analytics Agent",
        "version": "2.0.0",
        "architecture": "async job queue",
        "endpoints": {
            "submit": "POST /api/analyze",
            "poll":   "GET  /api/job/{job_id}",
        },
    }
@app.get("/api/models", summary="List available LLM models")
async def list_models():
    """
    Returns all models the user can choose from.
    Pass the `id` value in the `model` field of POST /api/analyze
    or POST /api/segment/chat.
    """
    return {
        "default": MODEL,
        "models": [
            {"id": model_id, "provider": provider}
            for model_id, provider in ALLOWED_MODELS.items()
        ],
    }
# ─── Session endpoints ─────────────────────────────────────────────────────────
@app.post("/api/session/new", summary="Create a new conversation session")
async def new_session():
    session_id = str(uuid.uuid4())
    return {
        "session_id": session_id,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "message": "New session created",
    }
@app.get("/api/session/{session_id}", summary="Get session metadata")
async def get_session(
    session_id: str,
    x_user_id: Optional[str] = Header(default=None, alias="X-User-Id"),
):
    _validate_session_id(session_id)
    _validate_x_user_id(x_user_id)
    _require_session_access(session_id, x_user_id)
    # Snapshot under the lock so cleanup_loop's pop() can't trigger
    # `dictionary changed size during iteration` while we filter.
    with _jobs_lock:
        active = [
            j for j in _jobs.values()
            if j["session_id"] == session_id and j["status"] in ("pending", "running")
        ]
    return {
        "session_id": session_id,
        "active_jobs": len(active),
    }
# ─── Main: submit query ────────────────────────────────────────────────────────
@app.post("/api/analyze", response_model=SubmitResponse, summary="Submit an analytics query")
async def analyze(
    req: AnalyzeRequest,
    x_user_id: Optional[str] = Header(default=None, alias="X-User-Id"),
):
    """
    Submit a query to the agent.
    Returns job_id immediately — agent runs in background.
    Poll GET /api/job/{job_id} to get the result.

    Optional `model` field selects the LLM. See GET /api/models for allowed values.
    `X-User-Id` (optional): isolates session ownership across tenants. Without it
    the session is bound to an anonymous owner and any subsequent X-User-Id
    request to the same session_id is rejected.
    """
    if req.model and req.model not in ALLOWED_MODELS:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown model '{req.model}'. Allowed: {list(ALLOWED_MODELS.keys())}",
        )
    _validate_x_user_id(x_user_id)
    # Validate the client-supplied session_id BEFORE it reaches
    # make_session_context() (which calls mkdir(parents=True) on the path
    # `TEMP_DIR/sessions/<session_id>`). Without this, a `..`/path-traversal
    # id would create directories outside TEMP_DIR.
    if req.session_id:
        _validate_session_id(req.session_id)
    session_id = req.session_id or str(uuid.uuid4())
    # Bind ownership BEFORE creating the job, then verify — same first-write-wins
    # pattern as segment chat. Without the verify step a client could write into
    # someone else's session.
    owner_repr = x_user_id or _ANON_OWNER
    _record_analytics_session_owner(session_id, owner_repr)
    if not _analytics_session_owned_by(session_id, owner_repr):
        raise HTTPException(status_code=403, detail="Session does not belong to this user")
    job_id = _new_job(session_id=session_id, query=req.query, model=req.model)
    # Fire and forget — keep a strong reference so the event loop doesn't
    # garbage-collect the task mid-flight (bare create_task only stores a
    # weak ref, see https://docs.python.org/3/library/asyncio-task.html).
    task = asyncio.create_task(_run_agent_job(job_id))
    _running_tasks.add(task)
    task.add_done_callback(_running_tasks.discard)
    return SubmitResponse(
        job_id=job_id,
        session_id=session_id,
        status="pending",
        message="Query accepted. Poll GET /api/job/{job_id} for result.",
    )
# ─── Poll job status ───────────────────────────────────────────────────────────
@app.get("/api/job/{job_id}", response_model=JobStatusResponse, summary="Poll job status / get result")
async def get_job(
    job_id: str,
    x_user_id: Optional[str] = Header(default=None, alias="X-User-Id"),
):
    """
    Poll the status of a submitted job.
    status: "pending" | "running" | "done" | "error"
    When status == "done", text_output, plots, tool_calls are populated.
    `X-User-Id` must match the owner of the underlying session.
    """
    _validate_job_id(job_id)
    _validate_x_user_id(x_user_id)
    with _jobs_lock:
        job = _jobs.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail=f"Job {job_id} not found (may have expired)")
    _require_session_access(job["session_id"], x_user_id)
    resp = JobStatusResponse(
        job_id=job["job_id"],
        session_id=job["session_id"],
        status=job["status"],
        created_at=job["created_at"],
        started_at=job["started_at"],
        finished_at=job["finished_at"],
        error=job["error"],
    )
    if job["status"] == "done" and job["result"]:
        r = job["result"]
        resp.success = r.get("success", True)
        resp.text_output = r.get("text_output", "")
        resp.plots = r.get("plots", [])
        # Forward virtual-fs metadata produced by the deepagents pipeline so
        # the frontend can deep-link without a separate /files request.
        resp.plot_urls = r.get("plot_urls", [])
        resp.parquet_paths = r.get("parquet_paths", [])
        resp.tool_calls = r.get("tool_calls", [])
        resp.error = r.get("error")
    return resp
# ─── Stats ────────────────────────────────────────────────────────────────────
@app.get("/api/chat-stats", summary="Database statistics")
async def chat_stats():
    # Snapshot under the lock to keep len() and the per-status tally consistent.
    with _jobs_lock:
        snapshot = list(_jobs.values())
    by_status: dict[str, int] = {}
    for j in snapshot:
        by_status[j["status"]] = by_status.get(j["status"], 0) + 1
    return {"total_jobs_in_memory": len(snapshot), "by_status": by_status}
# ─── Observability / Debug endpoints ─────────────────────────────────────────
# These endpoints are for developer use only (agent optimization analysis).
# They are NOT intended for the end-user frontend.

@app.get("/debug/sessions", tags=["debug"], summary="List all logged sessions",
         dependencies=[Depends(_require_debug_auth)])
async def debug_sessions():
    """
    List all sessions with aggregated stats:
    turns, total tool calls, estimated token usage, first/last activity.
    """
    try:
        from chat_logger import get_logger
        from config import DB_PATH
        return {"sessions": get_logger(DB_PATH).get_sessions()}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/debug/session/{session_id}", tags=["debug"], summary="Full session log with tool calls",
         dependencies=[Depends(_require_debug_auth)])
async def debug_session_logs(session_id: str):
    """
    Full chronological log of a session grouped by turn.

    Each turn contains events in order:
      human       → user question
      ai_thinking → agent reasoning before tool use (if any)
      tool_call   → tool invocation with full args (SQL, Python code, etc.)
      tool_result → full tool response (row_count, data stats, analysis output)
      ai_answer   → final agent response shown to user

    Useful for: reviewing what SQL the agent wrote, how many iterations it took,
    whether it used the right tables, whether tool results were large/expensive.
    """
    _validate_session_id(session_id)
    try:
        from chat_logger import get_logger
        from config import DB_PATH
        logs = get_logger(DB_PATH).get_session_logs(session_id)
        if not logs:
            raise HTTPException(status_code=404, detail="Session not found or not yet logged")

        # Group by turn_index, parse JSON content for readability
        turns: dict[int, list] = {}
        for row in logs:
            if row.get("content"):
                try:
                    row["content"] = json.loads(row["content"])
                except Exception:
                    pass  # leave as plain string if not JSON
            turns.setdefault(row["turn_index"], []).append(row)

        return {
            "session_id": session_id,
            "total_turns": len(turns),
            "turns": [
                {"turn_index": idx, "events": events}
                for idx, events in sorted(turns.items())
            ],
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/debug/session/{session_id}/turn/{turn_index}", tags=["debug"], summary="Log for one specific turn",
         dependencies=[Depends(_require_debug_auth)])
async def debug_turn_logs(session_id: str, turn_index: int):
    """
    Detailed event log for a single turn within a session.
    Useful for deep-diving into one specific question the user asked.
    """
    _validate_session_id(session_id)
    try:
        from chat_logger import get_logger
        from config import DB_PATH
        events = get_logger(DB_PATH).get_turn(session_id, turn_index)
        if not events:
            raise HTTPException(
                status_code=404,
                detail=f"Turn {turn_index} not found for session {session_id}"
            )
        # Parse JSON content fields
        for ev in events:
            if ev.get("content"):
                try:
                    ev["content"] = json.loads(ev["content"])
                except Exception:
                    pass
        return {"session_id": session_id, "turn_index": turn_index, "events": events}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/debug/stats", tags=["debug"], summary="Aggregate optimization stats",
         dependencies=[Depends(_require_debug_auth)])
async def debug_stats():
    """
    Aggregate statistics across all logged sessions.

    Key metrics for optimization analysis:
      - list_tables_calls: should be ~0 (schema is in system prompt)
      - avg_ch_result_tokens: if high → agent fetching too much data
      - tool_calls_total / human_turns: avg tool calls per user question
    """
    try:
        from chat_logger import get_logger
        from config import DB_PATH
        return get_logger(DB_PATH).get_stats()
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ─── Segment Builder endpoints ────────────────────────────────────────────────

class SegmentChatRequest(BaseModel):
    message: str
    session_id: Optional[str] = None
    model: Optional[str] = None  # None → default model from config


class SegmentChatResponse(BaseModel):
    success: bool
    session_id: str
    text_output: str
    segment_saved: bool
    error: Optional[str] = None


@app.post(
    "/api/segment/chat",
    response_model=SegmentChatResponse,
    tags=["segmentation"],
    summary="One turn in a segmentation dialogue",
)
async def segment_chat(
    req: SegmentChatRequest,
    x_user_id: Optional[str] = Header(default=None, alias="X-User-Id"),
):
    """
    Диалог с агентом-сегментатором (synchronous — ответ возвращается сразу).

    Сохраняй `session_id` между вызовами чтобы держать контекст диалога.
    Если `session_id` не передан — создаётся новая сессия.
    Флаг `segment_saved: true` означает что сегмент был сохранён в этом ходу.

    Заголовок `X-User-Id` (опционально): изолирует сегменты по пользователю.
    Без заголовка — сегменты попадают в общее пространство "__shared__".
    """
    if req.model and req.model not in ALLOWED_MODELS:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown model '{req.model}'. Allowed: {list(ALLOWED_MODELS.keys())}",
        )
    if req.session_id:
        _validate_session_id(req.session_id)
    _validate_x_user_id(x_user_id)
    from segment_agent import get_segment_agent
    from segment_store import _SHARED_OWNER, get_segment_store
    owner = x_user_id or _SHARED_OWNER
    session_id = req.session_id or str(uuid.uuid4())
    # Bind the session to the current owner on first contact (INSERT OR IGNORE
    # so subsequent messages can't switch ownership) AND verify ownership
    # immediately. Without the second check a hostile B can write a message
    # into A's session — record_session_owner silently no-ops because A is
    # already the owner, but agent.chat would still execute against the same
    # thread_id and pollute A's conversation memory.
    store = get_segment_store()
    await asyncio.to_thread(store.record_session_owner, session_id, owner)
    if not await asyncio.to_thread(store.session_owned_by, session_id, owner):
        raise HTTPException(status_code=403, detail="Session does not belong to this user")
    agent = get_segment_agent(req.model)
    result = await asyncio.to_thread(agent.chat, req.message, session_id, owner)
    return SegmentChatResponse(
        success=result["success"],
        session_id=session_id,
        text_output=result.get("text_output", ""),
        segment_saved=result.get("segment_saved", False),
        error=result.get("error"),
    )


@app.get(
    "/api/segment/chat/{session_id}/history",
    tags=["segmentation"],
    summary="Get segmentation dialogue history",
)
async def get_segment_chat_history(
    session_id: str,
    x_user_id: Optional[str] = Header(default=None, alias="X-User-Id"),
):
    """История диалога сессии сегментации в формате [{role, content}].

    Validate session_id shape AND require the same X-User-Id used to create
    the session (or the shared placeholder for anonymous sessions). Without
    that check anyone who learns a uuid4 could read another user's
    segmentation dialogue (which contains SQL and segment plan text).
    """
    _validate_session_id(session_id)
    _validate_x_user_id(x_user_id)
    from segment_agent import get_segment_agent
    from segment_store import _SHARED_OWNER, get_segment_store
    owner = x_user_id or _SHARED_OWNER
    store = get_segment_store()
    if not await asyncio.to_thread(store.session_owned_by, session_id, owner):
        # Mask presence: same 404 whether the session exists for someone else
        # or doesn't exist at all.
        raise HTTPException(status_code=404, detail="Session not found")
    agent = get_segment_agent()
    history = await asyncio.to_thread(agent.get_session_history, session_id)
    return {"session_id": session_id, "history": history}


@app.get(
    "/api/segments",
    tags=["segmentation"],
    summary="List all saved segments",
)
async def list_segments(
    x_user_id: Optional[str] = Header(default=None, alias="X-User-Id"),
):
    """Список сегментов текущего пользователя (X-User-Id), отсортированных по дате обновления."""
    _validate_x_user_id(x_user_id)
    from segment_store import _SHARED_OWNER, get_segment_store
    owner = x_user_id or _SHARED_OWNER
    store = get_segment_store()
    segments = await asyncio.to_thread(store.list_all, owner)
    return {"segments": segments}


@app.get(
    "/api/segments/{segment_id}",
    tags=["segmentation"],
    summary="Get segment by ID",
)
async def get_segment(
    segment_id: str,
    x_user_id: Optional[str] = Header(default=None, alias="X-User-Id"),
):
    """Получить сегмент по ID. Возвращает 404 если сегмент не найден или принадлежит другому пользователю."""
    _validate_segment_id(segment_id)
    _validate_x_user_id(x_user_id)
    from segment_store import _SHARED_OWNER, get_segment_store
    owner = x_user_id or _SHARED_OWNER
    store = get_segment_store()
    seg = await asyncio.to_thread(store.get_by_id, segment_id, owner)
    if not seg:
        raise HTTPException(status_code=404, detail="Segment not found")
    return seg


@app.delete(
    "/api/segments/{segment_id}",
    tags=["segmentation"],
    summary="Delete segment by ID",
)
async def delete_segment(
    segment_id: str,
    x_user_id: Optional[str] = Header(default=None, alias="X-User-Id"),
):
    """Удалить сегмент. Возвращает 404 если сегмент не найден или принадлежит другому пользователю."""
    _validate_segment_id(segment_id)
    _validate_x_user_id(x_user_id)
    from segment_store import _SHARED_OWNER, get_segment_store
    owner = x_user_id or _SHARED_OWNER
    store = get_segment_store()
    deleted = await asyncio.to_thread(store.delete, segment_id, owner)
    if not deleted:
        raise HTTPException(status_code=404, detail="Segment not found")
    return {"success": True}


# ─── Tables: named ClickHouse queries for frontend ────────────────────────────

def _serialize_value(v):
    """Конвертирует любое значение из ClickHouse в JSON-совместимый тип."""
    if v is None:
        return None
    if isinstance(v, bool):
        return v
    if isinstance(v, (_datetime, _date)):
        return v.isoformat()
    if isinstance(v, _decimal.Decimal):
        f = float(v)
        return None if _math.isnan(f) or _math.isinf(f) else round(f, 2)
    try:
        import numpy as _np
        if isinstance(v, _np.integer):
            return int(v)
        if isinstance(v, _np.floating):
            return None if _np.isnan(v) else round(float(v), 2)
    except ImportError:
        pass
    if isinstance(v, float):
        return None if (_math.isnan(v) or _math.isinf(v)) else round(v, 2)
    if isinstance(v, int):
        return v
    if isinstance(v, (list, tuple)):
        return [_serialize_value(i) for i in v]
    if isinstance(v, dict):
        return {str(k): _serialize_value(val) for k, val in v.items()}
    return str(v)


_ALLOWED_ZONE_STATUSES = {"red", "green", "yellow"}

# Адаптивный список кабинетов: вычитывается из ClickHouse (SELECT DISTINCT cabinet_name)
# и кэшируется на _CABINET_CACHE_TTL секунд. При истечении TTL (или в случае ошибки
# дискавери при пустом кэше) бэк пытается перечитать; пока cache непустой — отдаёт его.
# `last_error_at` is set when a refresh fails so /api/tables can advertise
# whether the list is stale — frontend then knows to warn the user.
_CABINET_CACHE_TTL = 3600  # 1 час
_cabinet_cache: dict = {"values": [], "fetched_at": 0.0, "last_error_at": 0.0}
_cabinet_cache_lock = _threading.Lock()


async def _get_available_cabinets(force_refresh: bool = False) -> list[str]:
    """
    Вернуть актуальный список кабинетов (LowCardinality(String)) из витрины
    magnetto.bad_placements. Используется для:
      • метаданных GET /api/tables (фронт сам строит селектор)
      • валидации параметра cabinet_name в GET /api/tables/{query_name}
    """
    import time

    now = time.time()
    fresh = (now - _cabinet_cache["fetched_at"]) < _CABINET_CACHE_TTL
    if not force_refresh and fresh and _cabinet_cache["values"]:
        return _cabinet_cache["values"]

    try:
        from tools import _get_ch_client
        import pandas as _pd

        ch = _get_ch_client()
        from config import CLICKHOUSE_DATABASE as _CH_DB
        sql = (
            f"SELECT DISTINCT cabinet_name FROM {_CH_DB}.bad_placements "
            "WHERE cabinet_name != '' ORDER BY cabinet_name"
        )
        result = await asyncio.to_thread(_ch_query_locked, ch, sql)
        if result.get("success"):
            df = _pd.read_parquet(result["parquet_path"])
            cabinets = [str(v) for v in df["cabinet_name"].dropna().tolist()]
            # Update under the lock so a concurrent reader doesn't see
            # `values` already updated but `fetched_at` still stale.
            with _cabinet_cache_lock:
                _cabinet_cache["values"] = cabinets
                _cabinet_cache["fetched_at"] = now
                _cabinet_cache["last_error_at"] = 0.0
            return cabinets
    except Exception as exc:
        # Сеть/ClickHouse упал — отдаём последние известные значения,
        # даже если TTL истёк. Пустой список означает "ещё не грузили".
        with _cabinet_cache_lock:
            _cabinet_cache["last_error_at"] = now
        print(f"[cabinets] refresh failed (cache stale): {exc}")

    return _cabinet_cache["values"]


def _cabinets_are_stale() -> bool:
    """True if the last cabinet refresh failed more recently than the last success."""
    return _cabinet_cache["last_error_at"] > _cabinet_cache["fetched_at"]


@app.get("/api/tables", tags=["tables"], summary="Список доступных именованных запросов")
async def list_table_queries():
    """Возвращает все доступные query_name с описаниями, колонками для сортировки
    и доступными кабинетами (для фильтруемых запросов)."""
    from queries import QUERIES

    cabinets = await _get_available_cabinets()
    return {
        "queries": [
            {
                "name": name,
                "description": q["description"],
                "sortable_columns": q["sortable_columns"],
                "filterable_zone_status": q.get("filterable_zone_status", False),
                "filterable_cabinet": q.get("filterable_cabinet", False),
                "cabinets": cabinets if q.get("filterable_cabinet") else [],
            }
            for name, q in QUERIES.items()
        ],
        "cabinets": cabinets,  # общий список (одинаков для всех filterable_cabinet таблиц)
        "cabinets_stale": _cabinets_are_stale(),
    }


@app.get("/api/tables/{query_name}", tags=["tables"], summary="Выполнить именованный запрос")
async def get_table_data(
    query_name: str,
    sort_by: Optional[str] = None,
    sort_dir: str = "desc",
    limit: int = 50,
    zone_status: Optional[str] = None,
    cabinet_name: Optional[str] = None,
):
    """
    Выполняет именованный SQL-запрос и возвращает табличные данные.
    Параметры: sort_by, sort_dir (asc/desc), limit (1-1000),
    zone_status (red/green/yellow), cabinet_name (из GET /api/tables → cabinets).
    """
    from queries import QUERIES
    import pandas as _pd

    if query_name not in QUERIES:
        raise HTTPException(status_code=404, detail=f"Query '{query_name}' not found")

    query = QUERIES[query_name]
    sql = query["sql"].strip()

    if zone_status is not None:
        if not query.get("filterable_zone_status"):
            raise HTTPException(
                status_code=400,
                detail=f"Query '{query_name}' does not support zone_status filter",
            )
        if zone_status not in _ALLOWED_ZONE_STATUSES:
            raise HTTPException(
                status_code=400,
                detail=f"Invalid zone_status '{zone_status}'. Allowed: {sorted(_ALLOWED_ZONE_STATUSES)}",
            )
        sql += f"\nAND zone_status = '{zone_status}'"

    if cabinet_name is not None:
        if not query.get("filterable_cabinet"):
            raise HTTPException(
                status_code=400,
                detail=f"Query '{query_name}' does not support cabinet filter",
            )
        # Defense in depth: ClickHouse cabinet_name strings are LowCardinality
        # alphanumerics; reject anything outside that shape BEFORE checking
        # the runtime allowlist (which is just a cache and could be empty).
        import re as _re_cab
        if not _re_cab.fullmatch(r"[A-Za-z0-9_-]+", cabinet_name):
            raise HTTPException(status_code=400, detail="Invalid cabinet_name")
        allowed_cabinets = await _get_available_cabinets()
        if cabinet_name not in allowed_cabinets:
            raise HTTPException(
                status_code=400,
                detail=f"Unknown cabinet '{cabinet_name}'. Available: {allowed_cabinets}",
            )
        sql += f"\nAND cabinet_name = '{cabinet_name}'"

    # Count query uses filtered SQL without ORDER BY / LIMIT
    count_sql = f"SELECT count() FROM ({sql}) AS _subq LIMIT 1"

    if sort_by is not None:
        if sort_by not in query["sortable_columns"]:
            raise HTTPException(
                status_code=400,
                detail=f"Cannot sort by '{sort_by}'. Allowed: {query['sortable_columns']}",
            )
        direction = "ASC" if sort_dir.lower() == "asc" else "DESC"
        sql += f"\nORDER BY {sort_by} {direction}"

    limit = max(1, min(limit, 1000))
    sql += f"\nLIMIT {limit}"

    try:
        from tools import _ch_lock, _get_ch_client
        ch = _get_ch_client()
        # Hold the lock across both queries so another caller can't slip in
        # between and stale the count vs the page we just fetched. The lock
        # is acquired inside the worker thread to avoid blocking the event
        # loop while we wait for ClickHouse — see _ch_query_locked.
        def _both():
            with _ch_lock:
                return (
                    ch.execute_query(sql),
                    ch.execute_query(count_sql),
                )
        result, count_result = await asyncio.to_thread(_both)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))

    if not result.get("success"):
        raise HTTPException(status_code=500, detail=result.get("error", "Query failed"))

    try:
        df = _pd.read_parquet(result["parquet_path"])
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Failed to read result: {exc}")

    total_count: Optional[int] = None
    if count_result.get("success"):
        try:
            count_df = _pd.read_parquet(count_result["parquet_path"])
            total_count = int(count_df.iloc[0, 0])
        except Exception:
            pass

    columns = df.columns.tolist()
    rows = [
        [_serialize_value(cell) for cell in row]
        for row in df.itertuples(index=False, name=None)
    ]

    return {
        "columns": columns,
        "rows": rows,
        "row_count": len(rows),
        "total_count": total_count,
    }


# ─── Budget reallocation ──────────────────────────────────────────────────────
# Мультитенантно: база читается из CLICKHOUSE_DATABASE (.env).
# Для magnetto-агента это 'magnetto'; weekly-series собирается UNION'ом из
# direct_custom_report_cab1..cab4, чтобы cabinet_name не терялся.
#
# Весь /api/budget работает через отдельного CH-пользователя
# (CLICKHOUSE_REPORTS_USER / _PASSWORD в .env). У основного юзера агента этих
# таблиц не видно, чтобы они не смешивались с витринами, которые использует
# чат-агент.
#
# Нужные права для reports-юзера:
#   GRANT SELECT ON magnetto.budget_reallocation          TO <reports_user>;
#   GRANT SELECT ON magnetto.direct_custom_report_cab1..4 TO <reports_user>;
#
# Если env-переменные не заданы — все основные запросы возвращают 500.
# weekly_series — опциональный запрос (required=False): при недоступности
# вернётся пустой массив, sparklines на фронте просто не нарисуются.


_reports_ch_client = None
_reports_ch_lock = _threading.Lock()


def _get_reports_client():
    """
    Singleton CH-клиент с кредами CLICKHOUSE_REPORTS_USER/_PASSWORD.
    Возвращает None, если переменные не заданы или не удалось подключиться.

    Init is guarded by _reports_ch_lock so two concurrent first-callers
    don't open duplicate clickhouse_connect HTTP sessions and leak the
    loser.
    """
    global _reports_ch_client
    # Cheap path: client already initialised, no lock needed.
    if _reports_ch_client is not None:
        return _reports_ch_client
    # Slow path: hold the lock from re-check through to assignment so
    # two parallel first-callers don't both end up calling get_client().
    # Earlier versions exited the `with` block before the construction —
    # that defeated the whole point of the lock.
    with _reports_ch_lock:
        if _reports_ch_client is not None:
            return _reports_ch_client

        import os
        user = (os.environ.get("CLICKHOUSE_REPORTS_USER") or "").strip()
        password = (os.environ.get("CLICKHOUSE_REPORTS_PASSWORD") or "").strip()
        if not user or not password:
            return None

        try:
            import clickhouse_connect
            from clickhouse_client import _ch_tls_verify_kwargs
            from config import (
                CLICKHOUSE_HOST,
                CLICKHOUSE_PORT,
                CLICKHOUSE_DATABASE,
            )
            connect_kwargs = {
                "host": CLICKHOUSE_HOST,
                "port": CLICKHOUSE_PORT,
                "username": user,
                "password": password,
                "database": CLICKHOUSE_DATABASE,
                "secure": True,
                "connect_timeout": 30,
                "send_receive_timeout": 600,
            }
            connect_kwargs.update(_ch_tls_verify_kwargs())
            _reports_ch_client = clickhouse_connect.get_client(**connect_kwargs)
            print(f"✅ Reports CH client connected as {user}")
            return _reports_ch_client
        except Exception as exc:
            print(f"⚠️  Reports CH client init failed: {exc}")
            return None


def _reports_query_dicts(sql: str, required: bool = True) -> list[dict]:
    """
    Выполнить SELECT через reports-клиент.
    required=True (по умолчанию): если клиент не настроен — RuntimeError
                                  (конвертируется в HTTP 500 выше по стеку).
    required=False: если клиент не настроен — вернуть [] (мягкая деградация
                    для опциональных запросов вроде weekly_series).
    """
    client = _get_reports_client()
    if client is None:
        if required:
            raise RuntimeError(
                "CLICKHOUSE_REPORTS_USER / CLICKHOUSE_REPORTS_PASSWORD не заданы в .env"
            )
        return []
    # Single shared HTTP session — concurrent .query() calls would interleave
    # and trigger "concurrent queries within the same session" errors.
    with _reports_ch_lock:
        qr = client.query(sql)
        cols = list(qr.column_names)
        return [
            {cols[i]: _serialize_value(row[i]) for i in range(len(cols))}
            for row in qr.result_rows
        ]


def _safe_float(v) -> float:
    try:
        return float(v) if v is not None else 0.0
    except (TypeError, ValueError):
        return 0.0


@app.get(
    "/api/budget",
    tags=["budget"],
    summary="Рекомендации по перераспределению недельного бюджета",
)
async def get_budget(cabinet_name: Optional[str] = None):
    """
    Формат ответа: {summary, cabinets[], campaigns[]}.
    cabinet_name — опциональный фильтр (tab1/tab2/tab3/tab4).
    """
    import re
    from config import CLICKHOUSE_DATABASE as CH_DB

    if cabinet_name is not None and not re.fullmatch(r"[A-Za-z0-9_-]+", cabinet_name):
        raise HTTPException(status_code=400, detail="Invalid cabinet_name")
    # Plus runtime allowlist: same shape check as /api/tables/{name}, prevents
    # an unknown cabinet from being interpolated into SQL even with a
    # well-formed regex match.
    if cabinet_name is not None:
        _allowed_cabs = await _get_available_cabinets()
        if _allowed_cabs and cabinet_name not in _allowed_cabs:
            raise HTTPException(
                status_code=400,
                detail=f"Unknown cabinet '{cabinet_name}'",
            )

    try:
        check_sql = (
            f"SELECT count() AS c FROM system.tables "
            f"WHERE database='{CH_DB}' AND name='budget_reallocation'"
        )
        check_rows = await asyncio.to_thread(_reports_query_dicts, check_sql)
        if not check_rows or int(check_rows[0].get("c") or 0) < 1:
            raise HTTPException(
                status_code=404,
                detail=f"budget_reallocation не развёрнут в БД {CH_DB}",
            )

        cabs_sql = f"""
            SELECT DISTINCT cabinet_name
            FROM {CH_DB}.budget_reallocation
            WHERE report_date = (SELECT max(report_date) FROM {CH_DB}.budget_reallocation)
            ORDER BY cabinet_name
        """
        cab_rows = await asyncio.to_thread(_reports_query_dicts, cabs_sql)
        cabinets = [str(r["cabinet_name"]) for r in cab_rows if r.get("cabinet_name")]

        where_cab = f" AND cabinet_name = '{cabinet_name}'" if cabinet_name else ""
        rec_sql = f"""
            SELECT
                campaign_id, campaign_name, cabinet_name,
                is_active, meta_state, search_strategy, network_strategy,
                explicit_weekly_budget, actual_weekly_spend_28d, current_weekly_budget,
                clicks_28d, cost_28d, revenue_28d,
                purchases_28d, calls_28d, orders_28d, cart_visits_28d, goal_score_28d,
                clicks_7d, cost_7d, revenue_7d, purchases_7d,
                roas_28d, roas_7d, cpo_28d, goal_score_rate, trend_factor,
                roas_pct_rank, rank_multiplier, final_multiplier,
                recommended_weekly_budget, delta_rub, delta_pct,
                zone_status, rationale,
                expected_weekly_cost, expected_weekly_revenue,
                expected_weekly_purchases, expected_weekly_calls, expected_weekly_orders,
                baseline_weekly_revenue, baseline_weekly_purchases, baseline_weekly_calls,
                forecast_elasticity, forecast_conf_low, forecast_conf_high,
                delta_revenue_weekly, delta_purchases_weekly, delta_roas,
                report_date
            FROM {CH_DB}.budget_reallocation
            WHERE report_date = (SELECT max(report_date) FROM {CH_DB}.budget_reallocation)
              AND is_active = 1{where_cab}
            ORDER BY cost_28d DESC
        """
        campaigns = await asyncio.to_thread(_reports_query_dicts, rec_sql)

        # Weekly-series для magnetto: UNION cab1..cab4.
        # SELECT-ом берём только нужные колонки и приводим типы явно —
        # без этого CH ловит Code:386 (no supertype) когда одна из cabN-таблиц
        # хранит Date/Conversions_* как String, а соседняя — как Date/Float.
        cab_subquery = """
            SELECT
                toUInt64(CampaignId)                    AS CampaignId,
                toDate(Date)                            AS Date,
                toFloat64(Cost)                         AS Cost,
                toFloat64(PurchaseRevenue)              AS PurchaseRevenue,
                toFloat64(Conversions_314553735_LSCCD)  AS c_314553735,
                toFloat64(Conversions_201619840_LSCCD)  AS c_201619840,
                toFloat64(Conversions_201619843_LSCCD)  AS c_201619843,
                toFloat64(Conversions_201619846_LSCCD)  AS c_201619846,
                toFloat64(Conversions_332069613_LSCCD)  AS c_332069613,
                toFloat64(Conversions_332069614_LSCCD)  AS c_332069614,
                toFloat64(Conversions_322914144_LSCCD)  AS c_322914144,
                toFloat64(Conversions_314248561_LSCCD)  AS c_314248561,
                toFloat64(Conversions_176145847_LSCCD)  AS c_176145847,
                toFloat64(Conversions_314248652_LSCCD)  AS c_314248652
            FROM {CH_DB}.direct_custom_report_{tbl}
            WHERE Date >= today() - 90
        """
        series_sql = f"""
            WITH src AS (
                {cab_subquery.format(CH_DB=CH_DB, tbl='cab1')}
                UNION ALL {cab_subquery.format(CH_DB=CH_DB, tbl='cab2')}
                UNION ALL {cab_subquery.format(CH_DB=CH_DB, tbl='cab3')}
                UNION ALL {cab_subquery.format(CH_DB=CH_DB, tbl='cab4')}
            )
            SELECT
                CampaignId                                AS campaign_id,
                toString(toStartOfWeek(Date, 1))          AS week,
                round(sum(Cost))                          AS cost,
                round(sum(PurchaseRevenue) + 5000 * (
                    sum(c_314553735) * 10 + sum(c_201619840) * 10 +
                    sum(c_201619843) * 10 + sum(c_201619846) * 10 +
                    sum(c_332069613) * 10 + sum(c_332069614) * 10 +
                    sum(c_322914144) *  3 + sum(c_314248561) *  3 +
                    sum(c_176145847) *  3 + sum(c_314248652) *  1
                ))                                        AS revenue,
                sum(c_332069614)                          AS purchases
            FROM src
            GROUP BY CampaignId, week
            ORDER BY CampaignId, week
        """
        # weekly_series — опциональный; если reports-клиент не настроен, вернём []
        series_rows = await asyncio.to_thread(_reports_query_dicts, series_sql, False)

        series_by_campaign: dict[str, list[dict]] = {}
        for r in series_rows:
            cid = str(r.get("campaign_id"))
            series_by_campaign.setdefault(cid, []).append({
                "week": r.get("week"),
                "cost": _safe_float(r.get("cost")),
                "revenue": _safe_float(r.get("revenue")),
                "purchases": int(_safe_float(r.get("purchases"))),
            })

        for c in campaigns:
            cid = str(c.get("campaign_id"))
            c["weekly_series"] = series_by_campaign.get(cid, [])

        summary = {
            "report_date": campaigns[0]["report_date"] if campaigns else None,
            "database": CH_DB,
            "cabinet": cabinet_name,
            "active_campaigns": len(campaigns),
            "current_total_wb": 0.0,
            "recommended_total_wb": 0.0,
            "delta_total": 0.0,
            "baseline_total_revenue_weekly": 0.0,
            "expected_total_revenue_weekly": 0.0,
            "delta_total_revenue_weekly": 0.0,
            "baseline_total_purchases_weekly": 0.0,
            "expected_total_purchases_weekly": 0.0,
            "delta_total_purchases_weekly": 0.0,
            "baseline_total_calls_weekly": 0.0,
            "expected_total_calls_weekly": 0.0,
            "delta_total_calls_weekly": 0.0,
            "baseline_total_leads_weekly": 0.0,
            "expected_total_leads_weekly": 0.0,
            "delta_total_leads_weekly": 0.0,
            "current_portfolio_roas": 0.0,
            "expected_portfolio_roas": 0.0,
            "zones": {"green": 0, "yellow": 0, "red": 0, "pending": 0},
        }

        for c in campaigns:
            summary["current_total_wb"] += _safe_float(c.get("current_weekly_budget"))
            summary["recommended_total_wb"] += _safe_float(c.get("recommended_weekly_budget"))
            summary["delta_total"] += _safe_float(c.get("delta_rub"))
            summary["baseline_total_revenue_weekly"] += _safe_float(c.get("baseline_weekly_revenue"))
            summary["expected_total_revenue_weekly"] += _safe_float(c.get("expected_weekly_revenue"))
            summary["delta_total_revenue_weekly"] += _safe_float(c.get("delta_revenue_weekly"))
            summary["baseline_total_purchases_weekly"] += _safe_float(c.get("baseline_weekly_purchases"))
            summary["expected_total_purchases_weekly"] += _safe_float(c.get("expected_weekly_purchases"))
            summary["delta_total_purchases_weekly"] += _safe_float(c.get("delta_purchases_weekly"))
            summary["baseline_total_calls_weekly"] += _safe_float(c.get("baseline_weekly_calls"))
            summary["expected_total_calls_weekly"] += _safe_float(c.get("expected_weekly_calls"))
            summary["delta_total_calls_weekly"] += (
                _safe_float(c.get("expected_weekly_calls"))
                - _safe_float(c.get("baseline_weekly_calls"))
            )
            leads_base_weekly = _safe_float(c.get("cart_visits_28d")) / 4.0
            summary["baseline_total_leads_weekly"] += leads_base_weekly
            current_wb = max(_safe_float(c.get("current_weekly_budget")), 1.0)
            summary["expected_total_leads_weekly"] += (
                leads_base_weekly * _safe_float(c.get("expected_weekly_cost")) / current_wb
            )
            z = c.get("zone_status")
            if z in summary["zones"]:
                summary["zones"][z] += 1

        summary["delta_total_leads_weekly"] = (
            summary["expected_total_leads_weekly"] - summary["baseline_total_leads_weekly"]
        )

        if summary["current_total_wb"] > 0:
            summary["current_portfolio_roas"] = (
                summary["baseline_total_revenue_weekly"] / summary["current_total_wb"]
            )
        if summary["recommended_total_wb"] > 0:
            summary["expected_portfolio_roas"] = (
                summary["expected_total_revenue_weekly"] / summary["recommended_total_wb"]
            )

        for k in (
            "current_total_wb", "recommended_total_wb", "delta_total",
            "baseline_total_revenue_weekly", "expected_total_revenue_weekly",
            "delta_total_revenue_weekly",
        ):
            summary[k] = round(summary[k])
        for k in (
            "baseline_total_purchases_weekly", "expected_total_purchases_weekly",
            "delta_total_purchases_weekly",
            "baseline_total_calls_weekly", "expected_total_calls_weekly",
            "delta_total_calls_weekly",
            "baseline_total_leads_weekly", "expected_total_leads_weekly",
            "delta_total_leads_weekly",
        ):
            summary[k] = round(summary[k], 1)
        summary["current_portfolio_roas"] = round(summary["current_portfolio_roas"], 1)
        summary["expected_portfolio_roas"] = round(summary["expected_portfolio_roas"], 1)

        return {
            "summary": summary,
            "cabinets": cabinets,
            "campaigns": campaigns,
        }
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"ClickHouse error: {exc}")


# ─── Command Center endpoints ────────────────────────────────────────────────
# Дневной снапшот по кампаниям/группам/объявлениям из command_center_* витрин.
# Каждый endpoint читает последний report_date и отдаёт готовый JSON для UI.
# Формат ответа стабилизирован — совместим с командным центром на фронте.

def _delta_pct(cur: float, prev: float) -> Optional[float]:
    if not prev:
        return None
    return round((cur - prev) / prev * 100, 1)


@app.get(
    "/api/command_center/campaigns",
    tags=["command_center"],
    summary="Дневной снапшот кампаний: summary + health_counts + campaigns[]",
)
async def get_command_center_campaigns():
    from config import CLICKHOUSE_DATABASE as CH_DB
    try:
        sql = f"""
            WITH last_d AS (SELECT max(report_date) AS d FROM {CH_DB}.command_center_campaigns)
            SELECT
                toString(report_date) AS report_date_str,
                toInt64(campaign_id)  AS campaign_id,
                campaign_name, campaign_type, meta_state, status, state,
                search_strategy, network_strategy, attribution_model,
                weekly_budget, traffic_mix, semantic_tags,
                cost_week, revenue_week,
                impressions_week, clicks_week, leads_week, calls_week, forms_week, orders_week,
                spam_traffic_week, targeted_calls_week, order_create_started_week, order_created_week,
                goal_507627231_week, unique_calls_week, quiz_completed_week, phone_clicks_week,
                cost_prev, revenue_prev,
                impressions_prev, clicks_prev, leads_prev, calls_prev, forms_prev, orders_prev,
                spam_traffic_prev, targeted_calls_prev, order_create_started_prev, order_created_prev,
                goal_507627231_prev, unique_calls_prev, quiz_completed_prev, phone_clicks_prev,
                roas_week, cpa_week, cpc_week, ctr_week,
                priority_goal_ids, priority_goal_values,
                health, health_reason, cabinet_name,
                arrayMap(x -> toString(x), history_weeks) AS history_weeks,
                history_cost, history_revenue,
                history_clicks, history_leads,
                history_calls, history_forms, history_orders
            FROM {CH_DB}.command_center_campaigns
            WHERE report_date = (SELECT d FROM last_d)
            ORDER BY cost_week DESC, weekly_budget DESC
        """
        rows = await asyncio.to_thread(_reports_query_dicts, sql)
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"ClickHouse error: {exc}")

    if not rows:
        return {"report_date": None, "summary": None, "campaigns": [], "health_counts": {"green": 0, "yellow": 0, "red": 0, "pending": 0}}

    report_date = str(rows[0].get("report_date_str") or "")
    sum_keys = (
        "cost", "revenue", "impressions", "clicks", "leads", "calls", "forms", "orders",
        "spam_traffic", "targeted_calls", "order_create_started", "order_created",
    )
    totals = {k: {"week": 0.0, "prev": 0.0} for k in sum_keys}
    campaigns: list[dict] = []

    for r in rows:
        for k in sum_keys:
            totals[k]["week"] += _safe_float(r.get(f"{k}_week"))
            totals[k]["prev"] += _safe_float(r.get(f"{k}_prev"))

        weeks = r.get("history_weeks") or []
        series = []
        for i in range(len(weeks)):
            series.append({
                "week":    str(weeks[i]),
                "cost":    _safe_float((r.get("history_cost")    or [0])[i] if i < len(r.get("history_cost")    or []) else 0),
                "revenue": _safe_float((r.get("history_revenue") or [0])[i] if i < len(r.get("history_revenue") or []) else 0),
                "clicks":  int(_safe_float((r.get("history_clicks") or [0])[i] if i < len(r.get("history_clicks") or []) else 0)),
                "leads":   int(_safe_float((r.get("history_leads")  or [0])[i] if i < len(r.get("history_leads")  or []) else 0)),
                "calls":   int(_safe_float((r.get("history_calls")  or [0])[i] if i < len(r.get("history_calls")  or []) else 0)),
                "forms":   int(_safe_float((r.get("history_forms")  or [0])[i] if i < len(r.get("history_forms")  or []) else 0)),
                "orders":  int(_safe_float((r.get("history_orders") or [0])[i] if i < len(r.get("history_orders") or []) else 0)),
            })

        campaigns.append({
            "campaign_id":       int(_safe_float(r.get("campaign_id"))),
            "campaign_name":     str(r.get("campaign_name") or ""),
            "campaign_type":     str(r.get("campaign_type") or ""),
            "meta_state":        str(r.get("meta_state") or ""),
            "status":            str(r.get("status") or ""),
            "state":             str(r.get("state") or ""),
            "search_strategy":   str(r.get("search_strategy") or ""),
            "network_strategy":  str(r.get("network_strategy") or ""),
            "attribution_model": str(r.get("attribution_model") or ""),
            "weekly_budget":     _safe_float(r.get("weekly_budget")),
            "traffic_mix":       str(r.get("traffic_mix") or ""),
            "semantic_tags":     list(r.get("semantic_tags") or []),

            "cost_week":        _safe_float(r.get("cost_week")),
            "revenue_week":     _safe_float(r.get("revenue_week")),
            "impressions_week": int(_safe_float(r.get("impressions_week"))),
            "clicks_week":      int(_safe_float(r.get("clicks_week"))),
            "leads_week":       int(_safe_float(r.get("leads_week"))),
            "calls_week":       int(_safe_float(r.get("calls_week"))),
            "forms_week":       int(_safe_float(r.get("forms_week"))),
            "orders_week":      int(_safe_float(r.get("orders_week"))),
            "spam_traffic_week":         int(_safe_float(r.get("spam_traffic_week"))),
            "targeted_calls_week":       int(_safe_float(r.get("targeted_calls_week"))),
            "order_create_started_week": int(_safe_float(r.get("order_create_started_week"))),
            "order_created_week":        int(_safe_float(r.get("order_created_week"))),
            "goal_507627231_week":       int(_safe_float(r.get("goal_507627231_week"))),
            "unique_calls_week":         int(_safe_float(r.get("unique_calls_week"))),
            "quiz_completed_week":       int(_safe_float(r.get("quiz_completed_week"))),
            "phone_clicks_week":         int(_safe_float(r.get("phone_clicks_week"))),

            "priority_goal_ids":    [int(_safe_float(x)) for x in (r.get("priority_goal_ids") or [])],
            "priority_goal_values": [_safe_float(x) for x in (r.get("priority_goal_values") or [])],

            "cost_prev":        _safe_float(r.get("cost_prev")),
            "revenue_prev":     _safe_float(r.get("revenue_prev")),
            "impressions_prev": int(_safe_float(r.get("impressions_prev"))),
            "clicks_prev":      int(_safe_float(r.get("clicks_prev"))),
            "leads_prev":       int(_safe_float(r.get("leads_prev"))),
            "calls_prev":       int(_safe_float(r.get("calls_prev"))),
            "forms_prev":       int(_safe_float(r.get("forms_prev"))),
            "orders_prev":      int(_safe_float(r.get("orders_prev"))),
            "spam_traffic_prev":         int(_safe_float(r.get("spam_traffic_prev"))),
            "targeted_calls_prev":       int(_safe_float(r.get("targeted_calls_prev"))),
            "order_create_started_prev": int(_safe_float(r.get("order_create_started_prev"))),
            "order_created_prev":        int(_safe_float(r.get("order_created_prev"))),
            "goal_507627231_prev":       int(_safe_float(r.get("goal_507627231_prev"))),
            "unique_calls_prev":         int(_safe_float(r.get("unique_calls_prev"))),
            "quiz_completed_prev":       int(_safe_float(r.get("quiz_completed_prev"))),
            "phone_clicks_prev":         int(_safe_float(r.get("phone_clicks_prev"))),

            "roas_week": _safe_float(r.get("roas_week")),
            "cpa_week":  _safe_float(r.get("cpa_week")),
            "cpc_week":  _safe_float(r.get("cpc_week")),
            "ctr_week":  _safe_float(r.get("ctr_week")),

            "health":        str(r.get("health") or ""),
            "health_reason": str(r.get("health_reason") or ""),
            "cabinet_name":  str(r.get("cabinet_name") or ""),

            "weekly_series": series,
        })

    cost_w = totals["cost"]["week"]; cost_p = totals["cost"]["prev"]
    clicks_w = totals["clicks"]["week"]; clicks_p = totals["clicks"]["prev"]
    avg_cpc_w = round(cost_w / clicks_w, 2) if clicks_w > 0 else 0.0
    avg_cpc_p = round(cost_p / clicks_p, 2) if clicks_p > 0 else 0.0

    def _metric(key: str, is_int: bool = False) -> dict:
        w = totals[key]["week"]; p = totals[key]["prev"]
        return {
            "week": int(w) if is_int else round(w, 2),
            "prev": int(p) if is_int else round(p, 2),
            "delta_pct": _delta_pct(w, p),
        }

    summary = {
        "cost":        _metric("cost"),
        "revenue":     _metric("revenue"),
        "avg_cpc":     {"week": avg_cpc_w, "prev": avg_cpc_p, "delta_pct": _delta_pct(avg_cpc_w, avg_cpc_p)},
        "impressions": _metric("impressions", is_int=True),
        "clicks":      _metric("clicks", is_int=True),
        "leads":       _metric("leads", is_int=True),
        "calls":       _metric("calls", is_int=True),
        "forms":       _metric("forms", is_int=True),
        "orders":      _metric("orders", is_int=True),
        "spam_traffic":         _metric("spam_traffic", is_int=True),
        "targeted_calls":       _metric("targeted_calls", is_int=True),
        "order_create_started": _metric("order_create_started", is_int=True),
        "order_created":        _metric("order_created", is_int=True),
    }

    health_counts = {"green": 0, "yellow": 0, "red": 0, "pending": 0}
    for c in campaigns:
        h = c["health"] or "pending"
        health_counts[h] = health_counts.get(h, 0) + 1

    return {
        "report_date": report_date,
        "summary": summary,
        "health_counts": health_counts,
        "campaigns": campaigns,
    }


@app.get(
    "/api/command_center/adgroups",
    tags=["command_center"],
    summary="Группы внутри кампании: totals + health_counts + groups[]",
)
async def get_command_center_adgroups(campaign_id: int):
    if campaign_id <= 0:
        raise HTTPException(status_code=400, detail="campaign_id обязателен (>0)")

    from config import CLICKHOUSE_DATABASE as CH_DB
    try:
        sql = f"""
            WITH last_d AS (SELECT max(report_date) AS d FROM {CH_DB}.command_center_adgroups)
            SELECT
                toString(report_date) AS report_date_str,
                toInt64(group_id)     AS group_id,
                group_name,
                toInt64(campaign_id)  AS campaign_id,
                campaign_name,
                status, serving_status, group_type,
                keyword_count, autotargeting_state, autotargeting_risky,
                cost_week, revenue_week,
                impressions_week, clicks_week, leads_week, calls_week, forms_week, orders_week,
                spam_traffic_week,
                cost_prev, revenue_prev, clicks_prev, leads_prev, calls_prev, forms_prev,
                spam_traffic_prev,
                roas_week, cpa_week, cpc_week, cpc_prev, ctr_week,
                health, health_reason,
                arrayMap(x -> toString(x), history_weeks) AS history_weeks,
                history_cost, history_clicks, history_leads
            FROM {CH_DB}.command_center_adgroups
            WHERE report_date = (SELECT d FROM last_d)
              AND campaign_id = {int(campaign_id)}
            ORDER BY cost_week DESC
        """
        rows = await asyncio.to_thread(_reports_query_dicts, sql)
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"ClickHouse error: {exc}")

    report_date = None
    totals = {k: 0 for k in ("cost_week", "cost_prev", "revenue_week", "clicks_week", "leads_week", "calls_week", "forms_week")}
    health_counts = {"green": 0, "yellow": 0, "red": 0, "pending": 0}
    groups: list[dict] = []

    for r in rows:
        report_date = str(r.get("report_date_str") or "")
        totals["cost_week"]    += _safe_float(r.get("cost_week"))
        totals["cost_prev"]    += _safe_float(r.get("cost_prev"))
        totals["revenue_week"] += _safe_float(r.get("revenue_week"))
        totals["clicks_week"]  += int(_safe_float(r.get("clicks_week")))
        totals["leads_week"]   += int(_safe_float(r.get("leads_week")))
        totals["calls_week"]   += int(_safe_float(r.get("calls_week")))
        totals["forms_week"]   += int(_safe_float(r.get("forms_week")))

        h = str(r.get("health") or "")
        health_counts[h] = health_counts.get(h, 0) + 1

        weeks = r.get("history_weeks") or []
        series = []
        for i in range(len(weeks)):
            series.append({
                "week":   str(weeks[i]),
                "cost":   _safe_float((r.get("history_cost")   or [0])[i] if i < len(r.get("history_cost")   or []) else 0),
                "clicks": int(_safe_float((r.get("history_clicks") or [0])[i] if i < len(r.get("history_clicks") or []) else 0)),
                "leads":  int(_safe_float((r.get("history_leads")  or [0])[i] if i < len(r.get("history_leads")  or []) else 0)),
            })

        groups.append({
            "group_id":            int(_safe_float(r.get("group_id"))),
            "group_name":          str(r.get("group_name") or ""),
            "campaign_id":         int(_safe_float(r.get("campaign_id"))),
            "campaign_name":       str(r.get("campaign_name") or ""),
            "status":              str(r.get("status") or ""),
            "serving_status":      str(r.get("serving_status") or ""),
            "group_type":          str(r.get("group_type") or ""),
            "keyword_count":       int(_safe_float(r.get("keyword_count"))),
            "autotargeting_state": str(r.get("autotargeting_state") or ""),
            "autotargeting_risky": int(_safe_float(r.get("autotargeting_risky"))),

            "cost_week":        _safe_float(r.get("cost_week")),
            "revenue_week":     _safe_float(r.get("revenue_week")),
            "impressions_week": int(_safe_float(r.get("impressions_week"))),
            "clicks_week":      int(_safe_float(r.get("clicks_week"))),
            "leads_week":       int(_safe_float(r.get("leads_week"))),
            "calls_week":       int(_safe_float(r.get("calls_week"))),
            "forms_week":       int(_safe_float(r.get("forms_week"))),
            "orders_week":      int(_safe_float(r.get("orders_week"))),
            "spam_traffic_week": int(_safe_float(r.get("spam_traffic_week"))),

            "cost_prev":        _safe_float(r.get("cost_prev")),
            "revenue_prev":     _safe_float(r.get("revenue_prev")),
            "clicks_prev":      int(_safe_float(r.get("clicks_prev"))),
            "leads_prev":       int(_safe_float(r.get("leads_prev"))),
            "calls_prev":       int(_safe_float(r.get("calls_prev"))),
            "forms_prev":       int(_safe_float(r.get("forms_prev"))),
            "spam_traffic_prev": int(_safe_float(r.get("spam_traffic_prev"))),

            "roas_week": _safe_float(r.get("roas_week")),
            "cpa_week":  _safe_float(r.get("cpa_week")),
            "cpc_week":  _safe_float(r.get("cpc_week")),
            "cpc_prev":  _safe_float(r.get("cpc_prev")),
            "ctr_week":  _safe_float(r.get("ctr_week")),

            "health":        h,
            "health_reason": str(r.get("health_reason") or ""),

            "weekly_series": series,
        })

    return {
        "report_date":  report_date,
        "campaign_id":  int(campaign_id),
        "totals": {
            "cost_week":    round(totals["cost_week"], 2),
            "cost_prev":    round(totals["cost_prev"], 2),
            "revenue_week": round(totals["revenue_week"], 2),
            "clicks_week":  totals["clicks_week"],
            "leads_week":   totals["leads_week"],
            "calls_week":   totals["calls_week"],
            "forms_week":   totals["forms_week"],
        },
        "health_counts": health_counts,
        "groups":        groups,
    }


@app.get(
    "/api/command_center/ads",
    tags=["command_center"],
    summary="Объявления внутри группы: health_counts + ads[]",
)
async def get_command_center_ads(adgroup_id: int):
    if adgroup_id <= 0:
        raise HTTPException(status_code=400, detail="adgroup_id обязателен (>0)")

    from config import CLICKHOUSE_DATABASE as CH_DB
    try:
        sql = f"""
            WITH last_d AS (SELECT max(report_date) AS d FROM {CH_DB}.command_center_ads)
            SELECT
                toString(report_date) AS report_date_str,
                toInt64(ad_id) AS ad_id, toInt64(adgroup_id) AS adgroup_id, toInt64(campaign_id) AS campaign_id,
                ad_type, ad_subtype, status, state, status_clarification,
                title, title2, text_body, final_url, has_image,
                vcard_moderation, ad_image_moderation, sitelinks_moderation,
                cabinet_name,
                cost_week, clicks_week, sessions_week, bounces_week, leads_week, spam_traffic_week,
                cpc_week, bounce_rate_week,
                cost_prev, clicks_prev, sessions_prev, bounces_prev, leads_prev, spam_traffic_prev,
                cpc_prev, bounce_rate_prev,
                health, health_reason
            FROM {CH_DB}.command_center_ads
            WHERE report_date = (SELECT d FROM last_d)
              AND adgroup_id = {int(adgroup_id)}
            ORDER BY (status = 'REJECTED') DESC, cost_week DESC
        """
        rows = await asyncio.to_thread(_reports_query_dicts, sql)
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"ClickHouse error: {exc}")

    report_date = None
    health_counts = {"green": 0, "yellow": 0, "red": 0, "pending": 0}
    ads: list[dict] = []

    for r in rows:
        report_date = str(r.get("report_date_str") or "")
        h = str(r.get("health") or "")
        health_counts[h] = health_counts.get(h, 0) + 1

        ads.append({
            "ad_id":                int(_safe_float(r.get("ad_id"))),
            "adgroup_id":           int(_safe_float(r.get("adgroup_id"))),
            "campaign_id":          int(_safe_float(r.get("campaign_id"))),
            "cabinet_name":         str(r.get("cabinet_name") or ""),
            "ad_type":              str(r.get("ad_type") or ""),
            "ad_subtype":           str(r.get("ad_subtype") or ""),
            "status":               str(r.get("status") or ""),
            "state":                str(r.get("state") or ""),
            "status_clarification": str(r.get("status_clarification") or ""),
            "title":                str(r.get("title") or ""),
            "title2":               str(r.get("title2") or ""),
            "text_body":            str(r.get("text_body") or ""),
            "final_url":            str(r.get("final_url") or ""),
            "has_image":            int(_safe_float(r.get("has_image"))),
            "vcard_moderation":     str(r.get("vcard_moderation") or ""),
            "ad_image_moderation":  str(r.get("ad_image_moderation") or ""),
            "sitelinks_moderation": str(r.get("sitelinks_moderation") or ""),
            "cost_week":         _safe_float(r.get("cost_week")),
            "clicks_week":       int(_safe_float(r.get("clicks_week"))),
            "sessions_week":     int(_safe_float(r.get("sessions_week"))),
            "bounces_week":      int(_safe_float(r.get("bounces_week"))),
            "leads_week":        int(_safe_float(r.get("leads_week"))),
            "spam_traffic_week": int(_safe_float(r.get("spam_traffic_week"))),
            "cpc_week":          _safe_float(r.get("cpc_week")),
            "bounce_rate_week":  _safe_float(r.get("bounce_rate_week")),
            "cost_prev":         _safe_float(r.get("cost_prev")),
            "clicks_prev":       int(_safe_float(r.get("clicks_prev"))),
            "sessions_prev":     int(_safe_float(r.get("sessions_prev"))),
            "bounces_prev":      int(_safe_float(r.get("bounces_prev"))),
            "leads_prev":        int(_safe_float(r.get("leads_prev"))),
            "spam_traffic_prev": int(_safe_float(r.get("spam_traffic_prev"))),
            "cpc_prev":          _safe_float(r.get("cpc_prev")),
            "bounce_rate_prev":  _safe_float(r.get("bounce_rate_prev")),
            "health":        h,
            "health_reason": str(r.get("health_reason") or ""),
        })

    return {
        "report_date":   report_date,
        "adgroup_id":    int(adgroup_id),
        "health_counts": health_counts,
        "ads":           ads,
    }


# ─── Entry point ──────────────────────────────────────────────────────────────
if __name__ == "__main__":
    uvicorn.run("api_server:app", host=HOST, port=PORT, log_level="info")
