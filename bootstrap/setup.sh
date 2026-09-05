#!/usr/bin/env bash
# One-command, fully-parameterized deploy for the RD Security Investigation demo.
# NOTHING is hardcoded to a workspace — every value is a flag (or env var). See --help
# and docs/DEPLOYMENT.md for which parameters must be set for a given workspace.
#
# Idempotent: safe to re-run. Handles the pieces DAB can't (Lakebase, governed data +
# ABAC, Genie space, hot-cache, SP grants), generates app/app.yaml per target, deploys
# the App via DAB, wires OBO scopes + the Lakebase resource, and verifies.
set -uo pipefail
cd "$(dirname "$0")/.."

# ------------------------------------------------------------------ parameters
PROFILE=""; CATALOG=""; SCHEMA="rd_security_demo"; WAREHOUSE_ID=""
ANALYST=""; PII_TAG="data_classification=pii"; SENS_TAG="gov_sensitivity=high"
APP_NAME="rd-security-investigation"; LB_PROJECT="rd-security-demo"; LB_BRANCH="production"
SYNCED="auto"   # auto | yes | no   (managed Lakebase synced table for the hot-cache)
SKIP_DATA="no"; ASSUME_YES="no"
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
  --synced <auto|yes|no>  Managed Lakebase synced table for hot-cache [auto: use if CREATE CATALOG allowed]
  --skip-data             Skip data generation + governance (reuse existing)
  -y|--yes                Skip the preflight confirmation prompt (non-interactive)
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
  --synced) SYNCED="$2"; shift 2;;
  --skip-data) SKIP_DATA="yes"; shift;;
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
LB_UC_CATALOG="lakebase_$(echo "$LB_PROJECT" | tr '-' '_')"
FQ="${CATALOG}.${SCHEMA}"
die(){ echo "ERROR: $*" >&2; exit 1; }
say(){ echo -e "\n=== $* ==="; }
q(){ databricks experimental aitools tools query "$1" --profile "$PROFILE" 2>&1 | grep -v -E "^\s+at "; }

# -------------------------------------------------------------------- preflight
say "Preflight"
ME_JSON="$(databricks current-user me --profile "$PROFILE" -o json 2>&1)" || die "auth failed for profile '$PROFILE' — run: databricks auth login --profile $PROFILE"
CURRENT_USER="$(echo "$ME_JSON" | python3 -c 'import sys,json;print(json.load(sys.stdin)["userName"])')"
[[ -z "$ANALYST" ]] && ANALYST="$CURRENT_USER"
HOST="$(databricks auth env --profile "$PROFILE" 2>/dev/null | python3 -c 'import sys,json;print(json.load(sys.stdin)["env"]["DATABRICKS_HOST"])')"
[[ "$HOST" != http* ]] && HOST="https://$HOST"

if [[ -z "$WAREHOUSE_ID" ]]; then
  WAREHOUSE_ID="$(databricks warehouses list --profile "$PROFILE" -o json 2>/dev/null \
    | python3 -c 'import sys,json;w=[x for x in json.load(sys.stdin) if x.get("enable_serverless_compute")];print(w[0]["id"] if w else "")')"
  [[ -z "$WAREHOUSE_ID" ]] && die "no serverless SQL warehouse found — pass --warehouse <id>"
fi

echo "  profile:    $PROFILE"
echo "  host:       $HOST"
echo "  user:       $CURRENT_USER"
echo "  catalog:    $CATALOG   schema: $SCHEMA"
echo "  warehouse:  $WAREHOUSE_ID"
echo "  analyst:    $ANALYST"
echo "  pii-tag:    $PII_TAG    sens-tag: $SENS_TAG"
echo "  app-name:   $APP_NAME   lakebase-project: $LB_PROJECT   synced: $SYNCED"

if [[ "$CREATE_TAGS" == "yes" ]]; then
  echo "  Governed tags: SELF-CREATE '$TAG_KEY' (values: pii,confidential,restricted,internal,public) — needs account admin"
  echo "                 mask → $PII_TAG   row filter → $SENS_TAG"
