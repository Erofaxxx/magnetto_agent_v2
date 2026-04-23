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
import json
import math as _math
import uuid
from datetime import date as _date, datetime, timezone
_datetime = datetime  # alias for _serialize_value
from typing import Optional, Literal
import uvicorn
from dotenv import load_dotenv
from fastapi import FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel
load_dotenv()
from config import ALLOWED_MODELS, HOST, MODEL, PORT, SERVER_URL

# ─── deepagents switch ────────────────────────────────────────────────────
# When USE_DEEPAGENTS=1, requests are routed through core.api_adapter
# (new deepagents-based agent). Otherwise — legacy agent.AnalyticsAgent.
import os as _os
_USE_DEEPAGENTS = _os.environ.get("USE_DEEPAGENTS", "0") in ("1", "true", "True", "yes")
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
)
# ─── CORS ─────────────────────────────────────────────────────────────────────
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
# ─── Job store ────────────────────────────────────────────────────────────────
# job_id → JobRecord dict
# Хранится в памяти; при рестарте сервера задачи теряются (это приемлемо).
JOB_TTL_SECONDS = 7200  # 2 часа
JobStatus = Literal["pending", "running", "done", "error"]
_jobs: dict[str, dict] = {}
def _new_job(session_id: str, query: str, model: Optional[str] = None) -> str:
    job_id = str(uuid.uuid4())
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
    _jobs[job_id]["status"] = "running"
    _jobs[job_id]["started_at"] = datetime.now(timezone.utc).isoformat()
def _set_done(job_id: str, result: dict) -> None:
    _jobs[job_id]["status"] = "done"
    _jobs[job_id]["finished_at"] = datetime.now(timezone.utc).isoformat()
    _jobs[job_id]["result"] = result
def _set_error(job_id: str, error: str) -> None:
    _jobs[job_id]["status"] = "error"
    _jobs[job_id]["finished_at"] = datetime.now(timezone.utc).isoformat()
    _jobs[job_id]["error"] = error
# ─── Request / Response models ────────────────────────────────────────────────
class AnalyzeRequest(BaseModel):
    query: str
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
    tool_calls: Optional[list[dict]] = None
    error: Optional[str] = None
# ─── Background worker ────────────────────────────────────────────────────────
async def _run_agent_job(job_id: str) -> None:
    """Run the agent in a thread pool and store the result in _jobs."""
    job = _jobs.get(job_id)
    if not job:
        return
    _set_running(job_id)
    started_at = datetime.now(timezone.utc).isoformat()
    try:
        if _USE_DEEPAGENTS:
            from core.api_adapter import analyze_deepagents
            result = await asyncio.to_thread(
                analyze_deepagents,
                query=job["query"],
                session_id=job["session_id"],
                model=job.get("model"),
            )
        else:
            from agent import get_agent
            agent = get_agent(job.get("model"))
            result = await asyncio.to_thread(
                agent.analyze,
                user_query=job["query"],
                session_id=job["session_id"],
            )
        _set_done(job_id, result)

        # ── Passive observability logging ──────────────────────────────────
        # Agent is already done and result is stored. Logger runs in a
        # daemon thread — any failure is silently swallowed, never affects agent.
        try:
            import threading as _threading
            from chat_logger import get_logger
            from config import DB_PATH
            logger = get_logger(DB_PATH)

            msgs = result.get("_messages", [])
            if msgs:
                _threading.Thread(
                    target=logger.log_turn,
                    args=(job["session_id"], msgs, started_at),
                    daemon=False,
                ).start()

            # Log router result (which skills Haiku selected for this turn)
            active_skills = result.get("_active_skills", [])
            from langchain_core.messages import HumanMessage as _HM
            turn_index = sum(1 for m in msgs if isinstance(m, _HM))
            _threading.Thread(
                target=logger.log_router,
                args=(job["session_id"], turn_index, active_skills,
                      job.get("query", ""), started_at),
                daemon=False,
            ).start()
        except Exception as log_exc:
            print(f"[ChatLogger] init error (non-fatal): {log_exc}")

    except Exception as exc:
        _set_error(job_id, str(exc))
        print(f"[job:{job_id}] ERROR: {exc}")
# ─── Cleanup loop ─────────────────────────────────────────────────────────────
async def _cleanup_loop() -> None:
    """Remove expired jobs and parquet files every 30 minutes."""
    while True:
        await asyncio.sleep(1800)
        now = datetime.now(timezone.utc).timestamp()
        # Clean expired jobs
        expired = [
            jid for jid, j in list(_jobs.items())
            if j["status"] in ("done", "error")
            and j["finished_at"]
            and (now - datetime.fromisoformat(j["finished_at"]).timestamp()) > JOB_TTL_SECONDS
        ]
        for jid in expired:
            del _jobs[jid]
        if expired:
            print(f"[cleanup] Removed {len(expired)} expired job(s)")
        # Clean parquet files
        try:
            from agent import get_agent
            n = await asyncio.to_thread(get_agent().cleanup_temp_files)
            if n:
                print(f"[cleanup] Removed {n} expired parquet file(s)")
        except Exception as exc:
            print(f"[cleanup] Parquet cleanup error: {exc}")
