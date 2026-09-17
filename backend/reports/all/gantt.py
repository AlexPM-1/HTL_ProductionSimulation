"""
reports/all/gantt.py
======================
Gantt payload builder for the "All Production" tab's per-line schedule
chart. ScheduleEvent and PackageTracker live in telemetry/records.py and
telemetry/packaging.py; this module only imports ScheduleEvent for type
hints and stays free of any config/SimPy dependency — the shift-splitting
helpers below reach the shift calendar only through its
is_line_on(line_name, dt) query surface, never domain.config directly.
"""

from __future__ import annotations

import datetime as _dt
from typing import Literal, Optional

from telemetry.records import ScheduleEvent
from reports.binning import TIME_UNIT_DIVISORS

# ---------------------------------------------------------------------------
# Shift-aware idle splitting — turns one raw idle gap into alternating
# "idle" (on-shift, genuinely nothing running) / "off_shift" (not scheduled
# to run at all) sub-segments.
#
# This module has no visibility into ShiftDefinition's actual start/end
# times (that lives in domain/config) — only the
# shift_calendar.is_line_on(line_name, dt) -> bool query it exposes. So a
# gap is located by coarse-scanning at _SHIFT_SCAN_STEP_S resolution and
# bisecting each detected on/off crossing down to _SHIFT_BOUNDARY_TOL_S,
# rather than computing boundaries analytically. This is an approximation,
# not an exact replay of the workbook's shift table — but a safe one as
# long as no configured shift is shorter than _SHIFT_SCAN_STEP_S, which no
# realistic Morning/Afternoon/Night shift calendar would be.
# ---------------------------------------------------------------------------

_SHIFT_SCAN_STEP_S = 900.0   # 15 min — coarse resolution when hunting for a
                              # shift transition inside an idle gap
_SHIFT_BOUNDARY_TOL_S = 1.0  # bisection stops once a found transition is
                              # pinned down to within this many sim seconds


def _is_line_on(shift_calendar, epoch: _dt.datetime, line_name: str, t_s: float) -> bool:
    return bool(shift_calendar.is_line_on(line_name, epoch + _dt.timedelta(seconds=t_s)))


def _bisect_shift_boundary(
    shift_calendar, epoch: _dt.datetime, line_name: str,
    lo_s: float, hi_s: float, lo_is_on: bool,
) -> float:
    """lo_s is known to be `lo_is_on`; hi_s is known to be the opposite.
    Narrow down to the transition instant, to within _SHIFT_BOUNDARY_TOL_S."""
    while hi_s - lo_s > _SHIFT_BOUNDARY_TOL_S:
        mid = (lo_s + hi_s) / 2.0
        if _is_line_on(shift_calendar, epoch, line_name, mid) == lo_is_on:
            lo_s = mid
        else:
            hi_s = mid
    return hi_s


def _split_gap_by_shift(
    shift_calendar, epoch: _dt.datetime, line_name: str,
    start_s: float, end_s: float,
) -> list[tuple[float, float, bool]]:
    """Walk [start_s, end_s) and return a list of (seg_start_s, seg_end_s,
    is_on) tuples covering it, split wherever the line's on/off-shift
    state changes."""
    segments: list[tuple[float, float, bool]] = []
    seg_start = start_s
    seg_is_on = _is_line_on(shift_calendar, epoch, line_name, seg_start)
    t = seg_start
    while t < end_s - 1e-6:
        nxt = min(t + _SHIFT_SCAN_STEP_S, end_s)
        nxt_is_on = _is_line_on(shift_calendar, epoch, line_name, nxt)
        if nxt_is_on == seg_is_on:
            t = nxt
            continue
        boundary = _bisect_shift_boundary(
            shift_calendar, epoch, line_name, t, nxt, seg_is_on
        )
        segments.append((seg_start, boundary, seg_is_on))
        seg_start = boundary
        seg_is_on = nxt_is_on
        t = nxt
    segments.append((seg_start, end_s, seg_is_on))
    return segments


# ---------------------------------------------------------------------------
# Gantt payload builder — fills idle gaps, converts to JSON-ready dict
# ---------------------------------------------------------------------------

