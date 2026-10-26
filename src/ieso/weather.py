"""Parser for Open-Meteo hourly weather JSON (forecast and archive APIs).

Files land in bronze as:
    weather/ingest_date=<day>/openmeteo_<city>.json                         (daily: last 7 days + today)
    weather/ingest_date=<day>/openmeteo_archive_<city>_<start>_<end>.json  (one-time history)

Both APIs return the same shape when called with timezone=GMT:
    {"utc_offset_seconds": 0,
     "hourly": {"time": ["2026-10-16T00:00", ...],
                "temperature_2m": [4.1, ...],
                "relative_humidity_2m": [81, ...]}}
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone

from .parsers import ParseResult
from .time_utils import utc_to_hour_ending

_CITY = re.compile(r"^openmeteo_(?P<archive>archive_)?(?P<city>[a-z]+)(?:_\d{4}-\d{2}-\d{2}_\d{4}-\d{2}-\d{2})?\.json$")

KNOWN_CITIES = {"toronto", "ottawa", "london"}

# Physical sanity limits for Ontario; anything outside is a data error.
TEMP_RANGE_C = (-60.0, 50.0)
HUMIDITY_RANGE = (0.0, 100.0)


def parse_openmeteo_json(content: str | bytes, source_file: str) -> ParseResult:
    m = _CITY.match(source_file)
    if not m or m.group("city") not in KNOWN_CITIES:
        raise ValueError(f"{source_file}: unexpected weather file name")
    city = m.group("city")
    weather_source = "archive" if m.group("archive") else "forecast_api"

    data = json.loads(content)
    if data.get("error"):
        raise ValueError(f"{source_file}: Open-Meteo error: {data.get('reason')}")
    if data.get("utc_offset_seconds", 0) != 0:
        raise ValueError(f"{source_file}: expected GMT timestamps, got offset {data.get('utc_offset_seconds')}")

    hourly = data.get("hourly") or {}
    times = hourly.get("time") or []
    temps = hourly.get("temperature_2m") or []
    hums = hourly.get("relative_humidity_2m") or []
    if not (len(times) == len(temps) == len(hums)):
        raise ValueError(f"{source_file}: hourly arrays have different lengths")

    result = ParseResult()
    for t, temp, hum in zip(times, temps, hums):
        base = {"source_file": source_file, "raw": {"city": city, "time": t, "temp": temp, "rh": hum}}
        try:
            start = datetime.fromisoformat(t).replace(tzinfo=timezone.utc)
        except (TypeError, ValueError):
            result.rejects.append({**base, "rejection_reason": "invalid timestamp"})
            continue
        if temp is None:
            result.rejects.append({**base, "rejection_reason": "missing temperature"})
            continue
        if not TEMP_RANGE_C[0] <= temp <= TEMP_RANGE_C[1]:
            result.rejects.append({**base, "rejection_reason": f"temperature out of range: {temp}"})
            continue
        if hum is not None and not HUMIDITY_RANGE[0] <= hum <= HUMIDITY_RANGE[1]:
            result.rejects.append({**base, "rejection_reason": f"humidity out of range: {hum}"})
            continue

        d, he = utc_to_hour_ending(start)
        result.rows.append({
            "delivery_date": d.isoformat(),
            "hour_ending": he,
            "interval_start_utc": start.isoformat(),
            "interval_end_utc": (start + timedelta(hours=1)).isoformat(),
            "city": city,
            "temperature_c": float(temp),
            "relative_humidity_pct": float(hum) if hum is not None else None,
            "weather_source": weather_source,
            "source_file": source_file,
        })
    return result
