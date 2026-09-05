"""Lakebase (Postgres Autoscaling) operational store for the app.

Holds low-latency, per-user app state that does NOT belong in system tables:
  * query_history  -- every SQL the user ran through the app (immediate; system.query.history
                      has a ~10-min lag, so this powers the "my recent queries" UI in real time)
  * saved_queries  -- SQL the user starred / saved (e.g. a Genie-suggested statement)

Auth: a short-lived Postgres OAuth credential minted via POST /api/2.0/postgres/credentials
(the Databricks SDK api_client, version-independent). In a deployed Databricks App the SDK
uses the app service principal (which owns the schema it creates on first run); locally it
uses DBX_LOCAL_DEV_PROFILE. Credentials last ~1h; we refresh every ~45 min.

If Lakebase is not configured (LAKEBASE_ENDPOINT/PGHOST unset), every function degrades to a
no-op / empty result so the app still runs without it.
"""
from __future__ import annotations
import os
import threading
import time
from typing import Any

_LOCK = threading.Lock()
_cred: dict[str, Any] = {"token": None, "ts": 0.0}

ENDPOINT = os.environ.get("LAKEBASE_ENDPOINT", "")   # projects/.../branches/production/endpoints/primary
PGHOST = os.environ.get("PGHOST", "")
PGDATABASE = os.environ.get("PGDATABASE", "databricks_postgres")
# Postgres role = the app's service principal. Databricks Apps inject DATABRICKS_CLIENT_ID
# (the app SP), so we default to it and never need to hardcode the SP id at deploy time.
PGUSER = os.environ.get("PGUSER") or os.environ.get("DATABRICKS_CLIENT_ID", "")
SCHEMA = os.environ.get("LAKEBASE_SCHEMA", "app_ops")
# Postgres table backing the hot-cache. Default: the app's own refreshed table; can point at a
# managed Lakebase synced table (e.g. public.mv_risk_behavior_daily) instead.
HOTCACHE_TABLE = os.environ.get("LAKEBASE_HOTCACHE_TABLE", f"{SCHEMA}.metric_hot_cache")
LOCAL_DEV_PROFILE = os.environ.get("DBX_LOCAL_DEV_PROFILE", "")


def enabled() -> bool:
    return bool(ENDPOINT and PGHOST and PGUSER)


def _wsc():
    from databricks.sdk import WorkspaceClient
    return WorkspaceClient(profile=LOCAL_DEV_PROFILE) if LOCAL_DEV_PROFILE else WorkspaceClient()


def _token() -> str:
    with _LOCK:
        if _cred["token"] and (time.time() - _cred["ts"] < 2700):
            return _cred["token"]
        resp = _wsc().api_client.do("POST", "/api/2.0/postgres/credentials", body={"endpoint": ENDPOINT})
        _cred["token"] = resp["token"]
        _cred["ts"] = time.time()
        return _cred["token"]


def _connect():
    import psycopg
    return psycopg.connect(host=PGHOST, dbname=PGDATABASE, user=PGUSER, password=_token(),
                           sslmode="require", connect_timeout=15)


def ensure_schema() -> None:
    """Create the app's schema + tables if absent. On a deployed app the service principal
    runs this on startup and thereby OWNS the schema (required by Lakebase's permission model)."""
    if not enabled():
        return
    stmts = [
        f"CREATE SCHEMA IF NOT EXISTS {SCHEMA}",
        f"""CREATE TABLE IF NOT EXISTS {SCHEMA}.query_history (
              id BIGSERIAL PRIMARY KEY, user_email TEXT NOT NULL, sql TEXT NOT NULL,
              source TEXT, row_count INT, status TEXT, error TEXT, statement_id TEXT,
              created_at TIMESTAMPTZ NOT NULL DEFAULT now())""",
        f"CREATE INDEX IF NOT EXISTS ix_qh_user_time ON {SCHEMA}.query_history (user_email, created_at DESC)",
        f"""CREATE TABLE IF NOT EXISTS {SCHEMA}.saved_queries (
              id BIGSERIAL PRIMARY KEY, user_email TEXT NOT NULL, title TEXT NOT NULL,
              sql TEXT NOT NULL, created_at TIMESTAMPTZ NOT NULL DEFAULT now())""",
    ]
    with _connect() as conn, conn.cursor() as cur:
        for s in stmts:
            cur.execute(s)
        conn.commit()


def log_query(user_email: str, sql: str, source: str, row_count: int | None,
              status: str, error: str | None, statement_id: str | None) -> None:
    if not enabled():
        return
    try:
        with _connect() as conn, conn.cursor() as cur:
            cur.execute(
                f"INSERT INTO {SCHEMA}.query_history "
                f"(user_email, sql, source, row_count, status, error, statement_id) VALUES (%s,%s,%s,%s,%s,%s,%s)",
                (user_email, sql, source, row_count, status, error, statement_id))
            conn.commit()
    except Exception:
        pass  # history logging must never break a query


def list_history(user_email: str, limit: int = 25) -> list[dict[str, Any]]:
    if not enabled():
        return []
    with _connect() as conn, conn.cursor() as cur:
        cur.execute(
            f"SELECT sql, source, row_count, status, statement_id, created_at "
            f"FROM {SCHEMA}.query_history WHERE user_email=%s ORDER BY created_at DESC LIMIT %s",
            (user_email, limit))
        return [{"sql": r[0], "source": r[1], "row_count": r[2], "status": r[3],
                 "statement_id": r[4], "created_at": r[5].isoformat()} for r in cur.fetchall()]


def save_query(user_email: str, title: str, sql: str) -> dict[str, Any]:
    if not enabled():
        raise RuntimeError("Lakebase is not configured")
    with _connect() as conn, conn.cursor() as cur:
        cur.execute(f"INSERT INTO {SCHEMA}.saved_queries (user_email, title, sql) VALUES (%s,%s,%s) RETURNING id",
                    (user_email, title, sql))
        new_id = cur.fetchone()[0]
        conn.commit()
        return {"id": new_id}


def hotcache_top(days: int = 7, limit: int = 10) -> dict[str, Any]:
    """Low-latency read of the pre-aggregation hot-cache (served from Postgres, not the warehouse)."""
    if not enabled():
        return {"enabled": False, "rows": []}
    try:
        with _connect() as conn, conn.cursor() as cur:
            cur.execute(
                f"SELECT employee_id, team, SUM(high_risk_events) hr, ROUND(AVG(avg_risk)::numeric,1) ar, "
                f"SUM(after_hours_events) ah FROM {HOTCACHE_TABLE} "
                f"WHERE event_date >= current_date - %s GROUP BY employee_id, team "
                f"ORDER BY hr DESC LIMIT %s", (days, limit))
            rows = [{"employee_id": r[0], "team": r[1], "high_risk_events": r[2],
                     "avg_risk": float(r[3]) if r[3] is not None else None, "after_hours_events": r[4]}
                    for r in cur.fetchall()]
        return {"enabled": True, "rows": rows}
    except Exception as e:
        return {"enabled": True, "rows": [], "error": str(e)[:200]}


def list_saved(user_email: str) -> list[dict[str, Any]]:
    if not enabled():
        return []
    with _connect() as conn, conn.cursor() as cur:
        cur.execute(f"SELECT id, title, sql, created_at FROM {SCHEMA}.saved_queries "
                    f"WHERE user_email=%s ORDER BY created_at DESC", (user_email,))
        return [{"id": r[0], "title": r[1], "sql": r[2], "created_at": r[3].isoformat()} for r in cur.fetchall()]
