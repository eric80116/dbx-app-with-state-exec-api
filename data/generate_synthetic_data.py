"""Synthetic data generator for the RD Security Investigation demo.

Story: an insider data-exfiltration incident. Over the last ~2 weeks a small set
of engineers (incl. a contractor) escalate high-risk behavior: after-hours access
to restricted repos, clone/export actions, and large downloads to personal cloud /
USB. The security team must answer "which engineers showed high-risk behavior this
week". The anomaly is concentrated in the recent window so the story is findable.

Tables (Delta) under <catalog>.<schema>:
  dim_employee, dim_asset,
  fact_access_events, fact_code_commits, fact_data_downloads

Run:  python data/generate_synthetic_data.py --catalog <cat> --schema <schema>
Compute: Databricks Connect serverless (profile via DATABRICKS_CONFIG_PROFILE).
"""
import argparse
from databricks.connect import DatabricksSession
from pyspark.sql import functions as F
from pyspark.sql.window import Window

N_EMPLOYEES = 500
N_ASSETS = 200
N_ACCESS = 200_000
N_COMMITS = 80_000
N_DOWNLOADS = 40_000
FULL_WINDOW_DAYS = 90
RECENT_WINDOW_DAYS = 14


def get_args():
    p = argparse.ArgumentParser()
    p.add_argument("--catalog", required=True)
    p.add_argument("--schema", required=True)
    return p.parse_args()


# ---- Pure-Spark generators (no worker deps; runs entirely on serverless) ----
_FIRST = ["Alex", "Sam", "Jordan", "Taylor", "Morgan", "Casey", "Riley", "Jamie",
          "Avery", "Quinn", "Drew", "Cameron", "Skyler", "Reese", "Dakota",
          "Emerson", "Finley", "Harper", "Kai", "Logan"]
_LAST = ["Chen", "Wang", "Lin", "Kumar", "Patel", "Smith", "Johnson", "Garcia",
         "Nguyen", "Kim", "Tanaka", "Sato", "Lee", "Brown", "Wilson", "Martin",
         "Lopez", "Singh", "Ito", "Park"]


def _pick(values, seed_col):
    """Pick from a python list by a Spark int column (0-based)."""
    arr = F.array(*[F.lit(v) for v in values])
    return F.element_at(arr, (F.pmod(seed_col, F.lit(len(values))) + 1).cast("int"))


def fake_name(idx_col):
    first = _pick(_FIRST, F.abs(F.hash(idx_col, F.lit(71))))
    last = _pick(_LAST, F.abs(F.hash(idx_col, F.lit(97))))
    return F.concat(first, F.lit(" "), last)


def fake_ip(seed_col, external_col):
    """Internal traffic -> 10.x; external -> public-looking octet."""
    o1 = F.pmod(F.abs(F.hash(seed_col, F.lit(1))), F.lit(223)) + 1
    o2 = F.pmod(F.abs(F.hash(seed_col, F.lit(2))), F.lit(256))
    o3 = F.pmod(F.abs(F.hash(seed_col, F.lit(3))), F.lit(256))
    o4 = F.pmod(F.abs(F.hash(seed_col, F.lit(4))), F.lit(254)) + 1
    public = F.concat_ws(".", o1, o2, o3, o4)
    internal = F.concat_ws(".", F.lit(10), o2, o3, o4)
    return F.when(external_col, public).otherwise(internal)


