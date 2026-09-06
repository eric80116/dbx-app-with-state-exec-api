#!/usr/bin/env bash
# One-command, fully-parameterized deploy for the RD Security Investigation demo.
# NOTHING is hardcoded to a workspace — every value is a flag (or env var). See --help
# and docs/DEPLOYMENT.md for which parameters must be set for a given workspace.
#
# Idempotent: safe to re-run. Handles the pieces DAB can't (Lakebase, governed data +
# ABAC, Genie space, SP grants), generates app/app.yaml per target, deploys
# the App via DAB, wires OBO scopes + the Lakebase resource, and verifies.
set -uo pipefail
cd "$(dirname "$0")/.."

# ------------------------------------------------------------------ parameters
PROFILE=""; CATALOG=""; SCHEMA="rd_security_demo"; WAREHOUSE_ID=""
ANALYST=""; PII_TAG="data_classification=pii"; SENS_TAG="gov_sensitivity=high"
APP_NAME="rd-security-investigation"; LB_PROJECT="rd-security-demo"; LB_BRANCH="production"
SKIP_DATA="no"; ASSUME_YES="no"; CHECK_ONLY="no"
CREATE_TAGS="no"; TAG_KEY="rd_data_class"   # self-create the governed tag (needs account admin)

usage(){ cat <<EOF
Usage: bootstrap/setup.sh --profile <p> --catalog <c> [options]

REQUIRED
  --profile <name>        Databricks CLI profile (workspace target; host comes from it)
  --catalog <name>        UC catalog for the demo data (created with default storage if missing)

GOVERNED TAGS — choose ONE:
  (A) Self-create (recommended for a fresh/customer workspace with no governed tags):
      --create-tags         Create a governed tag policy and use it (NEEDS ACCOUNT ADMIN).
      --tag-key <key>       The governed tag key to create/use [rd_data_class]
                            → uses <key>=pii for masking and <key>=restricted for the row filter.
  (B) Use existing governed tags already registered in the account:
      --pii-tag key=value   Governed tag marking PII columns [data_classification=pii]
      --sens-tag key=value  Governed tag marking the row-scope column [gov_sensitivity=high]
      (keys must be registered governed tags; values are policy-constrained AND case-sensitive)

OPTIONS (defaults in brackets)
  --schema <name>         Demo schema [rd_security_demo]
  --warehouse <id>        Serverless SQL warehouse id [auto-discovered]
  --analyst <principal>   Principal exempted from ABAC masks/row-filter [current user]
  --app-name <name>       Databricks App name [rd-security-investigation]
  --lakebase-project <id> Lakebase project id [rd-security-demo]
  --skip-data             Skip data generation + governance (reuse existing)
  --check                 Run the prerequisite check only, then exit (no deploy)
  -y|--yes                Non-interactive: skip prompts, use provided/auto/default values
  -h|--help               This help
EOF
}

while [[ $# -gt 0 ]]; do case "$1" in
  --profile) PROFILE="$2"; shift 2;;
  --catalog) CATALOG="$2"; shift 2;;
  --schema) SCHEMA="$2"; shift 2;;
  --warehouse) WAREHOUSE_ID="$2"; shift 2;;
  --analyst) ANALYST="$2"; shift 2;;
  --pii-tag) PII_TAG="$2"; shift 2;;
  --sens-tag) SENS_TAG="$2"; shift 2;;
  --app-name) APP_NAME="$2"; shift 2;;
  --lakebase-project) LB_PROJECT="$2"; shift 2;;
  --skip-data) SKIP_DATA="yes"; shift;;
  --check) CHECK_ONLY="yes"; shift;;
  --create-tags) CREATE_TAGS="yes"; shift;;
  --tag-key) TAG_KEY="$2"; shift 2;;
  --yes|-y) ASSUME_YES="yes"; shift;;
  -h|--help) usage; exit 0;;
  *) echo "unknown arg: $1"; usage; exit 2;;
esac; done

[[ -z "$PROFILE" || -z "$CATALOG" ]] && { echo "ERROR: --profile and --catalog are required."; usage; exit 2; }
export DATABRICKS_CONFIG_PROFILE="$PROFILE"

# In self-create mode, the tags are the governed key we create, with fixed values.
if [[ "$CREATE_TAGS" == "yes" ]]; then PII_TAG="${TAG_KEY}=pii"; SENS_TAG="${TAG_KEY}=restricted"; fi

