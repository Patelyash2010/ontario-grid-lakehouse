# Ontario Grid Lakehouse

An automated Azure lakehouse for Ontario's public electricity data: hourly
demand, generation by fuel type and day-ahead prices from the IESO (Ontario's
grid operator), joined with hourly weather for Toronto, Ottawa and London.

> **Status:** ingestion (Data Factory), silver and gold (Databricks, Delta) run
> automatically every day, with history back to Jan 1, 2025. Power BI is next.

## Questions it answers

- When does Ontario's demand peak, and how does temperature drive it?
- What is the supply mix (nuclear, hydro, gas, wind, solar) hour by hour?
- How does price respond to demand and to gas generation?
- How did pricing change after the May 2025 market redesign?

## Architecture

```
 IESO public reports (CSV/XML)          Open-Meteo API (JSON)
            │                                    │
            └────── Azure Data Factory (daily 9:15 AM) ──────┘
                 parameterized pipelines, ForEach, retries
                              │
         ADLS Gen2 ── BRONZE   raw files, one folder per ingest date, never modified
                              │
      Databricks Job (daily 10:30 AM, serverless, 4 sources in parallel)
          notebooks/10_silver_load.py  ← tested parsers in src/ieso
                              │
         ADLS Gen2 ── SILVER   Delta tables, UTC, newest version per key (MERGE)
                    └─ QUARANTINE  rejected rows + reason
                    └─ OPS         file manifest + run log
                              │
         ADLS Gen2 ── GOLD     star schema + marts (rebuilt after silver)
                              │
                          Power BI                         (next)
```

Governance: Unity Catalog (`ontgrid` catalog, one schema per layer, external
locations per container). Security: managed identities + RBAC everywhere -
**no keys, passwords or tokens are stored anywhere**, including in the
Git-synced Data Factory JSON.

## Data

| Source | Report / endpoint | Grain | Silver table |
|---|---|---|---|
| IESO | `Demand/PUB_Demand_<yyyy>.csv` | hour (MW) | `silver.ieso_demand_hourly` |
| IESO | `GenOutputbyFuelHourly/PUB_GenOutputbyFuelHourly_<yyyy>.xml` | hour × fuel (MWh) | `silver.ieso_fuel_hourly` |
| IESO | `DAHourlyOntarioZonalPrice/PUB_DAHourlyOntarioZonalPrice_<yyyymmdd>.xml` | hour ($/MWh) | `silver.ieso_da_price_hourly` |
| Open-Meteo | forecast API (`past_days=7`) daily + archive API backfill | hour × city | `silver.weather_hourly` |

History runs from 2025-01-01 (backfilled with the same pipelines, different
parameters). Gold `fact_grid_hourly` joins demand, generation and weather for
8,759 hours of 2025 and every completed hour of 2026; day-ahead prices exist
only since the May 2025 market redesign.

Gold tables: `dim_date` (with Ontario holidays and business days), `dim_hour`
(IESO on-peak window), `dim_fuel` (fuel groups, approximate CO2 factors),
`fact_grid_hourly`, `fact_generation_hourly`, `mart_daily_summary`,
`mart_temp_vs_demand`, `mart_pipeline_health`.

## Engineering decisions

**1. Bronze is immutable, partitioned by ingest date.** Every daily download
lands in `<source>/ingest_date=YYYY-MM-DD/`, so any day can be reprocessed
without calling the source again.

**2. Incremental silver loads with a file manifest.** `ops.processed_files`
records every bronze file loaded, keyed on path + modification time. Each run
parses only unseen files; a re-run with nothing new does nothing (idempotent).
Files Data Factory re-downloads on the same day have a newer modification time
and are picked up.

**3. Newest version wins.** IESO's yearly files repeat in every daily folder and
IESO revises data, so the same hour arrives many times. Rows are deduplicated
per business key keeping the most recently *created* IESO file, then merged
with a Delta `MERGE` that only overwrites when the incoming row is newer.
The first load parsed 37,227 demand rows and kept 15,912.

**4. Hour-ending, standard-time timestamps converted to UTC.** IESO labels hours
1-24 as hour-ending in Eastern Standard Time all year (the demand file has 24
rows on the spring-forward day). Every table stores `interval_start_utc` plus
the IESO `delivery_date` / `hour_ending`, so IESO and weather data join cleanly.

**5. Bad rows are quarantined, not dropped.** Out-of-range hours, invalid dates,
missing or impossible values go to `quarantine.ieso_rejects` with a reason.
Negative electricity *prices* are kept - they are valid market outcomes.