def build_employees(spark, catalog, schema):
    # ~6 insiders: employee_idx 0..5, concentrated on the Platform team.
    df = spark.range(0, N_EMPLOYEES, numPartitions=8).select(
        F.col("id").alias("employee_idx"),
        F.concat(F.lit("EMP-"), F.lpad(F.col("id").cast("string"), 5, "0")).alias("employee_id"),
        fake_name(F.col("id")).alias("name"),
        (F.col("id") < 6).alias("is_insider"),
    )
    df = df.withColumn(
        "email",
        F.concat(F.lower(F.regexp_replace(F.col("name"), r"[^A-Za-z]", ".")),
                 F.col("employee_idx"), F.lit("@northpeak.com")),
    )
    # Insiders forced onto Platform team; others weighted across teams.
    df = df.withColumn(
        "team",
        F.when(F.col("is_insider"), F.lit("Platform"))
        .when(F.rand(1) < 0.22, F.lit("Platform"))
        .when(F.rand(1) < 0.44, F.lit("Backend"))
        .when(F.rand(1) < 0.62, F.lit("Data"))
        .when(F.rand(1) < 0.78, F.lit("Frontend"))
        .when(F.rand(1) < 0.90, F.lit("Mobile"))
        .otherwise(F.lit("Security")),
    )
    df = df.withColumn(
        "role",
        F.when(F.rand(2) < 0.10, F.lit("Staff Engineer"))
        .when(F.rand(2) < 0.35, F.lit("Senior Engineer"))
        .when(F.rand(2) < 0.85, F.lit("Engineer"))
        .otherwise(F.lit("Engineering Manager")),
    )
    df = df.withColumn(
        "level",
        F.when(F.col("role") == "Staff Engineer", F.lit("L6"))
        .when(F.col("role") == "Engineering Manager", F.lit("L6"))
        .when(F.col("role") == "Senior Engineer", F.lit("L5"))
        .otherwise(F.element_at(F.array(F.lit("L3"), F.lit("L4")), (F.floor(F.rand(3) * 2) + 1).cast("int"))),
    )
    df = df.withColumn(
        "location",
        F.element_at(
            F.array(F.lit("Taipei"), F.lit("Singapore"), F.lit("Tokyo"), F.lit("Seattle"), F.lit("Dublin")),
            (F.floor(F.rand(4) * 5) + 1).cast("int"),
        ),
    )
    df = df.withColumn(
        "join_date",
        F.date_sub(F.current_date(), (F.rand(5) * 2000 + 30).cast("int")),
    )
    # Insiders skew to contractor; ~12% contractors overall.
    df = df.withColumn(
        "is_contractor",
        F.when(F.col("is_insider") & (F.col("employee_idx") < 2), F.lit(True))
        .when(F.rand(6) < 0.12, F.lit(True))
        .otherwise(F.lit(False)),
    )
    out = (
        df.select(
            "employee_idx", "employee_id", "name", "email", "team", "role",
            "level", "location", "join_date", "is_contractor", "is_insider",
        )
    )
    out.write.mode("overwrite").option("overwriteSchema", "true").saveAsTable(f"{catalog}.{schema}.dim_employee")
    return spark.table(f"{catalog}.{schema}.dim_employee")


def build_assets(spark, catalog, schema):
    df = spark.range(0, N_ASSETS, numPartitions=8).select(
        F.col("id").alias("asset_idx"),
        F.concat(F.lit("AST-"), F.lpad(F.col("id").cast("string"), 4, "0")).alias("asset_id"),
    )
    df = df.withColumn(
        "asset_type",
        F.when(F.rand(11) < 0.5, F.lit("git_repo"))
        .when(F.rand(11) < 0.75, F.lit("database"))
        .when(F.rand(11) < 0.9, F.lit("dataset"))
        .otherwise(F.lit("service")),
    )
    # sensitivity skewed: restricted rare (~10%), most internal.
    df = df.withColumn(
        "sensitivity_level",
        F.when(F.rand(12) < 0.10, F.lit("restricted"))
        .when(F.rand(12) < 0.30, F.lit("confidential"))
        .when(F.rand(12) < 0.75, F.lit("internal"))
        .otherwise(F.lit("public")),
    )
    df = df.withColumn(
        "owner_team",
        F.element_at(
            F.array(F.lit("Platform"), F.lit("Backend"), F.lit("Data"), F.lit("Frontend"), F.lit("Mobile"), F.lit("Security")),
            (F.floor(F.rand(13) * 6) + 1).cast("int"),
        ),
    )
    df = df.withColumn(
        "asset_name",
        F.concat(
            F.col("owner_team"), F.lit("-"),
            F.when(F.col("asset_type") == "git_repo", F.lit("repo"))
             .when(F.col("asset_type") == "database", F.lit("db"))
             .when(F.col("asset_type") == "dataset", F.lit("ds"))
             .otherwise(F.lit("svc")),
            F.lit("-"), F.lpad((F.col("asset_idx") % 50).cast("string"), 3, "0"),
        ),
    )
    out = df.select("asset_idx", "asset_id", "asset_name", "asset_type", "sensitivity_level", "owner_team")
    out.write.mode("overwrite").option("overwriteSchema", "true").saveAsTable(f"{catalog}.{schema}.dim_asset")
    return spark.table(f"{catalog}.{schema}.dim_asset")


def _recency_days(seed_a, seed_b, insider_col):
    """Insiders concentrate in the recent window; others spread over full window."""
    return F.when(
        insider_col & (F.rand(seed_a) < 0.65), F.rand(seed_b) * RECENT_WINDOW_DAYS
    ).otherwise(F.rand(seed_b) * FULL_WINDOW_DAYS)


def _event_ts(days_ago_col):
    return (F.unix_timestamp(F.current_timestamp()) - (days_ago_col * 86400)).cast("timestamp")