LB_ENDPOINT="projects/${LB_PROJECT}/branches/${LB_BRANCH}/endpoints/primary"
FQ="${CATALOG}.${SCHEMA}"
die(){ echo "ERROR: $*" >&2; exit 1; }
say(){ echo -e "\n=== $* ==="; }
q(){ databricks experimental aitools tools query "$1" --profile "$PROFILE" 2>&1 | grep -v -E "^\s+at "; }

# -------------------------------------------------------------------- preflight
# Interactive prerequisite check: verify each requirement, and prompt to pick/fill values that
# are missing or ambiguous. With --yes everything is non-interactive (auto/first/defaults).
FAILED=0
ck(){ case "$1" in OK) echo "  [ OK ] $2";; WARN) echo "  [WARN] $2";; FAIL) echo "  [FAIL] $2"; FAILED=1;; esac; }
# Interactive only during a real deploy — never in --check (a read-only report) or --yes.
interactive(){ [[ "$ASSUME_YES" != "yes" && "$CHECK_ONLY" != "yes" ]]; }
ask(){ local v; if ! interactive; then echo "$2"; return; fi; read -r -p "  ↳ $1 [$2]: " v </dev/tty; echo "${v:-$2}"; }
say "Preflight — checking prerequisites"

# CLI
ck OK "Databricks CLI $(databricks --version 2>/dev/null | awk '{print $NF}')"

# Auth + identity + host
if ME_JSON="$(databricks current-user me --profile "$PROFILE" -o json 2>/dev/null)"; then
  CURRENT_USER="$(echo "$ME_JSON" | python3 -c 'import sys,json;print(json.load(sys.stdin)["userName"])')"
  ck OK "Authenticated as $CURRENT_USER (profile $PROFILE)"
else
  ck FAIL "Not authenticated on profile '$PROFILE' — run: databricks auth login --profile $PROFILE"
  die "fix auth and re-run"
fi
[[ -z "$ANALYST" ]] && ANALYST="$CURRENT_USER"
HOST="$(databricks auth env --profile "$PROFILE" 2>/dev/null | python3 -c 'import sys,json;print(json.load(sys.stdin)["env"]["DATABRICKS_HOST"])' 2>/dev/null)"
[[ "$HOST" != http* ]] && HOST="https://$HOST"
ck OK "Workspace host: $HOST"

# Serverless SQL warehouse (interactive pick if several, auto if one, prompt id if none/--warehouse)
WH_JSON="$(databricks warehouses list --profile "$PROFILE" -o json 2>/dev/null || echo '[]')"
if [[ -n "$WAREHOUSE_ID" ]]; then
  ck OK "SQL warehouse: $WAREHOUSE_ID (provided)"
else
  # bash 3.2 compatible (no mapfile): one "id<TAB>name" per serverless warehouse
  WH_LIST="$(echo "$WH_JSON" | python3 -c 'import sys,json
for w in json.load(sys.stdin):
    if w.get("enable_serverless_compute"): print(w["id"]+"\t"+(w.get("name") or ""))' 2>/dev/null)"
  WH_COUNT="$(printf '%s\n' "$WH_LIST" | grep -c . || true)"
  if [[ -z "$WH_LIST" ]]; then
    ck FAIL "No serverless SQL warehouse found — create one or pass --warehouse <id>"
  elif [[ "$WH_COUNT" -eq 1 ]] || ! interactive; then
    WAREHOUSE_ID="$(printf '%s\n' "$WH_LIST" | head -1 | cut -f1)"
    ck OK "SQL warehouse: $WAREHOUSE_ID"
    [[ "$WH_COUNT" -gt 1 ]] && ck WARN "$WH_COUNT serverless warehouses found — auto-picked the first; pass --warehouse <id> to choose"
  else
    echo "  Multiple serverless warehouses — pick one:"
    printf '%s\n' "$WH_LIST" | cut -f1,2 | nl -w6 -s') '
    read -r -p "  ↳ number [1]: " n </dev/tty; n="${n:-1}"
    WAREHOUSE_ID="$(printf '%s\n' "$WH_LIST" | sed -n "${n}p" | cut -f1)"
    ck OK "SQL warehouse: $WAREHOUSE_ID"
  fi
fi

