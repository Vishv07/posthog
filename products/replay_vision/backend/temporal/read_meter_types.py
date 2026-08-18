"""Types and pure throttle math for the scanner read metering — a dependency leaf, so the
sweep activity can use the throttle without importing the workflow module."""

import datetime as dt

from pydantic import BaseModel

from products.replay_vision.backend.temporal.constants import (
    DEEP_SPEND_WINDOW_DAYS,
    DEEP_SWEEP_MAX_FACTOR,
    SWEEP_READ_BUDGET_BYTES_24H,
    SWEEP_THROTTLE_MAX_FACTOR,
)


class MeterScannerReadsInputs(BaseModel, frozen=True):
    pass


class MeterScannerReadsResult(BaseModel, frozen=True):
    scanners_updated: int = 0


def sweep_spend_bytes_24h(read_bytes_by_hour: dict[str, int] | None, now: dt.datetime) -> int:
    """Trailing-24h read spend from the metered hour buckets; tolerates stale, unpruned entries."""
    return sum(_parsed_buckets(read_bytes_by_hour, now - dt.timedelta(hours=24)).values())


def deep_spend_bytes_per_day(deep_by_hour: dict[str, int] | None, now: dt.datetime) -> int:
    """The deep pass's daily read rate, averaged over a window wider than its longest interval.

    A trailing 24h sum cannot price a pass that runs less than daily: the pass writes one bucket, that
    bucket ages out, spend reads zero, and the interval collapses back to the floor. Averaging over
    `DEEP_SPEND_WINDOW_DAYS` keeps a stretched pass priced by what it actually costs per day.
    """
    window = dt.timedelta(days=DEEP_SPEND_WINDOW_DAYS)
    return sum(_parsed_buckets(deep_by_hour, now - window).values()) // DEEP_SPEND_WINDOW_DAYS


def _parsed_buckets(by_hour: dict[str, int] | None, cutoff: dt.datetime) -> dict[dt.datetime, int]:
    """Hour buckets at or past the cutoff, keyed by parsed hour; unparseable keys are skipped.

    Tolerant because this runs inside the sweep: raising here would stop the scanner, not just
    mis-report its spend.
    """
    if not by_hour:
        return {}
    parsed: dict[dt.datetime, int] = {}
    for hour_iso, read_bytes in by_hour.items():
        try:
            hour = dt.datetime.fromisoformat(hour_iso)
            if hour.tzinfo is None:
                hour = hour.replace(tzinfo=dt.UTC)
            if hour >= cutoff:
                parsed[hour] = int(read_bytes)
        except (TypeError, ValueError):
            continue
    return parsed


def sweep_throttle_factor(spend_bytes: int, override: int | None) -> int:
    """Cadence-stretch multiplier: 1 means sweep normally, N means sweep every N schedule intervals."""
    return _throttle_factor(spend_bytes, override, SWEEP_THROTTLE_MAX_FACTOR)


def deep_sweep_throttle_factor(spend_bytes_per_day: int) -> int:
    """Interval-stretch multiplier for the deep pass: N means it runs every N x DEEP_SWEEP_INTERVAL.

    Takes no override: the one on the scanner steers the frequent sweep, and a single knob that moved
    both would undo the independence this split exists for.
    """
    return _throttle_factor(spend_bytes_per_day, None, DEEP_SWEEP_MAX_FACTOR)


def _throttle_factor(spend_bytes: int, override: int | None, max_factor: int) -> int:
    if override is not None:
        return max(1, min(override, max_factor))
    # Rounds up, so any spend past the budget stretches. Rounding to nearest would leave a scanner
    # between one and one and a half budgets running at full rate despite being over.
    factor = -(-spend_bytes // SWEEP_READ_BUDGET_BYTES_24H)
    return max(1, min(factor, max_factor))
