"""A small synthetic gold layer (same tables and columns as Databricks) for tests.

Values are made up but shaped like the real data, so queries behave the same.
"""
from __future__ import annotations

import math
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pandas as pd

FUELS = [  # fuel_type, fuel_name, fuel_group, is_renewable, co2, sort, typical MWh
    ("NUCLEAR", "Nuclear", "Low carbon", False, 0.0, 1, 9000),
    ("HYDRO", "Hydro", "Low carbon", True, 0.0, 2, 4200),
    ("WIND", "Wind", "Low carbon", True, 0.0, 3, 1300),
    ("SOLAR", "Solar", "Low carbon", True, 0.0, 4, 80),
    ("BIOFUEL", "Biofuel", "Low carbon", True, 0.0, 5, 25),
    ("GAS", "Natural gas", "Fossil", False, 0.42, 6, 3500),
    ("OTHER", "Other", "Other", False, 0.0, 7, 60),
]
EST = timezone(timedelta(hours=-5))


def make_gold(out_dir: Path, start: date = date(2026, 7, 1), end: date = date(2026, 7, 31),
              price_start: date = date(2026, 7, 10)) -> Path:
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    days = pd.date_range("2025-01-01", "2026-12-31", freq="D")
    holidays = {date(2026, 7, 1): "Canada Day", date(2026, 1, 1): "New Year's Day"}
    dim_date = pd.DataFrame({
        "date_key": days.strftime("%Y%m%d").astype(int),
        "date": days.date,
        "year": days.year, "quarter": days.quarter, "month": days.month,
        "month_name": days.strftime("%B"), "year_month": days.strftime("%Y-%m"),
        "day_of_month": days.day,
        "day_of_week": (days.dayofweek + 1) % 7 + 1,       # 1 = Sunday
        "day_name": days.strftime("%A"),
    })
    dim_date["is_weekend"] = dim_date["day_of_week"].isin([1, 7])
    dim_date["is_holiday"] = dim_date["date"].isin(holidays)
    dim_date["holiday_name"] = dim_date["date"].map(holidays)
    dim_date["is_business_day"] = ~dim_date["is_weekend"] & ~dim_date["is_holiday"]
    dim_date["season"] = dim_date["month"].map(
        lambda m: "Winter" if m in (12, 1, 2) else "Spring" if m <= 5 else "Summer" if m <= 8 else "Fall")

    he = list(range(1, 25))
    dim_hour = pd.DataFrame({
        "hour_ending": he,
        "hour_label": [f"{h - 1:02d}:00-{h:02d}:00" for h in he],
        "day_part": ["Night" if h <= 6 else "Morning" if h <= 12 else "Afternoon" if h <= 18 else "Evening" for h in he],
        "is_peak_window": [8 <= h <= 23 for h in he],
    })
    dim_fuel = pd.DataFrame([f[:6] for f in FUELS], columns=[
        "fuel_type", "fuel_name", "fuel_group", "is_renewable", "co2_t_per_mwh", "sort_order"])

    business = dict(zip(dim_date["date"], dim_date["is_business_day"]))
    grid, gen = [], []
    d = start
    while d <= end:
        for h in he:
            t = 24 + 6 * math.sin((d.toordinal() % 7) / 7 * 6.28) + 4 * math.sin((h - 9) / 24 * 6.28)
            demand = round(15000 + 260 * max(t - 18, 0) + 120 * math.sin((h - 6) / 24 * 6.28) * 10, 0)
            gas = round(max(demand - 14500, 800) * 0.9, 0)
            rows = {f[0]: f[6] for f in FUELS}
            rows["GAS"] = gas
            total = sum(rows.values())
            start_utc = datetime(d.year, d.month, d.day, tzinfo=EST) + timedelta(hours=h - 1)
            start_utc = start_utc.astimezone(timezone.utc).replace(tzinfo=None)
            on_peak = business[d] and 8 <= h <= 23
            price = round(20 + (demand - 14000) / 120 + (15 if on_peak else 0), 2) if d >= price_start else None
            grid.append({
                "date_key": int(d.strftime("%Y%m%d")), "delivery_date": d, "hour_ending": h,
                "interval_start_utc": start_utc, "ontario_demand_mw": demand,
                "market_demand_mw": demand + 1500, "total_generation_mwh": total,
                "nuclear_mwh": rows["NUCLEAR"], "hydro_mwh": rows["HYDRO"], "gas_mwh": gas,
                "wind_mwh": rows["WIND"], "solar_mwh": rows["SOLAR"], "biofuel_mwh": rows["BIOFUEL"],
                "other_mwh": rows["OTHER"], "low_carbon_share": (total - gas - rows["OTHER"]) / total,
                "emissions_t": gas * 0.42, "carbon_intensity_t_per_mwh": gas * 0.42 / total,
                "price_cad_per_mwh": price, "market_era": "POST_MRP",
                "avg_temp_c": round(t, 1), "toronto_temp_c": round(t + 1, 1), "avg_humidity_pct": 60.0,
                "is_on_peak": on_peak, "has_generation": True, "has_price": price is not None,
                "has_weather": True,
            })
            for f in FUELS:
                mwh = rows[f[0]]
                gen.append({
                    "date_key": int(d.strftime("%Y%m%d")), "delivery_date": d, "hour_ending": h,
                    "interval_start_utc": start_utc, "fuel_type": f[0], "output_mwh": mwh,
                    "emissions_t": mwh * f[4], "share_of_hour": mwh / total, "output_quality": 0,
                })
        d += timedelta(days=1)
    fact_grid = pd.DataFrame(grid)
    fact_gen = pd.DataFrame(gen)

    g = fact_grid.groupby("delivery_date")
    daily = pd.DataFrame({
        "date_key": g["date_key"].first(),
        "hours_reported": g.size(),
        "peak_demand_mw": g["ontario_demand_mw"].max(),
        "peak_hour_ending": fact_grid.loc[g["ontario_demand_mw"].idxmax(), ["delivery_date", "hour_ending"]]
            .set_index("delivery_date")["hour_ending"],
        "avg_demand_mw": g["ontario_demand_mw"].mean().round(0),
        "ontario_energy_mwh": g["ontario_demand_mw"].sum(),
        "generation_mwh": g["total_generation_mwh"].sum(),
        "nuclear_mwh": g["nuclear_mwh"].sum(), "hydro_mwh": g["hydro_mwh"].sum(),
        "gas_mwh": g["gas_mwh"].sum(), "wind_mwh": g["wind_mwh"].sum(), "solar_mwh": g["solar_mwh"].sum(),
        "emissions_t": g["emissions_t"].sum(),
        "avg_temp_c": g["avg_temp_c"].mean().round(1), "min_temp_c": g["avg_temp_c"].min(),
        "max_temp_c": g["avg_temp_c"].max(),
        "avg_price_cad_per_mwh": g["price_cad_per_mwh"].mean().round(2),
    }).reset_index()
    daily["is_complete_day"] = daily["hours_reported"] == 24
    daily["gas_share"] = daily["gas_mwh"] / daily["generation_mwh"]
    daily["low_carbon_share"] = 1 - daily["gas_share"]
    daily["carbon_intensity_t_per_mwh"] = daily["emissions_t"] / daily["generation_mwh"]
    daily["heating_degree_days"] = (18 - daily["avg_temp_c"]).clip(lower=0)
    daily["cooling_degree_days"] = (daily["avg_temp_c"] - 18).clip(lower=0)
    on = fact_grid[fact_grid["is_on_peak"]].groupby("delivery_date")["price_cad_per_mwh"].mean().round(2)
    off = fact_grid[~fact_grid["is_on_peak"]].groupby("delivery_date")["price_cad_per_mwh"].mean().round(2)
    daily["on_peak_price_cad_per_mwh"] = daily["delivery_date"].map(on)
    daily["off_peak_price_cad_per_mwh"] = daily["delivery_date"].map(off)

    fg = fact_grid.merge(dim_date[["date", "is_business_day"]], left_on="delivery_date", right_on="date")
    fg["temp_band_c"] = (fg["avg_temp_c"] // 2 * 2).astype(int)
    fg["day_type"] = fg["is_business_day"].map({True: "Business day", False: "Weekend / holiday"})
    temp = fg.groupby(["temp_band_c", "day_type"]).agg(
        hours=("ontario_demand_mw", "size"), avg_demand_mw=("ontario_demand_mw", "mean"),
        p90_demand_mw=("ontario_demand_mw", lambda s: s.quantile(0.9)),
        avg_gas_share=("gas_mwh", "mean")).reset_index()

    health = pd.DataFrame([{
        "run_id": f"r{i}", "report": rep, "layer": "silver", "status": st,
        "run_date": date(2026, 7, 28), "started_at": datetime(2026, 7, 28, 14, 30),
        "finished_at": datetime(2026, 7, 28, 14, 31), "duration_s": 30, "files_processed": 1,
        "rows_parsed": 100, "rows_after_dedup": 90, "rows_quarantined": q, "rows_inserted": 24,
        "rows_updated": 66, "message": "" if st != "FAILED" else "parse error: header changed",
    } for i, (rep, st, q) in enumerate([
        ("Demand", "SUCCEEDED", 0), ("GenOutputbyFuelHourly", "SUCCEEDED", 2),
        ("DAHourlyOntarioZonalPrice", "FAILED", 0), ("weather", "NO_NEW_FILES", 0)])])

    for name, df in {
        "dim_date": dim_date, "dim_hour": dim_hour, "dim_fuel": dim_fuel,
        "fact_grid_hourly": fact_grid, "fact_generation_hourly": fact_gen,
        "mart_daily_summary": daily, "mart_temp_vs_demand": temp, "mart_pipeline_health": health,
    }.items():
        df.to_parquet(out_dir / f"{name}.parquet", index=False)
    return out_dir


if __name__ == "__main__":  # python tests/gold_fixture.py data/gold_demo
    import sys

    target = Path(sys.argv[1] if len(sys.argv) > 1 else "data/gold_demo")
    make_gold(target, start=date(2026, 5, 1), end=date(2026, 10, 27), price_start=date(2026, 7, 30))
    print(f"synthetic gold written to {target}")
