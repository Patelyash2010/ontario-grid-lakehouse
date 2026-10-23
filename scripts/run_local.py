
"""Local mini-pipeline: IESO -> bronze (raw files) -> silver (clean CSV + quarantine).

Run this on your own machine before building anything in Azure. It does,
in plain Python, exactly what Data Factory + Databricks will do later:

  1. List a report directory on reports-public.ieso.ca
  2. Compare against a manifest of files already downloaded (the "watermark")
  3. Download only new versioned files into data/bronze/... (never modified)
  4. Parse the LATEST version per period into data/silver/... CSVs
  5. Write rejected rows to data/quarantine/... with a reason
  6. Append a line to data/audit_log.csv (files, rows, rejects, duration)

Usage (from the repo root):
    python scripts/run_local.py                  # current year, all 3 reports
    python scripts/run_local.py --years 2025 2026
    python scripts/run_local.py --price-days 14  # day-ahead price files to fetch

Run it twice in a row: the second run downloads nothing. That is idempotency.
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import sys
import time
import urllib.request
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ieso import (  # noqa: E402
    latest_versions,
    parse_da_zonal_price_xml,
    parse_demand_csv,
    parse_filename,
    parse_fuel_hourly_xml,
    select_new_files,
)

BASE_URL = "https://reports-public.ieso.ca/public"
DATA = Path("data")
MANIFEST = DATA / "manifest.json"

REPORTS = {
    "Demand": parse_demand_csv,
    "GenOutputbyFuelHourly": parse_fuel_hourly_xml,
    "DAHourlyOntarioZonalPrice": parse_da_zonal_price_xml,
}


def http_get(url: str, retries: int = 3) -> bytes:
    """GET with simple exponential-backoff retries (Data Factory does this for you later)."""
    for attempt in range(1, retries + 1):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "ontario-grid-lakehouse/0.1 (portfolio project)"})
            with urllib.request.urlopen(req, timeout=60) as resp:
                return resp.read()
        except Exception as exc:  # noqa: BLE001
            if attempt == retries:
                raise
            wait = 2 ** attempt
            print(f"  retry {attempt}/{retries} after error: {exc} (waiting {wait}s)")
            time.sleep(wait)
    raise RuntimeError("unreachable")


def list_directory(report: str) -> list[str]:
    html = http_get(f"{BASE_URL}/{report}/").decode("latin-1")
    return sorted(set(re.findall(r'href="(PUB_[^"/]+\.(?:xml|csv))"', html)))


def wanted(file, years: set[str], price_since: str) -> bool:
    """Limit the backfill so the first run stays small."""
    if file.period is None:
        return False
    if len(file.period) == 4:                 # yearly files
        return file.period in years
    return file.period >= price_since         # daily files (yyyymmdd)


def load_manifest() -> dict:
    return json.loads(MANIFEST.read_text()) if MANIFEST.exists() else {}


def save_manifest(m: dict) -> None:
    MANIFEST.write_text(json.dumps(m, indent=2, sort_keys=True))


def write_csv(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        return
    rows = [{k: (json.dumps(v) if isinstance(v, dict) else v) for k, v in r.items()} for r in rows]
    with path.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--years", nargs="+", default=[str(date.today().year)])
    ap.add_argument("--price-days", type=int, default=7)
    args = ap.parse_args()

    started = time.time()
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    ingest_date = date.today().isoformat()
    price_since = (date.today() - timedelta(days=args.price_days)).strftime("%Y%m%d")
    manifest = load_manifest()
    audit = {"run_id": run_id, "files_downloaded": 0, "rows_loaded": 0, "rows_quarantined": 0}

    for report, parser in REPORTS.items():
        print(f"\n== {report}")
        processed = manifest.get(report, [])
        new_files = [f for f in select_new_files(list_directory(report), processed)
                     if wanted(f, set(args.years), price_since)]
        print(f"  {len(new_files)} new file(s) to download")

        # ---- bronze: land raw files exactly as received
        for f in new_files:
            target = DATA / "bronze" / report / f"ingest_date={ingest_date}" / f.file_name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(http_get(f"{BASE_URL}/{report}/{f.file_name}"))
            processed.append(f.file_name)
            audit["files_downloaded"] += 1
            print(f"  landed {f.file_name}")
        manifest[report] = sorted(set(processed))
        save_manifest(manifest)   # save after each report so a crash does not lose progress

        # ---- silver: parse the latest version of each period from everything in bronze
        bronze_files = {p.name: p for p in (DATA / "bronze" / report).rglob("PUB_*")}
        best = latest_versions(parse_filename(n) for n in bronze_files)
        all_rows, all_rejects = [], []
        for rf in sorted(best.values(), key=lambda x: x.period):
            content = bronze_files[rf.file_name].read_text(encoding="utf-8", errors="replace")
            result = parser(content, rf.file_name)
            all_rows += result.rows
            all_rejects += [{**r, "run_id": run_id} for r in result.rejects]

        write_csv(DATA / "silver" / f"{report}.csv", all_rows)
        write_csv(DATA / "quarantine" / f"{report}_{run_id}.csv", all_rejects)
        audit["rows_loaded"] += len(all_rows)
        audit["rows_quarantined"] += len(all_rejects)
        print(f"  silver: {len(all_rows):,} rows | quarantine: {len(all_rejects)} rows")

    audit["duration_s"] = round(time.time() - started, 1)
    log = DATA / "audit_log.csv"
    new_log = not log.exists()
    with log.open("a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(audit.keys()))
        if new_log:
            w.writeheader()
        w.writerow(audit)
    print(f"\nRun {run_id} done: {audit}")


if __name__ == "__main__":
    main()
