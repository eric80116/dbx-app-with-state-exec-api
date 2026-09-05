#!/usr/bin/env bash
# Tear down everything bootstrap/setup.sh created, in reverse order. Fully parameterized;
# destructive, so it confirms first (skip with --yes). Only removes what this demo creates.
#
# Usage: bootstrap/teardown.sh --profile <p> --catalog <c> [options]
set -uo pipefail
cd "$(dirname "$0")/.."

PROFILE=""; CATALOG=""; SCHEMA="rd_security_demo"
APP_NAME="rd-security-investigation"; LB_PROJECT="rd-security-demo"; TAG_KEY="rd_data_class"
DROP_CATALOG="no"; DROP_TAG="no"; ASSUME_YES="no"

usage(){ cat <<EOF
Usage: bootstrap/teardown.sh --profile <p> --catalog <c> [options]

REQUIRED
  --profile <name>        Databricks CLI profile (the workspace to clean)
  --catalog <name>        UC catalog the demo used

OPTIONS (defaults in brackets)
  --schema <name>         Demo schema [rd_security_demo]   (DROP SCHEMA CASCADE)
  --app-name <name>       Databricks App to delete [rd-security-investigation]
  --lakebase-project <id> Lakebase project to delete [rd-security-demo]  (removes app_ops + synced tables)
  --tag-key <key>         Governed tag key created by setup [rd_data_class]
  --drop-catalog          Also DROP the catalog (off by default — it may hold other data)
  --drop-tag              Also delete the governed tag policy (off by default — account-level; needs admin)
  -y|--yes                Skip the confirmation prompt
  -h|--help               This help

By default it deletes: the App, the Lakebase project (incl. its Postgres data), the Lakebase
UC catalog, the Genie space, and the demo SCHEMA. Add --drop-catalog / --drop-tag for those.
EOF
}

while [[ $# -gt 0 ]]; do case "$1" in
  --profile) PROFILE="$2"; shift 2;;
  --catalog) CATALOG="$2"; shift 2;;
  --schema) SCHEMA="$2"; shift 2;;
  --app-name) APP_NAME="$2"; shift 2;;
  --lakebase-project) LB_PROJECT="$2"; shift 2;;
  --tag-key) TAG_KEY="$2"; shift 2;;
  --drop-catalog) DROP_CATALOG="yes"; shift;;
  --drop-tag) DROP_TAG="yes"; shift;;
  -y|--yes) ASSUME_YES="yes"; shift;;
  -h|--help) usage; exit 0;;
  *) echo "unknown arg: $1"; usage; exit 2;;
esac; done
[[ -z "$PROFILE" || -z "$CATALOG" ]] && { echo "ERROR: --profile and --catalog are required."; usage; exit 2; }
export DATABRICKS_CONFIG_PROFILE="$PROFILE"
LB_UC_CATALOG="lakebase_$(echo "$LB_PROJECT" | tr '-' '_')"
say(){ echo -e "\n=== $* ==="; }
q(){ databricks experimental aitools tools query "$1" --profile "$PROFILE" 2>&1 | grep -v -E "^\s+at "; }

cat <<EOF

About to DELETE from workspace '$PROFILE':
  - App:              $APP_NAME
  - Lakebase project: $LB_PROJECT   (all Postgres data, incl. app_ops + synced tables)
  - Lakebase catalog: $LB_UC_CATALOG (if present)
  - Genie space:      titled "RD Security Investigation"
  - Schema:           ${CATALOG}.${SCHEMA}  (DROP SCHEMA CASCADE)
  - Catalog drop:     $DROP_CATALOG      Governed tag drop ($TAG_KEY): $DROP_TAG
EOF
if [[ "$ASSUME_YES" != "yes" ]]; then
  read -r -p "Type 'delete' to proceed: " ok; [[ "$ok" == "delete" ]] || { echo "aborted."; exit 0; }
fi

say "Delete App ($APP_NAME)"
databricks apps delete "$APP_NAME" --profile "$PROFILE" 2>&1 | grep -iE "deleted|error|not found" | head -1 || echo "  (app absent)"

say "Delete Lakebase project ($LB_PROJECT)"
databricks postgres delete-project "projects/${LB_PROJECT}" --profile "$PROFILE" 2>&1 | grep -iE "error|not found" | head -1 || echo "  deleted (or absent)"

say "Delete Lakebase UC catalog ($LB_UC_CATALOG)"
databricks catalogs delete "$LB_UC_CATALOG" --force --profile "$PROFILE" 2>&1 | grep -iE "error|not found" | head -1 || echo "  deleted (or absent)"

say "Trash Genie space(s) titled 'RD Security Investigation'"
databricks genie list-spaces --profile "$PROFILE" -o json 2>/dev/null \
  | python3 -c "import sys,json;d=json.load(sys.stdin);sp=d.get('spaces',d) if isinstance(d,dict) else d;print('\n'.join(s['space_id'] for s in sp if s.get('title')=='RD Security Investigation'))" 2>/dev/null \
  | while read -r sid; do [[ -n "$sid" ]] && { echo "  trashing $sid"; databricks genie trash-space "$sid" --profile "$PROFILE" >/dev/null 2>&1 || true; }; done

say "Drop schema ${CATALOG}.${SCHEMA}"
q "DROP SCHEMA IF EXISTS ${CATALOG}.${SCHEMA} CASCADE" | grep -iE "success|error" | head -1

if [[ "$DROP_CATALOG" == "yes" ]]; then
  say "Drop catalog ${CATALOG}"
  q "DROP CATALOG IF EXISTS ${CATALOG} CASCADE" | grep -iE "success|error" | head -1
fi

if [[ "$DROP_TAG" == "yes" ]]; then
  say "Delete governed tag policy ($TAG_KEY)"
  databricks tag-policies delete-tag-policy "$TAG_KEY" --profile "$PROFILE" 2>&1 | grep -iE "error|not found" | head -1 || echo "  deleted (or absent)"
fi

say "Optional: remove the bundle's workspace files"
echo "  databricks bundle destroy -t dev --var app_name=$APP_NAME --var warehouse_id=unused --auto-approve --profile $PROFILE"
say "Teardown complete"
