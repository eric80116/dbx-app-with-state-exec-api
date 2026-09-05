"""Databricks access helpers, all executed On-Behalf-Of the calling user (OBO).

Every function takes the caller's access token and acts strictly as that user, so
Unity Catalog grants, ABAC column masks, and row filters are enforced per request.
Two capabilities:
  * Statement Execution API  -> run_sql / catalog listing (SQL warehouse)
  * Genie One managed MCP     -> genie_ask / genie_poll / genie_get_result
"""
from __future__ import annotations
import json
import time
from typing import Any

import httpx
from databricks.sdk import WorkspaceClient
from databricks.sdk.service.sql import StatementState

from . import config

# --------------------------------------------------------------------------- SQL


def _client(token: str) -> WorkspaceClient:
    return WorkspaceClient(host=config.DATABRICKS_HOST, token=token, auth_type="pat")


def run_sql(token: str, sql: str, row_limit: int | None = None) -> dict[str, Any]:
    """Execute SQL via the Statement Execution API as the user. Returns
    {columns: [{name,type}], rows: [[...]], row_count, truncated, statement_id}."""
    w = _client(token)
    limit = row_limit or config.MAX_ROWS
    resp = w.statement_execution.execute_statement(
        warehouse_id=config.WAREHOUSE_ID,
        statement=sql,
        wait_timeout=config.STATEMENT_TIMEOUT,
        row_limit=limit,
    )
    stmt_id = resp.statement_id
    # Poll to terminal state if still running.
    while resp.status and resp.status.state in (StatementState.PENDING, StatementState.RUNNING):
        time.sleep(1)
        resp = w.statement_execution.get_statement(stmt_id)

    state = resp.status.state if resp.status else None
    if state != StatementState.SUCCEEDED:
        err = resp.status.error.message if (resp.status and resp.status.error) else f"state={state}"
        raise RuntimeError(err)

    cols = []
    if resp.manifest and resp.manifest.schema and resp.manifest.schema.columns:
        cols = [{"name": c.name, "type": c.type_text} for c in resp.manifest.schema.columns]
    rows = (resp.result.data_array if resp.result and resp.result.data_array else []) or []
    truncated = bool(resp.manifest.truncated) if resp.manifest else False
    return {
        "columns": cols,
        "rows": rows,
        "row_count": len(rows),
        "truncated": truncated,
        "statement_id": stmt_id,
    }


# ----------------------------------------------------------------------- catalog


def list_objects(token: str) -> list[dict[str, Any]]:
    """Tables + views + metric views the user can see in the demo schema, with comments."""
    # Exclude DLT/synced-table pipeline artifacts (e.g. event_log_<uuid>) — not part of the model.
    sql = f"""
      SELECT table_name, table_type, comment
      FROM {config.CATALOG}.information_schema.tables
      WHERE table_schema = '{config.SCHEMA}'
        AND table_name NOT LIKE 'event_log%'
      ORDER BY table_name
    """
    out = run_sql(token, sql, row_limit=200)
    return [
        {"name": r[0], "type": r[1], "comment": r[2]}
        for r in out["rows"]
    ]


def describe_object(token: str, table_name: str) -> dict[str, Any]:
    """Columns (with comments) + governed tags for one object."""
    # Guard the identifier: only allow objects that exist in the demo schema.
    safe = table_name.replace("`", "")
    cols = run_sql(token, f"""
      SELECT column_name, full_data_type, comment
      FROM {config.CATALOG}.information_schema.columns
      WHERE table_schema = '{config.SCHEMA}' AND table_name = '{safe}'
      ORDER BY ordinal_position
    """, row_limit=500)
    tags = run_sql(token, f"""
      SELECT column_name, tag_name, tag_value
      FROM {config.CATALOG}.information_schema.column_tags
      WHERE schema_name = '{config.SCHEMA}' AND table_name = '{safe}'
    """, row_limit=500)
    tag_map: dict[str, list[str]] = {}
    for c, k, v in tags["rows"]:
        tag_map.setdefault(c, []).append(f"{k}={v}" if v else k)
    return {
        "name": table_name,
        "columns": [
            {"name": r[0], "type": r[1], "comment": r[2], "tags": tag_map.get(r[0], [])}
            for r in cols["rows"]
        ],
    }


# ------------------------------------------------------------------- Genie (MCP)

_MCP_GENIE_ONE = "/api/2.0/mcp/genie"


def _parse_sse_or_json(text: str) -> dict[str, Any]:
    """MCP streamable-HTTP returns either JSON or an SSE stream ('data: {...}')."""
    result: dict[str, Any] = {}
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("data:"):
            line = line[len("data:"):].strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict) and ("result" in obj or "error" in obj):
            result = obj
    return result


def _mcp_call(token: str, tool: str, arguments: dict[str, Any], path: str = _MCP_GENIE_ONE) -> dict[str, Any]:
    """One stateless MCP tool call: initialize -> initialized -> tools/call."""
    url = config.DATABRICKS_HOST + path
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }
    with httpx.Client(timeout=120) as c:
        init = c.post(url, headers=headers, json={
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                       "clientInfo": {"name": "rd-security-app", "version": "1.0"}},
        })
        init.raise_for_status()
        sess = init.headers.get("Mcp-Session-Id") or init.headers.get("mcp-session-id")
        if sess:
            headers["Mcp-Session-Id"] = sess
        c.post(url, headers=headers, json={"jsonrpc": "2.0", "method": "notifications/initialized"})
        resp = c.post(url, headers=headers, json={
            "jsonrpc": "2.0", "id": 2, "method": "tools/call",
            "params": {"name": tool, "arguments": arguments},
        })
        resp.raise_for_status()
        parsed = _parse_sse_or_json(resp.text)
    if "error" in parsed:
        raise RuntimeError(parsed["error"].get("message", str(parsed["error"])))
    return parsed.get("result", {})


