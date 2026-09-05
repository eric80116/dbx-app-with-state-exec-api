"""Apply metadata (comments), governed tags, metric views, and ABAC governance
for the RD Security Investigation demo. Idempotent (CREATE OR REPLACE / SET TAGS).

GOVERNANCE = ABAC policies + governed tags (production-realistic; central schema-level
policies auto-apply to any tagged column, incl. future tables). Verified on fevm1.

  - PII columns (email, name, source_ip) tagged `data_classification=pii`.
    ABAC COLUMN MASK policy masks them for everyone EXCEPT the analyst principal.
  - fact `asset_sensitivity` columns tagged `gov_sensitivity=high`.
    ABAC ROW FILTER policy hides `restricted`-asset rows for everyone EXCEPT the analyst.
  - Persona toggle = the policy's TO ... EXCEPT ... principal. Analyst (excepted) sees
    plaintext + all rows; a regular engineer sees masked PII and no restricted rows.
    Production swaps the explicit principal for an account group (e.g. `security_analysts`).

PREREQUISITE (portability): the governed tag keys `data_classification` and
`gov_sensitivity` must exist in the target account (account-admin defines governed tags).
If they don't, the SET TAGS / policy steps fail with a clear message -- see docs/DEPLOYMENT.md.

Run: python data/apply_metadata_and_governance.py --catalog <cat> --schema <schema> \
        [--analyst-principal user@co.com]
"""
import argparse
import os
from databricks.connect import DatabricksSession


def get_args():
    p = argparse.ArgumentParser()
    p.add_argument("--catalog", required=True)
    p.add_argument("--schema", required=True)
    p.add_argument("--analyst-principal", default="eric.liou@databricks.com",
                   help="Principal exempted from masking/row-filter (the 'analyst' persona). "
                        "Use an account group name in production.")
    # Governed-tag keys/values differ per account. Defaults are for the fevm1 account.
    p.add_argument("--pii-tag", default="data_classification=pii",
                   help="Governed tag key=value marking PII columns for the mask policy.")
    p.add_argument("--sens-tag", default="gov_sensitivity=high",
                   help="Governed tag key=value marking the row-scope (sensitivity) column.")
    return p.parse_args()


