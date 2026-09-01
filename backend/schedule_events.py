"""
schedule_events.py
===================
Step vi — Timestamped Schedule Event Log
-----------------------------------------
Adds a lightweight, dependency-free event log that captures WHEN each
changeover and each finished package happened on each line, in simulation
seconds. This is the missing link between the KPI aggregates already
produced by process_logic_sequential_v2.line_kpi_summary() (utilisation,
buffer fill, mean cycle time — no timeline) and a real Gantt chart.

Design goals
------------
1.  Zero changes to entities_resources_v4.py. Events are collected in a
    plain list[ScheduleEvent] that the caller owns and passes into
    run_line() / changeover() / part_lifecycle() by reference. SimPy is
    single-threaded/cooperative, so a shared list across the 3 concurrent
    run_line() processes is safe with no locking.
2.  Package-level granularity, not per-part. A "package" is
    cfg.packaging.package_size finished parts of the same product on the
    same line — matching how the dashboard's Gantt chart already reasons
    about blocks ("throughput rate per package/rectangle").
3.  Idle time is NOT actively emitted (nothing in the current process
    logic models starvation/idle waiting explicitly). Instead,
    build_gantt_payload() derives idle segments as the gaps between
    consecutive job/setup events on the same line. This keeps the
    simulation core simple and puts all "what does the chart show"
    logic in one place.
    v6: when a config_loader_v6.LineShiftCalendar + epoch are supplied,
    each derived gap is further split into "idle" (line on-shift, gate
    genuinely free) vs "off_shift" (line not scheduled to run at all
    per the workbook's "Shifts" sheet) sub-segments — the Gantt-chart
    counterpart to mixed_runner.production_status_at()'s per-instant
    "off_shift" state, so a frontend Gantt can show scheduled downtime
    distinctly from genuine idle time. Optional and purely additive:
    omit shift_calendar/epoch (or load a workbook with no Shifts sheet)
    and every gap is still reported as a single plain "idle" segment,
    exactly as before.

Usage
-----
    from schedule_events import ScheduleEvent, build_gantt_payload

    event_log: list[ScheduleEvent] = []
    env.process(run_line(sim_env, line_id=1, orders=..., n_workers=1,
                          event_log=event_log))
    ...
    env.run()

    payload = build_gantt_payload(event_log, sim_time_s=env.now,
                                   line_names=["HTL3", "HTL5", "HTL6"])
    # v6, optional: split idle gaps into idle vs off_shift sub-segments
    #   payload = build_gantt_payload(event_log, sim_time_s=env.now,
    #                                  line_names=["HTL3", "HTL5", "HTL6"],
    #                                  shift_calendar=cfg.shift_calendar,
    #                                  epoch=push_ctx.epoch)
    # payload is JSON-serialisable and matches the shape the
    # ProductionPlannerDashboard Gantt component expects.
"""

from __future__ import annotations

import datetime as _dt
import json
from dataclasses import dataclass, asdict, field
from typing import Literal, Optional

EventType = Literal["setup", "job"]
SimClass = Literal["pull", "push"]


# ---------------------------------------------------------------------------
# ScheduleEvent — one rectangle's worth of data
# ---------------------------------------------------------------------------

@dataclass
class ScheduleEvent:
    """
    One timestamped segment on one production line.

    Attributes
    ----------
    line_name       : "HTL3" | "HTL5" | "HTL6"
    line_id         : 1-based line index (matches run_line's line_id)
    event_type      : "setup" (changeover) or "job" (package produced)
    start_s         : simulation start time, seconds
    end_s           : simulation end time, seconds
    sachnummer      : product identifier (job events; "from->to" not stored
                       here — see from_sachnummer/to_sachnummer for setup)
    kunde           : customer / market designation (job events only)
    product_class   : "Stift" | "DRS" | "Stab_Lochfilter" | etc. (job events)
    package_size    : nominal pieces per package (job events only)
    units_in_segment: actual pieces produced in this segment
                       (== package_size, except a possible partial final
                       package at the end of an order)
    n_workers       : changeover crew size (setup events only)
    from_sachnummer : outgoing product (setup events only)
    to_sachnummer   : incoming product (setup events only)
    note            : free-text (e.g. setup-time lookup source, or a
                       warning when TTNr data was missing)
    sim_class       : "pull" (class-1 Kanban) or "push" (class-2), or None
                       for callers that don't know/care about the
                       pull-vs-push distinction (e.g. the single-mode
                       kanban-only or push-only runners). Populated
                       AFTER construction by whoever owns that knowledge
                       (mixed_runner.py, for the mixed Option-3 run) —
                       see that module for why it's tagged post-hoc
                       rather than threaded through the constructor.
    """
    line_name:        str
    line_id:           int
    event_type:        EventType
    start_s:            float
    end_s:              float
    sachnummer:         Optional[str] = None
    kunde:              Optional[str] = None
    product_class:      Optional[str] = None
    package_size:       Optional[int] = None
    units_in_segment:   Optional[int] = None
    n_workers:          Optional[int] = None
    from_sachnummer:    Optional[str] = None
    to_sachnummer:      Optional[str] = None
    note:               Optional[str] = None
    sim_class:          Optional[SimClass] = None

    @property
    def duration_s(self) -> float:
        return round(self.end_s - self.start_s, 3)

    @property
    def duration_h(self) -> float:
        return self.duration_s / 3600.0

    @property
    def throughput_pcs_per_h(self) -> Optional[float]:
        """Pieces per hour for a 'job' segment (None for setup/zero-duration)."""
        if self.event_type != "job" or self.units_in_segment is None:
            return None
        if self.duration_h <= 0:
            return None
        return round(self.units_in_segment / self.duration_h, 1)

    def __repr__(self) -> str:
        tag = f" <{self.sim_class}>" if self.sim_class else ""
        if self.event_type == "job":
            return (
                f"ScheduleEvent(job{tag} {self.sachnummer!r} on {self.line_name} "
                f"[{self.start_s:.1f}, {self.end_s:.1f}]s  "
                f"{self.units_in_segment}/{self.package_size} pcs  "
                f"rate={self.throughput_pcs_per_h} pcs/h)"
            )
        return (
            f"ScheduleEvent(setup{tag} {self.from_sachnummer!r}->{self.to_sachnummer!r} "
            f"on {self.line_name} [{self.start_s:.1f}, {self.end_s:.1f}]s "
            f"{self.n_workers}MA)"
        )