**6. Schema changes fail loudly.** If a source changes its layout (or an API
returns an error), the parser raises, the run is logged as `FAILED`, and
nothing partial is written.

**7. Market-era column.** Ontario moved from a single price (HOEP) to zonal
prices on 2025-05-01; price rows carry `market_era` so the series are never mixed.

**8. Unexpected source categories are quarantined, then decided on deliberately.**
The first full run quarantined 24 rows: a category `CONTROL ACTIONS` appeared in
the fuel report on a single day (2026-07-15). It is not in IESO's documentation
and is not generation, so it is kept with `is_generation = false` and excluded
from supply-mix totals. Any other unknown category still goes to quarantine.

**9. Weather forecasts are replaced by actuals.** The daily weather call returns
the last 7 days plus today; hours not yet finished at download time are flagged
`is_forecast` and are overwritten by the next day's download. The 7-day overlap
also means a missed run never leaves a gap.

**10. Ingestion and transformation are scheduled independently.** Data Factory
(9:15) and the Databricks job (10:30) are linked by time rather than by a
cross-service call. Because the silver loader is incremental and idempotent, a
late or failed ingestion is simply picked up the next day. (A direct ADF →
Databricks call needs the ADF managed identity registered as a Databricks
service principal, which the hosting university tenant does not allow.)

**11. Gaps at the source are kept as gaps.** Checking 2025 hour by hour against
a full date × hour grid found exactly one missing demand hour:
**2025-05-01 hour 1** - the first hour of Ontario's redesigned market. IESO's own
file jumps from `2025-04-30,24` to `2025-05-01,2`. Nothing is invented: the hour
is absent, and `mart_daily_summary` flags the day (`hours_reported = 23`,
`is_complete_day = false`).

**12. Blank values are quarantined, not zero-filled.** The 2025 fuel file has
hours where the small `OTHER` category has no output value. These rows are
quarantined with the reason `missing output value`; gold totals simply exclude
them (negligible next to ~9,600 MW of nuclear), and no value is guessed.

**13. Gold is rebuilt, silver is incremental.** Silver must handle revisions and
reruns, so it merges only new files. Gold is a pure function of silver; at this
size a full `CREATE OR REPLACE` takes seconds and can never drift out of sync.

**14. Tested logic, thin notebooks.** All parsing, deduplication and typing is
plain Python in `src/ieso` with 50 unit tests on fixtures built from real IESO
values. Notebooks only list files, call that code and write Delta tables.

## Repo layout

```
src/ieso/              parsers, time conversion, silver rules (stdlib only, tested)
tests/                 50 unit tests + real-format fixtures
notebooks/             00_setup_catalog.sql (Unity Catalog), 10_silver_load.py, 20_gold_model.sql
adf/                   Data Factory pipelines, datasets, linked services, trigger (Git-synced)
scripts/run_local.py   local bronze → silver pipeline (same logic, no cloud)
docs/                  setup guides
```

## Run it

```bash
python -m pip install pytest
python -m pytest                 # 50 tests
python scripts/run_local.py      # local pipeline against the live IESO site
```

In Azure: run `notebooks/00_setup_catalog.sql` once. The Databricks job
`job_lakehouse_daily` (10:30 daily) runs `10_silver_load` four times in parallel
(`report` = `Demand`, `GenOutputbyFuelHourly`, `DAHourlyOntarioZonalPrice`,
`weather`), then `20_gold_model` once all four succeed.

Backfill a past year: run `pl_ingest_ieso_file` (e.g. `PUB_GenOutputbyFuelHourly_2025.xml`)
and `pl_backfill_weather` (`startDate`/`endDate`), then run the job.

## Roadmap

- [x] Day 1: Azure resource group, ADLS Gen2, Key Vault, budget alert
- [x] Day 2: parsers, manifest, quarantine, tests, local pipeline
- [x] Day 3: Data Factory: IESO reports → bronze, daily trigger
- [x] Day 4: weather source (daily + backfill), Data Factory connected to GitHub
- [x] Day 5: Databricks + Unity Catalog, incremental silver loads with Delta MERGE
- [x] Day 6: weather in silver, Databricks job with daily schedule
- [x] Day 7-8: gold star schema and marts, 2025 backfill
- [ ] Day 9: Power BI report + architecture diagram
- [ ] Day 10: end-to-end check, demo, write-up

## Data sources

Electricity data © Independent Electricity System Operator (IESO), public
reports site. Weather data by [Open-Meteo](https://open-meteo.com/) (CC BY 4.0).
This is an independent portfolio project, not affiliated with either.