# Managed services reachable
databricks postgres list-projects --profile "$PROFILE" >/dev/null 2>&1 && ck OK "Lakebase (Autoscaling) reachable" || ck FAIL "Lakebase not reachable — enable Lakebase for this workspace"
databricks apps list --profile "$PROFILE" >/dev/null 2>&1 && ck OK "Databricks Apps reachable (OBO user-auth must be enabled)" || ck FAIL "Databricks Apps not reachable — enable Apps"
databricks genie list-spaces --profile "$PROFILE" >/dev/null 2>&1 && ck OK "Genie reachable" || ck WARN "Could not list Genie spaces (Genie may still work)"

# Local tools
for t in uv bun jq python3; do command -v "$t" >/dev/null 2>&1 && ck OK "local tool: $t" || ck FAIL "missing local tool: $t"; done
command -v psql >/dev/null 2>&1 && ck OK "local tool: psql" || ck WARN "psql not found — Lakebase grants step will be skipped (run them manually, see DEPLOYMENT.md)"

# Catalog: exists, or will be created
if databricks catalogs get "$CATALOG" --profile "$PROFILE" >/dev/null 2>&1; then
  ck OK "Catalog '$CATALOG' exists"
else
  CATALOG="$(ask "Catalog '$CATALOG' not found — name to CREATE (needs catalog-create rights)" "$CATALOG")"
  ck WARN "Catalog '$CATALOG' will be created (default storage)"
fi

# Analyst persona (exempted from ABAC masks/row-filter)
ANALYST="$(ask "Analyst principal (exempted from PII masks / row-filter)" "$ANALYST")"
ck OK "Analyst persona: $ANALYST"

# Governed tags — the per-account gotcha. Validate, and offer to self-create if missing.
if [[ "$CREATE_TAGS" == "yes" ]]; then
  ck OK "Governed tags: will SELF-CREATE '$TAG_KEY' (needs account admin) → mask $PII_TAG / row-filter $SENS_TAG"
else
  AVAIL="$(q "SELECT DISTINCT tag_name FROM system.information_schema.column_tags ORDER BY tag_name" 2>/dev/null | grep -oE '"[a-zA-Z0-9_.]+"' | tr -d '"' | grep -v '^tag_name$' | sort -u)"
  pk="${PII_TAG%%=*}"; sk="${SENS_TAG%%=*}"
  if echo "$AVAIL" | grep -qx "$pk" && echo "$AVAIL" | grep -qx "$sk"; then
    ck OK "Governed tags present: pii '$PII_TAG', sens '$SENS_TAG' (confirm VALUES are allowed for the key)"
  else
    ck WARN "Governed tag key(s) not found in account — pii key '$pk' / sens key '$sk'"
    echo "     Available governed tag keys:"; echo "$AVAIL" | sed 's/^/       - /' | head -30
    if interactive; then
      read -r -p "  ↳ [c]reate a governed tag now (needs admin) / [e]nter existing key=value pairs / [k]eep as-is: " g </dev/tty
      case "$g" in
        c|C) CREATE_TAGS="yes"; PII_TAG="${TAG_KEY}=pii"; SENS_TAG="${TAG_KEY}=restricted"; ck OK "Will self-create '$TAG_KEY'";;
        e|E) PII_TAG="$(ask "pii tag (key=value)" "$PII_TAG")"; SENS_TAG="$(ask "sensitivity tag (key=value)" "$SENS_TAG")";;
        *) ck WARN "Keeping '$PII_TAG' / '$SENS_TAG' — governance will fail if these aren't valid";;
      esac
    else
      ck WARN "--check: pass --create-tags (self-create, needs admin) or --pii-tag/--sens-tag at deploy time"
    fi
  fi
fi

echo ""
echo "  Summary:  catalog=$CATALOG schema=$SCHEMA warehouse=$WAREHOUSE_ID app=$APP_NAME lakebase=$LB_PROJECT"
echo "            analyst=$ANALYST  pii=$PII_TAG  sens=$SENS_TAG  create-tags=$CREATE_TAGS"
[[ "$FAILED" -eq 1 ]] && die "one or more prerequisites FAILED (see above) — resolve them and re-run"
if [[ "$CHECK_ONLY" == "yes" ]]; then say "Prerequisite check PASSED (--check) — not deploying"; exit 0; fi

if [[ "$ASSUME_YES" != "yes" ]]; then
  read -r -p "Proceed with these settings? [y/N] " ok </dev/tty; [[ "$ok" == "y" || "$ok" == "Y" ]] || { echo "aborted."; exit 0; }
fi

# ----------------------------------------------------------------------- venvs
say "Python venvs"
[[ -d .venv ]] || uv venv --python 3.12 .venv >/dev/null
./.venv/bin/python -c "import databricks.connect" 2>/dev/null || \
  (source .venv/bin/activate && uv pip install -q "databricks-connect>=16.4,<17.4" faker numpy pandas)
