# RD Security Investigation — Secure Statement Execution Demo

A best-practice reference for **safely exposing the Databricks Statement Execution API to
users**. The M2M API stays server-side; a **Databricks App** fronts it and runs every query
**On-Behalf-Of the user (OBO)**, so Unity Catalog governance is enforced per person — on both
the UI path and a programmatic REST API.

| Layer | What |
|---|---|
| Serving | **Databricks App** (FastAPI + React) — UI *and* REST API sharing one OBO execution path |
| Semantic | **UC metric views** (`mv_risk_behavior`, `mv_data_movement`) — the Cube replacement |
| Agent | **Genie One** managed MCP (workspace-wide, OBO) + a curated Genie space (certified path) |
| Governance | **ABAC policies + governed tags** — column masks + row filter, enforced under OBO |
| App store | **Lakebase** — real-time query history + saved queries |
| Audit | `system.query.history` / `system.access.audit` (per-user via OBO; ~10-min lag) |

**Scenario:** insider data-exfiltration investigation over synthetic R&D engineering activity.
Answers *"which engineers showed high-risk behavior this week?"* — the seeded signal points to
6 Platform engineers (2 contractors).

## Quick start
```bash
# Deploy everything. Nothing is workspace-hardcoded — pass your values (see DEPLOYMENT.md §2).
# --pii-tag / --sens-tag keys must be governed tags registered in the target account.
bootstrap/setup.sh --profile <profile> --catalog <catalog> \
    [--pii-tag data_classification=pii] [--sens-tag gov_sensitivity=high]

./run_tests.sh --profile <profile> --app-url <app-url>   # 19-check acceptance test
```
The deploy runs a **preflight** that lists the account's governed tags and your resolved
settings, then asks you to confirm before making any changes.

## Docs
- **[docs/DEPLOYMENT.md](docs/DEPLOYMENT.md)** — deploy, config, governed-tag prerequisites, audit queries, teardown.
- **[docs/DEMO_GUIDE.md](docs/DEMO_GUIDE.md)** — the room walkthrough with talk-track and the persona toggle.
- **[PROGRESS.md](PROGRESS.md)** — build state / resume log.

## Layout
```
data/          synthetic data gen + metadata/tags/ABAC governance (databricks-connect)
metric_views/  the governed metric views as YAML (mv_risk_behavior, mv_data_movement)
bootstrap/     setup.sh (deploy), teardown.sh, genie_agent.json
app/           FastAPI backend (backend/) + React frontend (frontend/) + app.yaml.example
tests/         pytest acceptance suite   ·   run_tests.sh (top level)
docs/          DEPLOYMENT.md, DEMO_GUIDE.md, architecture_explainer.html
databricks.yml DAB (deploys the App)
```
Metric views are defined once in `metric_views/*.yaml` (with a `{{FQ}}` placeholder for
`<catalog>.<schema>`) and applied by `data/apply_metadata_and_governance.py`.

## Notes
- Governance uses ABAC + **governed tags** (`data_classification`, `gov_sensitivity`) — these
  keys must be registered in the target account (see DEPLOYMENT.md §6).
- Genie One MCP scales without the 30-table cap of a curated space and respects UC permissions.
- The app supports a hybrid Genie scope: the curated space (fast, scoped) or Genie One (broad).
