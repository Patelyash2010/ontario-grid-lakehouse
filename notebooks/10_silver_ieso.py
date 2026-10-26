# Databricks notebook source
# MAGIC %md
# MAGIC # 10 · IESO bronze → silver
# MAGIC
# MAGIC Incrementally loads one IESO report from the bronze volume into a silver Delta table.
# MAGIC
# MAGIC 1. List files under `/Volumes/ontgrid/bronze/raw/<report>/`
# MAGIC 2. Skip files already recorded in `ontgrid.ops.processed_files` (path + modification time)
# MAGIC 3. Parse new files with the tested parsers in `src/ieso` (UTC timestamps, quarantine of bad rows)
# MAGIC 4. Keep the newest IESO version of each row, then `MERGE` into silver (only newer data overwrites)
# MAGIC 5. Write rejects to `ontgrid.quarantine.ieso_rejects`, mark files processed, log the run
# MAGIC
# MAGIC Run it again with no new files and nothing changes.

# COMMAND ----------

dbutils.widgets.dropdown(
    "report", "Demand",
    ["Demand", "GenOutputbyFuelHourly", "DAHourlyOntarioZonalPrice"],
    "IESO report",
)
REPORT = dbutils.widgets.get("report")

# COMMAND ----------

import os
import sys
import uuid
from datetime import datetime, timezone

# Make the repo's src/ folder importable (this notebook lives in <repo>/notebooks/)
sys.path.insert(0, os.path.abspath(os.path.join(os.getcwd(), "..", "src")))

from ieso.silver import (  # noqa: E402
    QUARANTINE_DDL,
    REPORTS,
    files_to_process,
    latest_per_key,
    merge_sql,
    parse_bronze_file,
    reject_tuples,
    to_tuples,
)
from pyspark.sql import functions as F  # noqa: E402

spark.conf.set("spark.sql.session.timeZone", "UTC")

CATALOG = "ontgrid"
BRONZE_ROOT = f"/Volumes/{CATALOG}/bronze/raw"
cfg = REPORTS[REPORT]
TARGET = f"{CATALOG}.silver.{cfg.table}"
RUN_ID = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:6]
STARTED_AT = datetime.now(timezone.utc)

print(f"report={REPORT}  target={TARGET}  run_id={RUN_ID}")

# COMMAND ----------

# MAGIC %md ## 1 · Find new bronze files

# COMMAND ----------

def list_files(path):
    """Recursive listing of a volume folder -> [{path, modification_time, size}]."""
    out = []
    for info in dbutils.fs.ls(path):
        p = info.path.replace("dbfs:", "").rstrip("/")
        if info.isDir():
            out.extend(list_files(p))
        else:
            out.append({"path": p, "modification_time": int(info.modificationTime), "size": int(info.size)})
    return out


listing = list_files(f"{BRONZE_ROOT}/{REPORT}")
processed = [
    (r.bronze_path, r.modification_time)
    for r in spark.table(f"{CATALOG}.ops.processed_files")
                  .where(F.col("report") == REPORT)
                  .select("bronze_path", "modification_time")
                  .collect()
]
new_files = files_to_process(listing, processed)

print(f"{len(listing)} file(s) in bronze, {len(processed)} already processed, {len(new_files)} new")
for f in new_files:
    print("  +", f["path"])

# COMMAND ----------

# MAGIC %md ## 2 · Parse (bad rows → quarantine)

# COMMAND ----------

def log_run(status, message="", **counts):
    row = {
        "run_id": RUN_ID, "layer": "silver", "report": REPORT,
        "started_at": STARTED_AT, "finished_at": datetime.now(timezone.utc),
        "status": status, "message": message,
        "files_processed": counts.get("files", 0),
        "rows_parsed": counts.get("parsed", 0),
        "rows_after_dedup": counts.get("dedup", 0),
        "rows_quarantined": counts.get("quarantined", 0),
        "rows_inserted": counts.get("inserted", 0),
        "rows_updated": counts.get("updated", 0),
    }
    cols = [c.name for c in spark.table(f"{CATALOG}.ops.pipeline_runs").schema]
    (spark.createDataFrame([tuple(row[c] for c in cols)], spark.table(f"{CATALOG}.ops.pipeline_runs").schema)
          .write.mode("append").saveAsTable(f"{CATALOG}.ops.pipeline_runs"))


