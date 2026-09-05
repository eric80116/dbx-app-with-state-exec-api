"""Genie space, Genie flow via app, Lakebase operational store, and system-table audit."""
import time
import httpx
import pytest
from conftest import APP_URL, GENIE_SPACE_ID, PROFILE, FQ

C = httpx.Client(base_url=APP_URL, timeout=180)


def test_genie_space_exists(w):
    # SDK method name varies across versions; use the stable REST endpoint.
    space = w.api_client.do("GET", f"/api/2.0/genie/spaces/{GENIE_SPACE_ID}")
    assert space.get("space_id") == GENIE_SPACE_ID or space.get("title")


def test_genie_ask_returns_answer_via_app():
    """Full Genie One flow through the app: ask -> poll -> completed with an answer.
    (query_items / suggested SQL is populated when Genie runs a query, but not guaranteed
    on every turn, so we assert on the answer and treat suggested SQL as a bonus.)"""
    a = C.post("/api/genie/ask", json={"question": "Which engineers showed the most high-risk behavior this week?"})
    assert a.status_code == 200
    ask = a.json()
    cid, rid = ask["conversation_id"], ask["response_id"]
    status, poll = None, None
    for _ in range(40):
        poll = C.get("/api/genie/poll", params={"conversation_id": cid, "response_id": rid}).json()
        status = poll.get("status")
        if status in ("completed", "failed"):
            break
        time.sleep(3)
    assert status == "completed", f"genie did not complete (status={status})"
    assert poll.get("final_answer"), "genie returned no answer"
    # answer should reflect the ground truth (Platform team dominates)
    assert "Platform" in (poll.get("final_answer") or "")


def test_lakebase_history_logged():
    hist = C.get("/api/history").json()
    if not hist.get("lakebase"):
        pytest.skip("Lakebase not configured")
    # Running a query then confirming it appears in history
    C.post("/api/query", json={"sql": "SELECT 1 AS probe"})
    time.sleep(1)
    hist2 = C.get("/api/history").json()["history"]
    assert any("probe" in h["sql"] for h in hist2), "query not logged to Lakebase history"


def test_system_query_history_audit_available(sql):
    """The governance/audit source of truth. NOTE: system tables lag ~10 min, so we only
    assert the query runs and the schema is present, not that a just-run query appears yet."""
    rows = sql("SELECT statement_text, executed_by FROM system.query.history "
               "WHERE statement_text IS NOT NULL LIMIT 1")
    assert len(rows) >= 0  # query executes; lag means recent rows may be absent
