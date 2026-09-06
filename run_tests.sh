#!/usr/bin/env bash
# One-command acceptance test for a DEPLOYED demo. Validates the data layer, ABAC governance,
# metric views, Genie, the app REST API + OBO, Lakebase (query history), and the system-table
# audit source. Everything is resolved from the target workspace — no baked-in defaults.
#
# Usage:
#   ./run_tests.sh --profile <name> --catalog <name> --app-url <url> [options]
#
# Options (auto-resolved from the workspace if omitted):
#   --schema <name>        Demo schema                         [rd_security_demo]
#   --warehouse <id>       SQL warehouse for the SQL checks    [first serverless warehouse]
#   --genie-space <id>     Curated Genie space id              [looked up by title]
#   --analyst <principal>  Analyst persona (for the plaintext check) [current user]
#   --token <token>        Bearer token for the app calls      [databricks auth token]
#   --tag-key <key>        Self-created governed tag key (if you deployed --create-tags)
#   --pii-tag key=value    Existing PII tag (if you deployed --pii-tag/--sens-tag)
#   --sens-tag key=value   Existing sensitivity tag
#   (tag flags are optional — omit them and the governance check is key-agnostic)
#
# The app checks need a token: a deployed Databricks App is gated by the platform, so a raw
# HTTP client must send `Authorization: Bearer <token>`. We mint one from the profile.
# NOTE: this runs the ACCEPTANCE suite only; teardown verification is verify_teardown.sh.
set -uo pipefail
cd "$(dirname "$0")"

PROFILE=""; CATALOG=""; APP_URL_ARG=""; SCHEMA="rd_security_demo"
WAREHOUSE=""; GENIE_SPACE=""; ANALYST=""; TOKEN=""
PII_TAG_KEY=""; SENS_TAG_KEY=""
GENIE_TITLE="RD Security Investigation"
while [[ $# -gt 0 ]]; do
  case "$1" in
    --profile) PROFILE="$2"; shift 2;;
    --catalog) CATALOG="$2"; shift 2;;
    --app-url) APP_URL_ARG="$2"; shift 2;;
    --schema) SCHEMA="$2"; shift 2;;
    --warehouse) WAREHOUSE="$2"; shift 2;;
    --genie-space) GENIE_SPACE="$2"; shift 2;;
    --analyst) ANALYST="$2"; shift 2;;
    --token) TOKEN="$2"; shift 2;;
    --tag-key) PII_TAG_KEY="$2"; SENS_TAG_KEY="$2"; shift 2;;
    --pii-tag) PII_TAG_KEY="${2%%=*}"; shift 2;;
    --sens-tag) SENS_TAG_KEY="${2%%=*}"; shift 2;;
    *) echo "unknown arg: $1"; exit 2;;
  esac
done
[[ -z "$PROFILE" || -z "$CATALOG" ]] && { echo "ERROR: --profile and --catalog are required."; exit 2; }
export DATABRICKS_CONFIG_PROFILE="$PROFILE"

# Warehouse: first serverless warehouse if not given (same choice setup.sh makes).
if [[ -z "$WAREHOUSE" ]]; then
  WAREHOUSE="$(databricks warehouses list --profile "$PROFILE" -o json 2>/dev/null \
    | python3 -c 'import sys,json
try: d=json.load(sys.stdin)
except Exception: d=[]
ws=d.get("warehouses",d) if isinstance(d,dict) else d
print(next((w["id"] for w in ws if w.get("enable_serverless_compute")), ""))' 2>/dev/null)"
fi
[[ -z "$WAREHOUSE" ]] && { echo "ERROR: no serverless warehouse found — pass --warehouse <id>."; exit 2; }

# Genie space: look up by title if not given (setup.sh creates it titled '$GENIE_TITLE').
if [[ -z "$GENIE_SPACE" ]]; then
  GENIE_SPACE="$(databricks genie list-spaces --profile "$PROFILE" -o json 2>/dev/null \
    | python3 -c "import sys,json
try: d=json.load(sys.stdin)
except Exception: d={}
sp=d.get('spaces',d) if isinstance(d,dict) else d
print(next((s['space_id'] for s in (sp or []) if s.get('title')=='$GENIE_TITLE'), ''))" 2>/dev/null)"
fi

# Analyst: default to the profile's current user.
if [[ -z "$ANALYST" ]]; then
  ANALYST="$(databricks current-user me --profile "$PROFILE" -o json 2>/dev/null \
    | python3 -c 'import sys,json;print(json.load(sys.stdin).get("userName",""))' 2>/dev/null)"
fi

APP_URL="${APP_URL_ARG:-${APP_URL:-http://127.0.0.1:8077}}"
# Bearer token for app calls (deployed app is platform-gated). Skip for local uvicorn.
if [[ -z "$TOKEN" && "$APP_URL" != http://127.0.0.1* && "$APP_URL" != http://localhost* ]]; then
  TOKEN="$(databricks auth token --profile "$PROFILE" -o json 2>/dev/null \
    | python3 -c 'import sys,json;print(json.load(sys.stdin).get("access_token",""))' 2>/dev/null)"
  [[ -z "$TOKEN" ]] && echo "WARN: could not mint a token (databricks auth token) — app checks will 401."
fi

export DBX_CATALOG="$CATALOG" DBX_SCHEMA="$SCHEMA" DBX_WAREHOUSE_ID="$WAREHOUSE"
export DBX_GENIE_SPACE_ID="$GENIE_SPACE" DBX_ANALYST_PRINCIPAL="$ANALYST"
export DBX_PII_TAG_KEY="$PII_TAG_KEY" DBX_SENS_TAG_KEY="$SENS_TAG_KEY"
export APP_URL APP_TOKEN="$TOKEN"

VENV="app/.venv"
if [[ ! -d "$VENV" ]]; then echo "Missing $VENV — set up the app venv first (see DEPLOYMENT.md)"; exit 1; fi
# shellcheck disable=SC1091
source "$VENV/bin/activate"
python -c "import pytest, httpx" 2>/dev/null || uv pip install -q pytest httpx >/dev/null 2>&1

echo "=================================================================="
echo " RD Security Demo — acceptance tests"
echo "   profile=$PROFILE  app=$APP_URL  token=$([[ -n "$TOKEN" ]] && echo yes || echo no)"
echo "   $DBX_CATALOG.$DBX_SCHEMA  warehouse=$DBX_WAREHOUSE_ID  genie=${GENIE_SPACE:-<none>}"
echo "=================================================================="
# Acceptance suite only — teardown verification lives in verify_teardown.sh.
pytest tests/test_data_and_governance.py tests/test_app_and_obo.py \
       tests/test_genie_lakebase_audit.py -v --tb=short -rA
