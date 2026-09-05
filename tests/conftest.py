"""Shared test config, parameterized entirely by environment so the same suite
validates any workspace after deployment. See tests/run_tests.sh."""
import os
import pytest
from databricks.sdk import WorkspaceClient
from databricks.sdk.service.sql import StatementState

PROFILE = os.environ.get("DATABRICKS_CONFIG_PROFILE", "fevm1")
CATALOG = os.environ.get("DBX_CATALOG", "serverless_stable_tlm05u_catalog")
SCHEMA = os.environ.get("DBX_SCHEMA", "rd_security_demo")
WAREHOUSE = os.environ.get("DBX_WAREHOUSE_ID", "884c290e9f7c6647")
APP_URL = os.environ.get("APP_URL", "http://127.0.0.1:8077").rstrip("/")
GENIE_SPACE_ID = os.environ.get("DBX_GENIE_SPACE_ID", "01f1a5158c701816bffd353db3d9130c")
ANALYST = os.environ.get("DBX_ANALYST_PRINCIPAL", "eric.liou@databricks.com")

FQ = f"{CATALOG}.{SCHEMA}"


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