[[ -d app/.venv ]] || uv venv --python 3.12 app/.venv >/dev/null
(source app/.venv/bin/activate && uv pip install -q -r app/requirements.txt)

# --------------------------------------------------------- 1. data + governance
if [[ "$CREATE_TAGS" == "yes" ]]; then
  say "Governed tag policy ($TAG_KEY) — self-created (needs account admin)"
  if databricks tag-policies list-tag-policies --profile "$PROFILE" -o json 2>/dev/null \
       | python3 -c "import sys,json;d=json.load(sys.stdin);pols=d.get('tag_policies',d) if isinstance(d,dict) else d;exit(0 if any(p.get('tag_key')=='$TAG_KEY' for p in pols) else 1)"; then
    echo "  $TAG_KEY already exists — reusing"
  else
    databricks tag-policies create-tag-policy --profile "$PROFILE" \
      --json "{\"tag_key\":\"$TAG_KEY\",\"description\":\"RD security demo data classification\",\"values\":[{\"name\":\"pii\"},{\"name\":\"confidential\"},{\"name\":\"restricted\"},{\"name\":\"internal\"},{\"name\":\"public\"}]}" \
      >/dev/null 2>&1 && echo "  created governed tag $TAG_KEY" \
      || die "could not create governed tag '$TAG_KEY' — you likely lack account-admin. Either get it created, or re-run with --pii-tag/--sens-tag pointing at existing governed tags."
  fi
fi

if [[ "$SKIP_DATA" == "no" ]]; then
  say "Catalog + governed data + metric views + ABAC"
  q "CREATE CATALOG IF NOT EXISTS ${CATALOG}" >/dev/null 2>&1 || echo "  (catalog exists or needs a MANAGED LOCATION — see DEPLOYMENT.md)"
  source .venv/bin/activate
  python data/generate_synthetic_data.py --catalog "$CATALOG" --schema "$SCHEMA" \
    || die "data generation failed"
  python data/apply_metadata_and_governance.py --catalog "$CATALOG" --schema "$SCHEMA" \
    --analyst-principal "$ANALYST" --pii-tag "$PII_TAG" --sens-tag "$SENS_TAG" \
    || die "governance failed — check the pii/sens governed tags exist in this account"
  deactivate
fi

# --------------------------------------------------------------- 2. Lakebase
say "Lakebase project ($LB_PROJECT)"
if ! databricks postgres list-projects --profile "$PROFILE" -o json 2>/dev/null \
     | python3 -c "import sys,json;d=json.load(sys.stdin);exit(0 if any(p['project_id']=='$LB_PROJECT' for p in (d if isinstance(d,list) else d.get('projects',[]))) else 1)"; then
  # NOTE: recreating a project whose name was JUST deleted can be rejected/slow while the old
  # name is still freeing up. If you just tore down, wait a few minutes or use --lakebase-project.
  databricks postgres create-project "$LB_PROJECT" --json "{\"spec\":{\"display_name\":\"$LB_PROJECT\"}}" --profile "$PROFILE" \
    || die "could not create Lakebase project '$LB_PROJECT' (if you just deleted one with this name, wait for the name to free up or pass --lakebase-project <new>)"
fi
# endpoint provisioning can take several minutes on a brand-new project
for i in $(seq 1 40); do
  LB_HOST="$(databricks postgres get-endpoint "$LB_ENDPOINT" --profile "$PROFILE" -o json 2>/dev/null \
    | python3 -c 'import sys,json;print(json.load(sys.stdin).get("status",{}).get("hosts",{}).get("host",""))' 2>/dev/null)"
  [[ -n "$LB_HOST" ]] && break; echo "  waiting for Lakebase endpoint... ($i/40)"; sleep 15
done
[[ -z "$LB_HOST" ]] && die "Lakebase endpoint not ready after ~10 min — check 'databricks postgres list-projects'"
echo "  lakebase host: $LB_HOST"

# --------------------------------------------------------------- 4. Genie space
say "Genie space"
GENIE_PARENT="/Workspace/Users/${CURRENT_USER}/genie_spaces"
databricks workspace mkdirs "$GENIE_PARENT" --profile "$PROFILE" 2>/dev/null || true
GENIE_SPACE_ID="$(databricks genie list-spaces --profile "$PROFILE" -o json 2>/dev/null \
  | python3 -c "import sys,json;d=json.load(sys.stdin);sp=d.get('spaces',d) if isinstance(d,dict) else d;print(next((s['space_id'] for s in sp if s.get('title')=='RD Security Investigation'),''))" 2>/dev/null || true)"
