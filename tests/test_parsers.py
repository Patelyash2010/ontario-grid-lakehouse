from datetime import date
from pathlib import Path

import pytest

from ieso import (
    hour_ending_to_utc,
    latest_versions,
    parse_da_zonal_price_xml,
    parse_demand_csv,
    parse_filename,
    parse_fuel_hourly_xml,
    select_new_files,
)

FIX = Path(__file__).parent / "fixtures"


def read(name: str) -> str:
    return (FIX / name).read_text()


# ---------------------------------------------------------------- time handling

def test_hour_ending_1_starts_at_midnight_est():
    start, end = hour_ending_to_utc(date(2026, 1, 1), 1)
    assert start.isoformat() == "2026-01-01T05:00:00+00:00"
    assert end.isoformat() == "2026-01-01T06:00:00+00:00"


def test_hour_ending_24_ends_at_next_midnight():
    start, end = hour_ending_to_utc(date(2026, 1, 1), 24)
    assert end.isoformat() == "2026-01-02T05:00:00+00:00"


def test_no_dst_shift_in_summer():
    # IESO uses EST all year, so July is still UTC-5 (not UTC-4).
    start, _ = hour_ending_to_utc(date(2026, 7, 1), 1)
    assert start.isoformat() == "2026-07-01T05:00:00+00:00"


@pytest.mark.parametrize("bad", [0, 25, -1])
def test_hour_out_of_range_raises(bad):
    with pytest.raises(ValueError):
        hour_ending_to_utc(date(2026, 1, 1), bad)


# ---------------------------------------------------------------- demand CSV

def test_demand_parses_real_format():
    r = parse_demand_csv(read("PUB_Demand_2026_v294.csv"), "PUB_Demand_2026_v294.csv")
    assert r.report_created_at == "2026-10-22 07:30:10"
    assert len(r.rows) == 48 and r.rejects == []
    first = r.rows[0]
    assert first["ontario_demand_mw"] == 16526
    assert first["market_demand_mw"] == 19489
    assert first["interval_start_utc"] == "2026-01-01T05:00:00+00:00"


def test_dst_day_has_24_unique_utc_hours():
    r = parse_demand_csv(read("PUB_Demand_2026_v294.csv"), "x.csv")
    dst_rows = [row for row in r.rows if row["delivery_date"] == "2026-03-08"]
    assert len(dst_rows) == 24
    assert len({row["interval_start_utc"] for row in dst_rows}) == 24


def test_demand_bad_rows_go_to_quarantine():
    r = parse_demand_csv(read("demand_bad.csv"), "demand_bad.csv")
    assert len(r.rows) == 1
    reasons = sorted(x["rejection_reason"] for x in r.rejects)
    assert reasons == [
        "hour out of range: '25'",
        "invalid date",
        "missing or non-numeric demand value",
        "negative demand",
    ]


def test_demand_unexpected_header_fails_loudly():
    with pytest.raises(ValueError):
        parse_demand_csv("Date,Hr,Demand\n2026-01-01,1,5\n", "schema_change.csv")


# ---------------------------------------------------------------- fuel XML

def test_fuel_parses_real_format():
    r = parse_fuel_hourly_xml(read("PUB_GenOutputbyFuelHourly_2026_v294.xml"), "fuel.xml")
    assert r.report_created_at == "2026-10-22T06:50:53"
    assert len(r.rows) == 14 and r.rejects == []          # 2 hours x 7 fuels
    nuclear_h1 = next(x for x in r.rows if x["fuel_type"] == "NUCLEAR" and x["hour_ending"] == 1)
    assert nuclear_h1["output_mwh"] == 9602
    gas_h1 = next(x for x in r.rows if x["fuel_type"] == "GAS" and x["hour_ending"] == 1)
    assert gas_h1["output_quality"] == -1