def build_access_events(spark, catalog, schema, emp, ast):
    emp_l = emp.select("employee_idx", "employee_id", "team", "is_insider", "is_contractor")
    ast_l = ast.select("asset_idx", "asset_id", "sensitivity_level", "asset_type")

    df = spark.range(0, N_ACCESS, numPartitions=16).select(
        F.concat(F.lit("ACC-"), F.lpad(F.col("id").cast("string"), 9, "0")).alias("event_id"),
        (F.abs(F.hash(F.col("id"), F.lit(101))) % N_EMPLOYEES).alias("employee_idx"),
        (F.abs(F.hash(F.col("id"), F.lit(202))) % N_ASSETS).alias("asset_idx"),
    )
    df = df.join(emp_l, "employee_idx").join(ast_l, "asset_idx")

    df = df.withColumn("days_ago", _recency_days(21, 22, F.col("is_insider")))
    df = df.withColumn("event_time", _event_ts(F.col("days_ago")))
    df = df.withColumn("event_hour", F.hour("event_time"))
    # after-hours: derived from hour, boosted for insiders
    df = df.withColumn(
        "is_after_hours",
        ((F.col("event_hour") < 7) | (F.col("event_hour") >= 20))
        | (F.col("is_insider") & (F.rand(23) < 0.55)),
    )
    df = df.withColumn(
        "action",
        F.when(F.col("is_insider") & (F.rand(24) < 0.45),
               F.element_at(F.array(F.lit("clone"), F.lit("export")), (F.floor(F.rand(25) * 2) + 1).cast("int")))
        .when(F.rand(24) < 0.70, F.lit("read"))
        .when(F.rand(24) < 0.88, F.lit("write"))
        .when(F.rand(24) < 0.96, F.lit("clone"))
        .otherwise(F.lit("export")),
    )
    df = df.withColumn(
        "is_external_ip",
        F.when(F.col("is_insider") & (F.rand(26) < 0.4), F.lit(True)).otherwise(F.rand(26) < 0.08),
    )
    df = df.withColumn("source_ip", fake_ip(F.col("event_id"), F.col("is_external_ip")))
    # risk score composed from factors, clipped 0..100
    risk = (
        F.rand(27) * 25
        + F.when(F.col("is_after_hours"), 20).otherwise(0)
        + F.when(F.col("sensitivity_level") == "restricted", 28)
         .when(F.col("sensitivity_level") == "confidential", 15).otherwise(0)
        + F.when(F.col("action").isin("clone", "export"), 20).otherwise(0)
        + F.when(F.col("is_external_ip"), 15).otherwise(0)
        + F.when(F.col("is_insider") & (F.col("days_ago") <= RECENT_WINDOW_DAYS), 22).otherwise(0)
    )
    df = df.withColumn("risk_score", F.least(F.lit(100.0), F.greatest(F.lit(0.0), risk)).cast("int"))

    out = df.select(
        "event_id", "employee_id", "asset_id", "event_time", "action",
        "source_ip", "is_after_hours", "is_external_ip", "risk_score",
        F.col("team"),  # denormalized for governance / exploration
        F.col("sensitivity_level").alias("asset_sensitivity"),
    )
    out.write.mode("overwrite").option("overwriteSchema", "true").saveAsTable(f"{catalog}.{schema}.fact_access_events")


def build_commits(spark, catalog, schema, emp, ast):
    emp_l = emp.select("employee_idx", "employee_id", "team", "is_insider")
    repos = ast.filter(F.col("asset_type") == "git_repo").select(
        "asset_idx", F.col("asset_id").alias("repo_asset_id"), "sensitivity_level"
    )
    n_repos = repos.count()
    df = spark.range(0, N_COMMITS, numPartitions=16).select(
        F.concat(F.lit("CMT-"), F.lpad(F.col("id").cast("string"), 9, "0")).alias("commit_id"),
        (F.abs(F.hash(F.col("id"), F.lit(303))) % N_EMPLOYEES).alias("employee_idx"),
        (F.abs(F.hash(F.col("id"), F.lit(404))) % F.lit(n_repos)).alias("repo_rownum"),
    )
    repos_indexed = repos.withColumn("repo_rownum", F.row_number().over(
        Window.orderBy("asset_idx")) - 1)
    df = df.join(emp_l, "employee_idx").join(
        repos_indexed.select("repo_rownum", "repo_asset_id", "sensitivity_level"), "repo_rownum")
    df = df.withColumn("days_ago", _recency_days(31, 32, F.col("is_insider")))
    df = df.withColumn("commit_time", _event_ts(F.col("days_ago")))
    df = df.withColumn("commit_hour", F.hour("commit_time"))
    df = df.withColumn(
        "is_after_hours",
        ((F.col("commit_hour") < 7) | (F.col("commit_hour") >= 20)) | (F.col("is_insider") & (F.rand(33) < 0.5)),
    )
    df = df.withColumn("files_changed", (F.rand(34) * 30 + 1).cast("int"))
    df = df.withColumn(
        "sensitive_files_touched",
        F.when(F.col("is_insider") & (F.rand(35) < 0.5), (F.rand(36) * 8 + 1).cast("int"))
        .when((F.col("sensitivity_level") == "restricted") & (F.rand(35) < 0.3), (F.rand(36) * 4).cast("int"))
        .otherwise(0),
    )
    out = df.select(
        "commit_id", "employee_id", "repo_asset_id", "commit_time",
        "files_changed", "sensitive_files_touched", "is_after_hours",
        F.col("team"),
        F.col("sensitivity_level").alias("repo_sensitivity"),
    )
    out.write.mode("overwrite").option("overwriteSchema", "true").saveAsTable(f"{catalog}.{schema}.fact_code_commits")