# ---------------------------------------------------------------------------
# PackageTracker — accumulates finished parts of one order into packages
# ---------------------------------------------------------------------------

@dataclass
class PackageTracker:
    """
    Stateful helper used by run_line() to turn a stream of per-part
    completions into package-level ScheduleEvents.

    One instance covers exactly one OrderRecord's run on one line.
    Call `on_finish(env_now, status)` after every part in the order
    finishes (passed or scrapped); call `flush(env_now)` once after the
    whole order's batch has cleared to emit any partial trailing package.
    """
    line_name:      str
    line_id:        int
    sachnummer:     str
    kunde:          str
    product_class:  str
    package_size:   int
    event_log:      list[ScheduleEvent]
    count_in_pkg:   int = field(default=0, init=False)
    pkg_start_s:    float = field(default=0.0, init=False)
    _started:       bool = field(default=False, init=False)

    def on_finish(self, env_now: float, status: str) -> None:
        """Register one finished part (any status) and emit a job event
        every time a full package's worth has completed."""
        if not self._started:
            self.pkg_start_s = env_now
            self._started = True

        # Only "passed" output counts toward a shippable package; scrapped
        # parts still consumed line time but are not packaged. Adjust here
        # if the packaging rule should include scrap as gross throughput.
        if status != "passed":
            return

        self.count_in_pkg += 1
        if self.count_in_pkg >= self.package_size:
            self._emit(env_now)
            self.count_in_pkg = 0
            self.pkg_start_s = env_now

    def flush(self, env_now: float) -> None:
        """Emit a partial package for whatever didn't reach package_size."""
        if self.count_in_pkg > 0:
            self._emit(env_now, units=self.count_in_pkg)
            self.count_in_pkg = 0

    def _emit(self, env_now: float, units: Optional[int] = None) -> None:
        units = units if units is not None else self.package_size
        start = self.pkg_start_s
        end = max(env_now, start)  # guard against zero-length rounding
        self.event_log.append(
            ScheduleEvent(
                line_name=self.line_name,
                line_id=self.line_id,
                event_type="job",
                start_s=round(start, 3),
                end_s=round(end, 3),
                sachnummer=self.sachnummer,
                kunde=self.kunde,
                product_class=self.product_class,
                package_size=self.package_size,
                units_in_segment=units,
            )
        )


# ---------------------------------------------------------------------------
# Shift-aware idle splitting (v6) — turns one raw idle gap into alternating
# "idle" (on-shift, genuinely nothing running) / "off_shift" (not scheduled
# to run at all) sub-segments.
#
# schedule_events.py has no visibility into ShiftDefinition's actual
# start/end times (that lives in config_loader_v6.py) — only the
# shift_calendar.is_line_on(line_name, dt) -> bool query it exposes (per
# design goal 1, this file stays dependency-free of the config/SimPy
# layers). So a gap is located by coarse-scanning at _SHIFT_SCAN_STEP_S
# resolution and bisecting each detected on/off crossing down to
# _SHIFT_BOUNDARY_TOL_S, rather than computing boundaries analytically.
# This is an approximation, not an exact replay of the workbook's shift
# table — but a safe one as long as no configured shift is shorter than
# _SHIFT_SCAN_STEP_S, which no realistic Morning/Afternoon/Night shift
# calendar would be.
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

    shift_calendar / epoch (v6, both optional, additive): when
    shift_calendar is a config_loader_v6.LineShiftCalendar with at least
    one configured shift (bool(shift_calendar.shifts)) and epoch (the
    real-world datetime sim time 0 corresponds to — mixed_runner.
    PushSchedulerContext.epoch) is also given, every synthesised gap is
    split into "idle" (line on-shift, gate genuinely free) vs
    "off_shift" (line not scheduled to run at all at that time) segments
    — see the module docstring and _split_gap_by_shift() above. Omitting
    either argument, or passing a shift_calendar with no configured
    shifts, falls back to the pre-v6 behaviour: every gap is reported as
    a single plain "idle" segment, exactly as before — no change for
    older callers (kanban-only/push-only runners) or shift-less
    workbooks.
    """
    divisor = {"h": 3600.0, "min": 60.0, "s": 1.0}[time_unit]

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


def dump_gantt_json(
    event_log: list[ScheduleEvent],
    sim_time_s: float,
    line_names: list[str],
    path: str,
    time_unit: Literal["h", "min", "s"] = "h",
) -> None:
    """Convenience: write build_gantt_payload(...) straight to a JSON file
    that the frontend (or a quick `fetch()`) can consume as-is."""
    payload = build_gantt_payload(event_log, sim_time_s, line_names, time_unit)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)


def event_log_to_dicts(event_log: list[ScheduleEvent]) -> list[dict]:
    """Flat JSON-serialisable dump of the raw event log (audit trail /
    debugging — includes both setup and job events, un-gapped)."""
    return [asdict(e) for e in event_log]
