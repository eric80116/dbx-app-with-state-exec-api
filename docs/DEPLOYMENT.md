# Deployment Guide — RD Security Investigation Demo

Secure Statement Execution over Unity Catalog metric views, fronted by a Databricks App
that runs every query **On-Behalf-Of the user (OBO)** so UC governance is enforced per user.

- **Serving layer**: Databricks App (FastAPI + React) — a UI *and* a REST API sharing one OBO execution path.
- **Semantic layer**: UC **metric views** (replaces Cube).
- **Agent**: **Genie One** managed MCP (workspace-wide, no table cap) + a curated Genie space (certified path).
- **Governance**: **ABAC policies + governed tags** (column masks + row filter), enforced under OBO.
- **Operational store / cache**: **Lakebase** (query history + saved queries + metric hot-cache).
- **Audit**: `system.query.history` / `system.access.audit` (authoritative; ~10-min lag).

---

## 1. Prerequisites

| Requirement | Notes |
|---|---|
| Databricks CLI ≥ v0.294 | `databricks --version` (tested on v1.14.0) |
| A workspace profile | `databricks auth login --profile <name>` (OAuth) |
| Serverless SQL warehouse | id → DAB var `warehouse_id` |
| Databricks Apps + **user authorization (OBO)** enabled | required for per-user execution |
| **Lakebase (Autoscaling)** enabled | for the operational store + hot-cache |
| **Genie** enabled | Genie One MCP: `POST /api/2.0/mcp/genie` |
| **Governed tags** `data_classification`, `gov_sensitivity` registered in the account | required by the ABAC policies — see §6 |
| `uv`, `bun`, `jq`, `python3.12` locally | for data-gen venv + frontend build |

Local tools that mint a token but **do not** carry into the deployed app: the app uses the
Apps-injected `X-Forwarded-Access-Token` (browser) or a caller Bearer token (API).

---

## 2. Parameters — what to set per workspace

Nothing is hardcoded to a workspace. `bootstrap/setup.sh` takes every value as a flag; the
workspace host comes from the `--profile`. A preflight prints your resolved settings and the
account's available governed tags, then asks you to confirm before doing anything.

| Flag | Required? | Default | How to get / notes |
|---|---|---|---|
| `--profile` | **Yes** | — | Your CLI profile. `databricks auth login --profile <name>` first. |
| `--catalog` | **Yes** | — | UC catalog for the demo data. Created with default storage if missing (needs catalog-create rights, or pre-create it). |
| `--schema` | No | `rd_security_demo` | Demo schema (created if missing). |
| `--warehouse` | No | auto | Serverless SQL warehouse id. Auto-picks the first serverless warehouse; override if you have several. |
| `--analyst` | No | current user | Principal exempted from ABAC masks/row-filter (the "analyst" persona). Use an **account group** in production. |
| `--app-name` | No | `rd-security-investigation` | Databricks App name (unique per workspace). |
| `--lakebase-project` | No | `rd-security-demo` | Lakebase project id (created if missing). |
| `--synced` | No | `auto` | `auto` uses a managed Lakebase synced table for the hot-cache **if** you have `CREATE CATALOG`; otherwise falls back to the manual refresh. Force with `yes`/`no`. |

### Governed tags — pick ONE mode

ABAC selects columns by **governed tag**. Governed tags are account-level (a "tag policy"
with allowed values), so a fresh workspace may have **none**. You have two choices:

**(A) Self-create — recommended, portable, no dependency on the customer's existing tags:**

> ⚠️ **`--create-tags` requires account-admin** (creating a governed tag policy is an
> account-level operation). If the person running the deploy is not an account admin, either
> (1) have an admin create a governed tag once, then use mode (B); or (2) point mode (B) at an
> existing governed tag. The one-line deploy `bootstrap/setup.sh --profile <p> --catalog <c>
> --create-tags --yes` therefore assumes the operator is an account admin.

| Flag | Default | Notes |
|---|---|---|
| `--create-tags` | off | The script **creates** a governed tag policy via `tag-policies create-tag-policy` and uses it. **Requires account-admin** on the target account. Idempotent (skips if it already exists). |
| `--tag-key <key>` | `rd_data_class` | The governed tag key to create. Masking uses `<key>=pii`; the row filter uses `<key>=restricted` (values `pii,confidential,restricted,internal,public` are created). |

