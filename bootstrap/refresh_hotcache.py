"""Refresh the Lakebase metric hot-cache (the Cube-style pre-aggregation cache).

Reads the pre-aggregated Delta table (serverless SQL) and materializes it into a plain
Lakebase Postgres table `app_ops.metric_hot_cache` for low-latency serving by the app.

This is the portable hot-cache path. The *managed* alternative — a Lakebase synced table
(`databricks postgres create-synced-table`) — needs the Lakebase DB registered as a UC
catalog (CREATE CATALOG on the metastore); use that when you have the permission. See docs.

Run on a schedule (DAB job) to keep the cache fresh. Idempotent (full refresh).

Env: DATABRICKS_CONFIG_PROFILE (or app SP creds), LAKEBASE_ENDPOINT, PGHOST, PGUSER,
     PGDATABASE (default databricks_postgres). Args: --catalog --schema [--warehouse].
"""
from __future__ import annotations
import argparse
import os

import psycopg
from databricks.sdk import WorkspaceClient
from databricks.sdk.service.sql import StatementState

SCHEMA_PG = os.environ.get("LAKEBASE_SCHEMA", "app_ops")
SOURCE = "mv_risk_behavior_daily"


def _pg_token(w: WorkspaceClient, endpoint: str) -> str:
    return w.api_client.do("POST", "/api/2.0/postgres/credentials", body={"endpoint": endpoint})["token"]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--catalog", required=True)
    ap.add_argument("--schema", required=True)
    ap.add_argument("--warehouse", default=os.environ.get("DBX_WAREHOUSE_ID", ""))
    ap.add_argument("--profile", default=os.environ.get("DATABRICKS_CONFIG_PROFILE"))
    args = ap.parse_args()

    w = WorkspaceClient(profile=args.profile) if args.profile else WorkspaceClient()

    # 1) read the pre-aggregation from UC via Statement Execution
    resp = w.statement_execution.execute_statement(
        warehouse_id=args.warehouse,
        statement=f"SELECT employee_id, event_date, team, access_events, high_risk_events, "
                  f"avg_risk, after_hours_events FROM {args.catalog}.{args.schema}.{SOURCE}",
        wait_timeout="50s", row_limit=100000,
    )
    while resp.status and resp.status.state in (StatementState.PENDING, StatementState.RUNNING):
        resp = w.statement_execution.get_statement(resp.statement_id)
    if resp.status.state != StatementState.SUCCEEDED:
        raise SystemExit(f"source read failed: {resp.status.state}")
    rows = resp.result.data_array or []
    print(f"read {len(rows)} pre-agg rows from UC")

    # 2) materialize into Lakebase
    endpoint = os.environ["LAKEBASE_ENDPOINT"]
    conn = psycopg.connect(host=os.environ["PGHOST"], dbname=os.environ.get("PGDATABASE", "databricks_postgres"),
                           user=os.environ["PGUSER"], password=_pg_token(w, endpoint), sslmode="require")
    with conn, conn.cursor() as cur:
        cur.execute(f"CREATE SCHEMA IF NOT EXISTS {SCHEMA_PG}")
        cur.execute(f"""CREATE TABLE IF NOT EXISTS {SCHEMA_PG}.metric_hot_cache (
            employee_id TEXT, event_date DATE, team TEXT, access_events INT,
            high_risk_events INT, avg_risk DOUBLE PRECISION, after_hours_events INT,
            PRIMARY KEY (employee_id, event_date))""")
        cur.execute(f"TRUNCATE {SCHEMA_PG}.metric_hot_cache")
        with cur.copy(f"COPY {SCHEMA_PG}.metric_hot_cache "
                      f"(employee_id, event_date, team, access_events, high_risk_events, avg_risk, after_hours_events) "
                      f"FROM STDIN") as cp:
            for r in rows:
                cp.write_row(r)
        cur.execute(f"SELECT count(*) FROM {SCHEMA_PG}.metric_hot_cache")
        print(f"hot-cache now has {cur.fetchone()[0]} rows")
    conn.close()


if __name__ == "__main__":
    main()
