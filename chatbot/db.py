"""Where the SQL runs: a local DuckDB copy of gold, or the live Databricks warehouse.

Both take the same input: guarded SQL in Databricks dialect using gold.<table>
names. DuckDB translates it with sqlglot first, so the model only ever has to
learn one dialect.
"""
from __future__ import annotations

import os
from pathlib import Path

import pandas as pd
import sqlglot

from .guardrails import DIALECT
from .schema import ALLOWED_TABLES


class DuckDBBackend:
    """Gold tables as Parquet files (exported once by scripts/export_gold.py)."""

    name = "duckdb"

    def __init__(self, gold_dir: Path):
        import duckdb

        gold_dir = Path(gold_dir)
        files = {p.stem: p for p in gold_dir.glob("*.parquet")}
        missing = sorted(ALLOWED_TABLES - set(files))
        if missing:
            raise FileNotFoundError(
                f"missing gold files in {gold_dir}: {', '.join(missing)}. "
                "Run: python scripts/export_gold.py"
            )
        self.con = duckdb.connect(database=":memory:")
        self.con.execute("CREATE SCHEMA gold")
        for name in sorted(ALLOWED_TABLES):
            path = str(files[name]).replace("'", "''")
            self.con.execute(f"CREATE TABLE gold.{name} AS SELECT * FROM read_parquet('{path}')")
        # tables are loaded into memory; from here on the model's SQL cannot
        # touch the file system at all, even if a guardrail were bypassed
        self.con.execute("SET enable_external_access = false")
        self.con.execute("SET lock_configuration = true")

    def to_dialect(self, sql: str) -> str:
        return sqlglot.transpile(sql, read=DIALECT, write="duckdb")[0]

    def run(self, sql: str) -> pd.DataFrame:
        return self.con.execute(self.to_dialect(sql)).fetchdf()


def databricks_connect(**kwargs):
    """Connect with DATABRICKS_TOKEN if set, otherwise sign in through the browser (OAuth)."""
    from databricks import sql as dbsql

    host = os.environ["DATABRICKS_HOST"].strip().strip('"').replace("https://", "").split("/")[0]
    token = os.getenv("DATABRICKS_TOKEN", "").strip().strip('"')
    auth = {"access_token": token} if token else {"auth_type": "databricks-oauth"}
    return dbsql.connect(
        server_hostname=host,
        http_path=os.environ["DATABRICKS_HTTP_PATH"].strip().strip('"'),
        # results come back through the main connection instead of a second
        # download from Databricks storage (that download fails on some laptops'
        # certificate setups, and gold tables are small enough not to need it)
        use_cloud_fetch=False,
        **auth,
        **kwargs,
    )


class DatabricksBackend:
    """Live gold tables through a Databricks SQL warehouse (wakes it up: costs credit)."""

    name = "databricks"

    def __init__(self):
        self.con = databricks_connect(
            catalog="ontgrid", schema="gold", session_configuration={"STATEMENT_TIMEOUT": "60"}
        )

    def to_dialect(self, sql: str) -> str:
        return sql

    def run(self, sql: str) -> pd.DataFrame:
        with self.con.cursor() as cur:
            cur.execute(sql)
            return cur.fetchall_arrow().to_pandas()


def make_backend(settings) -> DuckDBBackend | DatabricksBackend:
    if settings.data_backend == "databricks":
        return DatabricksBackend()
    if settings.data_backend == "duckdb":
        return DuckDBBackend(settings.gold_dir)
    raise ValueError(f"unknown DATA_BACKEND: {settings.data_backend}")
