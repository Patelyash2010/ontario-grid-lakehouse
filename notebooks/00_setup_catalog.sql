-- Databricks notebook source
-- MAGIC %md
-- MAGIC # 00 · Unity Catalog setup (run once)
-- MAGIC
-- MAGIC Creates the `ontgrid` catalog, one schema per layer, and a read-only
-- MAGIC volume over the bronze container that Data Factory writes to.
-- MAGIC
-- MAGIC Prerequisites (done in the Azure portal / Catalog Explorer):
-- MAGIC * Access Connector `ac-ontgrid-yp2610` with *Storage Blob Data Contributor* on `stontgridyp2610`
-- MAGIC * Storage credential `cred_ontgrid`
-- MAGIC * External locations `ext_bronze`, `ext_silver`, `ext_gold`, `ext_quarantine`, `ext_meta`

-- COMMAND ----------

CREATE CATALOG IF NOT EXISTS ontgrid
  MANAGED LOCATION 'abfss://meta@stontgridyp2610.dfs.core.windows.net/uc/catalog';

CREATE SCHEMA IF NOT EXISTS ontgrid.bronze
  MANAGED LOCATION 'abfss://meta@stontgridyp2610.dfs.core.windows.net/uc/bronze';
CREATE SCHEMA IF NOT EXISTS ontgrid.silver
  MANAGED LOCATION 'abfss://silver@stontgridyp2610.dfs.core.windows.net/tables';
CREATE SCHEMA IF NOT EXISTS ontgrid.gold
  MANAGED LOCATION 'abfss://gold@stontgridyp2610.dfs.core.windows.net/tables';
CREATE SCHEMA IF NOT EXISTS ontgrid.quarantine
  MANAGED LOCATION 'abfss://quarantine@stontgridyp2610.dfs.core.windows.net/tables';
CREATE SCHEMA IF NOT EXISTS ontgrid.ops
  MANAGED LOCATION 'abfss://meta@stontgridyp2610.dfs.core.windows.net/uc/ops';

CREATE EXTERNAL VOLUME IF NOT EXISTS ontgrid.bronze.raw
  LOCATION 'abfss://bronze@stontgridyp2610.dfs.core.windows.net/';

-- COMMAND ----------

-- Operational tables used by every silver notebook
CREATE TABLE IF NOT EXISTS ontgrid.ops.processed_files (
  report            STRING,
  bronze_path       STRING,
  modification_time BIGINT,
  size              BIGINT,
  run_id            STRING,
  processed_at      TIMESTAMP
);

CREATE TABLE IF NOT EXISTS ontgrid.ops.pipeline_runs (
  run_id            STRING,
  layer             STRING,
  report            STRING,
  started_at        TIMESTAMP,
  finished_at       TIMESTAMP,
  status            STRING,
  files_processed   INT,
  rows_parsed       BIGINT,
  rows_after_dedup  BIGINT,
  rows_quarantined  BIGINT,
  rows_inserted     BIGINT,
  rows_updated      BIGINT,
  message           STRING
);

CREATE TABLE IF NOT EXISTS ontgrid.quarantine.ieso_rejects (
  report           STRING,
  source_file      STRING,
  bronze_path      STRING,
  rejection_reason STRING,
  raw              STRING,
  run_id           STRING,
  quarantined_at   TIMESTAMP
);

-- COMMAND ----------

LIST '/Volumes/ontgrid/bronze/raw/';