def main():
    args = get_args()
    cat, sch = args.catalog, args.schema
    fq = f"{cat}.{sch}"
    analyst = args.analyst_principal
    pii_key, pii_val = args.pii_tag.split("=", 1)
    sens_key, sens_val = args.sens_tag.split("=", 1)
    spark = DatabricksSession.builder.serverless(True).getOrCreate()

    def run(sql, soft=False):
        label = " ".join(sql.split())[:90]
        try:
            spark.sql(sql)
            print(f"  OK  {label}")
        except Exception as e:
            msg = str(e).splitlines()[0][:170]
            if soft:
                print(f"  SKIP {label}  -- {msg}")
            else:
                print(f"  FAIL {label}\n       {msg}")
                raise

    # ---------------------------------------------------------------- comments
    print("== Table & column comments ==")
    run(f"COMMENT ON TABLE {fq}.dim_employee IS "
        f"'Engineer/employee master. One row per employee (team, role, contractor flag). "
        f"is_insider flags the seeded exfiltration actors (demo ground-truth).'")
    run(f"COMMENT ON TABLE {fq}.dim_asset IS "
        f"'Asset master: git repos, databases, datasets, services, with sensitivity classification.'")
    run(f"COMMENT ON TABLE {fq}.fact_access_events IS "
        f"'Access/activity events on assets (read/write/clone/export) with per-event risk_score (0-100), "
        f"after-hours and external-IP flags. Grain: one access event. team & asset_sensitivity denormalized.'")
    run(f"COMMENT ON TABLE {fq}.fact_code_commits IS "
        f"'Code commits to git repos, incl. sensitive files touched and after-hours flag. Grain: one commit.'")
    run(f"COMMENT ON TABLE {fq}.fact_data_downloads IS "
        f"'Data download/egress events with destination (internal/personal_cloud/usb/email), bytes, and "
        f"flagged status. Grain: one download. Core signal for exfiltration detection.'")

    col_comments = {
        "dim_employee": {
            "employee_id": "Primary key, format EMP-#####",
            "team": "Engineering team (Platform, Backend, Data, Frontend, Mobile, Security)",
            "role": "Job role (Engineer, Senior Engineer, Staff Engineer, Engineering Manager)",
            "is_contractor": "True if external contractor (higher baseline risk)",
            "is_insider": "DEMO ground-truth: True for seeded exfiltration actors",
            "email": "Corporate email (PII; masked for non-analysts via ABAC)",
        },
        "dim_asset": {
            "asset_id": "Primary key, format AST-####",
            "sensitivity_level": "Data classification: public < internal < confidential < restricted",
            "asset_type": "git_repo, database, dataset, or service",
        },
        "fact_access_events": {
            "risk_score": "Composite risk 0-100. >=70 considered HIGH RISK.",
            "action": "read, write, clone, or export (clone/export are higher risk)",
            "is_after_hours": "Event occurred outside 07:00-20:00 local",
            "is_external_ip": "Source IP outside corporate network",
            "source_ip": "Source IP (PII; masked for non-analysts via ABAC)",
            "team": "Employee's team (denormalized)",
            "asset_sensitivity": "Accessed asset's sensitivity (denormalized; drives ABAC row filter)",
        },
        "fact_data_downloads": {
            "destination_type": "Where data went: internal, personal_cloud, usb, email",
            "bytes_downloaded": "Egress size in bytes (log-normal; large spikes are suspicious)",
            "is_flagged": "True when sensitive data went to personal_cloud/usb or exceeded size threshold",
            "asset_sensitivity": "Asset sensitivity (denormalized; drives ABAC row filter)",
        },
    }
    for tbl, cols in col_comments.items():
        for col, c in cols.items():
            c_esc = c.replace("'", "''")  # escape single quotes for SQL string literal
            run(f"ALTER TABLE {fq}.{tbl} ALTER COLUMN {col} COMMENT '{c_esc}'")

    # ------------------------------------------------------------ governed tags
    # These keys must be registered governed tags in the account (see PREREQUISITE).
    print(f"== Governed tags (ABAC selectors: pii={pii_key}={pii_val}, sens={sens_key}={sens_val}) ==")
    run(f"ALTER TABLE {fq}.dim_employee ALTER COLUMN email SET TAGS ('{pii_key}'='{pii_val}')")
    run(f"ALTER TABLE {fq}.dim_employee ALTER COLUMN name SET TAGS ('{pii_key}'='{pii_val}')")
    run(f"ALTER TABLE {fq}.fact_access_events ALTER COLUMN source_ip SET TAGS ('{pii_key}'='{pii_val}')")
    run(f"ALTER TABLE {fq}.fact_access_events ALTER COLUMN asset_sensitivity SET TAGS ('{sens_key}'='{sens_val}')")
    run(f"ALTER TABLE {fq}.fact_data_downloads ALTER COLUMN asset_sensitivity SET TAGS ('{sens_key}'='{sens_val}')")

    # ---------------------------------------------------------- metric views
    # Definitions live as YAML in ../metric_views/*.yaml (single source of truth). Each file's
    # {{FQ}} is substituted with <catalog>.<schema> and wrapped in CREATE VIEW ... WITH METRICS.
    print("== Metric views ==")
    mv_dir = os.path.join(os.path.dirname(__file__), "..", "metric_views")
    for view_name in ("mv_risk_behavior", "mv_data_movement"):
        with open(os.path.join(mv_dir, f"{view_name}.yaml")) as f:
            body = f.read().replace("{{FQ}}", fq)
        run(f"CREATE OR REPLACE VIEW {fq}.{view_name}\nWITH METRICS LANGUAGE YAML AS $$\n{body}\n$$")

    # ----------------------------------------------- ABAC functions + policies
    print("== ABAC functions ==")
    run(f"""CREATE OR REPLACE FUNCTION {fq}.mask_pii(v STRING)
RETURNS STRING
COMMENT 'PII column mask used by ABAC policy rd_mask_pii'
RETURN CASE WHEN v IS NULL THEN NULL ELSE '***masked***' END""")
    run(f"""CREATE OR REPLACE FUNCTION {fq}.rf_sensitivity(sens STRING)
RETURNS BOOLEAN
COMMENT 'Row filter used by ABAC policy rd_rf_sensitivity: hides restricted-asset rows'
RETURN sens <> 'restricted'""")

    print("== ABAC policies (analyst principal exempted) ==")
    run(f"""CREATE OR REPLACE POLICY rd_mask_pii ON SCHEMA {fq}
COMMENT 'Mask PII (data_classification=pii) for all except the analyst persona'
COLUMN MASK {fq}.mask_pii
TO `account users` EXCEPT `{analyst}`
FOR TABLES
MATCH COLUMNS has_tag_value('{pii_key}', '{pii_val}') AS c
ON COLUMN c""")
    run(f"""CREATE OR REPLACE POLICY rd_rf_sensitivity ON SCHEMA {fq}
COMMENT 'Hide restricted-asset rows for all except the analyst persona'
ROW FILTER {fq}.rf_sensitivity
TO `account users` EXCEPT `{analyst}`
FOR TABLES
MATCH COLUMNS has_tag_value('{sens_key}', '{sens_val}') AS s
USING COLUMNS (s)""")

    print("Done.")


if __name__ == "__main__":
    main()