def build_downloads(spark, catalog, schema, emp, ast):
    emp_l = emp.select("employee_idx", "employee_id", "team", "is_insider")
    ast_l = ast.select("asset_idx", "asset_id", "sensitivity_level")
    df = spark.range(0, N_DOWNLOADS, numPartitions=16).select(
        F.concat(F.lit("DL-"), F.lpad(F.col("id").cast("string"), 9, "0")).alias("download_id"),
        (F.abs(F.hash(F.col("id"), F.lit(505))) % N_EMPLOYEES).alias("employee_idx"),
        (F.abs(F.hash(F.col("id"), F.lit(606))) % N_ASSETS).alias("asset_idx"),
    )
    df = df.join(emp_l, "employee_idx").join(ast_l, "asset_idx")
    df = df.withColumn("days_ago", _recency_days(41, 42, F.col("is_insider")))
    df = df.withColumn("event_time", _event_ts(F.col("days_ago")))
    # destination: insiders skew to personal_cloud/usb
    df = df.withColumn(
        "destination_type",
        F.when(F.col("is_insider") & (F.rand(43) < 0.55),
               F.element_at(F.array(F.lit("personal_cloud"), F.lit("usb"), F.lit("email")),
                            (F.floor(F.rand(44) * 3) + 1).cast("int")))
        .when(F.rand(43) < 0.82, F.lit("internal"))
        .when(F.rand(43) < 0.92, F.lit("email"))
        .when(F.rand(43) < 0.97, F.lit("personal_cloud"))
        .otherwise(F.lit("usb")),
    )
    # bytes: log-normal; insiders much larger. Use exp of normal via randn.
    base_log = F.when(F.col("is_insider") & (F.col("days_ago") <= RECENT_WINDOW_DAYS),
                      F.lit(19.0) + F.randn(45) * 1.0).otherwise(F.lit(15.0) + F.randn(45) * 1.2)
    df = df.withColumn("bytes_downloaded", F.exp(base_log).cast("bigint"))
    df = df.withColumn(
        "is_flagged",
        (F.col("destination_type").isin("personal_cloud", "usb"))
        & ((F.col("sensitivity_level").isin("restricted", "confidential")) | (F.col("bytes_downloaded") > 200_000_000)),
    )
    out = df.select(
        "download_id", "employee_id", "asset_id", "event_time",
        "bytes_downloaded", "destination_type", "is_flagged",
        F.col("team"),
        F.col("sensitivity_level").alias("asset_sensitivity"),
    )
    out.write.mode("overwrite").option("overwriteSchema", "true").saveAsTable(f"{catalog}.{schema}.fact_data_downloads")


def main():
    args = get_args()
    catalog, schema = args.catalog, args.schema
    spark = DatabricksSession.builder.serverless(True).getOrCreate()
    spark.sql(f"CREATE SCHEMA IF NOT EXISTS {catalog}.{schema}")

    print("Building dim_employee ...")
    emp = build_employees(spark, catalog, schema)
    print("Building dim_asset ...")
    ast = build_assets(spark, catalog, schema)
    print("Building fact_access_events ...")
    build_access_events(spark, catalog, schema, emp, ast)
    print("Building fact_code_commits ...")
    build_commits(spark, catalog, schema, emp, ast)
    print("Building fact_data_downloads ...")
    build_downloads(spark, catalog, schema, emp, ast)
    print("Done.")


if __name__ == "__main__":
    main()
