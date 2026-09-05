#!/usr/bin/env bash
# Verify a teardown fully removed the demo — run AFTER bootstrap/teardown.sh.
# Asserts every resource is gone (App, Lakebase project/endpoint, Genie space, Lakebase UC
# catalog, demo schema; catalog + governed tag if you tore those down too). Cost-critical.
#
# Usage:
#   ./verify_teardown.sh --profile <p> --catalog <c> [--dropped-catalog] [--dropped-tag] [options]
# Pass --dropped-catalog / --dropped-tag ONLY if you ran teardown with --drop-catalog / --drop-tag.
set -uo pipefail
cd "$(dirname "$0")"

PROFILE="fevm"; export DBX_CATALOG="${DBX_CATALOG:-rd_security_demo_cat}"
export DBX_SCHEMA="${DBX_SCHEMA:-rd_security_demo}"
export APP_NAME="${APP_NAME:-rd-security-investigation}"
export LB_PROJECT="${LB_PROJECT:-rd-security-demo}"
export TAG_KEY="${TAG_KEY:-rd_data_class}"
export DROP_CATALOG="0"; export DROP_TAG="0"
while [[ $# -gt 0 ]]; do case "$1" in
  --profile) PROFILE="$2"; shift 2;;
  --catalog) export DBX_CATALOG="$2"; shift 2;;
  --schema) export DBX_SCHEMA="$2"; shift 2;;
  --app-name) export APP_NAME="$2"; shift 2;;
  --lakebase-project) export LB_PROJECT="$2"; shift 2;;
  --tag-key) export TAG_KEY="$2"; shift 2;;
  --dropped-catalog) export DROP_CATALOG="1"; shift;;
  --dropped-tag) export DROP_TAG="1"; shift;;
  *) echo "unknown arg: $1"; exit 2;;
esac; done
export DATABRICKS_CONFIG_PROFILE="$PROFILE"

VENV="app/.venv"; [[ -d "$VENV" ]] || { echo "missing $VENV"; exit 1; }
# shellcheck disable=SC1091
source "$VENV/bin/activate"
python -c "import pytest" 2>/dev/null || uv pip install -q pytest >/dev/null 2>&1

echo "=================================================================="
echo " Teardown verification — profile=$PROFILE  catalog=$DBX_CATALOG"
echo "   drop-catalog=$DROP_CATALOG  drop-tag=$DROP_TAG"
echo "=================================================================="
pytest tests/test_teardown.py -v --tb=short -rA