# ─── Startup ──────────────────────────────────────────────────────────────────
@app.on_event("startup")
async def startup() -> None:
    if _USE_DEEPAGENTS:
        # Warm up: schema cache + agent factory build one agent for default model.
        from core.agent_factory import build_agent
        await asyncio.to_thread(build_agent, "magnetto", MODEL)
        print("✅ deepagents ready (USE_DEEPAGENTS=1)")
    else:
        from agent import get_agent
        get_agent()
    asyncio.create_task(_cleanup_loop())
    print(f"✅ ClickHouse Analytics Agent API started | {SERVER_URL}")


# ─── Session files endpoint (deepagents only) ─────────────────────────────────
# Permits frontend to fetch a plot PNG / parquet file from this session's dir
# using the virtual path (/plots/..., /parquet/...). Only when USE_DEEPAGENTS=1.

@app.get("/api/session/{session_id}/file", summary="Download a file from session directory")
async def get_session_file(session_id: str, path: str):
    """
    Serve a file from the session's virtual filesystem.
    Path must start with /plots/ or /parquet/ (prevents arbitrary FS access).

    Example: GET /api/session/abc/file?path=/plots/2026-04-20_roas.png
    """
    from fastapi.responses import FileResponse
    if not _USE_DEEPAGENTS:
        raise HTTPException(status_code=400, detail="Files endpoint only available with USE_DEEPAGENTS=1")
    if not path.startswith(("/plots/", "/parquet/", "/memories/")):
        raise HTTPException(status_code=400, detail="Path must start with /plots/, /parquet/, or /memories/")
    import re
    if ".." in path or re.search(r"[/\\]\.[/\\]", path):
        raise HTTPException(status_code=400, detail="Invalid path")

    from config import TEMP_DIR
    import pathlib
    # Strip leading slash and prepend session root
    rel = path.lstrip("/")
    full = pathlib.Path(TEMP_DIR) / "sessions" / session_id / rel
    if not full.exists() or not full.is_file():
        raise HTTPException(status_code=404, detail=f"File not found: {path}")
    return FileResponse(str(full))


@app.get("/api/session/{session_id}/files", summary="List files in session directory")
async def list_session_files(session_id: str):
    """List all files (plots + parquet + memories) created in this session."""
    if not _USE_DEEPAGENTS:
        raise HTTPException(status_code=400, detail="Files endpoint only available with USE_DEEPAGENTS=1")
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
async def get_session(session_id: str):
    # Count pending/running jobs for this session
    active = [j for j in _jobs.values() if j["session_id"] == session_id and j["status"] in ("pending", "running")]
    return {
        "session_id": session_id,
        "active_jobs": len(active),
    }
# ─── Main: submit query ────────────────────────────────────────────────────────
@app.post("/api/analyze", response_model=SubmitResponse, summary="Submit an analytics query")
async def analyze(req: AnalyzeRequest):
    """
    Submit a query to the agent.
    Returns job_id immediately — agent runs in background.
    Poll GET /api/job/{job_id} to get the result.

    Optional `model` field selects the LLM. See GET /api/models for allowed values.
    """
    if req.model and req.model not in ALLOWED_MODELS:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown model '{req.model}'. Allowed: {list(ALLOWED_MODELS.keys())}",
        )
    session_id = req.session_id or str(uuid.uuid4())
    job_id = _new_job(session_id=session_id, query=req.query, model=req.model)
    # Fire and forget
    asyncio.create_task(_run_agent_job(job_id))
    return SubmitResponse(
        job_id=job_id,
        session_id=session_id,
        status="pending",
        message="Query accepted. Poll GET /api/job/{job_id} for result.",
    )
# ─── Poll job status ───────────────────────────────────────────────────────────
@app.get("/api/job/{job_id}", response_model=JobStatusResponse, summary="Poll job status / get result")
async def get_job(job_id: str):
    """
    Poll the status of a submitted job.
    status: "pending" | "running" | "done" | "error"
    When status == "done", text_output, plots, tool_calls are populated.
    """
    job = _jobs.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail=f"Job {job_id} not found (may have expired)")
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
        resp.tool_calls = r.get("tool_calls", [])
        resp.error = r.get("error")
    return resp
# ─── Stats ────────────────────────────────────────────────────────────────────
@app.get("/api/chat-stats", summary="Database statistics")
async def chat_stats():
    total = len(_jobs)
    by_status = {}
    for j in _jobs.values():
        by_status[j["status"]] = by_status.get(j["status"], 0) + 1
    return {"total_jobs_in_memory": total, "by_status": by_status}
