-- Databricks notebook source
-- MAGIC %md
-- MAGIC # 20 · Gold model (star schema + marts)
-- MAGIC
-- MAGIC Rebuilds the gold layer from silver on every run (`CREATE OR REPLACE TABLE`).
-- MAGIC At this size (~70k silver rows) a full rebuild takes seconds, is always consistent
-- MAGIC with silver, and needs no incremental logic. Runs after the four silver tasks
-- MAGIC in `job_silver_daily`.
-- MAGIC
-- MAGIC | Table | Grain |
-- MAGIC |---|---|
-- MAGIC | `dim_date`, `dim_hour`, `dim_fuel` | date / hour-ending / fuel |
-- MAGIC | `fact_grid_hourly` | one row per hour (wide) |
-- MAGIC | `fact_generation_hourly` | hour × fuel |
-- MAGIC | `mart_daily_summary` | one row per day |
-- MAGIC | `mart_temp_vs_demand` | temperature band × day type |
-- MAGIC | `mart_pipeline_health` | one row per pipeline run |
-- MAGIC
-- MAGIC All times follow IESO conventions: `delivery_date` + `hour_ending` (1-24, Eastern Standard Time),
-- MAGIC with `interval_start_utc` as the unambiguous key.

-- COMMAND ----------

USE CATALOG ontgrid;
USE SCHEMA gold;

-- COMMAND ----------

-- MAGIC %md ## Dimensions

-- COMMAND ----------

-- Ontario statutory holidays (used for business days and IESO on-peak hours)
CREATE OR REPLACE TABLE dim_date AS
WITH holidays AS (
  SELECT * FROM VALUES
    (DATE'2025-01-01', 'New Year''s Day'), (DATE'2025-02-17', 'Family Day'),
    (DATE'2025-04-18', 'Good Friday'),     (DATE'2025-05-19', 'Victoria Day'),
    (DATE'2025-07-01', 'Canada Day'),      (DATE'2025-09-01', 'Labour Day'),
    (DATE'2025-10-13', 'Thanksgiving'),    (DATE'2025-12-25', 'Christmas Day'),
    (DATE'2025-12-26', 'Boxing Day'),
    (DATE'2026-01-01', 'New Year''s Day'), (DATE'2026-02-16', 'Family Day'),
    (DATE'2026-04-03', 'Good Friday'),     (DATE'2026-05-18', 'Victoria Day'),
    (DATE'2026-07-01', 'Canada Day'),      (DATE'2026-09-07', 'Labour Day'),
    (DATE'2026-10-12', 'Thanksgiving'),    (DATE'2026-12-25', 'Christmas Day'),
    (DATE'2026-12-26', 'Boxing Day')
    AS h(holiday_date, holiday_name)
),
days AS (
  SELECT explode(sequence(DATE'2025-01-01', DATE'2026-12-31', INTERVAL 1 DAY)) AS date
)
SELECT
  CAST(date_format(d.date, 'yyyyMMdd') AS INT)           AS date_key,
  d.date,
  year(d.date)                                           AS year,
  quarter(d.date)                                        AS quarter,
  month(d.date)                                          AS month,
  date_format(d.date, 'MMMM')                            AS month_name,
  date_format(d.date, 'yyyy-MM')                         AS year_month,
  dayofmonth(d.date)                                     AS day_of_month,
  dayofweek(d.date)                                      AS day_of_week,      -- 1 = Sunday
  date_format(d.date, 'EEEE')                            AS day_name,
  dayofweek(d.date) IN (1, 7)                            AS is_weekend,
  h.holiday_date IS NOT NULL                             AS is_holiday,
  h.holiday_name,
  NOT (dayofweek(d.date) IN (1, 7)) AND h.holiday_date IS NULL AS is_business_day,
  CASE WHEN month(d.date) IN (12, 1, 2) THEN 'Winter'
       WHEN month(d.date) IN (3, 4, 5)  THEN 'Spring'
       WHEN month(d.date) IN (6, 7, 8)  THEN 'Summer'
       ELSE 'Fall' END                                   AS season
FROM days d
LEFT JOIN holidays h ON h.holiday_date = d.date;

-- COMMAND ----------