def test_control_actions_kept_but_not_generation():
    # Real case from PUB_GenOutputbyFuelHourly_2026_v294: 2026-07-15 had a
    # "CONTROL ACTIONS" category, empty in early hours and 0 afterwards.
    xml = read("PUB_GenOutputbyFuelHourly_2026_v294.xml").replace(
        "</HourlyData>",
        "<FuelTotal><Fuel>CONTROL ACTIONS</Fuel><EnergyValue>"
        "<OutputQuality>0</OutputQuality><Output></Output></EnergyValue></FuelTotal></HourlyData>",
        1,
    )
    r = parse_fuel_hourly_xml(xml, "fuel.xml")
    assert r.rejects == []
    ca = [x for x in r.rows if x["fuel_type"] == "CONTROL ACTIONS"]
    assert len(ca) == 1 and ca[0]["is_generation"] is False and ca[0]["output_mwh"] is None
    assert all(x["is_generation"] for x in r.rows if x["fuel_type"] != "CONTROL ACTIONS")


def test_fuel_bad_records_quarantined():
    r = parse_fuel_hourly_xml(read("fuel_bad.xml"), "fuel_bad.xml")
    assert [x["fuel_type"] for x in r.rows] == ["NUCLEAR"]
    reasons = sorted(x["rejection_reason"] for x in r.rejects)
    assert reasons == ["missing output value", "negative output", "unknown fuel type: 'COAL'"]


# ---------------------------------------------------------------- price XML

def test_price_parses_real_format():
    r = parse_da_zonal_price_xml(read("PUB_DAHourlyOntarioZonalPrice_20260924_v3.xml"), "p.xml")
    assert len(r.rows) == 24 and r.rejects == []
    peak = max(r.rows, key=lambda x: x["price_cad_per_mwh"])
    assert peak["hour_ending"] == 19 and peak["price_cad_per_mwh"] == 64.43
    assert all(x["market_era"] == "POST_MRP" for x in r.rows)
    assert r.rows[0]["congestion_component"] == -0.2


def test_negative_price_is_kept_not_rejected():
    xml = read("PUB_DAHourlyOntarioZonalPrice_20260924_v3.xml").replace(
        "<ZonalPrice>24.05</ZonalPrice>", "<ZonalPrice>-3.5</ZonalPrice>")
    r = parse_da_zonal_price_xml(xml, "neg.xml")
    assert r.rows[0]["price_cad_per_mwh"] == -3.5 and r.rejects == []


# ---------------------------------------------------------------- file names & manifest

@pytest.mark.parametrize("name,report,period,version", [
    ("PUB_Demand_2026_v294.csv", "Demand", "2026", 294),
    ("PUB_DAHourlyOntarioZonalPrice_20260924_v3.xml", "DAHourlyOntarioZonalPrice", "20260924", 3),
    ("PUB_GenOutputbyFuelHourly_2015.xml", "GenOutputbyFuelHourly", "2015", None),
    ("PUB_Demand.csv", "Demand", None, None),
])
def test_parse_filename(name, report, period, version):
    rf = parse_filename(name)
    assert (rf.report, rf.period, rf.version) == (report, period, version)


def test_parse_filename_rejects_other_files():
    assert parse_filename("index.html") is None


def test_manifest_skips_aliases_and_seen_files():
    listing = [
        "PUB_DAHourlyOntarioZonalPrice.xml",               # alias - skip
        "PUB_DAHourlyOntarioZonalPrice_20260924.xml",      # alias - skip
        "PUB_DAHourlyOntarioZonalPrice_20260924_v1.xml",   # already processed
        "PUB_DAHourlyOntarioZonalPrice_20260924_v2.xml",
        "PUB_DAHourlyOntarioZonalPrice_20260924_v3.xml",
        "PUB_DAHourlyOntarioZonalPrice_20260925_v1.xml",
    ]
    new = select_new_files(listing, already_processed=["PUB_DAHourlyOntarioZonalPrice_20260924_v1.xml"])
    assert [f.file_name for f in new] == [
        "PUB_DAHourlyOntarioZonalPrice_20260924_v2.xml",
        "PUB_DAHourlyOntarioZonalPrice_20260924_v3.xml",
        "PUB_DAHourlyOntarioZonalPrice_20260925_v1.xml",
    ]


def test_latest_version_wins():
    files = [parse_filename(n) for n in [
        "PUB_DAHourlyOntarioZonalPrice_20260924_v1.xml",
        "PUB_DAHourlyOntarioZonalPrice_20260924_v3.xml",
        "PUB_DAHourlyOntarioZonalPrice_20260924_v2.xml",
    ]]
    best = latest_versions(files)
    assert best[("DAHourlyOntarioZonalPrice", "20260924")].version == 3