def _content_text(result: dict[str, Any]) -> str:
    parts = []
    for item in result.get("content", []) or []:
        if item.get("type") == "text":
            parts.append(item.get("text", ""))
    return "\n".join(parts)


# Positioning: bias every ask toward "which tables/columns + a reference SQL statement".
_PREAMBLE = ("For the analysis below, recommend which tables and columns to use and provide a "
             "reference SQL statement, then briefly answer. Analysis: ")


def _norm_status(s: Any) -> str:
    s = str(s or "").lower()
    if s in ("completed", "complete", "succeeded", "success"):
        return "completed"
    if s in ("failed", "error", "cancelled", "canceled"):
        return "failed"
    return "in_progress"


def _use_space(scope: str) -> str:
    """Return a space_id to route to the curated Genie space, or '' for workspace-wide Genie One."""
    return config.GENIE_SPACE_ID if (scope == "security" and config.GENIE_SPACE_ID) else ""


def _flatten_rows(data: Any) -> list:
    """Coerce Genie result rows to plain string arrays [[v, ...]] regardless of source format
    (Statement Execution data_array is already [[...]]; Genie returns [{values:[{string_value:v}]}])."""
    out = []
    for row in data or []:
        if isinstance(row, list):
            out.append(row)
        elif isinstance(row, dict) and "values" in row:
            out.append([(next(iter(v.values())) if isinstance(v, dict) else v) for v in row["values"]])
        else:
            out.append(row)
    return out


def genie_ask(token: str, question: str, conversation_id: str | None = None,
              scope: str = "security") -> dict[str, Any]:
    """scope='security' -> curated Genie space (fast, scoped); 'workspace' -> Genie One (broad)."""
    q = _PREAMBLE + question
    sid = _use_space(scope)
    if sid:  # curated space MCP: query_space_<id> / poll_response_<id>
        args: dict[str, Any] = {"query": q}
        if conversation_id:
            args["conversation_id"] = conversation_id
        res = _mcp_call(token, f"query_space_{sid}", args, path=f"{_MCP_GENIE_ONE}/{sid}")
        sc = res.get("structuredContent", {}) or {}
        return {"conversation_id": sc.get("conversationId"), "response_id": sc.get("messageId"),
                "status": _norm_status(sc.get("status")), "scope": "security"}
    args = {"question": q}
    if conversation_id:
        args["conversation_id"] = conversation_id
    res = _mcp_call(token, "genie_ask", args)
    sc = res.get("structuredContent", {}) or {}
    return {"conversation_id": sc.get("conversation_id"), "response_id": sc.get("response_id"),
            "status": _norm_status(sc.get("status")), "scope": "workspace"}


def genie_poll(token: str, conversation_id: str, response_id: str,
               scope: str = "security") -> dict[str, Any]:
    """Normalized result: {status, final_answer, deep_link, query_items[{sql}], progress_steps,
    columns, rows}. Result rows are included when available so the UI can chart the answer."""
    sid = _use_space(scope)
    if sid:
        res = _mcp_call(token, f"poll_response_{sid}",
                        {"conversation_id": conversation_id, "message_id": response_id},
                        path=f"{_MCP_GENIE_ONE}/{sid}")
        sc = res.get("structuredContent", {}) or {}
        content = sc.get("content", {}) or {}
        texts = content.get("textAttachments") or []
        qas = content.get("queryAttachments") or []
        query_items, columns, rows = [], None, None
        for i, qa in enumerate(qas):
            query_items.append({"item_id": str(i), "sql": qa.get("query")})
            sr = qa.get("statement_response") or {}
            man, r = sr.get("manifest") or {}, sr.get("result") or {}
            if man.get("schema"):
                columns = [{"name": c["name"], "type": c.get("type_text")}
                           for c in man["schema"].get("columns", [])]
            if r.get("data_array") is not None:
                rows = _flatten_rows(r["data_array"])
        return {"status": _norm_status(sc.get("status")),
                "final_answer": "\n\n".join(t for t in texts if t) or None,
                "deep_link": None, "query_items": query_items,
                "progress_steps": [], "columns": columns, "rows": rows}

    # Genie One
    res = _mcp_call(token, "genie_poll_response",
                    {"conversation_id": conversation_id, "response_id": response_id})
    sc = res.get("structuredContent", {}) or {}
    out = {"status": _norm_status(sc.get("status")), "final_answer": sc.get("final_answer"),
           "deep_link": sc.get("deep_link"), "query_items": sc.get("query_items", []),
           "progress_steps": sc.get("progress_steps", []), "columns": None, "rows": None}
    # fetch rows for the last query item so the UI can chart the answer
    if out["status"] == "completed" and out["query_items"]:
        try:
            item_id = out["query_items"][-1].get("item_id")
            gr = _mcp_call(token, "genie_get_query_result",
                           {"conversation_id": conversation_id, "response_id": response_id, "item_id": item_id})
            grsc = gr.get("structuredContent", {}) or {}
            # genie_get_query_result returns top-level columns[{name,type_text}] + rows[[...]]
            if grsc.get("columns"):
                out["columns"] = [{"name": c["name"], "type": c.get("type_text")} for c in grsc["columns"]]
            if grsc.get("rows") is not None:
                out["rows"] = _flatten_rows(grsc["rows"])
        except Exception:
            pass
    return out