CREATE OR REPLACE TABLE dim_hour AS
SELECT
  hour_ending,
  concat(lpad(CAST(hour_ending - 1 AS STRING), 2, '0'), ':00-',
         lpad(CAST(hour_ending AS STRING), 2, '0'), ':00')      AS hour_label,
  CASE WHEN hour_ending BETWEEN 1 AND 6   THEN 'Night'
       WHEN hour_ending BETWEEN 7 AND 12  THEN 'Morning'
       WHEN hour_ending BETWEEN 13 AND 18 THEN 'Afternoon'
       ELSE 'Evening' END                                       AS day_part,
  hour_ending BETWEEN 8 AND 23                                  AS is_peak_window  -- IESO on-peak: HE 8-23 on business days
FROM (SELECT explode(sequence(1, 24)) AS hour_ending);

-- COMMAND ----------

-- Emission factors (tonnes CO2 per MWh generated) are APPROXIMATE and for
-- illustration: nuclear, hydro, wind and solar emit no CO2 when generating;
-- biofuel is treated as biogenic (0); gas uses a typical fleet average.
CREATE OR REPLACE TABLE dim_fuel AS
SELECT * FROM VALUES
  ('NUCLEAR', 'Nuclear',    'Low carbon', false, 0.000, 1),
  ('HYDRO',   'Hydro',      'Low carbon', true,  0.000, 2),
  ('WIND',    'Wind',       'Low carbon', true,  0.000, 3),
  ('SOLAR',   'Solar',      'Low carbon', true,  0.000, 4),
  ('BIOFUEL', 'Biofuel',    'Low carbon', true,  0.000, 5),
  ('GAS',     'Natural gas','Fossil',     false, 0.420, 6),
  ('OTHER',   'Other',      'Other',      false, 0.000, 7)
  AS f(fuel_type, fuel_name, fuel_group, is_renewable, co2_t_per_mwh, sort_order);

-- COMMAND ----------

-- MAGIC %md ## Facts

-- COMMAND ----------

CREATE OR REPLACE TABLE fact_generation_hourly AS
SELECT
  CAST(date_format(g.delivery_date, 'yyyyMMdd') AS INT)  AS date_key,
  g.delivery_date,
  g.hour_ending,
  g.interval_start_utc,
  g.fuel_type,
  g.output_mwh,
  g.output_mwh * f.co2_t_per_mwh                          AS emissions_t,
  g.output_mwh / NULLIF(SUM(g.output_mwh) OVER (PARTITION BY g.interval_start_utc), 0) AS share_of_hour,
  g.output_quality
FROM silver.ieso_fuel_hourly g
JOIN dim_fuel f USING (fuel_type)          -- excludes non-generation rows (CONTROL ACTIONS)
WHERE g.is_generation;

-- COMMAND ----------

CREATE OR REPLACE TABLE fact_grid_hourly AS
WITH gen AS (
  SELECT interval_start_utc,
         SUM(output_mwh)                                         AS total_generation_mwh,
         SUM(CASE WHEN fuel_type = 'NUCLEAR' THEN output_mwh END) AS nuclear_mwh,
         SUM(CASE WHEN fuel_type = 'HYDRO'   THEN output_mwh END) AS hydro_mwh,
         SUM(CASE WHEN fuel_type = 'GAS'     THEN output_mwh END) AS gas_mwh,
         SUM(CASE WHEN fuel_type = 'WIND'    THEN output_mwh END) AS wind_mwh,
         SUM(CASE WHEN fuel_type = 'SOLAR'   THEN output_mwh END) AS solar_mwh,
         SUM(CASE WHEN fuel_type = 'BIOFUEL' THEN output_mwh END) AS biofuel_mwh,
         SUM(CASE WHEN fuel_type = 'OTHER'   THEN output_mwh END) AS other_mwh,
         SUM(emissions_t)                                         AS emissions_t
  FROM fact_generation_hourly
  GROUP BY interval_start_utc
),
price AS (
  SELECT interval_start_utc, price_cad_per_mwh, market_era
  FROM silver.ieso_da_price_hourly
  WHERE price_type = 'DA_ONTARIO_ZONAL'
),
wx AS (
  SELECT interval_start_utc,
         AVG(temperature_c)                                       AS avg_temp_c,
         MAX(CASE WHEN city = 'toronto' THEN temperature_c END)   AS toronto_temp_c,
         AVG(relative_humidity_pct)                               AS avg_humidity_pct,
         COUNT(*)                                                 AS weather_cities
  FROM silver.weather_hourly
  WHERE NOT is_forecast                     -- actual observations only
  GROUP BY interval_start_utc
)
SELECT
  CAST(date_format(d.delivery_date, 'yyyyMMdd') AS INT)        AS date_key,
  d.delivery_date,
  d.hour_ending,
  d.interval_start_utc,
  d.ontario_demand_mw,
  d.market_demand_mw,
  gen.total_generation_mwh,
  gen.nuclear_mwh, gen.hydro_mwh, gen.gas_mwh, gen.wind_mwh,
  gen.solar_mwh, gen.biofuel_mwh, gen.other_mwh,
  (gen.total_generation_mwh - COALESCE(gen.gas_mwh, 0) - COALESCE(gen.other_mwh, 0))
      / NULLIF(gen.total_generation_mwh, 0)                     AS low_carbon_share,
  gen.emissions_t,
  gen.emissions_t / NULLIF(gen.total_generation_mwh, 0)         AS carbon_intensity_t_per_mwh,
  price.price_cad_per_mwh,
  price.market_era,
  wx.avg_temp_c,
  wx.toronto_temp_c,
  wx.avg_humidity_pct,
  dd.is_business_day AND dh.is_peak_window                      AS is_on_peak,
  gen.interval_start_utc IS NOT NULL                            AS has_generation,
  price.interval_start_utc IS NOT NULL                          AS has_price,
  COALESCE(wx.weather_cities, 0) = 3                            AS has_weather
