"""Bronze -> silver logic shared by the Databricks notebooks.

Everything here is plain Python (no Spark), so it is unit-tested locally.
The notebook only lists files, calls these functions, and writes Delta tables.

Incremental design
------------------
* A file is identified by (bronze_path, modification_time). Data Factory
  writes one folder per day, and re-running ADF on the same day overwrites a
  file with a newer modification time - both cases count as "new".
* `ops.processed_files` stores what was already loaded, so a re-run of the
  notebook with no new files does nothing (idempotent).
* The yearly IESO files repeat every day with more rows, so the same hour
  appears in many files. `latest_per_key` keeps the row from the file IESO
  created most recently; the Delta MERGE applies the same rule against rows
  already in silver.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, datetime
from typing import Callable, Iterable, Optional

from .parsers import (
    ParseResult,
    parse_da_zonal_price_xml,
    parse_demand_csv,
    parse_fuel_hourly_xml,
)

_COMMON_TAIL = [
    ("source_file", "STRING"),
    ("report_created_at", "STRING"),   # IESO local time, ISO format, used for "latest wins"
    ("bronze_path", "STRING"),
]


@dataclass(frozen=True)
class ReportConfig:
    parser: Callable[..., ParseResult]
    binary: bool                          # XML parsers take bytes, CSV takes text
    table: str                            # silver table name
    keys: tuple[str, ...]                 # business key of one row
    columns: tuple[tuple[str, str], ...]  # (name, Spark SQL type), in table order

    @property
    def ddl(self) -> str:
        return ", ".join(f"{n} {t}" for n, t in self.columns)

    @property
    def column_names(self) -> list[str]:
        return [n for n, _ in self.columns]


_TIME = [
    ("delivery_date", "DATE"),
    ("hour_ending", "INT"),
    ("interval_start_utc", "TIMESTAMP"),
    ("interval_end_utc", "TIMESTAMP"),
]

REPORTS: dict[str, ReportConfig] = {
    "Demand": ReportConfig(
        parser=parse_demand_csv,
        binary=False,
        table="ieso_demand_hourly",
        keys=("interval_start_utc",),
        columns=tuple(_TIME + [
            ("market_demand_mw", "INT"),
            ("ontario_demand_mw", "INT"),
        ] + _COMMON_TAIL),
    ),
    "GenOutputbyFuelHourly": ReportConfig(
        parser=parse_fuel_hourly_xml,
        binary=True,
        table="ieso_fuel_hourly",
        keys=("interval_start_utc", "fuel_type"),
        columns=tuple(_TIME + [
            ("fuel_type", "STRING"),
            ("output_mwh", "DOUBLE"),
            ("output_quality", "INT"),
            ("is_generation", "BOOLEAN"),
        ] + _COMMON_TAIL),
    ),
    "DAHourlyOntarioZonalPrice": ReportConfig(
        parser=parse_da_zonal_price_xml,
        binary=True,
        table="ieso_da_price_hourly",
        keys=("interval_start_utc", "price_type"),
        columns=tuple(_TIME + [
            ("price_type", "STRING"),
            ("market_era", "STRING"),
            ("price_cad_per_mwh", "DOUBLE"),
            ("loss_component", "DOUBLE"),
            ("congestion_component", "DOUBLE"),
            ("flag", "STRING"),
        ] + _COMMON_TAIL),
    ),
}

QUARANTINE_COLUMNS: tuple[tuple[str, str], ...] = (
    ("report", "STRING"),
    ("source_file", "STRING"),
    ("bronze_path", "STRING"),
    ("rejection_reason", "STRING"),
    ("raw", "STRING"),
    ("run_id", "STRING"),
)
QUARANTINE_DDL = ", ".join(f"{n} {t}" for n, t in QUARANTINE_COLUMNS)


# --------------------------------------------------------------------------- file selection

def files_to_process(listing: Iterable[dict], processed: Iterable[tuple[str, int]]) -> list[dict]:
    """Return files not loaded before, oldest first.

    listing:   dicts with keys path, modification_time (ms), size
    processed: (path, modification_time) pairs already in ops.processed_files
    Only IESO report files (PUB_*.csv / PUB_*.xml) are considered.
    """
    done = {(p, int(m)) for p, m in processed}
    new = []
    for f in listing:
        name = f["path"].rstrip("/").rsplit("/", 1)[-1]
        if not (name.startswith("PUB_") and name.endswith((".csv", ".xml"))):
            continue
        if (f["path"], int(f["modification_time"])) in done:
            continue
        new.append(f)
    return sorted(new, key=lambda f: (int(f["modification_time"]), f["path"]))


# --------------------------------------------------------------------------- parsing

def normalize_created_at(value: Optional[str]) -> Optional[str]:
    """IESO uses '2026-10-22 07:30:10' (CSV) and '2026-10-22T06:50:53' (XML).
    Return one sortable ISO format so string comparison == time comparison."""
    if not value:
        return None
    return datetime.fromisoformat(value.strip().replace(" ", "T")).strftime("%Y-%m-%dT%H:%M:%S")


def parse_bronze_file(report: str, bronze_path: str, data: bytes) -> ParseResult:
    cfg = REPORTS[report]
    file_name = bronze_path.rstrip("/").rsplit("/", 1)[-1]
    content = data if cfg.binary else data.decode("utf-8-sig", errors="replace")
    result = cfg.parser(content, file_name)
    created = normalize_created_at(result.report_created_at)
    for r in result.rows:
        r["report_created_at"] = created
        r["bronze_path"] = bronze_path
    for r in result.rejects:
        r["bronze_path"] = bronze_path
    return result


def latest_per_key(rows: Iterable[dict], keys: Iterable[str]) -> list[dict]:
    """Keep one row per business key: the one from the most recently created
    IESO file. Ties (same file content landed twice) keep the first seen."""
    keys = tuple(keys)
    best: dict[tuple, dict] = {}
    for r in rows:
        k = tuple(r[c] for c in keys)
        current = best.get(k)
        if current is None or (r["report_created_at"] or "") > (current["report_created_at"] or ""):
            best[k] = r
    return list(best.values())


# --------------------------------------------------------------------------- typing for Spark

def _convert(value, spark_type: str):
    if value is None:
        return None
    if spark_type == "DATE":
        return value if isinstance(value, date) else date.fromisoformat(value)
    if spark_type == "TIMESTAMP":
        return value if isinstance(value, datetime) else datetime.fromisoformat(value)
    if spark_type == "INT":
        return int(value)
    if spark_type == "DOUBLE":
        return float(value)
    if spark_type == "BOOLEAN":
        return bool(value)
    return str(value)


def to_tuples(rows: Iterable[dict], columns: Iterable[tuple[str, str]]) -> list[tuple]:
    """Convert parser dicts into tuples in table column order with Python types
    Spark maps directly (date, tz-aware datetime, int, float, bool, str)."""
    columns = tuple(columns)
    return [tuple(_convert(r.get(n), t) for n, t in columns) for r in rows]


def reject_tuples(report: str, rejects: Iterable[dict], run_id: str) -> list[tuple]:
    return [
        (report, r.get("source_file"), r.get("bronze_path"), r.get("rejection_reason"),
         json.dumps(r.get("raw"), default=str), run_id)
        for r in rejects
    ]


def merge_sql(target: str, source_view: str, keys: Iterable[str]) -> str:
    """Delta MERGE: insert new keys; update an existing key only when the
    incoming row comes from a NEWER IESO file. Re-running with the same data
    changes nothing (idempotent)."""
    on = " AND ".join(f"t.{k} = s.{k}" for k in keys)
    return (
        f"MERGE INTO {target} t USING {source_view} s ON {on} "
        f"WHEN MATCHED AND s.report_created_at > t.report_created_at THEN UPDATE SET * "
        f"WHEN NOT MATCHED THEN INSERT *"
    )
