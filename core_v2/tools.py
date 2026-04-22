"""
Session-aware tool wrappers for deepagents agent.

Tools exposed:
  - clickhouse_query   — executes SQL, saves parquet INTO session dir (persistent between turns)
  - python_analysis    — runs Python, saves plot PNGs to session /plots/ + updates /plots/index.md
  - list_tables        — lists all CH tables (for fallback)
  - think_tool         — records hypothesis/plan/reflection

The underlying sandbox and ClickHouse client stay untouched — we add persistence
logic on top so files survive between turns of the same session.
"""
from __future__ import annotations

import base64
import hashlib
import io
import json
import re
import shutil
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

from langchain_core.tools import tool

from .session_context import current_parquet_dir, current_plots_dir, get_current_session


# ─── Hard cap on tool result text (same as legacy) ──────────────────────────
_MAX_RESULT_CHARS = 50_000


def _cap_result(text: str) -> str:
    if len(text) <= _MAX_RESULT_CHARS:
        return text
    half = _MAX_RESULT_CHARS // 2
    return (
        text[:half]
        + f"\n… [result truncated: {len(text)} chars total, showing first and last {half}] …\n"
        + text[-half:]
    )


# ─── Lazy ClickHouse ────────────────────────────────────────────────────────
_ch_client = None
_ch_lock = threading.Lock()  # single connection: serialize queries


def _get_ch_client():
    global _ch_client
    if _ch_client is None:
        from clickhouse_client import ClickHouseClient  # legacy module, untouched
        _ch_client = ClickHouseClient()
    return _ch_client


# ─── Helpers ────────────────────────────────────────────────────────────────

def _relocate_parquet_to_session(physical_path: str) -> str:
    """
    The ClickHouseClient currently writes parquet into its own TEMP_DIR.
    If session directory differs, move the file there so the file survives
    between turns in this session (not shared with other sessions).
    Returns the final physical path.
    """
    try:
        src = Path(physical_path)
        if not src.exists():
            return physical_path
        dst_dir = current_parquet_dir()
        if dst_dir.resolve() == src.parent.resolve():
            return physical_path  # already in session dir
        dst = dst_dir / src.name
        # move (hardlink if same FS, else copy+delete)
        try:
            src.replace(dst)
        except OSError:
            shutil.copy2(src, dst)
            src.unlink(missing_ok=True)
        return str(dst)
    except Exception:
        return physical_path  # never fail the tool for a filesystem hiccup


def _slugify(text: str, max_len: int = 40) -> str:
    s = re.sub(r"[^\w\-]+", "-", text.strip().lower())
    s = re.sub(r"-{2,}", "-", s).strip("-")
    return s[:max_len] or "chart"


def _save_plots_to_session(plots_b64: list[str], *, hint: str = "") -> list[dict]:
    """
    Persist base64 PNGs into session /plots/ and update /plots/index.md.

    Returns list of {"path": str, "url": str, "base64": str}.
    `url` is the virtual path inside the agent's filesystem (/plots/<filename>).
    `path` is the physical absolute path.
    """
    if not plots_b64:
        return []
    plots_dir = current_plots_dir()
    date_prefix = datetime.utcnow().strftime("%Y-%m-%d_%H%M%S")
    slug = _slugify(hint)

    saved = []
    for idx, b64 in enumerate(plots_b64, 1):
        # strip data URI prefix if present
        clean_b64 = b64.split(",", 1)[1] if b64.startswith("data:image") else b64
        raw = base64.b64decode(clean_b64)
        fname = f"{date_prefix}_{slug}_{idx}.png" if slug else f"{date_prefix}_chart_{idx}.png"
        fpath = plots_dir / fname
        fpath.write_bytes(raw)
        saved.append({
            "path": str(fpath),
            "url": f"/plots/{fname}",
            "base64": b64,  # keep original for UI artifact
        })

    # Append to /plots/index.md
    try:
        ctx = get_current_session()
        if ctx is not None:
            idx_path = ctx.plots_index
            ts = datetime.utcnow().isoformat(timespec="seconds") + "Z"
            lines = []
            if not idx_path.exists():
                lines.append("# Plots Index\n\nСписок графиков, построенных в этой сессии.\n")
            for s in saved:
                lines.append(f"- `{s['url']}` ({ts}): {hint or 'untitled chart'}")
            with idx_path.open("a", encoding="utf-8") as f:
                f.write("\n" + "\n".join(lines) + "\n")
    except Exception:
        pass  # non-critical

    return saved


# ─── Tool: list_tables ──────────────────────────────────────────────────────
@tool
def list_tables() -> str:
    """
    Получить список ВСЕХ таблиц ClickHouse с именами колонок и типами.

    ВАЖНО: карта данных (какая таблица для чего) уже в твоём system prompt
    через /data_map.md — обращайся к нему в первую очередь. Используй
    list_tables ТОЛЬКО если карта кажется неполной или таблица не найдена.

    Возвращает JSON [{"table": "...", "columns": [{"name": "...", "type": "..."}]}].
    """
    try:
        tables = _get_ch_client().list_tables()
        return json.dumps(tables, ensure_ascii=False)
    except Exception as exc:
        return json.dumps({"error": str(exc)})


