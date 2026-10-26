import json
from datetime import date, datetime, timezone
from pathlib import Path

import pytest

from ieso.silver import REPORTS, bronze_folder, files_to_process, latest_per_key, parse_bronze_file, to_tuples
from ieso.time_utils import hour_ending_to_utc, utc_to_hour_ending
from ieso.weather import parse_openmeteo_json

FIX = Path(__file__).parent / "fixtures"
VOL = "/Volumes/ontgrid/bronze/raw"


def ms(dt):
    return int(dt.timestamp() * 1000)


def test_utc_to_hour_ending_is_inverse_of_hour_ending_to_utc():
    for he in (1, 12, 24):
        start, _ = hour_ending_to_utc(date(2026, 7, 1), he)
        assert utc_to_hour_ending(start) == (date(2026, 7, 1), he)


def test_utc_midnight_belongs_to_previous_ieso_day():
    assert utc_to_hour_ending(datetime(2026, 1, 1, 0, tzinfo=timezone.utc)) == (date(2025, 12, 31), 20)


def test_parse_daily_file():
    r = parse_openmeteo_json((FIX / "openmeteo_toronto.json").read_text(), "openmeteo_toronto.json")
    assert len(r.rows) == 24 and r.rejects == []
    first = r.rows[0]
    assert first["city"] == "toronto" and first["weather_source"] == "forecast_api"
    assert first["interval_start_utc"] == "2026-10-26T00:00:00+00:00"
    assert (first["delivery_date"], first["hour_ending"]) == ("2026-10-25", 20)
    assert first["temperature_c"] == 6.1 and first["relative_humidity_pct"] == 84.0


def test_archive_file_name_gives_city_and_source():
    name = "openmeteo_archive_london_2026-01-01_2026-10-16.json"
    r = parse_openmeteo_json((FIX / name).read_text(), name)
    assert {x["city"] for x in r.rows} == {"london"}
    assert {x["weather_source"] for x in r.rows} == {"archive"}


def test_bad_weather_values_quarantined():
    r = parse_openmeteo_json((FIX / "openmeteo_ottawa.json").read_text(), "openmeteo_ottawa.json")
    assert len(r.rows) == 2                      # 1.0 C and 3.0 C (null humidity allowed)
    assert sorted(x["rejection_reason"] for x in r.rejects) == [
        "humidity out of range: 120", "missing temperature", "temperature out of range: 75.0"]


@pytest.mark.parametrize("name", ["openmeteo_paris.json", "weather.json", "openmeteo_toronto.csv"])
def test_unexpected_file_name_fails_loudly(name):
    with pytest.raises(ValueError):
        parse_openmeteo_json((FIX / "openmeteo_toronto.json").read_text(), name)


def test_api_error_and_wrong_timezone_fail_loudly():
    with pytest.raises(ValueError):
        parse_openmeteo_json(json.dumps({"error": True, "reason": "bad date"}), "openmeteo_toronto.json")
    doc = json.loads((FIX / "openmeteo_toronto.json").read_text())
    doc["utc_offset_seconds"] = -14400
    with pytest.raises(ValueError):
        parse_openmeteo_json(json.dumps(doc), "openmeteo_toronto.json")


def test_mismatched_array_lengths_fail_loudly():
    doc = json.loads((FIX / "openmeteo_toronto.json").read_text())
    doc["hourly"]["temperature_2m"].pop()
    with pytest.raises(ValueError):
        parse_openmeteo_json(json.dumps(doc), "openmeteo_toronto.json")


def test_bronze_weather_uses_file_time_and_flags_forecast_hours():
    fetched = datetime(2026, 10, 26, 13, 30, tzinfo=timezone.utc)
    path = f"{VOL}/weather/ingest_date=2026-10-26/openmeteo_toronto.json"
    r = parse_bronze_file("weather", path, (FIX / "openmeteo_toronto.json").read_bytes(), ms(fetched))
    assert r.rows[0]["report_created_at"] == "2026-10-26T13:30:00"
    flags = {x["interval_start_utc"][11:13]: x["is_forecast"] for x in r.rows}
    assert flags["12"] is False      # 12:00-13:00 finished before 13:30
    assert flags["13"] is True       # 13:00-14:00 still in progress
    assert flags["23"] is True


def test_weather_requires_modification_time():
    with pytest.raises(ValueError):
        parse_bronze_file("weather", "p/openmeteo_toronto.json", (FIX / "openmeteo_toronto.json").read_bytes())


def test_newer_fetch_replaces_forecast_with_actual():
    path = f"{VOL}/weather/x/openmeteo_toronto.json"
    data = (FIX / "openmeteo_toronto.json").read_bytes()
    today = parse_bronze_file("weather", path, data, ms(datetime(2026, 10, 26, 13, tzinfo=timezone.utc))).rows
    tomorrow = parse_bronze_file("weather", path, data, ms(datetime(2026, 10, 27, 13, tzinfo=timezone.utc))).rows
    best = latest_per_key(today + tomorrow, REPORTS["weather"].keys)
    assert len(best) == 24 and not any(r["is_forecast"] for r in best)


def test_weather_file_selection_and_folder():
    cfg = REPORTS["weather"]
    listing = [{"path": f"{VOL}/weather/d/openmeteo_toronto.json", "modification_time": 1, "size": 1},
               {"path": f"{VOL}/weather/d/PUB_Demand_2026.csv", "modification_time": 1, "size": 1}]
    new = files_to_process(listing, [], cfg.file_prefixes, cfg.file_suffixes)
    assert [f["path"].rsplit("/", 1)[-1] for f in new] == ["openmeteo_toronto.json"]
    assert bronze_folder("weather") == "weather" and bronze_folder("Demand") == "Demand"


def test_weather_tuples_match_schema():
    cfg = REPORTS["weather"]
    r = parse_bronze_file("weather", "p/openmeteo_toronto.json", (FIX / "openmeteo_toronto.json").read_bytes(),
                          ms(datetime(2026, 10, 27, tzinfo=timezone.utc)))
    t = to_tuples(r.rows, cfg.columns)[0]
    names = cfg.column_names
    assert t[names.index("city")] == "toronto"
    assert t[names.index("is_forecast")] is False
    assert t[names.index("interval_start_utc")] == datetime(2026, 10, 26, tzinfo=timezone.utc)
