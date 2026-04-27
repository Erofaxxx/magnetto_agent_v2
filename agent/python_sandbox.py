"""
Python code execution sandbox for the Analytics Agent.

Loads data from a Parquet file into a pandas DataFrame and executes
user-provided code in a controlled namespace. Captures:
  - stdout (print statements)
  - matplotlib/seaborn figures → base64 PNG strings
  - `result` variable → final text/table output
"""

import base64
import builtins as _builtins_module
import io
import re
import threading
import traceback

# IMPORTANT: set non-interactive backend BEFORE importing pyplot
import matplotlib

matplotlib.use("Agg")

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
import seaborn as sns

# ─── Global matplotlib / seaborn settings ─────────────────────────────────────
plt.rcParams["figure.figsize"] = (12, 7)
plt.rcParams["figure.dpi"] = 100
plt.rcParams["font.size"] = 12
plt.rcParams["axes.unicode_minus"] = False  # Fix minus sign rendering

# Try to enable Cyrillic fonts if available
try:
    import matplotlib.font_manager as fm

    # Try DejaVu which has decent Unicode coverage
    plt.rcParams["font.family"] = "DejaVu Sans"
except Exception:
    pass

sns.set_style("whitegrid")
sns.set_palette("husl")

# ─── Thread-local guard: plt.close / plt.show / plt.savefig protection ────────
# When agent code does `import matplotlib.pyplot as plt` it replaces the proxy
# with the real module singleton.  We patch the module-level functions ONCE so
# our patched versions remain effective regardless of how the module is imported.
# Thread-local flag ensures the guard only blocks close() during exec() and
# does NOT interfere with the sandbox's own cleanup in the finally block.
_tls = threading.local()
_figure_lock = threading.Lock()  # guards plt.get_fignums() snapshots across parallel calls

_orig_plt_close   = plt.close
_orig_plt_savefig = plt.savefig
_orig_plt_show    = plt.show
_orig_plt_clf     = plt.clf
_orig_plt_cla     = plt.cla
_orig_style_use   = plt.style.use


def _guarded_close(*a, **kw):
    """No-op while exec is active; delegates to real close otherwise."""
    if getattr(_tls, "exec_active", False):
        return
    return _orig_plt_close(*a, **kw)


def _guarded_savefig(*a, **kw):
    """Always no-op: sandbox captures figures directly via fig.savefig(buf)."""
    return


def _guarded_show(*a, **kw):
    """Always no-op: non-interactive Agg backend, no display available."""
    return


def _guarded_clf(*a, **kw):
    """No-op while exec is active: prevents agent from clearing captured figures."""
    if getattr(_tls, "exec_active", False):
        return
    return _orig_plt_clf(*a, **kw)


def _guarded_cla(*a, **kw):
    """No-op while exec is active: prevents agent from clearing axes."""
    if getattr(_tls, "exec_active", False):
        return
    return _orig_plt_cla(*a, **kw)


def _guarded_style_use(style, *a, **kw):
    """No-op while exec is active: prevents agent from changing global rcParams via style."""
    if getattr(_tls, "exec_active", False):
        return
    return _orig_style_use(style, *a, **kw)


plt.close     = _guarded_close
plt.savefig   = _guarded_savefig
plt.show      = _guarded_show
plt.clf       = _guarded_clf
plt.cla       = _guarded_cla
plt.style.use = _guarded_style_use


# ─── Safe pd.read_parquet: applies the same coercions as the sandbox ──────────
# Agent code often calls pd.read_parquet() directly to load additional datasets.
# This wrapper ensures those DataFrames receive the same type normalisations
# (numeric/datetime coercion, numpy array → list) as the primary `df`.
_orig_pd_read_parquet = pd.read_parquet


def _coerce_df(df: pd.DataFrame) -> pd.DataFrame:
    """
    Apply ClickHouse-specific type coercions to a freshly loaded DataFrame.

    1. Object columns that are numeric strings → float64
    2. Object columns that are date strings → datetime64
    3. numpy ndarray cells (ClickHouse Array columns) → Python list
    """
    for col in list(df.select_dtypes(include="object").columns):
        non_null = df[col].dropna()
        if len(non_null) == 0:
            continue
        converted = pd.to_numeric(df[col], errors="coerce")
        if converted.notna().sum() / len(non_null) > 0.8:
            df[col] = converted
            continue
        try:
            dt_converted = pd.to_datetime(df[col], errors="coerce", format="mixed")
            if dt_converted.notna().sum() / len(non_null) > 0.8:
                df[col] = dt_converted
        except Exception:
            pass
    for col in df.columns:
        non_null = df[col].dropna()
        if len(non_null) == 0:
            continue
        if isinstance(non_null.iloc[0], np.ndarray):
            df[col] = df[col].apply(
                lambda v: v.tolist() if isinstance(v, np.ndarray) else v
            )
    # Restore whole-number float64 columns to nullable integer (Int64).
    # ClickHouse Int*/UInt* columns often arrive as float64 after coerce due
    # to NaN presence.  47.0 → 47 prevents confusing LLM output and broken
    # equality checks (orders == 47 fails for 47.0).
    for col in list(df.select_dtypes("float64").columns):
        s = df[col].dropna()
        if len(s) > 0 and (s == s.astype("int64")).all():
            df[col] = df[col].astype("Int64")
    return df