# ─── Tool: clickhouse_query ─────────────────────────────────────────────────
@tool
def clickhouse_query(sql: str) -> str:
    """
    Выполнить SELECT-запрос к ClickHouse. Результат сохраняется в parquet
    (путь возвращается в поле `parquet_path`) и живёт между turn'ами в
    пределах текущей сессии — можно повторно читать через pd.read_parquet
    или передавать в python_analysis.

    Правила: только SELECT; всегда с LIMIT; для нескольких таблиц используй
    WITH/CTE в одном запросе; для dm_direct_performance / dm_traffic_performance
    обязательно `WHERE date < today()` (сегодня неполное); для snapshot-таблиц
    (bad_*, dm_funnel_velocity, dm_step_goal_impact, dm_active_clients_scoring,
    dm_path_templates) — `WHERE snapshot_date = (SELECT max(snapshot_date) FROM X)`.

    Возвращает JSON: row_count, columns, col_stats, parquet_path, cached.

    Args:
        sql: SELECT-запрос.
    """
    try:
        with _ch_lock:
            result = _get_ch_client().execute_query(sql)
        if isinstance(result, dict) and result.get("parquet_path"):
            result["parquet_path"] = _relocate_parquet_to_session(result["parquet_path"])
        return _cap_result(json.dumps(result, ensure_ascii=False, default=str))
    except Exception as exc:
        return json.dumps({"success": False, "error": str(exc)})


# ─── Tool: python_analysis ──────────────────────────────────────────────────
@tool(response_format="content_and_artifact")
def python_analysis(code: str, parquet_path: str, chart_hint: Optional[str] = None) -> tuple[str, list[str]]:
    """
    Выполнить Python-анализ над parquet-файлом. `df` (pandas DataFrame) уже
    загружен с применёнными нормализациями (Array → list, numeric/datetime
    coerce). Доступны: df, pd, np, plt, sns, result=None, df_info.

    Графики (matplotlib/seaborn) автоматически сохраняются как PNG в
    /plots/<timestamp>_<slug>.png в пределах сессии и попадают в
    /plots/index.md — маркетолог может ссылаться на них позже в диалоге.

    Правила:
    1. `result` — Markdown-строка (показывается пользователю).
    2. Используй `print()` для отладочного вывода.
    3. НЕ вызывай plt.close() — графики захватываются автоматически.
    4. Подписи графиков — на русском; числа с разделителями тысяч.
    5. Для Array-колонок используй .apply(len) или [:5], не выводи целиком.

    Args:
        code:         Python-код. `df` уже загружен из parquet_path.
        parquet_path: Путь из clickhouse_query (физический).
        chart_hint:   Короткое описание графика (используется в имени файла
                       и в /plots/index.md). Пример: "roas-top10-campaigns".
    """
    try:
        from python_sandbox import PythonSandbox  # legacy, untouched
        result = PythonSandbox().execute(code=code, parquet_path=parquet_path)
        plots_b64: list[str] = result.pop("plots", [])

        # Persist plots to session /plots/ and build metadata entries
        saved = _save_plots_to_session(plots_b64, hint=chart_hint or "")
        saved_urls = [s["url"] for s in saved]

        # Enrich tool content with plot file paths (so the agent knows where they live)
        result["plots_count"] = len(saved)
        if saved_urls:
            result["plot_urls"] = saved_urls
            result["plots_index"] = "/plots/index.md"

        content = _cap_result(json.dumps(result, ensure_ascii=False, default=str))
        # Artifacts are base64 strings (deepagents/UI expects these for inline display)
        return content, plots_b64
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


# ─── Tool: think_tool ───────────────────────────────────────────────────────
@tool
def think_tool(thought: str) -> str:
    """
    Записать гипотезу, план или рефлексию ДО действия. Не делает ничего кроме
    сохранения мысли в контексте — используется для дисциплины мышления.

    КОГДА ИСПОЛЬЗОВАТЬ (обязательно):
    - Перед первым вызовом clickhouse_query / delegate_to_generalist / task
      в многошаговой задаче: запиши план.
    - После получения parquet/результата: оцени качество данных и следующий шаг.
    - Перед делегированием: сформулируй что ИМЕННО нужно от subagent'а.

    Примеры:
    - "План: 1) взять ROAS-топ-5 из direct-optimizer, 2) для клиентов этих
      кампаний — scoring, 3) синтезировать."
    - "Гипотеза: падение ROAS связано с новой кампанией X. Проверю через
      сравнение недель."
    - "Данные: 1500 строк, NULL в колонке cost для некоторых — фильтровать
      nullIf(cost, 0) в знаменателях."

    Args:
        thought: мысль/план/гипотеза в свободной форме.

    Returns:
        Подтверждение записи.
    """
    return f"Thought recorded: {thought}"
