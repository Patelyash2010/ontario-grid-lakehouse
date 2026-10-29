"""What the model is allowed to know about the gold layer.

Only these tables can be queried (the guardrails enforce the same list), and
this text is the model's only description of them, so it is kept precise.
"""
from __future__ import annotations

TABLES: dict[str, dict] = {
    "fact_grid_hourly": {
        "grain": "one row per hour (the main table for demand, generation, price and weather)",
        "columns": {
            "date_key": "int yyyymmdd, joins dim_date.date_key",
            "delivery_date": "date, IESO delivery date (Eastern Standard Time)",
            "hour_ending": "int 1-24, IESO hour-ending in EST (HE 1 = 00:00-01:00)",
            "interval_start_utc": "timestamp, start of the hour in UTC",
            "ontario_demand_mw": "Ontario demand, MW (hourly average, so MW = MWh for one hour)",
            "market_demand_mw": "Ontario demand plus exports, MW",
            "total_generation_mwh": "total Ontario generation, MWh",
            "nuclear_mwh, hydro_mwh, gas_mwh, wind_mwh, solar_mwh, biofuel_mwh, other_mwh": "generation by fuel, MWh",
            "low_carbon_share": "0-1, share of generation that is not gas or other",
            "emissions_t": "estimated CO2 tonnes (gas x 0.42 t/MWh, approximate)",
            "carbon_intensity_t_per_mwh": "emissions_t / total_generation_mwh (x1000 = g/kWh)",
            "price_cad_per_mwh": "day-ahead Ontario zonal price, CAD/MWh; NULL where no price exists (see price history start below)",
            "market_era": "PRE_MRP or POST_MRP (market redesign on 2025-05-01)",
            "avg_temp_c": "average of Toronto, Ottawa and London temperature, C (actuals only)",
            "toronto_temp_c": "Toronto temperature, C",
            "avg_humidity_pct": "average relative humidity, %",
            "is_on_peak": "boolean, IESO on-peak: business day and HE 8-23",
            "has_generation, has_price, has_weather": "booleans, whether that source exists for the hour",
        },
    },
    "fact_generation_hourly": {
        "grain": "one row per hour x fuel",
        "columns": {
            "date_key": "int yyyymmdd",
            "delivery_date": "date (EST)",
            "hour_ending": "int 1-24",
            "interval_start_utc": "timestamp UTC",
            "fuel_type": "NUCLEAR, HYDRO, WIND, SOLAR, BIOFUEL, GAS, OTHER (joins dim_fuel)",
            "output_mwh": "generation, MWh",
            "emissions_t": "estimated CO2 tonnes",
            "share_of_hour": "0-1, this fuel's share of that hour's generation",
        },
    },
    "mart_daily_summary": {
        "grain": "one row per delivery_date (use this for daily peaks and daily questions)",
        "columns": {
            "date_key": "int yyyymmdd",
            "delivery_date": "date",
            "hours_reported": "int, 24 for a complete day",
            "is_complete_day": "boolean; filter on it for peaks and daily totals (today is partial)",
            "peak_demand_mw": "highest hourly Ontario demand that day, MW",
            "peak_hour_ending": "hour-ending of the peak",
            "avg_demand_mw": "average hourly demand, MW",
            "ontario_energy_mwh": "daily Ontario energy, MWh",
            "generation_mwh, nuclear_mwh, hydro_mwh, gas_mwh, wind_mwh, solar_mwh": "daily generation, MWh",
            "gas_share, low_carbon_share": "0-1 shares of daily generation",
            "emissions_t": "daily CO2 tonnes",
            "carbon_intensity_t_per_mwh": "daily carbon intensity",
            "avg_temp_c, min_temp_c, max_temp_c": "daily temperatures, C (3-city average)",
            "heating_degree_days, cooling_degree_days": "base 18 C",
            "avg_price_cad_per_mwh, on_peak_price_cad_per_mwh, off_peak_price_cad_per_mwh": "daily prices, CAD/MWh (NULL before the price history start)",
        },
    },
    "mart_temp_vs_demand": {
        "grain": "one row per 2 C temperature band x day type, all years combined",
        "columns": {
            "temp_band_c": "int lower bound of the band, e.g. 20 = [20, 22)",
            "day_type": "'Business day' or 'Weekend / holiday'",
            "hours": "number of hours in the band",
            "avg_demand_mw, p90_demand_mw": "demand, MW",
            "avg_gas_share": "0-1",
        },
    },
    "dim_date": {
        "grain": "one row per calendar date, 2025-01-01 to 2026-12-31",
        "columns": {
            "date_key": "int yyyymmdd",
            "date": "date (join fact.delivery_date = dim_date.date)",
            "year, quarter, month, day_of_month": "ints",
            "month_name": "January..December",
            "year_month": "string yyyy-MM",
            "day_of_week": "int 1 = Sunday .. 7 = Saturday",
            "day_name": "Monday..Sunday",
            "is_weekend, is_holiday, is_business_day": "booleans (Ontario statutory holidays)",
            "holiday_name": "string or NULL",
            "season": "Winter (Dec-Feb), Spring, Summer, Fall",
        },
    },
    "dim_hour": {
        "grain": "one row per hour_ending 1-24",
        "columns": {
            "hour_ending": "int 1-24",
            "hour_label": "e.g. '17:00-18:00'",
            "day_part": "Night (HE1-6), Morning (7-12), Afternoon (13-18), Evening (19-24)",
            "is_peak_window": "boolean, HE 8-23",
        },
    },
    "dim_fuel": {
        "grain": "one row per fuel_type",
        "columns": {
            "fuel_type": "NUCLEAR, HYDRO, WIND, SOLAR, BIOFUEL, GAS, OTHER",
            "fuel_name": "display name, e.g. 'Natural gas'",
            "fuel_group": "'Low carbon', 'Fossil' or 'Other'",
            "is_renewable": "boolean",
            "co2_t_per_mwh": "approximate emission factor",
        },
    },
    "mart_pipeline_health": {
        "grain": "one row per silver load run (data engineering monitoring)",
        "columns": {
            "run_id": "string",
            "report": "Demand, GenOutputbyFuelHourly, DAHourlyOntarioZonalPrice or weather",
            "layer": "'silver'",
            "status": "SUCCEEDED, NO_NEW_FILES or FAILED",
            "run_date": "date",
            "started_at, finished_at": "timestamps UTC",
            "duration_s": "seconds",
            "files_processed, rows_parsed, rows_after_dedup, rows_quarantined, rows_inserted, rows_updated": "counts",
            "message": "error text for failed runs",
        },
    },
}