**(B) Use existing governed tags already registered in the account:**

| Flag | Default | Notes |
|---|---|---|
| `--pii-tag key=value` | `data_classification=pii` | Marks PII columns. KEY must be a registered governed tag; VALUE is policy-constrained and **case-sensitive**. |
| `--sens-tag key=value` | `gov_sensitivity=high` | Marks the row-scope column. Same rules — e.g. one account needs `sensitivity=HIGH`, another `gov_sensitivity=high`. |

Discover what already exists (for mode B):
```sql
SELECT DISTINCT tag_name, tag_value FROM system.information_schema.column_tags ORDER BY 1,2;
```
`apply_metadata_and_governance.py` fails fast if a tag key/value is not valid, so the deploy
stops early rather than half-applying. If you have neither account-admin (for A) nor suitable
existing tags (for B), an account admin must register a governed tag first.

**Prerequisite toggles to confirm in the workspace:** Databricks Apps **user authorization (OBO)**,
**Lakebase (Autoscaling)**, **Genie**, and a **serverless SQL warehouse**.

---

## 3. One-command deploy

```bash
# Recommended for a fresh/customer workspace — self-creates the governed tag (needs account admin):
bootstrap/setup.sh --profile <p> --catalog <c> --create-tags

# Or point at existing governed tags instead of creating one:
bootstrap/setup.sh --profile <p> --catalog <c> --pii-tag <key=value> --sens-tag <key=value>

# Full option list:
bootstrap/setup.sh --profile <p> --catalog <c> \
    [--create-tags [--tag-key rd_data_class] | --pii-tag key=value --sens-tag key=value] \
    [--schema <s>] [--warehouse <id>] [--analyst <principal>] \
    [--app-name <n>] [--lakebase-project <id>] [--synced auto|yes|no]
```

Idempotent; re-runnable. It runs, in this order (the order matters — see notes):
1. **Preflight** — resolve host/user/warehouse, list the account's governed tags, confirm.
2. **Venvs** — `.venv` (data-gen) and `app/.venv` (app deps incl. `psycopg`).
3. **Governed tag** — with `--create-tags`, create the governed tag policy (idempotent; needs
   account admin). Then **data + governance** — create the catalog, generate data, apply
   metadata + metric views + ABAC (using the self-created tag or your `--pii-tag`/`--sens-tag`),
   build the pre-aggregation table.
4. **Lakebase** — create the project (if missing), read its endpoint host.
5. **Hot-cache** — try a managed synced table (needs `CREATE CATALOG`); else fall back to the
   manual refresh. Sets the hot-cache table name accordingly.
6. **Genie space** — create it (remapped to your catalog/schema) if absent.
7. **App deploy** — build the frontend, generate `app/app.yaml`, then a **clean create**:
   `bundle destroy` clears any prior app + bundle state, `bundle deploy` **creates** the app
   (OBO scopes come from the bundle at create), `bundle run` deploys the code and **starts**
   compute, and `apps create-update` (update_mask=resources) attaches the Lakebase `postgres`
   resource. The app reads its own SP id from the injected `DATABRICKS_CLIENT_ID`, so no
   pre-deploy service-principal lookup is needed. (Delete-then-create avoids a CLI update-mask
   bug where updating an app with user authorization sends a rejected `forward_user_access_token`.)
8. **Lakebase grants** — as the project owner, create `app_ops` + `GRANT` it (and the hot-cache
   table) to the app SP; populate the manual hot-cache if not using the synced table.

Then validate (§5).

