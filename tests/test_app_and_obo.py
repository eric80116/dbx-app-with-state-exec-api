"""App REST API + OBO execution validation (requires the app running at APP_URL)."""
import pytest
from conftest import FQ, app_client, require_app

C = app_client(timeout=60)


def test_health():
    require_app()
    r = C.get("/api/health")
    assert r.status_code == 200 and r.json()["status"] == "ok"


def test_me_identity():
    require_app()
    r = C.get("/api/me")
    assert r.status_code == 200
    assert "@" in r.json()["email"]


def test_objects_lists_metric_views():
    require_app()
    r = C.get("/api/objects")
    names = {o["name"] for o in r.json()["objects"]}
    assert {"mv_risk_behavior", "dim_employee"}.issubset(names)


def test_describe_has_columns_and_tags():
    require_app()
    r = C.get("/api/objects/dim_employee")
    cols = {c["name"]: c for c in r.json()["columns"]}
    assert "email" in cols
    # tag key varies per deploy (rd_data_class / data_classification / customer key) — just
    # assert the PII column carries at least one governed tag.
    assert cols["email"]["tags"], "email has no governed tags"


def test_query_obo_runs():
    require_app()
    r = C.post("/api/query", json={"sql": f"SELECT count(*) AS n FROM {FQ}.dim_employee"})
    assert r.status_code == 200
    body = r.json()
    assert int(body["rows"][0][0]) > 0
    assert "@" in body.get("executed_as", "")


def test_query_read_only_guard():
    require_app()
    r = C.post("/api/query", json={"sql": "DROP TABLE whatever"})
    assert r.status_code == 400
