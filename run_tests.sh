#!/usr/bin/env bash
# One-command acceptance test for the whole demo. Validates data layer, ABAC governance,
# metric views, Genie, the app REST API + OBO, Lakebase (query history), and the
# system-table audit source. Parameterized by env so it works on any deployed workspace.
#
# Usage:
#   ./run_tests.sh [--profile <name>] [--app-url <url>]
# Env overrides: DBX_CATALOG DBX_SCHEMA DBX_WAREHOUSE_ID DBX_GENIE_SPACE_ID APP_URL
#
# Prereq: the app must be reachable at APP_URL (local uvicorn or the deployed Databricks App).
set -uo pipefail
cd "$(dirname "$0")"

PROFILE="fevm1"
APP_URL_ARG=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --profile) PROFILE="$2"; shift 2;;
    --app-url) APP_URL_ARG="$2"; shift 2;;
    *) echo "unknown arg: $1"; exit 2;;
  esac
done

export DATABRICKS_CONFIG_PROFILE="$PROFILE"
export DBX_CATALOG="${DBX_CATALOG:-serverless_stable_tlm05u_catalog}"
export DBX_SCHEMA="${DBX_SCHEMA:-rd_security_demo}"
export DBX_WAREHOUSE_ID="${DBX_WAREHOUSE_ID:-884c290e9f7c6647}"
export DBX_GENIE_SPACE_ID="${DBX_GENIE_SPACE_ID:-01f1a5158c701816bffd353db3d9130c}"
export APP_URL="${APP_URL_ARG:-${APP_URL:-http://127.0.0.1:8077}}"

VENV="app/.venv"
if [[ ! -d "$VENV" ]]; then echo "Missing $VENV — set up the app venv first (see DEPLOYMENT.md)"; exit 1; fi
# shellcheck disable=SC1091
source "$VENV/bin/activate"
python -c "import pytest" 2>/dev/null || uv pip install -q pytest >/dev/null 2>&1

echo "=================================================================="
echo " RD Security Demo — acceptance tests"
echo "   profile=$PROFILE  app=$APP_URL"
echo "   $DBX_CATALOG.$DBX_SCHEMA  warehouse=$DBX_WAREHOUSE_ID"
echo "=================================================================="
pytest tests/ -v --tb=short -rA
