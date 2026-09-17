"""
domain/epoch.py
================
Simulation start time (t=0) and horizon sizing. The epoch is the fixed
constant domain.constants.SIM_START (edit it there); this module reads
it and works out how many days to run for. Called by sim.resources.build
(build_environment / build_kanban_environment) when wiring up a run.
"""

from __future__ import annotations

import datetime as _dt
from typing import Optional

from domain.constants import SIM_START
from domain.timeparse import parse_datetime, row_datetime_string


def compute_epoch() -> _dt.datetime:
    """Return the simulation's t=0 datetime (domain.constants.SIM_START)."""
    return SIM_START


def estimate_horizon_s(
    cfg,
    day_length_s: float = 24 * 3600.0,
    drain_days: int = 1,
    fallback_horizon_s: float = 24 * 3600.0,
) -> float:
    """
    Derive a safe env.run(until=...) horizon for a (possibly multi-day)
    CustomerDemandKanban sheet, sized from the latest parseable timestamp
    in cfg.kanban_withdrawals relative to SIM_START.

    Returns fallback_horizon_s unchanged if no row has an absolute
    timestamp (legacy, single-day sheet).
    """
    latest_dt: Optional[_dt.datetime] = None
    for evt in getattr(cfg, "kanban_withdrawals", []) or []:
        dt = parse_datetime(row_datetime_string(evt))
        if dt is not None and (latest_dt is None or dt > latest_dt):
            latest_dt = dt

    if latest_dt is None:
        return fallback_horizon_s

    latest_t = (latest_dt - SIM_START).total_seconds()
    last_day_index = int(latest_t // day_length_s)
    return (last_day_index + 1 + drain_days) * day_length_s
