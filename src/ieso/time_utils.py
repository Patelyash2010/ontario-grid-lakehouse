"""Time handling for IESO reports.

Two facts drive this module:

1. IESO uses HOUR-ENDING labels 1..24. Hour 1 covers 00:00-01:00,
   hour 24 covers 23:00-24:00 of the same delivery date.

2. IESO reports in Eastern STANDARD Time all year (no daylight saving).
   Evidence: the Hourly Demand report has exactly 24 rows on
   2026-03-08 (the spring-forward day) and on the fall-back day, instead
   of 23 and 25. So the offset is a fixed UTC-5, and every UTC hour is
   unique - no gaps or duplicated hours to handle.

We store interval_start_utc as the primary time key, plus the original
date/hour columns for traceability back to the source file.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

EST = timezone(timedelta(hours=-5), name="EST")


def hour_ending_to_utc(delivery_date: date, hour_ending: int) -> tuple[datetime, datetime]:
    """Convert an IESO (date, hour-ending) pair to a UTC [start, end) interval.

    >>> hour_ending_to_utc(date(2026, 1, 1), 1)[0].isoformat()
    '2026-01-01T05:00:00+00:00'
    """
    if not 1 <= hour_ending <= 24:
        raise ValueError(f"hour_ending must be 1..24, got {hour_ending}")
    start_local = datetime(delivery_date.year, delivery_date.month, delivery_date.day, tzinfo=EST) + timedelta(
        hours=hour_ending - 1
    )
    start_utc = start_local.astimezone(timezone.utc)
    return start_utc, start_utc + timedelta(hours=1)


def utc_to_hour_ending(start_utc: datetime) -> tuple[date, int]:
    """Inverse of hour_ending_to_utc: UTC interval start -> IESO (date, hour-ending).

    Used for sources that report in UTC (weather) so every silver table shares
    the same delivery_date / hour_ending columns and joins cleanly.

    >>> utc_to_hour_ending(datetime(2026, 1, 1, 5, tzinfo=timezone.utc))
    (datetime.date(2026, 1, 1), 1)
    """
    if start_utc.tzinfo is None:
        raise ValueError("start_utc must be timezone-aware")
    local = start_utc.astimezone(EST)
    return local.date(), local.hour + 1
