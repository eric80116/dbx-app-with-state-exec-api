"""Teardown verification — asserts every resource setup.sh created is ACTUALLY gone.

Run this AFTER bootstrap/teardown.sh to confirm nothing is left billing. Cost-critical
resources (Lakebase project compute, the App compute) get their own explicit checks.

Parameterized by env (see verify_teardown.sh):
  DATABRICKS_CONFIG_PROFILE, DBX_CATALOG, DBX_SCHEMA, DBX_WAREHOUSE_ID, APP_NAME,
  LB_PROJECT, TAG_KEY, DROP_CATALOG (1/0), DROP_TAG (1/0)
"""
import json
import os
import subprocess

import pytest

PROFILE = os.environ.get("DATABRICKS_CONFIG_PROFILE", "fevm")
CATALOG = os.environ.get("DBX_CATALOG", "rd_security_demo_cat")
SCHEMA = os.environ.get("DBX_SCHEMA", "rd_security_demo")
WAREHOUSE = os.environ.get("DBX_WAREHOUSE_ID", "")
APP_NAME = os.environ.get("APP_NAME", "rd-security-investigation")
LB_PROJECT = os.environ.get("LB_PROJECT", "rd-security-demo")
LB_UC_CATALOG = "lakebase_" + LB_PROJECT.replace("-", "_")
TAG_KEY = os.environ.get("TAG_KEY", "rd_data_class")
GENIE_TITLE = "RD Security Investigation"
DROP_CATALOG = os.environ.get("DROP_CATALOG", "0") == "1"
DROP_TAG = os.environ.get("DROP_TAG", "0") == "1"


def cli(*args, want_json=True):
    """Run the databricks CLI; return (returncode, parsed_or_text)."""
    cmd = ["databricks", *args, "--profile", PROFILE]
    if want_json:
        cmd += ["-o", "json"]
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    out = (p.stdout or "") + (p.stderr or "")
    if want_json and p.returncode == 0:
        try:
            return p.returncode, json.loads(p.stdout)
        except Exception:
            return p.returncode, out
    return p.returncode, out


def sql_scalar(statement):
    """Run one SQL via aitools; return stdout text (raises via returncode check by caller)."""
    p = subprocess.run(
        ["databricks", "experimental", "aitools", "tools", "query", statement, "--profile", PROFILE],
        capture_output=True, text=True, timeout=120)
    return p.returncode, (p.stdout or "") + (p.stderr or "")


# --------------------------------------------------------------- compute (cost)
def test_app_deleted():
    """The Databricks App (and its compute + service principal) must be gone."""
    rc, out = cli("apps", "get", APP_NAME, want_json=False)
    assert rc != 0 and ("does not exist" in out or "deleted" in out or "NOT_FOUND" in out), \
        f"App '{APP_NAME}' still exists (still billing compute):\n{out[:300]}"


def test_lakebase_project_deleted():
    """The Lakebase project (Postgres compute + storage) must be gone — the biggest cost risk."""
    rc, data = cli("postgres", "list-projects")
    assert rc == 0, f"could not list Lakebase projects: {data}"
    ids = [p.get("project_id") for p in (data if isinstance(data, list) else data.get("projects", []))]
    assert LB_PROJECT not in ids, f"Lakebase project '{LB_PROJECT}' still exists (still billing): {ids}"


def test_lakebase_endpoint_gone():
    """No reachable endpoint for the project (defense-in-depth vs. project check)."""
    rc, out = cli("postgres", "get-endpoint",
                  f"projects/{LB_PROJECT}/branches/production/endpoints/primary", want_json=False)
    assert rc != 0, f"Lakebase endpoint for '{LB_PROJECT}' still resolves:\n{out[:200]}"


# ------------------------------------------------------------------ governance
def test_genie_space_deleted():
    rc, data = cli("genie", "list-spaces")
    assert rc == 0, f"could not list Genie spaces: {data}"
    spaces = data.get("spaces", data) if isinstance(data, dict) else data
    titles = [s.get("title") for s in (spaces or [])]
    assert GENIE_TITLE not in titles, f"Genie space '{GENIE_TITLE}' still exists: {titles}"


def test_lakebase_uc_catalog_deleted():
    """The Lakebase-backed UC catalog (from any managed synced table) must be gone."""
    rc, out = cli("catalogs", "get", LB_UC_CATALOG, want_json=False)
    assert rc != 0 and ("does not exist" in out or "NOT_FOUND" in out or "DOES_NOT_EXIST" in out), \
        f"Lakebase UC catalog '{LB_UC_CATALOG}' still exists:\n{out[:200]}"


def test_demo_schema_dropped():
    """The demo schema (and its tables/metric views/policies/functions) must be gone."""
    if DROP_CATALOG:
        pytest.skip("catalog dropped — covered by test_catalog_dropped")
    rc, out = sql_scalar(
        f"SELECT count(*) AS n FROM {CATALOG}.information_schema.schemata WHERE schema_name = '{SCHEMA}'")
    assert rc == 0, f"could not query information_schema (catalog may be gone): {out[:200]}"
    assert '"n": "0"' in out or '"n":0' in out or '"n": 0' in out, \
        f"schema {CATALOG}.{SCHEMA} still exists:\n{out[:200]}"


def test_catalog_dropped():
    if not DROP_CATALOG:
        pytest.skip("--drop-catalog not requested; catalog intentionally kept")
    rc, out = cli("catalogs", "get", CATALOG, want_json=False)
    assert rc != 0 and ("does not exist" in out or "NOT_FOUND" in out or "DOES_NOT_EXIST" in out), \
        f"catalog '{CATALOG}' still exists:\n{out[:200]}"


def test_governed_tag_dropped():
    if not DROP_TAG:
        pytest.skip("--drop-tag not requested; governed tag intentionally kept (account-level)")
    rc, data = cli("tag-policies", "list-tag-policies")
    assert rc == 0, f"could not list tag policies: {data}"
    keys = [p.get("tag_key") for p in (data if isinstance(data, list) else data.get("tag_policies", []))]
    assert TAG_KEY not in keys, f"governed tag '{TAG_KEY}' still exists: {keys}"
