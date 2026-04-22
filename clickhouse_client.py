"""
ClickHouse client for the Analytics Agent.
Executes SELECT queries and saves results to Parquet.
"""

import hashlib
import json
import time
from pathlib import Path
from typing import Any

import clickhouse_connect
import numpy as np
import pandas as pd

from config import (
    CLICKHOUSE_DATABASE,
    CLICKHOUSE_HOST,
    CLICKHOUSE_PASSWORD,
    CLICKHOUSE_PORT,
    CLICKHOUSE_SSL_CERT,
    CLICKHOUSE_USER,
    TEMP_DIR,
    TEMP_FILE_TTL_SECONDS,
)


def _safe_json_value(v: Any) -> Any:
    """Convert numpy/pandas types and complex types to JSON-serializable values."""
    if isinstance(v, (list, dict, set, tuple)):
        return str(v)
    if isinstance(v, np.integer):
        return int(v)
    if isinstance(v, np.floating):
        return float(v) if not np.isnan(v) else None
    if isinstance(v, np.ndarray):
        return v.tolist()
    # Check for pandas NA / NaT / NaN
    try:
        if pd.isna(v):
            return None
    except (TypeError, ValueError):
        pass
    return v


class ClickHouseClient:
    """Direct connection to ClickHouse. Executes queries and saves data to Parquet."""

    def __init__(self) -> None:
        connect_kwargs: dict = {
            "host": CLICKHOUSE_HOST,
            "port": CLICKHOUSE_PORT,
            "username": CLICKHOUSE_USER,
            "password": CLICKHOUSE_PASSWORD,
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

        self.client = clickhouse_connect.get_client(**connect_kwargs)
        print(
            f"✅ ClickHouse connected: {CLICKHOUSE_HOST}:{CLICKHOUSE_PORT}"
            f"/{CLICKHOUSE_DATABASE}"
        )

    def list_tables(self) -> list[dict]:
        """
        Return all tables in the current database with column names and types.
        Result: [{"table": "visits", "columns": [{"name": "date", "type": "Date"}, ...]}, ...]
        """
        result = self.client.query(
            "SELECT table, name, type "
            "FROM system.columns "
            "WHERE database = currentDatabase() "
            "ORDER BY table, position"
        )
        tables: dict[str, list] = {}
        for row in result.result_rows:
            table_name, col_name, col_type = row[0], row[1], row[2]
            if table_name not in tables:
                tables[table_name] = []
            tables[table_name].append({"name": col_name, "type": col_type})
        return [{"table": t, "columns": cols} for t, cols in tables.items()]

    @staticmethod
    def _build_col_stats(df: pd.DataFrame) -> dict:
        """
        Build a compact per-column statistics dict instead of returning raw preview rows.

        For numeric columns: {type, min, max, nulls}
        For datetime columns: {type, min, max, nulls}
        For other (string/categorical): {type, unique, sample: [top-3], nulls}

        This is smaller in tokens than 5 preview rows and more useful for writing Python.
        """
        stats: dict = {}
        for col in df.columns:
            series = df[col]
            non_null = series.dropna()
            null_count = int(series.isna().sum())

            if pd.api.types.is_numeric_dtype(series):
                stats[col] = {
                    "type": str(series.dtype),
                    "min": _safe_json_value(non_null.min()) if len(non_null) else None,
                    "max": _safe_json_value(non_null.max()) if len(non_null) else None,
                    "nulls": null_count,
                }
            elif pd.api.types.is_datetime64_any_dtype(series):
                stats[col] = {
                    "type": str(series.dtype),
                    "min": str(non_null.min()) if len(non_null) else None,
                    "max": str(non_null.max()) if len(non_null) else None,
                    "nulls": null_count,
                }
            else:
                # Check if values are lists (ClickHouse Array columns → unhashable)
                first_val = non_null.iloc[0] if len(non_null) else None
                if isinstance(first_val, list):
                    # Flatten first 10 rows to extract unique sample values so the
                    # agent can see what's inside the array without needing python_analysis.
                    flat: list[str] = []
                    for lst in non_null.iloc[:10]:
                        if isinstance(lst, list):
                            flat.extend(str(v) for v in lst if v is not None and str(v).strip())
                    # Deduplicate while preserving order, cap at 5 unique values
                    seen: dict[str, None] = {}
                    for v in flat:
                        seen.setdefault(v, None)
                    sample_vals = [v[:50] + "…" if len(v) > 50 else v for v in list(seen)[:5]]
                    stats[col] = {
                        "type": "Array",
                        "avg_len": round(non_null.apply(len).mean(), 1) if len(non_null) else 0,
                        "max_len": int(non_null.apply(len).max()) if len(non_null) else 0,
                        "sample_values": sample_vals,
                        "nulls": null_count,
                    }
                else:
                    try:
                        raw_sample = non_null.value_counts().head(3).index.tolist()
                    except TypeError:
                        # Fallback for any other unhashable types
                        raw_sample = non_null.head(3).tolist()
                    # Truncate each value to avoid huge strings (e.g. long joined-path strings)
                    _MAX_SAMPLE_LEN = 150
                    sample = [
                        str(v)[:_MAX_SAMPLE_LEN] + ("…" if len(str(v)) > _MAX_SAMPLE_LEN else "")
                        for v in raw_sample
                    ]
                    stats[col] = {
                        "type": str(series.dtype),
                        "unique": int(non_null.nunique()),
                        "sample": sample,
                        "nulls": null_count,
                    }
        return stats

    def execute_query(self, sql: str, limit: int = 500000) -> dict:
        """
        Execute a read-only query against ClickHouse.
        1. Appends LIMIT if missing.
        2. Checks Parquet cache (keyed by MD5 of SQL) — returns cached result if fresh.
        3. If not cached: runs query, saves to Parquet, returns metadata + col_stats.

        Note: write-protection is enforced at the DB level (user has SELECT grants only).
        WITH/CTE queries (starting with WITH) and leading SQL comments are fully supported.
        """
        sql_stripped = sql.strip()

        # Auto-add LIMIT if missing
        if "LIMIT" not in sql_stripped.upper():
            sql_stripped = f"{sql_stripped.rstrip().rstrip(';')} LIMIT {limit}"

        # Parquet cache: keyed by SQL hash (no timestamp) so retries reuse the file
        query_hash = hashlib.md5(sql_stripped.encode()).hexdigest()[:10]
        parquet_path = str(TEMP_DIR / f"query_{query_hash}.parquet")
        p = Path(parquet_path)

        # ── Cache hit ───────────────────────────────────────────────────────
        if p.exists():
            age = time.time() - p.stat().st_mtime
            if age < TEMP_FILE_TTL_SECONDS:
                try:
                    df = pd.read_parquet(parquet_path)
                    return {
                        "success": True,
                        "cached": True,
                        "row_count": len(df),
                        "columns": list(df.columns),
                        "col_stats": self._build_col_stats(df),
                        "parquet_path": parquet_path,
                    }
                except Exception:
                    # Corrupted cache file — fall through to re-query
                    p.unlink(missing_ok=True)
            else:
                # Expired — delete and re-query
                p.unlink(missing_ok=True)

        # ── Cache miss: query ClickHouse ─────────────────────────────────────
        try:
            result = self.client.query(sql_stripped)
            df = pd.DataFrame(result.result_rows, columns=result.column_names)

            # Save to Parquet (preserves complex types like Array, Map, Decimal)
            df.to_parquet(parquet_path, engine="pyarrow", index=False)

            return {
                "success": True,
                "cached": False,
                "row_count": len(df),
                "columns": list(df.columns),
                "col_stats": self._build_col_stats(df),
                "parquet_path": parquet_path,
            }

        except Exception as exc:
            return {
                "success": False,
                "error": str(exc),
                "sql": sql_stripped,
            }