if [[ -z "$GENIE_SPACE_ID" ]]; then
  python3 -c "s=open('bootstrap/genie_agent.json').read().replace('serverless_stable_tlm05u_catalog','$CATALOG').replace('rd_security_demo','$SCHEMA');open('/tmp/genie_deploy.json','w').write(s)"
  GENIE_SPACE_ID="$(databricks genie create-space --profile "$PROFILE" --json "{
    \"warehouse_id\":\"$WAREHOUSE_ID\",\"title\":\"RD Security Investigation\",
    \"description\":\"Engineer access risk + exfiltration investigation.\",
    \"parent_path\":\"$GENIE_PARENT\",\"serialized_space\":$(jq -c '.' /tmp/genie_deploy.json | jq -Rs '.')}" \
    2>/dev/null | python3 -c 'import sys,json;print(json.load(sys.stdin).get("space_id",""))')"
fi
echo "  genie space: $GENIE_SPACE_ID"

# -------------------------------------------------- 5. build frontend + deploy
say "Build frontend"
(cd app/frontend && bun install >/dev/null 2>&1 && bun run build >/dev/null) || die "frontend build failed"

# Generate app/app.yaml. PGUSER is NOT set here — the app defaults it to the injected
# DATABRICKS_CLIENT_ID (the app's own service principal), so no SP lookup is needed pre-deploy.
gen_app_yaml(){
  cat > app/app.yaml <<EOF
# GENERATED by bootstrap/setup.sh — do not edit by hand for a deploy.
command: ["uvicorn", "backend.main:app", "--host", "0.0.0.0", "--port", "8000"]
env:
  - name: DBX_WAREHOUSE_ID
    value: "$WAREHOUSE_ID"
  - name: DBX_CATALOG
    value: "$CATALOG"
  - name: DBX_SCHEMA
    value: "$SCHEMA"
  - name: DBX_GENIE_SPACE_ID
    value: "$GENIE_SPACE_ID"
  - name: DBX_MAX_ROWS
    value: "500"
  - name: LAKEBASE_ENDPOINT
    value: "$LB_ENDPOINT"
  - name: PGHOST
    value: "$LB_HOST"
  - name: PGDATABASE
    value: "databricks_postgres"
  - name: LAKEBASE_SCHEMA
    value: "app_ops"
EOF
}

say "Deploy App"
gen_app_yaml
# Clean create every run: the bundle then only ever CREATEs the app. (Updating an app that has
# user authorization trips a CLI update-mask bug — 'forward_user_access_token' is rejected — so
# we never take the bundle update path.) `bundle destroy` deletes the app AND clears bundle
# state (a plain `apps delete` leaves stale state, and the next deploy still does an UPDATE).
if databricks apps get "$APP_NAME" --profile "$PROFILE" >/dev/null 2>&1 \
   || [[ -d ".databricks/bundle/dev" ]]; then
  echo "  clearing any prior app + bundle state (bundle destroy)"
  databricks bundle destroy -t dev --auto-approve --profile "$PROFILE" \
    --var app_name="$APP_NAME" --var warehouse_id="$WAREHOUSE_ID" >/dev/null 2>&1 || true
  databricks apps delete "$APP_NAME" --profile "$PROFILE" >/dev/null 2>&1 || true
  # deletion is async — WAIT until the app is actually gone, else create fails 409 ALREADY_EXISTS
  for _ in $(seq 1 30); do databricks apps get "$APP_NAME" --profile "$PROFILE" >/dev/null 2>&1 || break; sleep 4; done
fi
databricks bundle deploy -t dev --profile "$PROFILE" --var app_name="$APP_NAME" --var warehouse_id="$WAREHOUSE_ID" || die "bundle deploy failed"
say "Start the App (bundle run deploys code AND starts compute)"
databricks bundle run rd_security_app -t dev --profile "$PROFILE" --var app_name="$APP_NAME" --var warehouse_id="$WAREHOUSE_ID" >/dev/null 2>&1 || die "app start (bundle run) failed"
SP="$(databricks apps get "$APP_NAME" --profile "$PROFILE" -o json | python3 -c 'import sys,json;print(json.load(sys.stdin).get("service_principal_client_id",""))')"
[[ -z "$SP" ]] && die "could not read app service principal"
echo "  app service principal: $SP"