def build_gantt_payload(
    event_log: list[ScheduleEvent],
    sim_time_s: float,
    line_names: list[str],
    time_unit: Literal["h", "min", "s"] = "h",
    shift_calendar=None,
    epoch: Optional[_dt.datetime] = None,
) -> dict:
    """
    Turn a flat event_log (mixed lines, mixed setup/job events) into the
    per-line segment structure the dashboard's GanttChart component
    consumes directly: {schedule: {line_name: [segments...]}, horizon}.

    Idle segments are synthesised for every gap between consecutive
    events on the same line (including a leading gap before the first
    event and a trailing gap up to sim_time_s).

    shift_calendar / epoch (both optional, additive): when shift_calendar
    has at least one configured shift (bool(shift_calendar.shifts)) and
    epoch (the real-world datetime sim time 0 corresponds to) is also
    given, every synthesised gap is split into "idle" (line on-shift,
    gate genuinely free) vs "off_shift" (line not scheduled to run at all
    at that time) segments — see _split_gap_by_shift() above. Omitting
    either argument, or passing a shift_calendar with no configured
    shifts, falls back to reporting every gap as a single plain "idle"
    segment.

    Every non-idle/off_shift segment's dict carries "crewId", a straight
    pass-through of the source ScheduleEvent's crew_id (set by
    sim.runner's crew wiring), same as simClass.
    """
    divisor = TIME_UNIT_DIVISORS[time_unit]

    has_shifts = (
        shift_calendar is not None
        and epoch is not None
        and bool(getattr(shift_calendar, "shifts", None))
    )

    schedule: dict[str, list[dict]] = {ln: [] for ln in line_names}
    class_duration: dict[str, dict[str, float]] = {
        ln: {"pull": 0.0, "push": 0.0, "unclassified": 0.0} for ln in line_names
    }

    def _emit_idle_gap(line: str, gap_start_s: float, gap_end_s: float) -> None:
        """Append one plain "idle" segment for [gap_start_s, gap_end_s)
        on `line` (pre-v6 behaviour), or — when shift data is available
        — one or more "idle"/"off_shift" segments split at shift
        boundaries within the gap."""
        if gap_end_s <= gap_start_s + 1e-6:
            return
        if not has_shifts:
            schedule[line].append({
                "type": "idle",
                "start": round(gap_start_s / divisor, 4),
                "duration": round((gap_end_s - gap_start_s) / divisor, 4),
            })
            return
        for seg_start_s, seg_end_s, is_on in _split_gap_by_shift(
            shift_calendar, epoch, line, gap_start_s, gap_end_s
        ):
            if seg_end_s <= seg_start_s + 1e-6:
                continue
            schedule[line].append({
                "type": "idle" if is_on else "off_shift",
                "start": round(seg_start_s / divisor, 4),
                "duration": round((seg_end_s - seg_start_s) / divisor, 4),
            })

    for line in line_names:
        line_events = sorted(
            (e for e in event_log if e.line_name == line),
            key=lambda e: e.start_s,
        )

        cursor = 0.0
        for e in line_events:
            _emit_idle_gap(line, cursor, e.start_s)
            bucket = e.sim_class if e.sim_class in ("pull", "push") else "unclassified"
            class_duration[line][bucket] += max(e.duration_s, 0.0)
            seg = {
                "type": e.event_type,
                "start": round(e.start_s / divisor, 4),
                "duration": round(max(e.duration_s, 0.0) / divisor, 4),
                "simClass": e.sim_class,
                "crewId": e.crew_id,
            }
            if e.event_type == "job":
                seg.update({
                    "sachnummer": e.sachnummer,
                    "kunde": e.kunde,
                    "productClass": e.product_class,
                    "packageSize": e.package_size,
                    "units": e.units_in_segment,
                    "rate": e.throughput_pcs_per_h,
                })
            else:
                seg.update({
                    "fromSku": e.from_sachnummer,
                    "toSku": e.to_sachnummer,
                    "workers": e.n_workers,
                    "note": e.note,
                })
            schedule[line].append(seg)
            cursor = max(cursor, e.end_s)

        _emit_idle_gap(line, cursor, sim_time_s)

    class_duration = {
        ln: {k: round(v / divisor, 4) for k, v in buckets.items()}
        for ln, buckets in class_duration.items()
    }

    return {
        "horizon": round(sim_time_s / divisor, 4),
        "time_unit": time_unit,
        "lines": line_names,
        "schedule": schedule,
        "class_duration": class_duration,
    }