def _safe_read_parquet(path, *args, **kwargs):
    return _coerce_df(_orig_pd_read_parquet(path, *args, **kwargs))


pd.read_parquet = _safe_read_parquet


class _PlotProxy:
    """
    Proxy for matplotlib.pyplot injected into the agent's execution namespace
    as the initial value of `plt`.

    Belt-and-suspenders on top of the module-level patches above: even if the
    agent uses `plt` directly (without re-importing), close/savefig/show are
    still no-ops.  All other attributes delegate to the real module.
    """
    def __getattr__(self, name: str):
        if name in ("close", "savefig", "show", "clf", "cla"):
            return lambda *a, **kw: None
        return getattr(plt, name)


_plt_proxy = _PlotProxy()


# ─── Detector: Python syntax leaked into result via broken f-strings ──────────
# Matches patterns that look like Python code accidentally rendered as text,
# e.g.: "2.9 if not np.isnan(t['avg_path']) else '—'"
# These arise when a ternary expression is written OUTSIDE the {} in an f-string.
_PYTHON_LEAK_RE = re.compile(
    r"(?:"
    r"\bif\s+(not\s+)?(?:np|pd)\.\w+\("       # "if not np.isnan(" / "if pd.notna("
    r"|else\s+['\"][^'\"]{0,20}['\"]"          # "else '—'" / "else 'N/A'"
    r"|\b(?:np|pd)\.\w+\(['\"]?\w"             # "np.isnan(x" / "pd.notna(v"
    r")",
    re.IGNORECASE,
)

# Only flag if the suspicious text sits inside a table cell or inline value
# (i.e., not inside a fenced code block where Python IS expected).
_CODE_FENCE_RE = re.compile(r"```[\s\S]*?```")


def _has_python_leak(text: str) -> bool:
    """Return True if result text appears to contain leaked Python expressions."""
    # Strip fenced code blocks — Python there is intentional
    stripped = _CODE_FENCE_RE.sub("", text)
    return bool(_PYTHON_LEAK_RE.search(stripped))


