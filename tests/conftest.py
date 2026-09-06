"""Shared test config, parameterized entirely by environment so the same suite
validates any workspace after deployment. run_tests.sh resolves and exports these —
there are deliberately NO workspace-specific defaults here (a blank means "not provided",
which fails loudly, rather than silently targeting some other workspace's resources)."""
import os
import httpx
import pytest
from databricks.sdk import WorkspaceClient
from databricks.sdk.service.sql import StatementState

PROFILE = os.environ.get("DATABRICKS_CONFIG_PROFILE", "")
CATALOG = os.environ.get("DBX_CATALOG", "")
SCHEMA = os.environ.get("DBX_SCHEMA", "rd_security_demo")
WAREHOUSE = os.environ.get("DBX_WAREHOUSE_ID", "")
APP_URL = os.environ.get("APP_URL", "http://127.0.0.1:8077").rstrip("/")
APP_TOKEN = os.environ.get("APP_TOKEN", "")
GENIE_SPACE_ID = os.environ.get("DBX_GENIE_SPACE_ID", "")
ANALYST = os.environ.get("DBX_ANALYST_PRINCIPAL", "")

FQ = f"{CATALOG}.{SCHEMA}"


def app_client(timeout=60):
    """httpx client for the app. A deployed Databricks App is platform-gated, so send the
    user's OAuth token as Bearer; the app then runs every query OBO as that user."""
    headers = {"Authorization": f"Bearer {APP_TOKEN}"} if APP_TOKEN else {}
    return httpx.Client(base_url=APP_URL, headers=headers, timeout=timeout)


_app_probe: dict = {}


def require_app():
    """Skip (not fail) app tests when the app isn't callable from a script. A DEPLOYED app
    that uses user-authorization (OBO) is fronted by the platform's OAuth gateway, which a
    script token can't satisfy — the gateway returns an HTML error page, not JSON. That's
    expected: exercise the deployed app in the browser (or with the UI's 'Copy token'),
    and run this suite fully against a LOCAL uvicorn where /api/health is directly reachable."""
    if "ok" not in _app_probe:
        try:
            r = app_client(timeout=15).get("/api/health")
            is_json = "json" in r.headers.get("content-type", "").lower()
            _app_probe["ok"] = r.status_code == 200 and is_json
            _app_probe["reason"] = f"app not script-callable (GET /api/health -> {r.status_code}, " \
                                   f"{r.headers.get('content-type', 'no content-type')})"
        except Exception as e:
            _app_probe["ok"] = False
            _app_probe["reason"] = f"app unreachable at {APP_URL}: {e}"
    if not _app_probe["ok"]:
        pytest.skip(_app_probe["reason"] + " — deployed OBO apps need the browser-forwarded "
                    "token; verify the UI manually or run against local uvicorn.")


@pytest.fixture(scope="session")
def w():
    return WorkspaceClient(profile=PROFILE)


@pytest.fixture(scope="session")
def sql(w):
    def _run(statement, row_limit=1000):
        r = w.statement_execution.execute_statement(
            warehouse_id=WAREHOUSE, statement=statement, wait_timeout="50s", row_limit=row_limit)
        while r.status and r.status.state in (StatementState.PENDING, StatementState.RUNNING):
            r = w.statement_execution.get_statement(r.statement_id)
        assert r.status.state == StatementState.SUCCEEDED, \
            f"SQL failed: {r.status.error.message if r.status.error else r.status.state}"
        return (r.result.data_array or []) if r.result else []
    return _run
