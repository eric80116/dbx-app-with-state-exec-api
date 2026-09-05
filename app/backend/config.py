"""Runtime config, sourced from environment (set via app.yaml on Databricks Apps,
or a local .env / shell for dev)."""
import os

# Databricks workspace host, e.g. https://xxx.cloud.databricks.com
# The Apps runtime injects DATABRICKS_HOST sometimes WITHOUT a scheme; the SDK tolerates
# that but our raw httpx MCP calls need a full URL, so normalize it here.
DATABRICKS_HOST = os.environ.get("DATABRICKS_HOST", "").rstrip("/")
if DATABRICKS_HOST and not DATABRICKS_HOST.startswith("http"):
    DATABRICKS_HOST = "https://" + DATABRICKS_HOST

# Serverless SQL warehouse used for Statement Execution (OBO).
WAREHOUSE_ID = os.environ.get("DBX_WAREHOUSE_ID", "")

# The governed demo data location.
CATALOG = os.environ.get("DBX_CATALOG", "serverless_stable_tlm05u_catalog")
SCHEMA = os.environ.get("DBX_SCHEMA", "rd_security_demo")

# Optional curated Genie space id (the "certified" path). Genie One (no space) is primary.
GENIE_SPACE_ID = os.environ.get("DBX_GENIE_SPACE_ID", "")

# Statement Execution limits
MAX_ROWS = int(os.environ.get("DBX_MAX_ROWS", "500"))
STATEMENT_TIMEOUT = os.environ.get("DBX_STATEMENT_TIMEOUT", "50s")

# Local-dev fallback: when the app is NOT running on Databricks Apps there is no
# X-Forwarded-Access-Token header. If set, use this profile's CLI creds so the app
# is runnable locally. NEVER set this in production (deployed apps use OBO headers).
LOCAL_DEV_PROFILE = os.environ.get("DBX_LOCAL_DEV_PROFILE", "")


def require(name: str, value: str) -> str:
    if not value:
        raise RuntimeError(f"Missing required config: {name}")
    return value