if not new_files:
    log_run("NO_NEW_FILES")
    dbutils.notebook.exit("No new files - nothing to do.")

all_rows, all_rejects = [], []
try:
    for f in new_files:
        with open(f["path"], "rb") as fh:
            result = parse_bronze_file(REPORT, f["path"], fh.read())
        all_rows += result.rows
        all_rejects += result.rejects
        print(f"  parsed {f['path'].rsplit('/', 1)[-1]}: {len(result.rows):,} rows, {len(result.rejects)} rejects")
except Exception as exc:
    # e.g. the source changed its column layout: stop loudly, record why
    log_run("FAILED", f"parse error: {exc}", files=len(new_files))
    raise

rows = latest_per_key(all_rows, cfg.keys)
print(f"parsed {len(all_rows):,} rows -> {len(rows):,} after keeping the newest version per key")

# COMMAND ----------

# MAGIC %md ## 3 · Merge into silver

# COMMAND ----------

src = (spark.createDataFrame(to_tuples(rows, cfg.columns), cfg.ddl)
            .withColumn("silver_loaded_at", F.current_timestamp()))

if not spark.catalog.tableExists(TARGET):
    src.write.saveAsTable(TARGET)
    inserted, updated = src.count(), 0
    print(f"created {TARGET} with {inserted:,} rows")
else:
    src.createOrReplaceTempView("src_batch")
    spark.sql(merge_sql(TARGET, "src_batch", cfg.keys))
    metrics = spark.sql(f"DESCRIBE HISTORY {TARGET} LIMIT 1").collect()[0]["operationMetrics"]
    inserted = int(metrics.get("numTargetRowsInserted", 0))
    updated = int(metrics.get("numTargetRowsUpdated", 0))
    print(f"merged into {TARGET}: {inserted:,} inserted, {updated:,} updated")

# COMMAND ----------

# MAGIC %md ## 4 · Quarantine, manifest, audit

# COMMAND ----------

if all_rejects:
    (spark.createDataFrame(reject_tuples(REPORT, all_rejects, RUN_ID), QUARANTINE_DDL)
          .withColumn("quarantined_at", F.current_timestamp())
          .write.mode("append").saveAsTable(f"{CATALOG}.quarantine.ieso_rejects"))

(spark.createDataFrame(
        [(REPORT, f["path"], f["modification_time"], f["size"], RUN_ID) for f in new_files],
        "report STRING, bronze_path STRING, modification_time BIGINT, size BIGINT, run_id STRING")
      .withColumn("processed_at", F.current_timestamp())
      .write.mode("append").saveAsTable(f"{CATALOG}.ops.processed_files"))

log_run("SUCCEEDED", files=len(new_files), parsed=len(all_rows), dedup=len(rows),
        quarantined=len(all_rejects), inserted=inserted, updated=updated)

print(f"done: {len(new_files)} file(s), {inserted:,} inserted, {updated:,} updated, "
      f"{len(all_rejects)} quarantined")

# COMMAND ----------

# MAGIC %md ## 5 · Quick checks

# COMMAND ----------

display(spark.sql(f"""
  SELECT COUNT(*)                       AS rows,
         MIN(interval_start_utc)        AS first_hour_utc,
         MAX(interval_start_utc)        AS last_hour_utc,
         COUNT(DISTINCT delivery_date)  AS days,
         MAX(report_created_at)         AS newest_ieso_file
  FROM {TARGET}
"""))

# COMMAND ----------

# Days without a full set of hours (usually just the current, still-filling day)
display(spark.sql(f"""
  SELECT delivery_date, COUNT(DISTINCT hour_ending) AS hours
  FROM {TARGET}
  GROUP BY delivery_date
  HAVING COUNT(DISTINCT hour_ending) < 24
  ORDER BY delivery_date
"""))