# ─── Observability / Debug endpoints ─────────────────────────────────────────
# These endpoints are for developer use only (agent optimization analysis).
# They are NOT intended for the end-user frontend.

@app.get("/debug/sessions", tags=["debug"], summary="List all logged sessions")
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


@app.get("/debug/session/{session_id}", tags=["debug"], summary="Full session log with tool calls")
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


@app.get("/debug/session/{session_id}/turn/{turn_index}", tags=["debug"], summary="Log for one specific turn")
async def debug_turn_logs(session_id: str, turn_index: int):
    """
    Detailed event log for a single turn within a session.
    Useful for deep-diving into one specific question the user asked.
    """
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


@app.get("/debug/stats", tags=["debug"], summary="Aggregate optimization stats")
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
    from segment_agent import get_segment_agent
    from segment_store import _SHARED_OWNER
    owner = x_user_id or _SHARED_OWNER
    session_id = req.session_id or str(uuid.uuid4())
    agent = get_segment_agent(req.model)
    loop = asyncio.get_event_loop()
    result = await loop.run_in_executor(None, agent.chat, req.message, session_id, owner)
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
async def get_segment_chat_history(session_id: str):
    """История диалога сессии сегментации в формате [{role, content}]."""
    from segment_agent import get_segment_agent
    agent = get_segment_agent()
    loop = asyncio.get_event_loop()
    history = await loop.run_in_executor(None, agent.get_session_history, session_id)
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
    from segment_store import _SHARED_OWNER, get_segment_store
    owner = x_user_id or _SHARED_OWNER
    store = get_segment_store()
    loop = asyncio.get_event_loop()
    segments = await loop.run_in_executor(None, store.list_all, owner)
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
    from segment_store import _SHARED_OWNER, get_segment_store
    owner = x_user_id or _SHARED_OWNER
    store = get_segment_store()
    loop = asyncio.get_event_loop()
    seg = await loop.run_in_executor(None, store.get_by_id, segment_id, owner)
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
    from segment_store import _SHARED_OWNER, get_segment_store
    owner = x_user_id or _SHARED_OWNER
    store = get_segment_store()
    loop = asyncio.get_event_loop()
    deleted = await loop.run_in_executor(None, store.delete, segment_id, owner)
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
_CABINET_CACHE_TTL = 3600  # 1 час
_cabinet_cache: dict = {"values": [], "fetched_at": 0.0}


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
        from tools import _ch_lock, _get_ch_client
        import pandas as _pd

        ch = _get_ch_client()
        sql = (
            "SELECT DISTINCT cabinet_name FROM magnetto.bad_placements "
            "WHERE cabinet_name != '' ORDER BY cabinet_name"
        )
        with _ch_lock:
            result = await asyncio.to_thread(ch.execute_query, sql)
        if result.get("success"):
            df = _pd.read_parquet(result["parquet_path"])
            cabinets = [str(v) for v in df["cabinet_name"].dropna().tolist()]
            _cabinet_cache["values"] = cabinets
            _cabinet_cache["fetched_at"] = now
            return cabinets
    except Exception:
        # Сеть/ClickHouse упал — отдаём последние известные значения,
        # даже если TTL истёк. Пустой список означает "ещё не грузили".
        pass

    return _cabinet_cache["values"]


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
        with _ch_lock:
            result = await asyncio.to_thread(ch.execute_query, sql)
        with _ch_lock:
            count_result = await asyncio.to_thread(ch.execute_query, count_sql)
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


def _get_reports_client():
    """
    Singleton CH-клиент с кредами CLICKHOUSE_REPORTS_USER/_PASSWORD.
    Возвращает None, если переменные не заданы или не удалось подключиться.
    """
    global _reports_ch_client
    if _reports_ch_client is not None:
        return _reports_ch_client

    import os
    user = (os.environ.get("CLICKHOUSE_REPORTS_USER") or "").strip()
    password = (os.environ.get("CLICKHOUSE_REPORTS_PASSWORD") or "").strip()
    if not user or not password:
        return None

    try:
        import clickhouse_connect
        from config import (
            CLICKHOUSE_HOST,
            CLICKHOUSE_PORT,
            CLICKHOUSE_DATABASE,
            CLICKHOUSE_SSL_CERT,
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
        if CLICKHOUSE_SSL_CERT:
            connect_kwargs["verify"] = True
            connect_kwargs["ca_cert"] = CLICKHOUSE_SSL_CERT
        else:
            connect_kwargs["verify"] = False
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

    if cabinet_name is not None and not re.match(r"^[A-Za-z0-9_-]+$", cabinet_name):
        raise HTTPException(status_code=400, detail="Invalid cabinet_name")

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