FROM silver.ieso_demand_hourly d
JOIN dim_date dd ON dd.date = d.delivery_date
JOIN dim_hour dh ON dh.hour_ending = d.hour_ending
LEFT JOIN gen   ON gen.interval_start_utc   = d.interval_start_utc
LEFT JOIN price ON price.interval_start_utc = d.interval_start_utc
LEFT JOIN wx    ON wx.interval_start_utc    = d.interval_start_utc;

-- COMMAND ----------

-- MAGIC %md ## Marts

-- COMMAND ----------

-- Heating / cooling degree-days use the common 18 C base on the daily mean temperature.
CREATE OR REPLACE TABLE mart_daily_summary AS
SELECT
  f.date_key,
  f.delivery_date,
  COUNT(*)                                                  AS hours_reported,
  COUNT(*) = 24                                             AS is_complete_day,
  MAX(f.ontario_demand_mw)                                  AS peak_demand_mw,
  max_by(f.hour_ending, f.ontario_demand_mw)                AS peak_hour_ending,
  ROUND(AVG(f.ontario_demand_mw), 0)                        AS avg_demand_mw,
  SUM(f.ontario_demand_mw)                                  AS ontario_energy_mwh,
  SUM(f.total_generation_mwh)                               AS generation_mwh,
  SUM(f.nuclear_mwh) AS nuclear_mwh, SUM(f.hydro_mwh) AS hydro_mwh, SUM(f.gas_mwh) AS gas_mwh,
  SUM(f.wind_mwh)    AS wind_mwh,    SUM(f.solar_mwh) AS solar_mwh,
  SUM(f.gas_mwh) / NULLIF(SUM(f.total_generation_mwh), 0)   AS gas_share,
  1 - (SUM(COALESCE(f.gas_mwh, 0)) + SUM(COALESCE(f.other_mwh, 0)))
      / NULLIF(SUM(f.total_generation_mwh), 0)              AS low_carbon_share,
  SUM(f.emissions_t)                                        AS emissions_t,
  SUM(f.emissions_t) / NULLIF(SUM(f.total_generation_mwh), 0) AS carbon_intensity_t_per_mwh,
  ROUND(AVG(f.avg_temp_c), 1)                               AS avg_temp_c,
  ROUND(MIN(f.avg_temp_c), 1)                               AS min_temp_c,
  ROUND(MAX(f.avg_temp_c), 1)                               AS max_temp_c,
  ROUND(GREATEST(18 - AVG(f.avg_temp_c), 0), 1)             AS heating_degree_days,
  ROUND(GREATEST(AVG(f.avg_temp_c) - 18, 0), 1)             AS cooling_degree_days,
  ROUND(AVG(f.price_cad_per_mwh), 2)                        AS avg_price_cad_per_mwh,
  ROUND(AVG(CASE WHEN f.is_on_peak     THEN f.price_cad_per_mwh END), 2) AS on_peak_price_cad_per_mwh,
  ROUND(AVG(CASE WHEN NOT f.is_on_peak THEN f.price_cad_per_mwh END), 2) AS off_peak_price_cad_per_mwh
