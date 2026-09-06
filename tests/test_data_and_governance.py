"""Data layer + ABAC governance validation."""
import os
import pytest
from conftest import FQ, CATALOG, SCHEMA

# Optional exact tag-key assertions. Deploys self-create `rd_data_class` (--create-tags) or
# reuse existing keys (--pii-tag/--sens-tag), so the key name varies per workspace. When
# run_tests.sh knows the keys it exports them; otherwise the check is key-agnostic.
PII_TAG_KEY = os.environ.get("DBX_PII_TAG_KEY", "")
SENS_TAG_KEY = os.environ.get("DBX_SENS_TAG_KEY", "")

EXPECTED_TABLES = ["dim_employee", "dim_asset", "fact_access_events", "fact_code_commits", "fact_data_downloads"]
EXPECTED_METRIC_VIEWS = ["mv_risk_behavior", "mv_data_movement"]


def test_tables_exist_and_populated(sql):
    for t in EXPECTED_TABLES:
        n = int(sql(f"SELECT count(*) FROM {FQ}.{t}")[0][0])
        assert n > 0, f"{t} is empty"


def test_metric_views_exist(sql):
    rows = sql(f"SELECT table_name, table_type FROM {CATALOG}.information_schema.tables "
               f"WHERE table_schema='{SCHEMA}'")
    names = {r[0] for r in rows}
    for mv in EXPECTED_METRIC_VIEWS:
        assert mv in names, f"missing metric view {mv}"


def test_metric_view_queryable(sql):
    rows = sql(f"SELECT `Team`, MEASURE(`High Risk Event Count`) FROM {FQ}.mv_risk_behavior "
               f"GROUP BY `Team` ORDER BY 2 DESC LIMIT 3")
    assert len(rows) > 0


def test_tables_have_comments(sql):
    rows = sql(f"SELECT table_name, comment FROM {CATALOG}.information_schema.tables "
               f"WHERE table_schema='{SCHEMA}' AND table_name IN "
               f"({','.join(chr(39)+t+chr(39) for t in EXPECTED_TABLES)})")
    for name, comment in rows:
        assert comment and len(comment) > 10, f"{name} lacks a comment"


def test_governed_tags_present(sql):
    """The PII columns must carry the governed tag the column-mask policy matches on, and a
    sensitivity tag must exist for the row filter. Tag KEYS vary per deploy (rd_data_class
    when self-created, or the customer's existing keys), so assert key-agnostically unless
    run_tests.sh pinned the exact keys via DBX_PII_TAG_KEY / DBX_SENS_TAG_KEY."""
    rows = sql(f"SELECT table_name, column_name, tag_name FROM {CATALOG}.information_schema.column_tags "
               f"WHERE schema_name='{SCHEMA}'")
    tags = {(r[0], r[1], r[2]) for r in rows}

    def col_tagged(tbl, col):
        return any(t[0] == tbl and t[1] == col and (not PII_TAG_KEY or t[2] == PII_TAG_KEY) for t in tags)

    key_hint = f" with key '{PII_TAG_KEY}'" if PII_TAG_KEY else ""
    assert col_tagged("dim_employee", "email"), f"dim_employee.email is not governed-tagged{key_hint} (PII mask relies on it)"
    assert col_tagged("fact_access_events", "source_ip"), f"fact_access_events.source_ip is not governed-tagged{key_hint}"
    if SENS_TAG_KEY:
        assert any(t[2] == SENS_TAG_KEY for t in tags), f"no '{SENS_TAG_KEY}' tag for the row filter"
    else:
        assert tags, "no governed column tags applied to the schema"


def test_abac_policies_exist(sql):
    rows = sql(f"SHOW POLICIES ON SCHEMA {FQ}")
    names = {r[0] for r in rows}  # first column is policy name
    assert "rd_mask_pii" in names, "column-mask policy missing"
    assert "rd_rf_sensitivity" in names, "row-filter policy missing"


def test_analyst_sees_plaintext(sql):
    """The analyst principal (running these tests) sees unmasked PII (EXCEPT clause)."""
    email = sql(f"SELECT email FROM {FQ}.dim_employee WHERE email LIKE '%@northpeak.com' LIMIT 1")[0][0]
    assert "@northpeak.com" in email and "masked" not in email


def test_insider_signal_present(sql):
    """Ground-truth: the seeded insiders (Platform) dominate recent high-risk events."""
    rows = sql(f"""
        SELECT e.is_insider, SUM(CASE WHEN a.risk_score>=70 THEN 1 ELSE 0 END) AS hr
        FROM {FQ}.fact_access_events a JOIN {FQ}.dim_employee e USING (employee_id)
        WHERE a.event_time > current_timestamp() - INTERVAL 7 DAYS
        GROUP BY e.is_insider ORDER BY e.is_insider""")
    # data_array returns booleans as strings ('true'/'false')
    by = {str(r[0]).lower(): int(r[1]) for r in rows}
    assert by.get("true", 0) > by.get("false", 0), f"insider high-risk signal not dominant: {by}"