> **Why the ordering and grants matter (both were real failure modes):**
> - `app/app.yaml` and `requirements.txt` must live at the **app source root** — Databricks Apps
>   installs deps from `app/requirements.txt`, not a nested path.
> - The DAB `database` resource key does **not** work on Lakebase Autoscaling; the `postgres` key
>   is attached via `apps create-update` **after** the final `bundle deploy` (a bundle deploy
>   resets the app's resources array).
> - Attaching/re-attaching the Lakebase resource can rotate the app SP's effective Postgres role,
>   so the SP may not own `app_ops`. setup.sh therefore **grants** the SP explicitly (as the
>   project owner) rather than relying on ownership — this is idempotent and survives redeploys.
> - Synced-table ACLs are managed by the sync pipeline, so the SP `GRANT SELECT` is applied
>   **after** the table is `ONLINE`.

---

## 4. Manual step-by-step (if not using setup.sh)

```bash
export DATABRICKS_CONFIG_PROFILE=<profile>
# data + governance
source .venv/bin/activate
python data/generate_synthetic_data.py       --catalog <cat> --schema <sch>
python data/apply_metadata_and_governance.py  --catalog <cat> --schema <sch> --analyst-principal <you@co>
# app
cd app/frontend && bun install && bun run build && cd ../..
databricks bundle deploy -t dev --profile <profile>
databricks apps deploy rd-security-investigation --profile <profile>
```

### App OBO scopes (declared in `databricks.yml`)
`sql.statement-execution`, `sql.warehouses`, `catalog.catalogs`, `catalog.schemas`,
`catalog.tables`, **`genie`**.
Gotchas: the Genie scope is **`genie`**, *not* `dashboards.genie`; and do **not** set
`iam.current-user:read` explicitly — it is auto-granted and rejected if declared.

### App environment (`app/app.yaml`, generated by setup.sh)
`DBX_WAREHOUSE_ID`, `DBX_CATALOG`, `DBX_SCHEMA`, `DBX_GENIE_SPACE_ID`; `DATABRICKS_HOST` is
injected by the runtime (the app prepends `https://` if the injected value lacks a scheme).
For Lakebase: `LAKEBASE_ENDPOINT`, `PGHOST`, `PGDATABASE`, `LAKEBASE_SCHEMA=app_ops`, and
`LAKEBASE_HOTCACHE_TABLE` (`public.mv_risk_behavior_daily` for a managed synced table, or
`app_ops.metric_hot_cache` for the manual refresh). `PGUSER` is **not set** — the app defaults
it to the injected `DATABRICKS_CLIENT_ID` (its own service principal).

### Lakebase grants (run as the project owner; setup.sh does this automatically)
The app SP needs access to `app_ops` and to the hot-cache table. Because attaching the Lakebase
resource can rotate the SP's effective Postgres role, grant it explicitly rather than relying on
ownership. Connect with the endpoint host + a generated credential
(`databricks postgres generate-database-credential <endpoint>`), then:
```sql
GRANT USAGE, CREATE ON SCHEMA app_ops TO "<app-sp-client-id>";
GRANT ALL ON ALL TABLES    IN SCHEMA app_ops TO "<app-sp-client-id>";
GRANT ALL ON ALL SEQUENCES IN SCHEMA app_ops TO "<app-sp-client-id>";
ALTER DEFAULT PRIVILEGES IN SCHEMA app_ops GRANT ALL ON TABLES    TO "<app-sp-client-id>";
ALTER DEFAULT PRIVILEGES IN SCHEMA app_ops GRANT ALL ON SEQUENCES TO "<app-sp-client-id>";
-- hot-cache (managed synced table shown; run AFTER it is ONLINE):
GRANT USAGE ON SCHEMA public TO "<app-sp-client-id>";
GRANT SELECT ON public.mv_risk_behavior_daily TO "<app-sp-client-id>";
```
Get the SP client id from `databricks apps get <app-name>` → `service_principal_client_id`.

---

## 5. Acceptance test (one command)

```bash
./run_tests.sh --profile <profile> --app-url <deployed-app-url>
```
19 checks across: data layer, ABAC governance, metric views, Genie space + Genie flow,
app REST API + OBO, Lakebase history, hot-cache, and the system-table audit source.
Locally the app runs at `http://127.0.0.1:8077` (see §7); pass that as `--app-url`.

---

## 6. Governed tags prerequisite (portability)

The ABAC policies select columns by governed tag, and the tag key/value are **parameters**
(`--pii-tag` / `--sens-tag` on setup.sh, or `--pii-tag`/`--sens-tag` on
`apply_metadata_and_governance.py`) — nothing is hardcoded:
- column mask `rd_mask_pii` → `has_tag_value(<pii-key>, <pii-value>)`  (default `data_classification=pii`)
- row filter `rd_rf_sensitivity` → `has_tag_value(<sens-key>, <sens-value>)`  (default `gov_sensitivity=high`)

These **tag keys must be registered governed tags in the target account** (only allowed keys
work in `has_tag`; free-form tags are rejected, and non-policy keys fail with *"Unknown tag
policy key"*). **Values are policy-constrained and case-sensitive** — e.g. one account accepts
`sensitivity=HIGH`, another `gov_sensitivity=high`. Discover valid keys/values with the query in
§2, pass them via the flags, and `apply_metadata_and_governance.py` fails loudly if they're wrong.

The persona toggle is the policy's `TO account users EXCEPT <analyst>` clause; in production
replace the explicit principal with an **account group** (e.g. `security_analysts`).

---

## 7. Local development

```bash
# backend (OBO falls back to your profile locally via DBX_LOCAL_DEV_PROFILE)
cd app && source .venv/bin/activate
export DBX_LOCAL_DEV_PROFILE=<profile> DBX_WAREHOUSE_ID=<wh> DBX_CATALOG=<cat> DBX_SCHEMA=<sch> \
       DBX_GENIE_SPACE_ID=<id> LAKEBASE_ENDPOINT=<ep> PGHOST=<host> PGDATABASE=databricks_postgres \
       PGUSER=<you@co> LAKEBASE_SCHEMA=app_ops
.venv/bin/uvicorn backend.main:app --host 127.0.0.1 --port 8077
# frontend hot-reload (proxies /api to :8077)
cd app/frontend && bun run dev
```

---

## 8. Audit — how to query it (works even if not shown in the demo)

Because queries run under **OBO**, they are attributed to the real user in system tables.
These have a **~10-minute lag**, so use them for after-the-fact review (the app's Lakebase
`query_history` covers the real-time view):

```sql
-- Who ran what, as themselves, in the last day (Statement Execution incl. the app):
SELECT executed_by, statement_text, statement_type, produced_row_count,
       total_duration_ms, start_time
FROM system.query.history
WHERE start_time > current_timestamp() - INTERVAL 1 DAY
  AND statement_text ILIKE '%rd_security_demo%'
ORDER BY start_time DESC;

-- API-action audit (who called which API, incl. app/OBO):
SELECT event_time, user_identity.email, action_name, request_params
FROM system.access.audit
WHERE event_time > current_timestamp() - INTERVAL 1 DAY
ORDER BY event_time DESC;
```

---

## 9. Metric hot-cache: managed synced-table alternative

`bootstrap/refresh_hotcache.py` materializes the pre-aggregation into a plain Lakebase table
(portable; works without extra privileges). If you have **CREATE CATALOG** on the metastore,
you can instead use a **managed Lakebase synced table** (auto-refreshing):

```bash
databricks postgres create-catalog lakebase_rd_security \
  --json '{"spec": {"postgres_database": "databricks_postgres", "branch": "projects/rd-security-demo/branches/production"}}'
databricks postgres create-synced-table lakebase_rd_security.public.mv_risk_behavior_daily \
  --json '{"spec": {"source_table_full_name": "<cat>.<sch>.mv_risk_behavior_daily",
    "primary_key_columns": ["employee_id","event_date"], "scheduling_policy": "TRIGGERED",
    "branch": "projects/rd-security-demo/branches/production", "postgres_database": "databricks_postgres",
    "create_database_objects_if_missing": true,
    "new_pipeline_spec": {"storage_catalog": "<cat>", "storage_schema": "<sch>"}}}'
```

---

## 10. Teardown

One command removes everything setup.sh created (App, Lakebase project incl. its Postgres data,
Lakebase UC catalog, Genie space, and the demo schema). It confirms first (`--yes` to skip):

```bash
bootstrap/teardown.sh --profile <p> --catalog <c>
# also drop the catalog and/or the self-created governed tag:
bootstrap/teardown.sh --profile <p> --catalog <c> --drop-catalog --drop-tag --yes
```
`--drop-catalog` and `--drop-tag` are **off by default** — the catalog may hold other data, and
the governed tag is account-level (deleting it needs account-admin). Options mirror `setup.sh`
(`--schema`, `--app-name`, `--lakebase-project`, `--tag-key`).
