"""
api/runs.py
=============
RunStore: a single-slot "last completed run wins" cache, holding the kenv/
cfg produced by the most recent POST /api/simulate_mixed (api/legacy.py).
Every /api/mixed/* endpoint (api/legacy.py, api/routes_movement.py,
api/routes_policy.py) reads the active run through run_store.require()
or run_store.kenv.

day_start_hour/day_length_s are cached alongside kenv/cfg because they're
per-request inputs to POST /api/simulate_mixed (not fixed constants), and
day_window_s() below needs the values actually used for the cached run.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from fastapi import HTTPException

from domain.config import SimConfig


@dataclass
class RunStore:
    kenv: object = None
    cfg: Optional[SimConfig] = None
    day_start_hour: Optional[int] = None
    day_length_s: Optional[float] = None

    def set(self, kenv, cfg: SimConfig, day_start_hour: int, day_length_s: float) -> None:
        self.kenv = kenv
        self.cfg = cfg
        self.day_start_hour = day_start_hour
        self.day_length_s = day_length_s

    def require(self) -> tuple:
        """Every /api/mixed/* endpoint needs a prior POST /api/simulate_mixed call."""
        if self.kenv is None or self.cfg is None:
            raise HTTPException(
                status_code=409,
                detail="No mixed simulation has been run yet. Call POST /api/simulate_mixed first.",
            )
        return self.kenv, self.cfg

    def day_window_s(self, day_index: Optional[int], hour: Optional[int]) -> Optional[tuple[float, float]]:
        """
        Raw sim-clock (start_s, end_s) window for the CACHED mixed run, in
        the single continuous clock sim.runner.run_mixed() uses (no
        per-day offset stitching — see
        reports.movement.trace.build_part_trace_payload's day_window_s /
        time_offset_s docstring for a runner that does need that).

        day_index: 0-based day number from sim start (t=0). None -> no
        windowing (whole run).
        hour: optional 0-23, narrows the window to that single hour within
        day_index. Required by the frontend today whenever day_index is
        given, to keep single-hour part lists from being enormous.

        NOTE: assumes day length is constant across the cached run (drain
        days included in the day count, not a different length).
        """
        if day_index is None:
            return None
        from domain.constants import DAY_LENGTH_S

        day_len = self.day_length_s or DAY_LENGTH_S
        start = day_index * day_len
        if hour is not None:
            start += hour * 3600.0
            end = start + 3600.0
        else:
            end = day_index * day_len + day_len
        return (start, end)


# Single module-level instance shared by every route module that needs
# the active run (api/legacy.py, api/routes_movement.py, api/routes_policy.py).
run_store = RunStore()
