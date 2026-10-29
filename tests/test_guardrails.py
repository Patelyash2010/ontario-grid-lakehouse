import pytest

pytest.importorskip("sqlglot")

from chatbot.guardrails import UnsafeSQL, validate_sql  # noqa: E402
from chatbot.schema import ALLOWED_TABLES  # noqa: E402


def ok(sql, max_rows=200):
    return validate_sql(sql, ALLOWED_TABLES, max_rows)


@pytest.mark.parametrize("sql", [
    "SELECT MAX(peak_demand_mw) FROM gold.mart_daily_summary",
    "select max(peak_demand_mw) from ontgrid.gold.mart_daily_summary;",
    "SELECT * FROM mart_daily_summary",
    "WITH t AS (SELECT delivery_date FROM gold.fact_grid_hourly) SELECT * FROM t",
    "SELECT hour_ending FROM gold.dim_hour UNION ALL SELECT day_of_week FROM gold.dim_date",
    "SELECT f.fuel_name, SUM(g.output_mwh) FROM gold.fact_generation_hourly g "
    "JOIN gold.dim_fuel f ON f.fuel_type = g.fuel_type GROUP BY 1",
])
def test_allowed_queries_pass(sql):
    assert "gold." in ok(sql)


@pytest.mark.parametrize("sql, reason", [
    ("DROP TABLE gold.dim_date", "only SELECT"),
    ("DELETE FROM gold.dim_date", "only SELECT"),
    ("UPDATE gold.dim_date SET year = 1", "only SELECT"),
    ("INSERT INTO gold.dim_date SELECT * FROM gold.dim_date", "only SELECT"),
    ("MERGE INTO gold.dim_date t USING gold.dim_date s ON t.date = s.date WHEN MATCHED THEN DELETE", "only SELECT"),
    ("CREATE TABLE gold.x AS SELECT 1", "only SELECT"),
    ("SET spark.sql.ansi.enabled = false", "only SELECT"),
    ("SELECT 1; DROP TABLE gold.dim_date", "one statement"),
    ("SELECT * FROM silver.ieso_demand_hourly", "not allowed"),
    ("SELECT * FROM ontgrid.ops.pipeline_runs", "not allowed"),
    ("SELECT * FROM silver.dim_date", "gold schema"),
    ("SELECT * FROM hive_metastore.gold.dim_date", "ontgrid catalog"),
    ("SELECT * FROM read_files('abfss://bronze@x.dfs.core.windows.net/')", "table functions"),
    ("SELECT current_user()", "current_user"),
    ("SELECT * FROM gold.dim_date WHERE date IN (SELECT date FROM silver.secret)", "not allowed"),
    ("", "empty"),
    ("this is not sql", None),
])
def test_unsafe_queries_blocked(sql, reason):
    with pytest.raises(UnsafeSQL, match=reason):
        ok(sql)


def test_limit_added_when_missing():
    assert ok("SELECT delivery_date FROM gold.mart_daily_summary").rstrip().endswith("LIMIT 200")


def test_large_limit_lowered_small_limit_kept():
    assert ok("SELECT date FROM gold.dim_date LIMIT 100000").rstrip().endswith("LIMIT 200")
    assert ok("SELECT date FROM gold.dim_date LIMIT 5").rstrip().endswith("LIMIT 5")


def test_tables_normalised_to_gold_schema():
    sql = ok("SELECT * FROM ontgrid.gold.dim_hour")
    assert "ontgrid" not in sql and "gold.dim_hour" in sql