else
  say "Governed tags available in this account (your --pii-tag / --sens-tag KEYS must be here)"
  q "SELECT DISTINCT tag_name FROM system.information_schema.column_tags ORDER BY tag_name" \
    | grep -oE '"[a-zA-Z0-9_.]+"' | tr -d '"' | grep -v '^tag_name$' | sed 's/^/  - /' | sort -u | head -40 || true
  echo "  (using pii='$PII_TAG', sens='$SENS_TAG' — confirm the KEYS appear above and the VALUES are allowed;"
  echo "   if this workspace has no suitable governed tags, re-run with --create-tags)"
fi

if [[ "$ASSUME_YES" != "yes" ]]; then
  read -r -p "Proceed with these settings? [y/N] " ok; [[ "$ok" == "y" || "$ok" == "Y" ]] || { echo "aborted."; exit 0; }
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
  say "Pre-aggregation source table (for the hot-cache)"
  q "CREATE OR REPLACE TABLE ${FQ}.mv_risk_behavior_daily AS
     SELECT employee_id, CAST(event_time AS DATE) AS event_date, ANY_VALUE(team) AS team,
       COUNT(*) access_events, SUM(CASE WHEN risk_score>=70 THEN 1 ELSE 0 END) high_risk_events,
       ROUND(AVG(risk_score),1) avg_risk, SUM(CASE WHEN is_after_hours THEN 1 ELSE 0 END) after_hours_events
     FROM ${FQ}.fact_access_events GROUP BY employee_id, CAST(event_time AS DATE)" >/dev/null
  q "ALTER TABLE ${FQ}.mv_risk_behavior_daily ALTER COLUMN employee_id SET NOT NULL" >/dev/null 2>&1 || true
  q "ALTER TABLE ${FQ}.mv_risk_behavior_daily ALTER COLUMN event_date SET NOT NULL" >/dev/null 2>&1 || true
  q "ALTER TABLE ${FQ}.mv_risk_behavior_daily ADD CONSTRAINT pk_rbd PRIMARY KEY (employee_id, event_date)" >/dev/null 2>&1 || true
  q "ALTER TABLE ${FQ}.mv_risk_behavior_daily SET TBLPROPERTIES (delta.enableChangeDataFeed = true)" >/dev/null 2>&1 || true
fi

# --------------------------------------------------------------- 2. Lakebase
say "Lakebase project ($LB_PROJECT)"
databricks postgres list-projects --profile "$PROFILE" -o json 2>/dev/null \
  | python3 -c "import sys,json;exit(0 if any(p['project_id']=='$LB_PROJECT' for p in json.load(sys.stdin)) else 1)" \
  || databricks postgres create-project "$LB_PROJECT" --json "{\"spec\":{\"display_name\":\"$LB_PROJECT\"}}" --profile "$PROFILE"
for i in $(seq 1 20); do
  LB_HOST="$(databricks postgres get-endpoint "$LB_ENDPOINT" --profile "$PROFILE" -o json 2>/dev/null \
    | python3 -c 'import sys,json;print(json.load(sys.stdin).get("status",{}).get("hosts",{}).get("host",""))' 2>/dev/null)"
  [[ -n "$LB_HOST" ]] && break; echo "  waiting for Lakebase endpoint..."; sleep 15
done
[[ -z "$LB_HOST" ]] && die "Lakebase endpoint not ready"
echo "  lakebase host: $LB_HOST"

