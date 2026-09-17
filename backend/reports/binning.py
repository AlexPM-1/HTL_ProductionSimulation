"""
reports/binning.py
===================
Shared time-unit divisor table and bin-edge helpers used across the
reports/ package, so every report that buckets a run into time bins
(reports/all/gantt.py, reports/all/supermarket_series.py,
reports/pull/card_flow.py, reports/pull/shortfall.py,
reports/movement/supermarket.py, ...) reads from one canonical source
instead of re-declaring its own copy.

reports/ modules may only import domain + telemetry; this module imports
neither — it is pure arithmetic over whatever snapshot objects/attribute
names the caller passes in.
"""

from __future__ import annotations

from typing import Optional

TIME_UNIT_DIVISORS: dict[str, float] = {"h": 3600.0, "min": 60.0, "s": 1.0}

# Private aliases kept for call sites that still import the underscore-
# prefixed names directly (e.g. reports/all/supermarket_series.py,
# reports/movement/trace.py).
_SM_DIVISORS = TIME_UNIT_DIVISORS
_TIME_DIVISORS = TIME_UNIT_DIVISORS
_DIVISORS = TIME_UNIT_DIVISORS


def validate_time_unit(time_unit: str) -> float:
    """Returns the divisor, or raises ValueError with the same message
    every endpoint used to hand-roll as an HTTPException detail string.
    Callers in api/ wrap this in HTTPException(400, ...) themselves so this
    module stays free of the fastapi import."""
    if time_unit not in TIME_UNIT_DIVISORS:
        raise ValueError(f"time_unit must be one of: {', '.join(TIME_UNIT_DIVISORS)}")
    return TIME_UNIT_DIVISORS[time_unit]


def default_n_bins(sim_time_s: float) -> int:
    """~15-real-world-minute bins by default. Every time-bucketed report
    builder in this package (reports/all/gantt.py,
    reports/all/supermarket_series.py, reports/pull/card_flow.py,
    reports/pull/shortfall.py) falls back to this same formula
    (sim_time_s / 600.0, rounded, floor 1) when n_bins isn't given."""
    return max(1, round(sim_time_s / 600.0))


def state_at(snaps: list, edge_s: float, fields: tuple[str, ...]) -> tuple:
    """
    Generic 'last reading at or before edge_s' lookup, shared by every
    per-bin/per-frame snapshot replay in this package: Main-runner
    SupermarketSnapshot (n_available, pcs_partial) in
    reports/all/supermarket_series.py, Exotic ExoticSlotSnapshot
    (occupant, n_cards), and reports/movement/supermarket.py's restmenge
    lookup. `snaps` must already be sorted by `.t` ascending.
    """
    current = tuple(0 for _ in fields)
    if fields and fields[0] == "occupant":
        current = (None,) + tuple(0 for _ in fields[1:])
    for s in snaps:
        if s.t > edge_s:
            break
        current = tuple(getattr(s, f) for f in fields)
    return current


# Private alias used by call sites that import _sm_state_at directly
# (reports/all/supermarket_series.py, reports/movement/supermarket.py).
_sm_state_at = state_at
