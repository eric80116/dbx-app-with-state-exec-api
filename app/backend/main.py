"""FastAPI backend for the RD Security Investigation app.

Serves the React UI *and* a documented REST API. Both the UI's "Run" button and the
public /api/query endpoint go through the SAME OBO execution path (dbx.run_sql), so a
SQL statement behaves identically whether run from the browser or called programmatically
with the user's own token. Unity Catalog enforces per-user grants, masks, and row filters.
"""
from __future__ import annotations
import os
import re

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import auth, config, dbx, lakebase

app = FastAPI(title="RD Security Investigation", version="1.0")


@app.on_event("startup")
def _startup():
    try:
        lakebase.ensure_schema()
    except Exception as e:  # app still runs without Lakebase
        print(f"[lakebase] ensure_schema skipped: {e}")

_READ_ONLY = re.compile(r"^\s*(with|select|show|describe|desc|explain)\b", re.IGNORECASE)


class QueryIn(BaseModel):
    sql: str
    row_limit: int | None = None


class AskIn(BaseModel):
    question: str
    conversation_id: str | None = None
    scope: str = "security"   # "security" = curated Genie space (fast); "workspace" = Genie One (broad)


class SaveIn(BaseModel):
    title: str
    sql: str


@app.exception_handler(RuntimeError)
async def _runtime_err(_: Request, exc: RuntimeError):
    return JSONResponse(status_code=400, content={"error": str(exc)})


@app.get("/api/health")
def health():
    return {"status": "ok", "catalog": config.CATALOG, "schema": config.SCHEMA}


@app.get("/api/me")
def me(request: Request):
    ident = auth.get_identity(request)
    return {"email": ident.email, "auth_source": ident.source,
            "catalog": config.CATALOG, "schema": config.SCHEMA,
            "warehouse_id": config.WAREHOUSE_ID, "host": config.DATABRICKS_HOST}


@app.get("/api/objects")
def list_objects(request: Request):
    ident = auth.get_identity(request)
    return {"objects": dbx.list_objects(ident.token)}


@app.get("/api/objects/{name}")
def describe(request: Request, name: str):
    ident = auth.get_identity(request)
    return dbx.describe_object(ident.token, name)


@app.post("/api/query")
def query(request: Request, body: QueryIn):
    """Execute SQL as the calling user (OBO). Read-only statements only.
    This is the shared execution path used by the UI and by external API callers."""
    ident = auth.get_identity(request)
    if not _READ_ONLY.match(body.sql or ""):
        raise HTTPException(status_code=400,
                            detail="Only read-only statements (SELECT/WITH/SHOW/DESCRIBE/EXPLAIN) are allowed.")
    source = "api" if ident.source == "bearer" else "ui"
    try:
        result = dbx.run_sql(ident.token, body.sql, body.row_limit)
    except Exception as e:
        lakebase.log_query(ident.email, body.sql, source, None, "error", str(e)[:500], None)
        raise
    lakebase.log_query(ident.email, body.sql, source, result["row_count"], "ok", None, result.get("statement_id"))
    result["executed_as"] = ident.email
    return result


@app.get("/api/history")
def history(request: Request):
    ident = auth.get_identity(request)
    return {"history": lakebase.list_history(ident.email), "lakebase": lakebase.enabled()}


@app.get("/api/saved")
def saved(request: Request):
    ident = auth.get_identity(request)
    return {"saved": lakebase.list_saved(ident.email), "lakebase": lakebase.enabled()}


@app.post("/api/saved")
def save(request: Request, body: SaveIn):
    ident = auth.get_identity(request)
    return lakebase.save_query(ident.email, body.title, body.sql)


@app.get("/api/hotcache")
def hotcache(request: Request, days: int = 7, limit: int = 10):
    """Low-latency top-risk list served from the Lakebase pre-aggregation cache."""
    auth.get_identity(request)  # require auth
    return lakebase.hotcache_top(days, limit)


@app.post("/api/genie/ask")
def genie_ask(request: Request, body: AskIn):
    ident = auth.get_identity(request)
    return dbx.genie_ask(ident.token, body.question, body.conversation_id, body.scope)


@app.get("/api/genie/poll")
def genie_poll(request: Request, conversation_id: str, response_id: str, scope: str = "security"):
    ident = auth.get_identity(request)
    return dbx.genie_poll(ident.token, conversation_id, response_id, scope)


@app.get("/api/token")
def token(request: Request):
    """Return the caller's OWN short-lived access token so a copied API snippet is runnable
    without hunting for a token. It is the caller's own OBO/bearer token — returning it to its
    owner grants no new access; it is short-lived and scoped to this app's user authorization."""
    ident = auth.get_identity(request)
    return {"token": ident.token, "email": ident.email,
            "note": "short-lived; your own token, scoped to this app's OBO scopes"}


# --- static SPA (built React app) -------------------------------------------
_STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")
if os.path.isdir(_STATIC_DIR):
    app.mount("/", StaticFiles(directory=_STATIC_DIR, html=True), name="static")
