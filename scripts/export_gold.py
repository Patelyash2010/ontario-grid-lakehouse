"""Copy the gold tables from Databricks to local Parquet files (data/gold/).

The chatbot can then run fully on your laptop with DuckDB: no warehouse
running while you develop, and the evaluation runs in seconds.

Needs DATABRICKS_HOST and DATABRICKS_HTTP_PATH in .env, plus DATABRICKS_TOKEN
(leave it empty to sign in through the browser instead).
Re-run it whenever you want fresher data. Stop the SQL warehouse afterwards.

    python scripts/export_gold.py
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from chatbot.config import Settings  # noqa: E402  (also loads .env)
from chatbot.db import databricks_connect  # noqa: E402
from chatbot.schema import ALLOWED_TABLES  # noqa: E402


def main() -> None:
    import pyarrow.parquet as pq

    out = Settings.from_env().gold_dir
    out.mkdir(parents=True, exist_ok=True)
    token = os.getenv("DATABRICKS_TOKEN", "").strip()
    print(f"Connecting with {'token (' + token[:4] + '...)' if token else 'browser sign-in (no DATABRICKS_TOKEN set)'}")
    con = databricks_connect()
    with con, con.cursor() as cur:
        for name in sorted(ALLOWED_TABLES):
            cur.execute(f"SELECT * FROM ontgrid.gold.{name}")
            table = cur.fetchall_arrow()
            pq.write_table(table, out / f"{name}.parquet")
            print(f"{name:<24} {table.num_rows:>8,} rows")
    print(f"\nSaved to {out}")


if __name__ == "__main__":
    main()
