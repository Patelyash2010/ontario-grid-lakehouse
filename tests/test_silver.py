from datetime import date, datetime, timezone
from pathlib import Path

from ieso.silver import (
    REPORTS,
    files_to_process,
    latest_per_key,
    merge_sql,
    normalize_created_at,
    parse_bronze_file,
    reject_tuples,
    to_tuples,
)

FIX = Path(__file__).parent / "fixtures"
VOL = "/Volumes/ontgrid/bronze/raw"


def test_files_to_process_skips_seen_and_non_report_files():
    listing = [
        {"path": f"{VOL}/Demand/ingest_date=2026-10-24/PUB_Demand_2026.csv", "modification_time": 200, "size": 1},
        {"path": f"{VOL}/Demand/ingest_date=2026-10-23/PUB_Demand_2026.csv", "modification_time": 100, "size": 1},
        {"path": f"{VOL}/Demand/ingest_date=2026-10-23/notes.txt", "modification_time": 50, "size": 1},
    ]
    processed = [(f"{VOL}/Demand/ingest_date=2026-10-23/PUB_Demand_2026.csv", 100)]
    new = files_to_process(listing, processed)
    assert [f["modification_time"] for f in new] == [200]


def test_overwritten_file_counts_as_new():
    path = f"{VOL}/Demand/ingest_date=2026-10-24/PUB_Demand_2026.csv"
    new = files_to_process([{"path": path, "modification_time": 300, "size": 1}], [(path, 200)])
    assert len(new) == 1


def test_files_sorted_oldest_first():
    listing = [{"path": f"{VOL}/D/PUB_b.csv", "modification_time": 9, "size": 1},
               {"path": f"{VOL}/D/PUB_a.csv", "modification_time": 3, "size": 1}]
    assert [f["modification_time"] for f in files_to_process(listing, [])] == [3, 9]


def test_normalize_created_at_both_formats():
    assert normalize_created_at("2026-10-22 07:30:10") == "2026-10-22T07:30:10"
    assert normalize_created_at("2026-10-22T06:50:53") == "2026-10-22T06:50:53"
    assert normalize_created_at(None) is None


def test_parse_bronze_demand_adds_lineage():
    path = f"{VOL}/Demand/ingest_date=2026-10-26/PUB_Demand_2026_v294.csv"
    r = parse_bronze_file("Demand", path, (FIX / "PUB_Demand_2026_v294.csv").read_bytes())
    assert len(r.rows) == 48
    assert r.rows[0]["bronze_path"] == path
    assert r.rows[0]["report_created_at"] == "2026-10-22T07:30:10"
    assert r.rows[0]["source_file"] == "PUB_Demand_2026_v294.csv"


def test_parse_bronze_xml_reads_bytes():
    path = f"{VOL}/GenOutputbyFuelHourly/ingest_date=2026-10-26/PUB_GenOutputbyFuelHourly_2026.xml"
    r = parse_bronze_file("GenOutputbyFuelHourly", path,
                          (FIX / "PUB_GenOutputbyFuelHourly_2026_v294.xml").read_bytes())
    assert len(r.rows) == 14 and r.rows[0]["report_created_at"] == "2026-10-22T06:50:53"


def test_latest_per_key_newer_file_wins():
    old = {"interval_start_utc": "2026-01-01T05:00:00+00:00", "ontario_demand_mw": 1,
           "report_created_at": "2026-10-21T07:30:00"}
    new = {**old, "ontario_demand_mw": 2, "report_created_at": "2026-10-22T07:30:00"}
    other = {**old, "interval_start_utc": "2026-01-01T06:00:00+00:00"}
    out = latest_per_key([new, old, other], ["interval_start_utc"])
    assert len(out) == 2
    assert next(r for r in out if r["interval_start_utc"].startswith("2026-01-01T05"))["ontario_demand_mw"] == 2


def test_latest_per_key_composite_key():
    a = {"interval_start_utc": "t1", "fuel_type": "GAS", "report_created_at": "x"}
    b = {"interval_start_utc": "t1", "fuel_type": "WIND", "report_created_at": "x"}
    assert len(latest_per_key([a, b], ["interval_start_utc", "fuel_type"])) == 2


def test_to_tuples_types_match_schema():
    path = f"{VOL}/Demand/x/PUB_Demand_2026_v294.csv"
    r = parse_bronze_file("Demand", path, (FIX / "PUB_Demand_2026_v294.csv").read_bytes())
    cfg = REPORTS["Demand"]
    t = to_tuples(r.rows, cfg.columns)[0]
    assert len(t) == len(cfg.columns)
    assert t[0] == date(2026, 1, 1)
    assert t[2] == datetime(2026, 1, 1, 5, tzinfo=timezone.utc)
    assert t[5] == 16526


def test_to_tuples_keeps_nulls():
    cfg = REPORTS["GenOutputbyFuelHourly"]
    row = {"delivery_date": "2026-07-15", "hour_ending": 1,
           "interval_start_utc": "2026-07-15T05:00:00+00:00", "interval_end_utc": "2026-07-15T06:00:00+00:00",
           "fuel_type": "CONTROL ACTIONS", "output_mwh": None, "output_quality": 0, "is_generation": False}
    t = to_tuples([row], cfg.columns)[0]
    assert t[cfg.column_names.index("output_mwh")] is None
    assert t[cfg.column_names.index("is_generation")] is False


def test_reject_tuples_serialises_raw():
    rejects = [{"source_file": "f", "bronze_path": "p", "rejection_reason": "bad", "raw": {"a": 1}}]
    assert reject_tuples("Demand", rejects, "run1") == [("Demand", "f", "p", "bad", '{"a": 1}', "run1")]


def test_merge_sql_updates_only_newer_rows():
    sql = merge_sql("ontgrid.silver.t", "src", ["interval_start_utc", "fuel_type"])
    assert "t.interval_start_utc = s.interval_start_utc AND t.fuel_type = s.fuel_type" in sql
    assert "WHEN MATCHED AND s.report_created_at > t.report_created_at THEN UPDATE SET *" in sql
    assert "WHEN NOT MATCHED THEN INSERT *" in sql


def test_every_report_has_lineage_columns_and_keys_in_columns():
    for cfg in REPORTS.values():
        names = cfg.column_names
        assert {"source_file", "report_created_at", "bronze_path"} <= set(names)
        assert set(cfg.keys) <= set(names)