ALLOWED_TABLES = frozenset(TABLES)

# Question -> SQL pairs that teach the model the conventions (Databricks SQL).
EXAMPLES = [
    (
        "What was the highest demand day in 2026 and how hot was it?",
        "SELECT delivery_date, peak_demand_mw, peak_hour_ending, max_temp_c\n"
        "FROM gold.mart_daily_summary\n"
        "WHERE is_complete_day AND year(delivery_date) = 2026\n"
        "ORDER BY peak_demand_mw DESC\nLIMIT 1",
    ),
    (
        "Average price by hour of day in September 2026",
        "SELECT hour_ending, ROUND(AVG(price_cad_per_mwh), 2) AS avg_price_cad_per_mwh\n"
        "FROM gold.fact_grid_hourly\n"
        "WHERE delivery_date BETWEEN DATE'2026-09-01' AND DATE'2026-09-30' AND has_price\n"
        "GROUP BY hour_ending\nORDER BY hour_ending",
    ),
    (
        "Share of generation by fuel last month",
        "SELECT f.fuel_name, ROUND(SUM(g.output_mwh) / SUM(SUM(g.output_mwh)) OVER (), 4) AS share\n"
        "FROM gold.fact_generation_hourly g\n"
        "JOIN gold.dim_fuel f ON f.fuel_type = g.fuel_type\n"
        "WHERE g.delivery_date >= add_months(date_trunc('MONTH', DATE'{today}'), -1)\n"
        "  AND g.delivery_date <  date_trunc('MONTH', DATE'{today}')\n"
        "GROUP BY f.fuel_name\nORDER BY share DESC",
    ),
]


def schema_context() -> str:
    """Compact text description of the allowed tables, for the system prompt."""
    lines = []
    for name, spec in TABLES.items():
        lines.append(f"gold.{name}  -- {spec['grain']}")
        for col, desc in spec["columns"].items():
            lines.append(f"  {col}: {desc}")
        lines.append("")
    return "\n".join(lines)