FROM fact_grid_hourly f
GROUP BY f.date_key, f.delivery_date;

-- COMMAND ----------

CREATE OR REPLACE TABLE mart_temp_vs_demand AS
SELECT
  CAST(FLOOR(f.avg_temp_c / 2) * 2 AS INT)                  AS temp_band_c,      -- e.g. -10 = [-10, -8)
  CASE WHEN dd.is_business_day THEN 'Business day' ELSE 'Weekend / holiday' END AS day_type,
  COUNT(*)                                                  AS hours,
  ROUND(AVG(f.ontario_demand_mw), 0)                        AS avg_demand_mw,
  ROUND(PERCENTILE(f.ontario_demand_mw, 0.9), 0)            AS p90_demand_mw,
  ROUND(AVG(f.gas_mwh / NULLIF(f.total_generation_mwh, 0)), 3) AS avg_gas_share
FROM fact_grid_hourly f
JOIN dim_date dd ON dd.date = f.delivery_date
WHERE f.avg_temp_c IS NOT NULL
GROUP BY 1, 2;

-- COMMAND ----------

CREATE OR REPLACE TABLE mart_pipeline_health AS
SELECT
  run_id,
  report,
  layer,
  status,
  CAST(finished_at AS DATE)                                 AS run_date,
  started_at,
  finished_at,
  unix_timestamp(finished_at) - unix_timestamp(started_at)  AS duration_s,
  files_processed,
  rows_parsed,
  rows_after_dedup,
  rows_quarantined,
  rows_inserted,
  rows_updated,
  message
FROM ontgrid.ops.pipeline_runs;

-- COMMAND ----------

-- MAGIC %md ## Checks

-- COMMAND ----------

-- Row counts per gold table
SELECT 'dim_date' AS table_name, COUNT(*) AS row_count FROM dim_date
UNION ALL SELECT 'dim_hour', COUNT(*) FROM dim_hour
UNION ALL SELECT 'dim_fuel', COUNT(*) FROM dim_fuel
UNION ALL SELECT 'fact_grid_hourly', COUNT(*) FROM fact_grid_hourly
UNION ALL SELECT 'fact_generation_hourly', COUNT(*) FROM fact_generation_hourly
UNION ALL SELECT 'mart_daily_summary', COUNT(*) FROM mart_daily_summary
UNION ALL SELECT 'mart_temp_vs_demand', COUNT(*) FROM mart_temp_vs_demand
UNION ALL SELECT 'mart_pipeline_health', COUNT(*) FROM mart_pipeline_health;

-- COMMAND ----------

-- Coverage by year: how many hours have generation, price and weather joined
SELECT year(delivery_date)            AS year,
       COUNT(*)                       AS hours,
       SUM(CAST(has_generation AS INT)) AS with_generation,
       SUM(CAST(has_price AS INT))      AS with_price,
       SUM(CAST(has_weather AS INT))    AS with_weather
FROM fact_grid_hourly
GROUP BY 1
ORDER BY 1;

-- COMMAND ----------

-- Sanity: generation should be close to market demand (Ontario demand + exports + losses)
SELECT ROUND(AVG(total_generation_mwh / market_demand_mw), 3) AS avg_generation_to_market_demand,
       ROUND(AVG(low_carbon_share), 3)                        AS avg_low_carbon_share,
       ROUND(AVG(carbon_intensity_t_per_mwh), 4)              AS avg_carbon_intensity_t_per_mwh
FROM fact_grid_hourly
WHERE has_generation;

-- COMMAND ----------

-- Preview: the 10 highest-demand days so far
SELECT delivery_date, peak_demand_mw, peak_hour_ending, avg_temp_c, max_temp_c,
       ROUND(gas_share, 3) AS gas_share, avg_price_cad_per_mwh
FROM mart_daily_summary
WHERE is_complete_day
ORDER BY peak_demand_mw DESC
LIMIT 10;