# Add the Lakebase postgres resource (update_mask=resources only — does NOT touch user_api_scopes,
# so it avoids the forward_user_access_token mask issue). Scopes came from the bundle at create.
say "Attach Lakebase resource"
cat > /tmp/app_resources.json <<EOF
{"update_mask":"resources","app":{"resources":[
  {"name":"sql-warehouse","sql_warehouse":{"id":"$WAREHOUSE_ID","permission":"CAN_USE"}},
  {"name":"postgres","postgres":{"branch":"projects/${LB_PROJECT}/branches/${LB_BRANCH}","database":"projects/${LB_PROJECT}/branches/${LB_BRANCH}/databases/databricks-postgres","permission":"CAN_CONNECT_AND_CREATE"}}
]}}
EOF
databricks apps create-update "$APP_NAME" --json @/tmp/app_resources.json --profile "$PROFILE" >/dev/null || echo "  WARN: could not attach postgres resource — attach it manually (see DEPLOYMENT.md)"

# ---------------------------------------------- 6. Lakebase grants (operational store)
say "Lakebase: create app_ops (query history + saved queries), grant the app SP"
PGTOKEN="$(databricks postgres generate-database-credential "$LB_ENDPOINT" --profile "$PROFILE" -o json 2>/dev/null | python3 -c 'import sys,json;print(json.load(sys.stdin)["token"])')"
if command -v psql >/dev/null 2>&1 && [[ -n "$PGTOKEN" ]]; then
  PGPASSWORD="$PGTOKEN" psql "host=$LB_HOST user=$CURRENT_USER dbname=databricks_postgres sslmode=require" -v ON_ERROR_STOP=0 -v sp="$SP" >/dev/null 2>&1 <<'SQL'
CREATE SCHEMA IF NOT EXISTS app_ops;
CREATE TABLE IF NOT EXISTS app_ops.query_history (id BIGSERIAL PRIMARY KEY, user_email TEXT NOT NULL, sql TEXT NOT NULL, source TEXT, row_count INT, status TEXT, error TEXT, statement_id TEXT, created_at TIMESTAMPTZ NOT NULL DEFAULT now());
CREATE INDEX IF NOT EXISTS ix_qh_user_time ON app_ops.query_history (user_email, created_at DESC);
CREATE TABLE IF NOT EXISTS app_ops.saved_queries (id BIGSERIAL PRIMARY KEY, user_email TEXT NOT NULL, title TEXT NOT NULL, sql TEXT NOT NULL, created_at TIMESTAMPTZ NOT NULL DEFAULT now());
GRANT USAGE, CREATE ON SCHEMA app_ops TO :"sp";
GRANT ALL ON ALL TABLES IN SCHEMA app_ops TO :"sp";
GRANT ALL ON ALL SEQUENCES IN SCHEMA app_ops TO :"sp";
ALTER DEFAULT PRIVILEGES IN SCHEMA app_ops GRANT ALL ON TABLES TO :"sp";
ALTER DEFAULT PRIVILEGES IN SCHEMA app_ops GRANT ALL ON SEQUENCES TO :"sp";
SQL
  echo "  app_ops ready + granted to SP"
else
  echo "  WARN: psql not found or no credential — run the grants in DEPLOYMENT.md §Lakebase grants manually."
fi

APP_URL="$(databricks apps get "$APP_NAME" --profile "$PROFILE" -o json 2>/dev/null | python3 -c 'import sys,json;print(json.load(sys.stdin).get("url",""))')"
if [[ "$CREATE_TAGS" == "yes" ]]; then TAG_FLAGS="--tag-key $TAG_KEY"; else TAG_FLAGS="--pii-tag $PII_TAG --sens-tag $SENS_TAG"; fi
say "DONE"
echo "  App:  ${APP_URL:-<databricks apps get $APP_NAME>}"
echo "  Verify:"
echo "    ./run_tests.sh --profile $PROFILE --catalog $CATALOG --schema $SCHEMA \\"
echo "      --warehouse $WAREHOUSE_ID --genie-space ${GENIE_SPACE_ID:-<space-id>} $TAG_FLAGS \\"
echo "      --app-url ${APP_URL:-<app-url>}"
echo "  (data/governance/Genie checks run headless; the app REST checks skip against a deployed"
echo "   OBO app — run them against a local uvicorn per DEPLOYMENT.md §5/§7.)"
