"""Parsers for the three IESO reports used in the silver layer.

Each parser returns a ParseResult:
    rows    -> clean records, one dict per row (ready for a DataFrame / Delta table)
    rejects -> records that failed a check, each with a `rejection_reason`
               (these go to the quarantine table instead of being dropped)

Every row carries `source_file` and `report_created_at` so any number in a
dashboard can be traced back to the exact IESO file it came from.
"""

from __future__ import annotations

import csv
import io
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Optional

from .time_utils import hour_ending_to_utc

NS = {"i": "http://www.ieso.ca/schema"}

# Market Renewal Program go-live. Before this date Ontario had a single HOEP
# price; from this date the day-ahead Ontario Zonal Price exists.
MRP_GO_LIVE = date(2025, 5, 1)

KNOWN_FUELS = {"NUCLEAR", "GAS", "HYDRO", "WIND", "SOLAR", "BIOFUEL", "OTHER"}

# Categories that appear in the fuel report but are not generation sources.
# Found in real data: "CONTROL ACTIONS" on 2026-07-15 (hours 1-9 empty,
# 10-24 zero). IESO's report help file does not define it, so it is kept in
# silver for traceability but flagged is_generation=False and excluded from
# supply-mix totals. Any OTHER unrecognised category is still quarantined.
NON_GENERATION_CATEGORIES = {"CONTROL ACTIONS"}


@dataclass
class ParseResult:
    rows: list[dict] = field(default_factory=list)
    rejects: list[dict] = field(default_factory=list)
    report_created_at: Optional[str] = None


# --------------------------------------------------------------------------- helpers

def _text(el: Optional[ET.Element]) -> Optional[str]:
    return el.text.strip() if el is not None and el.text is not None else None


def _find(parent: ET.Element, tag: str) -> Optional[ET.Element]:
    """Find a child in the IESO namespace (falls back to no namespace)."""
    el = parent.find(f"i:{tag}", NS)
    return el if el is not None else parent.find(tag)


def _findall(parent: ET.Element, tag: str) -> list[ET.Element]:
    found = parent.findall(f"i:{tag}", NS)
    return found if found else parent.findall(tag)


def _to_int(value: Optional[str]) -> Optional[int]:
    try:
        return int(value) if value not in (None, "") else None
    except ValueError:
        return None


def _to_float(value: Optional[str]) -> Optional[float]:
    try:
        return float(value) if value not in (None, "") else None
    except ValueError:
        return None


def _time_cols(d: date, hour: int) -> dict:
    start, end = hour_ending_to_utc(d, hour)
    return {
        "delivery_date": d.isoformat(),
        "hour_ending": hour,
        "interval_start_utc": start.isoformat(),
        "interval_end_utc": end.isoformat(),
    }


def _header_created_at(root: ET.Element) -> Optional[str]:
    header = _find(root, "DocHeader")
    return _text(_find(header, "CreatedAt")) if header is not None else None


# --------------------------------------------------------------------------- demand (CSV)

def parse_demand_csv(content: str, source_file: str) -> ParseResult:
    """Parse PUB_Demand_<year>_v<N>.csv.

    Format (verified Oct 2026):
        \\Hourly Demand Report,,,
        \\Created at 2026-10-22 07:30:10,,,
        \\For 2026,,,
        Date,Hour,Market Demand,Ontario Demand
        2026-01-01,1,19489,16526
    Values are MW.
    """
    result = ParseResult()
    data_lines = []
    for line in content.splitlines():
        if line.startswith("\\"):
            if line.lower().startswith("\\created at"):
                result.report_created_at = line.split(",")[0][len("\\Created at "):].strip()
            continue
        if line.strip():
            data_lines.append(line)

    reader = csv.DictReader(io.StringIO("\n".join(data_lines)))
    expected = {"Date", "Hour", "Market Demand", "Ontario Demand"}
    if reader.fieldnames is None or not expected.issubset({f.strip() for f in reader.fieldnames}):
        raise ValueError(f"{source_file}: unexpected header {reader.fieldnames}")

    for line_no, raw in enumerate(reader, start=1):
        raw = {k.strip(): (v or "").strip() for k, v in raw.items() if k}
        base = {"source_file": source_file, "report_created_at": result.report_created_at, "raw": raw}

        try:
            d = date.fromisoformat(raw["Date"])
        except ValueError:
            result.rejects.append({**base, "rejection_reason": "invalid date"})
            continue
        hour = _to_int(raw["Hour"])
        if hour is None or not 1 <= hour <= 24:
            result.rejects.append({**base, "rejection_reason": f"hour out of range: {raw['Hour']!r}"})
            continue
        market = _to_int(raw["Market Demand"])
        ontario = _to_int(raw["Ontario Demand"])
        if market is None or ontario is None:
            result.rejects.append({**base, "rejection_reason": "missing or non-numeric demand value"})
            continue
        if market < 0 or ontario < 0:
            result.rejects.append({**base, "rejection_reason": "negative demand"})
            continue

        result.rows.append({
            **_time_cols(d, hour),
            "market_demand_mw": market,
            "ontario_demand_mw": ontario,
            "source_file": source_file,
            "report_created_at": result.report_created_at,
        })
    return result


# --------------------------------------------------------------------------- fuel mix (XML)

