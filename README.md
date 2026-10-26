# Ontario Grid Lakehouse

An automated Azure lakehouse for Ontario's public electricity data: hourly
demand, generation by fuel type, and day-ahead prices from the IESO (Ontario's
grid operator), modeled for analysis in Power BI.

> Status: **in progress**: parsers, file manifest, quarantine logic and local
> pipeline done and tested. Azure setup and Data Factory ingestion are next.

## Questions it answers

- When does Ontario's demand peak, and how does temperature drive it?
- What is the supply mix (nuclear, hydro, gas, wind, solar) hour by hour?
- How does price respond to demand and to gas generation?
- How did pricing change after the May 2025 market redesign?

## Architecture

```
IESO public reports (XML/CSV)     Open-Meteo weather API (JSON)
            │                                │
            └──────── Azure Data Factory ────┘
                (list → compare manifest → download new versions)
                              │
              ADLS Gen2 ── BRONZE   raw files, never modified
                              │
                   Databricks (PySpark)  ← uses src/ieso parsers
                              │
              ADLS Gen2 ── SILVER   clean Delta tables, UTC, latest version
                         └─ QUARANTINE  rejected rows + reason
                              │
              ADLS Gen2 ── GOLD     star schema + marts
                              │
                          Power BI
```

## Source data (verified October 2026)

| Report                | Directory                    | File pattern                                        | Grain             |
| --------------------- | ---------------------------- | --------------------------------------------------- | ----------------- |
| Hourly demand         | `Demand/`                    | `PUB_Demand_<yyyy>_v<N>.csv`                        | hour (MW)         |
| Output by fuel        | `GenOutputbyFuelHourly/`     | `PUB_GenOutputbyFuelHourly_<yyyy>_v<N>.xml`         | hour × fuel (MWh) |
| Day-ahead zonal price | `DAHourlyOntarioZonalPrice/` | `PUB_DAHourlyOntarioZonalPrice_<yyyymmdd>_v<N>.xml` | hour ($/MWh)      |

Base URL: `https://reports-public.ieso.ca/public/`

## Engineering decisions

**1. Ingest by file version, not by timestamp.**
IESO republishes files as new versions (e.g. the day-ahead price for
2026-09-24 exists as v1, v2 and v3). A manifest of already-landed file names
acts as the watermark: each run downloads only unseen versioned files.
Unversioned "alias" files are skipped because they duplicate a versioned one.
See `src/ieso/manifest.py`.

**2. Bronze keeps every version; silver keeps the latest.**
Bronze is an audit trail and lets any day be reprocessed without calling the
source again. Silver selects the highest version per (report, period).

**3. Hour-ending, standard-time timestamps converted to UTC.**
IESO labels hours 1-24 as hour-ending, in Eastern Standard Time year-round.
The demand file has exactly 24 rows on the spring-forward day (2026-03-08),
which confirms there is no daylight-saving shift to handle. Every row stores
`interval_start_utc` plus the original date/hour. See `src/ieso/time_utils.py`.

**4. Bad rows are quarantined, not dropped.**
Out-of-range hours, invalid dates, missing or negative values and unknown fuel
types go to a quarantine table with a `rejection_reason`. Negative _prices_
are kept: they are valid in electricity markets.

**5. Schema changes fail loudly.**
If a source file's header changes, the parser raises an error instead of
silently loading wrong columns.

**6. Market-era column.**
Ontario moved from a single price (HOEP) to zonal/locational prices on
2025-05-01. Price rows carry `market_era` so the two series are never mixed.

**7. Full lineage.**
Every silver row carries `source_file` and `report_created_at`.

**8. Unexpected source categories are quarantined, then decided on deliberately.**
The first full run quarantined 24 rows: a category called `CONTROL ACTIONS`
appeared in the fuel report on a single day (2026-07-15), empty for hours 1-9
and zero afterwards. It is not in IESO's report documentation and is not a
generation source. It is now kept in silver with `is_generation = False` and
excluded from fuel-mix totals. Any other unknown category still goes to quarantine.

## Repo layout

```
src/ieso/          parsers, time conversion, file-name + manifest logic (stdlib only)
tests/             21 unit tests; fixtures use real IESO values
scripts/run_local.py   local bronze → silver pipeline (same logic as Azure)
docs/              setup guides and design notes
```

## Quick start

```bash
python -m pip install pytest
python -m pytest                 # run tests
python scripts/run_local.py      # fetch real data into ./data
```

## Roadmap

- [x] Day 1: Azure resource group, ADLS Gen2, Key Vault, budget alert
- [x] Day 2: parsers, manifest, quarantine, tests, local pipeline
- [x] Day 3: Data Factory pipeline: demand → bronze, with manifest
- [x ] Day 4: add fuel mix, price and weather sources
- [ ] Day 5-6: Databricks silver notebooks + Delta MERGE, backfill
- [ ] Day 7-8: gold star schema, marts, daily schedule
- [ ] Day 9: Power BI report + architecture diagram
- [ ] Day 10: end-to-end run, write-up

## Data source

Data © Independent Electricity System Operator (IESO), from its public
reports site. This is an independent portfolio project, not affiliated with
the IESO.