# ---------------------------------------------- 3. hot-cache: synced (or manual)
HOTCACHE_TABLE="app_ops.metric_hot_cache"; USE_SYNCED="no"
if [[ "$SYNCED" != "no" ]]; then
  say "Attempting managed Lakebase synced table (needs CREATE CATALOG)"
  if databricks postgres create-catalog "$LB_UC_CATALOG" \
       --json "{\"spec\":{\"postgres_database\":\"databricks_postgres\",\"branch\":\"projects/${LB_PROJECT}/branches/${LB_BRANCH}\"}}" \
       --profile "$PROFILE" >/dev/null 2>&1 || databricks catalogs get "$LB_UC_CATALOG" --profile "$PROFILE" >/dev/null 2>&1; then
    if databricks postgres get-synced-table "synced_tables/${LB_UC_CATALOG}.public.mv_risk_behavior_daily" --profile "$PROFILE" >/dev/null 2>&1; then
      HOTCACHE_TABLE="public.mv_risk_behavior_daily"; USE_SYNCED="yes"; echo "  managed synced table already exists — reusing"
    elif databricks postgres create-synced-table "${LB_UC_CATALOG}.public.mv_risk_behavior_daily" \
      --json "{\"spec\":{\"source_table_full_name\":\"${FQ}.mv_risk_behavior_daily\",\"primary_key_columns\":[\"employee_id\",\"event_date\"],\"scheduling_policy\":\"SNAPSHOT\",\"branch\":\"projects/${LB_PROJECT}/branches/${LB_BRANCH}\",\"postgres_database\":\"databricks_postgres\",\"create_database_objects_if_missing\":true,\"new_pipeline_spec\":{\"storage_catalog\":\"${CATALOG}\",\"storage_schema\":\"${SCHEMA}\"}}}" \
      --no-wait --profile "$PROFILE" >/dev/null 2>&1; then
      HOTCACHE_TABLE="public.mv_risk_behavior_daily"; USE_SYNCED="yes"; echo "  managed synced table creating (SNAPSHOT)"
    else
      echo "  synced table create failed — falling back to manual hot-cache"
    fi
  else
    echo "  no CREATE CATALOG permission — falling back to manual hot-cache refresh"
  fi
fi
echo "  hot-cache table: $HOTCACHE_TABLE"

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
  - name: LAKEBASE_HOTCACHE_TABLE
    value: "$HOTCACHE_TABLE"
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
  sleep 5
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

# ---------------------------------------------- 6. Lakebase grants + hot-cache
say "Lakebase: create app_ops, grant the app SP, populate hot-cache"
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
  if [[ "$USE_SYNCED" == "yes" ]]; then
    # wait for the synced table to come ONLINE, then grant SELECT (ACL managed by pipeline)
    for i in $(seq 1 20); do
      st="$(databricks postgres get-synced-table "synced_tables/${LB_UC_CATALOG}.public.mv_risk_behavior_daily" --profile "$PROFILE" -o json 2>/dev/null | python3 -c 'import sys,json;print(json.load(sys.stdin).get("status",{}).get("detailed_state",""))' 2>/dev/null)"
      [[ "$st" == *ONLINE* ]] && break; echo "  synced table: $st"; sleep 15
    done
    PGPASSWORD="$PGTOKEN" psql "host=$LB_HOST user=$CURRENT_USER dbname=databricks_postgres sslmode=require" -v sp="$SP" >/dev/null 2>&1 <<'SQL'
GRANT USAGE ON SCHEMA public TO :"sp";
GRANT SELECT ON public.mv_risk_behavior_daily TO :"sp";
SQL
    echo "  synced hot-cache granted to SP"
  else
    say "Populate manual hot-cache (app_ops.metric_hot_cache)"
    (source app/.venv/bin/activate && LAKEBASE_ENDPOINT="$LB_ENDPOINT" PGHOST="$LB_HOST" PGDATABASE=databricks_postgres \
      PGUSER="$CURRENT_USER" LAKEBASE_SCHEMA=app_ops DBX_WAREHOUSE_ID="$WAREHOUSE_ID" \
      python bootstrap/refresh_hotcache.py --catalog "$CATALOG" --schema "$SCHEMA" --profile "$PROFILE") \
      && PGPASSWORD="$PGTOKEN" psql "host=$LB_HOST user=$CURRENT_USER dbname=databricks_postgres sslmode=require" -v sp="$SP" >/dev/null 2>&1 <<'SQL'
GRANT SELECT ON app_ops.metric_hot_cache TO :"sp";
SQL
  fi
else
  echo "  WARN: psql not found or no credential — run the grants in DEPLOYMENT.md §Lakebase grants manually."
fi

APP_URL="$(databricks apps get "$APP_NAME" --profile "$PROFILE" -o json 2>/dev/null | python3 -c 'import sys,json;print(json.load(sys.stdin).get("url",""))')"
say "DONE"
echo "  App:  ${APP_URL:-<databricks apps get $APP_NAME>}"
echo "  Verify: ./run_tests.sh --profile $PROFILE --app-url ${APP_URL:-<app-url>}"
echo "  (set the same DBX_CATALOG/DBX_SCHEMA/DBX_WAREHOUSE_ID/DBX_GENIE_SPACE_ID env for run_tests if non-default)"
