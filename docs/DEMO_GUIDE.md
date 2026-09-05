# Demo Guide — RD Security Investigation

A 10–15 min walkthrough showing how to **safely expose the Statement Execution API to users**
via a Databricks App with **OBO**, using **UC metric views** as the governed semantic layer
(replacing Cube) and **Genie** as the analyst's agent.

---

## The story (say this first)

> "Your Statement Execution API is machine-to-machine — one service principal identity. Hand
> that to users directly and everyone shares that identity; Unity Catalog governance can't tell
> them apart. Our answer: put a **Databricks App in front that runs every query as the actual
> user (On-Behalf-Of)**. UC grants, column masks, and row filters are enforced per person — on
> the UI path *and* the API path. The metrics come from **governed metric views** (your Cube
> replacement), and an **agent (Genie)** helps analysts find the right data and SQL."

**Scenario:** an insider data-exfiltration investigation. Over the last two weeks, a few
engineers on the **Platform** team (including two contractors) escalated risky behavior —
after-hours access to restricted repos, clone/export actions, and large downloads to personal
cloud / USB. The demo answers *"which engineers showed high-risk behavior this week?"* and
shows that governance holds no matter how the data is reached.

**Ground truth** (so you know what "right" looks like): the top ~6 high-risk engineers this week
are all Platform — Taylor Ito, Jordan Lee, Avery Brown, **Cameron Singh (contractor)**, Kai
Tanaka, **Reese Ito (contractor)** — with a sharp drop-off to everyone else.

---

## Setup before the room
- App deployed and reachable (`databricks apps get rd-security-investigation` → URL).
- Run `./run_tests.sh --profile <p> --app-url <url>` — expect **19 passed**.
- Log in to the app as the **analyst** persona (the principal in the ABAC `EXCEPT` clause).
- Have a second browser / user ready for the "engineer" view, *or* use the live policy flip (below).

---

## Flow

### 1. Data Explorer — governed, self-service discovery
- Open the app → **Data Explorer**.
- Point out the object list: 5 tables + **two `mv_*` metric views** (METRIC_VIEW badge) — "this
  is the governed semantic layer that replaces Cube; measures are defined once, centrally."
- Click **`mv_risk_behavior`** → show dimensions/measures with descriptions.
- Click **`dim_employee`** → point at `email` / `name` carrying governed tags
  `data_classification=pii` — "these tags drive governance automatically."

**Say:** "Users explore only what they can access, with full descriptions — no tribal knowledge."

### 2. Ask Genie — the analyst's agent
- Go to **Ask Genie**, click the chip *"Which engineers showed the most high-risk behavior this week?"* → **Ask Genie**.
- (~30–80s.) Genie returns a narrative + a table naming the **6 Platform engineers** (contractors
  flagged), plus a **Suggested SQL** block and an *Open in Databricks* link.

**Say:** "Genie runs **as the user** through the managed MCP — so it only ever sees data this
person is entitled to, and new metric views become askable automatically, with no 30-table cap."

### 3. SQL Runner — governed execution + the API handoff
- Click **Run in SQL Runner →** on the suggested SQL (or use the prefilled query) → **Run (as you, OBO)**.
- Results appear; note the footer *"… as \<your email\>"* — "executed as you, not a shared SP."
- Click **Copy as API call** → show the **curl** and **python** snippets.

**Say:** "Same governed execution path, now callable programmatically with the user's own token
(App OAuth per-user). Paste this into a notebook or a cron job — UC still enforces their
permissions. That's how you expose Statement Execution safely."

### 4. Governance is real — same SQL, different results (the money shot)
Show that a non-analyst sees masked PII and no restricted-asset rows.

**Option A — two users:** open the app as a regular engineer (not in the `EXCEPT` clause), run the
same query → `source_ip`/`email` show `***masked***` and restricted-asset rows disappear.

**Option B — single-user live flip** (no second login). In a SQL cell / CLI, temporarily apply
the mask/row-filter to yourself, refresh the app, then restore:
```sql
-- engineer view (mask applies to everyone):
CREATE OR REPLACE POLICY rd_mask_pii ON SCHEMA <cat>.rd_security_demo
  COLUMN MASK <cat>.rd_security_demo.mask_pii TO `account users`
  FOR TABLES MATCH COLUMNS has_tag_value('data_classification','pii') AS c ON COLUMN c;
-- ...show masked results in the app, then RESTORE analyst view:
CREATE OR REPLACE POLICY rd_mask_pii ON SCHEMA <cat>.rd_security_demo
  COLUMN MASK <cat>.rd_security_demo.mask_pii TO `account users` EXCEPT `<you@co>`
  FOR TABLES MATCH COLUMNS has_tag_value('data_classification','pii') AS c ON COLUMN c;
```
**Say:** "One central ABAC policy, driven by governed tags — it covers every tagged column,
including tables you add later. In production the exemption is an account group, not a person."

### 5. Lakebase — real-time history + a fast cache
- Open **History**: your recent queries appear **instantly** (Lakebase), unlike system tables.
- **Say:** "Lakebase is the app's operational store — query history, saved queries — and a
  **pre-aggregation hot-cache** (`/api/hotcache`) that serves the top-risk list from Postgres in
  milliseconds. That's the Cube pre-aggregation idea, on Databricks."

### 6. Audit trail — governance closes the loop
- **Say:** "Because everything ran under OBO, the authoritative audit is automatic in system
  tables — attributed to the real user." Show (note the ~10-min lag → use for after-the-fact review):
```sql
SELECT executed_by, statement_text, produced_row_count, start_time
FROM system.query.history
WHERE start_time > current_timestamp() - INTERVAL 1 DAY
  AND statement_text ILIKE '%rd_security_demo%'
ORDER BY start_time DESC;
```

---

## Closing (tie back to the ask)
> "M2M Statement Execution stays server-side. Users get a governed UI **and** a governed API,
> both running as themselves. Metric views are your semantic layer, Genie is the analyst's
> agent, Lakebase is the app store + cache, and audit is free from system tables. Nothing here
> is bespoke — it's Databricks Apps + OBO + UC ABAC + Genie + Lakebase."

## If something misbehaves
- **Genie slow / times out** → use the prefilled SQL in SQL Runner (the governed-execution and
  API story stands on its own); Genie latency is model-side.
- **Genie returns no Suggested SQL block** → the answer + table are still in the reply; open the
  *Open in Databricks* link to show the query.
- **Hot-cache empty** → run `bootstrap/refresh_hotcache.py`; the SQL Runner path is unaffected.
- **Masked view not toggling** → confirm you edited the right policy and refreshed; re-run the
  RESTORE statement to return to analyst view.