def parse_fuel_hourly_xml(content: str | bytes, source_file: str) -> ParseResult:
    """Parse PUB_GenOutputbyFuelHourly_<year>_v<N>.xml.

    Structure: DocBody > DailyData(Day) > HourlyData(Hour) > FuelTotal(Fuel)
               > EnergyValue(OutputQuality, Output)
    Output is MWh for the hour. OutputQuality is kept as-is (IESO quality flag;
    0 = normal, negative values flag estimated/partial data).
    """
    root = ET.fromstring(content.encode() if isinstance(content, str) else content)
    result = ParseResult(report_created_at=_header_created_at(root))
    body = _find(root, "DocBody")
    if body is None:
        raise ValueError(f"{source_file}: no DocBody")

    for day_el in _findall(body, "DailyData"):
        day_txt = _text(_find(day_el, "Day"))
        for hour_el in _findall(day_el, "HourlyData"):
            hour = _to_int(_text(_find(hour_el, "Hour")))
            for fuel_el in _findall(hour_el, "FuelTotal"):
                fuel = (_text(_find(fuel_el, "Fuel")) or "").upper()
                energy = _find(fuel_el, "EnergyValue")
                # Tag is <Output>; accept <o> too in case of formatting differences.
                out_el = None
                if energy is not None:
                    out_el = _find(energy, "Output")
                    if out_el is None:
                        out_el = _find(energy, "o")
                output = _to_float(_text(out_el))
                quality = _to_int(_text(_find(energy, "OutputQuality"))) if energy is not None else None

                base = {"source_file": source_file, "report_created_at": result.report_created_at,
                        "raw": {"day": day_txt, "hour": hour, "fuel": fuel, "output": _text(out_el)}}
                try:
                    d = date.fromisoformat(day_txt or "")
                except ValueError:
                    result.rejects.append({**base, "rejection_reason": "invalid date"})
                    continue
                if hour is None or not 1 <= hour <= 24:
                    result.rejects.append({**base, "rejection_reason": "hour out of range"})
                    continue
                if fuel in NON_GENERATION_CATEGORIES:
                    # Kept as-is (value may be empty); never counted as generation.
                    result.rows.append({
                        **_time_cols(d, hour),
                        "fuel_type": fuel,
                        "output_mwh": output,
                        "output_quality": quality,
                        "is_generation": False,
                        "source_file": source_file,
                        "report_created_at": result.report_created_at,
                    })
                    continue
                if fuel not in KNOWN_FUELS:
                    result.rejects.append({**base, "rejection_reason": f"unknown fuel type: {fuel!r}"})
                    continue
                if output is None:
                    result.rejects.append({**base, "rejection_reason": "missing output value"})
                    continue
                if output < 0:
                    result.rejects.append({**base, "rejection_reason": "negative output"})
                    continue

                result.rows.append({
                    **_time_cols(d, hour),
                    "fuel_type": fuel,
                    "output_mwh": output,
                    "output_quality": quality,
                    "is_generation": True,
                    "source_file": source_file,
                    "report_created_at": result.report_created_at,
                })
    return result


# --------------------------------------------------------------------------- day-ahead price (XML)

def parse_da_zonal_price_xml(content: str | bytes, source_file: str) -> ParseResult:
    """Parse PUB_DAHourlyOntarioZonalPrice_<yyyymmdd>_v<N>.xml.

    Structure: DocBody > DeliveryDate, HourlyPriceComponents(PricingHour,
               ZonalPrice, LossPriceCapped, CongestionPriceCapped, Flag)
    Prices are $/MWh. Negative prices are VALID in electricity markets
    (surplus supply), so they are kept, not rejected.
    """
    root = ET.fromstring(content.encode() if isinstance(content, str) else content)
    result = ParseResult(report_created_at=_header_created_at(root))
    body = _find(root, "DocBody")
    if body is None:
        raise ValueError(f"{source_file}: no DocBody")

    day_txt = _text(_find(body, "DeliveryDate"))
    try:
        d = date.fromisoformat(day_txt or "")
    except ValueError:
        result.rejects.append({"source_file": source_file, "raw": {"DeliveryDate": day_txt},
                               "rejection_reason": "invalid delivery date"})
        return result

    for comp in _findall(body, "HourlyPriceComponents"):
        hour = _to_int(_text(_find(comp, "PricingHour")))
        price = _to_float(_text(_find(comp, "ZonalPrice")))
        base = {"source_file": source_file, "report_created_at": result.report_created_at,
                "raw": {"date": day_txt, "hour": hour, "price": _text(_find(comp, "ZonalPrice"))}}
        if hour is None or not 1 <= hour <= 24:
            result.rejects.append({**base, "rejection_reason": "hour out of range"})
            continue
        if price is None:
            result.rejects.append({**base, "rejection_reason": "missing zonal price"})
            continue

        result.rows.append({
            **_time_cols(d, hour),
            "price_type": "DA_ONTARIO_ZONAL",
            "market_era": "POST_MRP" if d >= MRP_GO_LIVE else "PRE_MRP",
            "price_cad_per_mwh": price,
            "loss_component": _to_float(_text(_find(comp, "LossPriceCapped"))),
            "congestion_component": _to_float(_text(_find(comp, "CongestionPriceCapped"))),
            "flag": _text(_find(comp, "Flag")),
            "source_file": source_file,
            "report_created_at": result.report_created_at,
        })
    return result