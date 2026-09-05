"""App REST API + OBO execution validation (requires the app running at APP_URL)."""
import httpx
import pytest
from conftest import APP_URL, FQ

C = httpx.Client(base_url=APP_URL, timeout=60)


def test_health():
    r = C.get("/api/health")
    assert r.status_code == 200 and r.json()["status"] == "ok"


def test_me_identity():
    r = C.get("/api/me")
    assert r.status_code == 200
    assert "@" in r.json()["email"]


def test_objects_lists_metric_views():
    r = C.get("/api/objects")
    names = {o["name"] for o in r.json()["objects"]}
    assert {"mv_risk_behavior", "dim_employee"}.issubset(names)


def test_describe_has_columns_and_tags():
    r = C.get("/api/objects/dim_employee")
    cols = {c["name"]: c for c in r.json()["columns"]}
    assert "email" in cols
    assert any("data_classification" in t for t in cols["email"]["tags"])


def test_query_obo_runs():
    r = C.post("/api/query", json={"sql": f"SELECT count(*) AS n FROM {FQ}.dim_employee"})
    assert r.status_code == 200
    body = r.json()
    assert int(body["rows"][0][0]) > 0
    assert "@" in body.get("executed_as", "")


def test_query_read_only_guard():
    r = C.post("/api/query", json={"sql": "DROP TABLE whatever"})
    assert r.status_code == 400


def test_hotcache_served_from_lakebase():
    r = C.get("/api/hotcache", params={"days": 7, "limit": 5})
    body = r.json()
    if not body.get("enabled"):
        pytest.skip("Lakebase not configured")
    assert len(body["rows"]) > 0
    # top of the recent hot-cache should be Platform (the insiders)
    assert body["rows"][0]["team"] == "Platform"