class PythonSandbox:
    """
    Executes Python analysis code with data pre-loaded from a Parquet file.
    Claude writes code that works with `df` — the sandbox handles parquet loading.
    """

    def execute(self, code: str, parquet_path: str) -> dict:
        """
        Execute Python code with data from parquet_path.

        The code receives:
          - df (pd.DataFrame): data from parquet
          - pd, np, plt, sns: pre-imported libraries
          - result (None → set by code for final text output)

        Returns:
          {
            "success": bool,
            "output": str,        # stdout from print()
            "result": str | None, # value of `result` variable (Markdown text/table)
            "plots": list[str],   # base64 PNG data URIs
            "error": str | None,
          }
        """
        # ── Load data from Parquet (with coercions applied by _safe_read_parquet) ─
        try:
            df = _orig_pd_read_parquet(parquet_path)  # load raw first for error handling
        except FileNotFoundError:
            return {
                "success": False,
                "output": "",
                "result": None,
                "plots": [],
                "error": (
                    f"Parquet file not found: '{parquet_path}'.\n"
                    "The file may have been deleted between turns. "
                    "Re-run the clickhouse_query that produced this data and use "
                    "the fresh parquet_path returned by that call."
                ),
            }
        except Exception as exc:
            return {
                "success": False,
                "output": "",
                "result": None,
                "plots": [],
                "error": f"Failed to load parquet file '{parquet_path}': {exc}",
            }

        # Apply coercions: numeric/datetime strings, numpy arrays → lists.
        # _coerce_df is also wired into pd.read_parquet so agent code loading
        # additional datasets gets the same normalisation automatically.
        df = _coerce_df(df)

        # ── Detect Array columns for df_info ───────────────────────────────
        # After _coerce_df, numpy arrays are already converted to Python lists.
        array_cols: list[str] = [
            col for col in df.columns
            if len(df[col].dropna()) > 0 and isinstance(df[col].dropna().iloc[0], list)
        ]

        # ── Snapshot existing figure numbers before exec ───────────────────
        # matplotlib is a global singleton shared across all parallel calls.
        # We record which figures exist BEFORE our exec, then after exec we
        # capture and close only the NEW figures created by THIS call.
        # _figure_lock makes the snapshot atomic so parallel calls don't
        # interleave their before/after reads.
        _saved_rc = dict(plt.rcParams)  # snapshot to restore after exec
        with _figure_lock:
            _before_fignums: set[int] = set(plt.get_fignums())

        # ── Prepare execution namespace ────────────────────────────────────
        # df_info shows dtype for regular cols and "Array" for list cols so
        # the agent knows to use .explode() / .apply(len) instead of to_markdown.
        df_info = {}
        for col, dtype in df.dtypes.items():
            null_count = int(df[col].isna().sum())
            if col in array_cols:
                entry = "Array"
            else:
                entry = str(dtype)
            if null_count > 0:
                entry += f" (nulls={null_count})"
            df_info[col] = entry

        # ── Thread-safe stdout capture ─────────────────────────────────────
        # contextlib.redirect_stdout is NOT thread-safe: it sets sys.stdout
        # globally, so two parallel calls overwrite each other's capture buffer.
        # Instead, we inject a custom print() into the execution builtins that
        # writes directly to our per-call StringIO — fully isolated.
        stdout_capture = io.StringIO()

        def _captured_print(*args, sep=" ", end="\n", file=None, flush=False):
            _builtins_module.print(
                *args, sep=sep, end=end,
                file=stdout_capture if file is None else file,
                flush=flush,
            )

        _patched_builtins = {**vars(_builtins_module), "print": _captured_print}

        plots: list[str] = []

        # Single namespace for exec: all names — pre-set vars, libraries, and
        # user-defined variables — live in one dict. This is essential because
        # when exec is called with separate globals/locals, functions and lambdas
        # defined inside the exec'd code use globals as their __globals__, making
        # any top-level local variable invisible inside those functions/lambdas.
        # One dict eliminates that split entirely.
        sandbox_globals = {
            "__builtins__": _patched_builtins,
            "pd": pd,
            "np": np,
            "plt": _plt_proxy,   # proxy: close()/savefig()/show() are no-ops
            "sns": sns,
            "df": df,
            "result": None,  # agent sets this for final text output
            "df_info": df_info,
        }

        # Activate the thread-local guard: plt.close() becomes a no-op for
        # THIS thread while exec runs, even if agent code replaces the proxy
        # via `import matplotlib.pyplot as plt`.  The finally block disables
        # the guard so the sandbox's own cleanup calls work normally.
        _tls.exec_active = True

        # ── Pandas display caps for the duration of exec ───────────────────
        # Prevent agent's `print(df)` / `print(df.head(500))` from blowing up
        # stdout to 50K+ chars. _MAX_OUTPUT below is the final safety net, but
        # we cap pandas BEFORE truncation kicks in so the model sees a clean
        # truncated table rather than a chopped one.
        _saved_display = {
            opt: pd.get_option(opt)
            for opt in ("display.max_rows", "display.max_columns", "display.width")
        }
        pd.set_option("display.max_rows", 100)
        pd.set_option("display.max_columns", 30)
        pd.set_option("display.width", 200)

        try:
            # ── Execute code ────────────────────────────────────────────────
            exec(code, sandbox_globals)  # noqa: S102

            # ── Capture only figures created by THIS call ──────────────────
            with _figure_lock:
                _my_fignums: set[int] = set(plt.get_fignums()) - _before_fignums
                for fig_num in sorted(_my_fignums):
                    fig = plt.figure(fig_num)
                    buf = io.BytesIO()
                    fig.savefig(buf, format="png", bbox_inches="tight", dpi=100)
                    buf.seek(0)
                    b64 = base64.b64encode(buf.read()).decode("utf-8")
                    plots.append(f"data:image/png;base64,{b64}")
                    buf.close()

            # ── Extract `result` variable ──────────────────────────────────
            result_value = sandbox_globals.get("result")
            if isinstance(result_value, pd.DataFrame):
                # Convert DataFrame to Markdown table
                result_value = result_value.to_markdown(index=False)
            elif result_value is not None:
                result_value = str(result_value)

            # ── Truncate stdout to avoid flooding LLM context ──────────────
            # 4 000 chars ≈ 50-80 rows of tabular data — enough to debug,
            # not enough to dump full datasets (those belong in parquet).
            # head+tail strategy keeps both schema rows and tail rows visible.
            _MAX_OUTPUT = 4000
            raw_output = stdout_capture.getvalue()
            if len(raw_output) > _MAX_OUTPUT:
                half = _MAX_OUTPUT // 2
                raw_output = (
                    raw_output[:half]
                    + f"\n… [stdout truncated, showing first+last {half} chars"
                    f" of {len(raw_output)} total — data in parquet is complete] …\n"
                    + raw_output[-half:]
                )

            # ── Auto-fill result from stdout when not explicitly set ───────
            # If the agent only used print() and forgot result, we use stdout
            # directly — no extra tool call needed to "fix" it.
            #
            # IMPORTANT: when this fires, the LLM tool message would otherwise
            # contain identical content TWICE (in `output` and in `result`),
            # doubling cache-prefix bloat across iterations. We track the flag
            # and blank `output` in the return dict — content is preserved in
            # `result`, the model sees one copy.
            result_was_autofilled = False
            if result_value is None and raw_output.strip():
                result_value = raw_output
                result_was_autofilled = True

            # ── Detect Python code leaked into result via broken f-strings ──
            # Pattern: f"{val:.1f} if pd.notna(val) else '—'" produces a
            # result string that contains Python syntax as literal text.
            # Python doesn't raise an error, but the output is wrong.
            # We detect common leaked patterns and prepend a warning so the
            # agent can fix its f-string in the same context without a retry.
            if result_value and _has_python_leak(result_value):
                result_value = (
                    "⚠️ ПРЕДУПРЕЖДЕНИЕ: в result обнаружен Python-код — вероятна ошибка в f-строке.\n"
                    "Используй промежуточную переменную вместо ternary внутри форматирующего {}:\n"
                    "  ❌  f\"| {val:.1f} if pd.notna(val) else '—' |\"\n"
                    "  ✅  val_str = f\"{val:.1f}\" if pd.notna(val) else \"—\"; f\"| {val_str} |\"\n\n"
                    + result_value
                )

            # ── Truncate result variable ────────────────────────────────────
            # result is shown to the user; keep it readable but bounded.
            # 4K matches the stdout cap — combined with auto-fill dedup below
            # this caps a single tool message at ~4K chars max.
            _MAX_RESULT = 4000
            if result_value and len(result_value) > _MAX_RESULT:
                half_r = _MAX_RESULT // 2
                result_value = (
                    result_value[:half_r]
                    + f"\n… [result truncated: {len(result_value)} chars total] …\n"
                    + result_value[-half_r:]
                )

            return {
                "success": True,
                # Blank output when result was just auto-filled from stdout —
                # preserves a single copy of the content (in `result`) and
                # halves tool-message size.  When the agent set result
                # explicitly, output is real stdout (debug trace) and stays.
                "output": "" if result_was_autofilled else raw_output,
                "result": result_value,
                "plots": plots,
                "error": None,
            }

        except Exception as exc:
            # Keep only the last 2 000 chars of the traceback — the tail contains
            # the actual error line and is sufficient for the LLM to self-correct.
            full_tb = f"{type(exc).__name__}: {exc}\n{traceback.format_exc()}"
            error_text = full_tb[-2000:] if len(full_tb) > 2000 else full_tb
            raw_output = stdout_capture.getvalue()
            if len(raw_output) > 8000:
                half = 4000
                raw_output = (
                    raw_output[:half]
                    + f"\n… [stdout truncated, showing first+last {half} chars"
                    f" of {len(raw_output)} total] …\n"
                    + raw_output[-half:]
                )
            return {
                "success": False,
                "output": raw_output,
                "result": None,
                "plots": plots,  # return any plots captured before the error
                "error": error_text,
            }

        finally:
            # Disable guard first so that plt.close() below is the real one.
            _tls.exec_active = False
            # Restore rcParams to pre-exec state so agent code cannot
            # permanently change styles for subsequent calls.
            plt.rcParams.update(_saved_rc)
            # Restore pandas display options as well — same reason.
            for opt, val in _saved_display.items():
                pd.set_option(opt, val)
            # Close only figures created by THIS call, not figures from
            # other parallel calls that may still be running.
            with _figure_lock:
                _to_close = set(plt.get_fignums()) - _before_fignums
                for fig_num in _to_close:
                    try:
                        _orig_plt_close(fig_num)  # call original directly — guard is off
                    except Exception:
                        pass
            sandbox_globals.clear()
