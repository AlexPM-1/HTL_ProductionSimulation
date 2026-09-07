"""
mixed_runner.py
================
Option 3 — mixed push/pull ("class-1 Kanban" + "class-2 push") runner.

This file reuses:

  - config_loader_v5.load_config()               (unchanged)
  - entities_resources_v4.build_kanban_environment()  (unchanged — it
    already builds a KanbanSimEnvironment on top of the SAME
    stations/buffers/inventories/chutes as build_environment(), so push
    order execution and kanban card execution share one line's
    resources without touching entities_resources_v4.py at all — EXCEPT
    KanbanChuteResource, which was extended in place to hold both
    classes' work; see that class's docstring in entities_resources_v4.py)
  - process_logic_sequential_v3.run_one_order()   (unchanged — runs ONE
    OrderRecord to completion; called here with a *chunked* quantity so
    push work yields the line every PUSH_CHUNK_SIZE pieces instead of
    monopolising it for a whole order)

Architecture — crew-based drain (v10)
--------------------------------------
Movement 1 (chute FILLING — unchanged by this version): pull cards
arrive via kanban_process_logic_v2's own withdrawal/collection-box path
calling KanbanChuteResource.push_batch(); push chunks arrive via
push_dispatch_process() calling push_chunk()/push_rush_entry(). Chute
ordering (priority ranking, the frozen zone, rush placement) is entirely
this movement's responsibility — see KanbanChuteResource's own
docstring. Nothing below ever reorders or reaches past the front of a
line's queue; it only ever looks at, and consumes from, the front.

Movement 2 (chute DEPLETION — this version's change): a fixed pool of
`n_crews` crew_process() instances (run_mixed(n_crews=...)) is the ONLY
consumer of every line's chute. Each crew, in a loop:
  1. Rule 1/2 (_select_line_for_crew): among lines that are on-shift
     (ShiftCalendar / PushSchedulerContext.is_line_on) and not currently
     held by another crew, picks the one with the most pending cards
     (KanbanChuteResource.total_pending_cards, pull+push combined). A
     crew already holding a line only moves to a DIFFERENT line if it is
     STRICTLY more loaded — equal load, or nothing more loaded, means
     stay. Two crews becoming idle at the same simulated instant are
     arbitrated by plain SimPy same-tick process ordering (see that
     function's docstring) — no extra bookkeeping needed.
  2. Holds that line's LinePriorityGate (now a plain per-line mutex —
     see below) and runs ONE "turn": the whole push order if a push
     entry is at the chute's front (_run_push_turn), or up to a 4-card
     same-product_number pull batch if a pull entry is at the front
     (_run_pull_turn) — carrying over an incomplete batch's count across
     visits via PendingPullBatch/ctx.pending_batches (Rule 1.a) whenever
     the chute's front doesn't currently offer more of that product;
     nothing is ever searched for out of order.
  3. Re-runs Rule 1/2 after every turn to decide whether to keep working
     this line or release it and move to a more-loaded one.
An idle crew (no on-shift line has any pending work) blocks on one
shared `ctx.activity_signal`, woken by ANY line's chute gaining new work
(see _install_crew_chute_hooks) or any crew finishing a turn.

  - LinePriorityGate: now a plain simpy.Resource(capacity=1) per line —
    a single mutex, not a priority queue. The old priority=0/1 (class-1
    vs class-2) split existed only to arbitrate two INDEPENDENT
    per-class drain loops fighting over one line; with crews as the
    sole consumer of a line at any moment, there is nothing left to
    arbitrate by priority. `current_rec` (for changeover()) and
    `last_activity_t`/`touch()` are unchanged and still shared across
    whichever class a crew is currently running on that line.
  - KanbanChuteResource (entities_resources_v5.py, one per line): the
    shared, frozen-zone-aware admission queue movement 1 fills and
    crew_process() drains, via peek_next_of_class()/pop_next_of_class()
    directly — chute.pop_next() itself is neutralised (always returns
    None, see _install_crew_chute_hooks) since kanban_process_logic_v2's
    own production_trigger_process is still spawned (for
    withdrawal_process/collection_box_emptying_process's sake) but must
    never be allowed to drain a card itself any more.
  - push_dispatch_process(): one per CustomerDemand row — UNCHANGED
    (movement 1, push side). Sleeps until the row enters its
    push_visibility_days window, resolves PRODUCT_MATRIX-priority
    candidate lines, then either finds a free compatible line (normal
    path) or — within rush_threshold_h of the row's due date —
    force-assigns the least-busy compatible line via push_rush_entry().
  - _compute_shared_epoch(): anchors t=0 for BOTH sheets onto one shared
    clock, since the two sheets are read independently and must not each
    invent their own epoch.

Event-log class tagging (pull vs push) — wired
-------------------------------------------------
kenv.event_log is ONE list shared by every crew, so a ScheduleEvent by
itself carries no signal of which class produced it. Both
_run_one_pull_card() and _run_push_turn() tag events post-hoc:
  - snapshot len(event_log) right before the gated run_one_kanban_batch/
    run_one_order call (a crew holding a line's gate is the ONLY thing
    that can touch that line_id's stations at that moment — capacity=1);
  - after the call returns, walk event_log[snapshot:] and set
    .sim_class on entries whose line_id matches ours.
Filtering by line_id (not just by index) is required because the SAME
window can contain events from OTHER lines' crews that interleaved their
own appends while we were yielded — SimPy is cooperative, so that
routinely happens.

Event-log crew tagging (v10) — wired the same way
--------------------------------------------------
Same snapshot-and-walk, same call sites, run right alongside the
sim_class tagging above: each ScheduleEvent's .crew_id gets set to the
calling crew_process()'s own crew_id (a plain parameter both
_run_one_pull_card() and _run_push_turn() already receive) for every
entry in event_log[snapshot:] whose line_id matches ours and whose
crew_id is still None. This is what lets schedule_events.
build_gantt_payload()'s "crewId" on each Gantt job segment actually
carry a value — see ScheduleEvent.crew_id's own docstring in
schedule_events.py for the contract this fulfils, and
GateActivityEntry.crew_id below for the (separately-populated, always
already-set) crew_id that gate_activity_log / kpi_by_crew / the
/api/mixed/crew_activity endpoint read instead.

STATUS
------
Everything described above is live, including the exotic Supermarket
deposit + overflow-flag tracking (see ExoticSupermarketTracker's
module-level caveat: it's currently a self-contained bookkeeping
structure, not yet wired into entities_resources_v5.py's real pull-side
Supermarket resources).

ASSUMPTIONS FLAGGED FOR VERIFICATION (ported from a reference copy of
kanban_process_logic.py's production_trigger_process/run_one_kanban_batch
shown alongside this refactor, not from reading the real
kanban_process_logic_v2.py module):
  - `_make_kanban_order_record(rt, line_name, product_type, quantity)`
    and `_return_card_to_supermarket(rt, kenv, sm, card, t, line_name,
    line_id, verbose)` exist in kanban_process_logic_v2 with these exact
    signatures — _run_one_pull_card() (below) calls them directly, since
    production_trigger_process (which used to own this orchestration) is
    now permanently neutralised.
  - Consecutive push ChuteEntry objects of the same product_type,
    dispatched back-to-back by push_dispatch_process for one
    CustomerDemand row, are a reasonable stand-in for "one push order"
    when deciding how much a push turn takes (_run_push_turn) — this has
    not been checked against a scenario where two DIFFERENT push orders
    of the same product happen to sit adjacent in one line's chute.

Shifts / line on-off (v6) — wired
----------------------------------
Config: SimConfig.shift_calendar (config_loader_v6.ShiftCalendar),
parsed from the workbook's "Shifts" sheet — see that module's
_parse_shifts_sheet(). Query surface: SimEnvironment.is_line_on() /
next_line_on_transition() (entities_resources_v5.py), wrapping
ShiftCalendar with the "no Shifts sheet at all -> every line always on"
fallback baked in, so nothing downstream needs to special-case an older
workbook. This module folds in the epoch conversion (sim-time seconds ->
wall-clock) on top of those via PushSchedulerContext.is_line_on() /
seconds_until_on() — the only shift-aware entry points crew_process()
uses. Card withdrawal from the supermarket into the chute happens
upstream, inside kanban_process_logic_v2.py, and is untouched by this
change — it is never gated by shift state.

Two rules, two integration points:
  - "Off lines cannot be used": push_dispatch_process's line search
    (rule 4) and its 12h-rush override (rule 6) both exclude off-shift
    lines from candidacy — including rush, which is a confirmed decision,
    not an oversight: an urgent order still just keeps retrying hourly
    (falling back to rule 5's wait) if every compatible line happens to
    be off when it goes urgent, rather than forcing an off line to run.
    _select_line_for_crew() applies the identical exclusion on the
    depletion side — an off-shift line is never a candidate for Rule 1/2,
    regardless of how loaded its chute is.
  - "Cards can leave the supermarket, but production starts when the
    line is on again": crew_process() waits out an off-shift period
    (looping on is_line_on(), re-checking after every wake — including
    the "no known next on-time" edge case) BEFORE running a turn on a
    line it's holding, so a pull batch or push chunk simply sitting on
    the chute is never, by itself, enough to start production on an off
    line. A turn already in progress when a shift ends is NOT preempted
    — the calendar is authoritative only for the decision to START the
    next unit of work, exactly as before.
"""

from __future__ import annotations

import datetime as _dt
import random
import re
import time as _wall_clock
from dataclasses import dataclass, field, replace as _dc_replace
from typing import Optional

import simpy

from config_loader_v6 import load_config, SimConfig, ShiftCalendar, OEEBinConfig
from entities_resources_v5 import build_kanban_environment, KanbanSimEnvironment, KanbanChuteResource
from dispatch_entities import OrderRecord, UnassignedOrder, MatrixPriorityStrategy
from product_line_matrix_v3 import lookup as _plm_lookup, ProductLineInfo
from process_logic_sequential_v3 import run_one_order, print_kpi
from kanban_process_logic_v2 import estimate_horizon_s, start_kanban_simulation
from schedule_events import ScheduleEvent


# ---------------------------------------------------------------------------
# Constants — mirrors kanban_runner.py's module-level knobs so both halves
# of the mixed sim agree on what a "day" is.
# ---------------------------------------------------------------------------
DAY_START_HOUR: int = 6
DAY_LENGTH_S: float = 24 * 3600.0
DRAIN_DAYS: int = 1
SIM_HORIZON_S: float = 24 * 3600.0

# How often _run_one_pull_card re-polls is_line_on() for a line
# that has no further on-transition in the configured shift calendar
# horizon at all (see that function's off-line correction) — only ever
# used in that edge case; a normal off period is woken exactly at its
# known on-transition instead, without polling.
_OFF_LINE_REPOLL_S: float = 3600.0

# Push "unit" size: an OrderRecord larger than this is split into several
# chunks (each run via one run_one_order() call), so class-1 never waits
# longer than one chunk's worth of production. Tune per real card size —
# there's no single right answer without knowing typical push order size
# vs. typical kanban batch_size; starting conservative.
PUSH_CHUNK_SIZE: int = 200

N_WORKERS: int = 1
DEFAULT_N_WORKERS: int = N_WORKERS   # alias kept for backward compatibility
SEED: int = 42


# ---------------------------------------------------------------------------
# PushPolicyConfig — the editable push/frozen-zone knobs.
#
# STATUS: data model only (step 1 of the frozen-zone / dynamic-push-
# assignment restructuring — see this module's plan). Nothing below reads
# this yet; run_mixed() accepts and stores it so later steps (the frozen-
# zone queue, the new rolling push dispatcher) have a single object to
# consume, and so a future frontend/API layer has one clearly-documented,
# JSON-round-trippable place to edit these values rather than scattered
# module constants.
# ---------------------------------------------------------------------------

@dataclass
class PushPolicyConfig:
    """
    Editable policy knobs for class-2 (push / "exoten") production.
    Plain dataclass, deliberately dependency-free (no SimPy/Excel types)
    so it's trivial to construct from — or serialize to — a frontend
    request (see to_dict/from_dict).

    Card/hour equivalence
    ----------------------
    One "card" = one push chunk = PUSH_CHUNK_SIZE pieces (200, today) =
    `card_production_time_min` minutes of line time (30 min, today) — the
    SAME unit both class-1 Kanban cards and class-2 push chunks are
    measured in, which is what lets a frozen zone sized in cards mean the
    same thing to both classes on a shared line queue. See
    frozen_zone_hours for the derived, informational hour figure — the
    canonical/edited value is `frozen_zone_cards`, per the "it will be set
    according to cards" instruction; frozen_zone_hours is computed FROM
    it, not stored independently, so the two can never drift apart.

    Attributes
    ----------
    frozen_zone_cards       : how many cards'-worth of imminent work at
                              the front of a line's queue is locked
                              (unreorderable / unremovable) at any given
                              moment. Default 8 (≈ 4h at 30 min/card).
    card_production_time_min : minutes of line time one card/chunk
                              represents. Default 30 — used only to
                              derive frozen_zone_hours; NOT the same
                              thing as a station's own cycle time, which
                              can vary by product/line (this is a fixed
                              planning/queueing unit, not a physics
                              measurement).
    push_visibility_days    : how many days of CustomerDemand push rows
                              are considered "visible" for scheduling at
                              any given moment (a rolling window, not a
                              one-shot read of the whole sheet). Default 3.
    ideal_lead_time_h       : target: finish/deliver a push order this
                              many hours BEFORE its due date. Default 5.
    max_lead_time_h         : outer bound: never start pursuing an order
                              earlier than this many hours before its due
                              date (delivering much earlier than needed
                              just occupies exotic supermarket slots for
                              no benefit). Default 24. Must be >=
                              ideal_lead_time_h.
    rush_threshold_h        : once a push order is within this many hours
                              of its due date and still not
                              placed/finished, the normal "try
                              highest-priority compatible line, else next
                              compatible line, else wait and retry" rule
                              (below) is overridden: force-assign to
                              whichever compatible line is currently
                              least busy, and insert it right before that
                              line's frozen zone (i.e. as the very next
                              thing to run once whatever's currently
                              frozen finishes). Default 12.
    retry_interval_h        : when every compatible line is busy (and the
                              order is not yet inside rush_threshold_h),
                              how long to wait before re-trying placement.
                              Default 1.
    """
    frozen_zone_cards: int = 8
    card_production_time_min: float = 30.0
    push_visibility_days: int = 3
    ideal_lead_time_h: float = 5.0
    max_lead_time_h: float = 24.0
    rush_threshold_h: float = 12.0
    retry_interval_h: float = 1.0

    def __post_init__(self) -> None:
        if self.max_lead_time_h < self.ideal_lead_time_h:
            raise ValueError(
                f"max_lead_time_h ({self.max_lead_time_h}) must be >= "
                f"ideal_lead_time_h ({self.ideal_lead_time_h})"
            )
        if self.frozen_zone_cards < 0:
            raise ValueError("frozen_zone_cards must be >= 0")

    @property
    def frozen_zone_hours(self) -> float:
        """Informational: frozen_zone_cards expressed in hours of line time."""
        return self.frozen_zone_cards * self.card_production_time_min / 60.0

    def to_dict(self) -> dict:
        """JSON-serializable form, for a frontend settings panel."""
        return {
            "frozen_zone_cards": self.frozen_zone_cards,
            "card_production_time_min": self.card_production_time_min,
            "push_visibility_days": self.push_visibility_days,
            "ideal_lead_time_h": self.ideal_lead_time_h,
            "max_lead_time_h": self.max_lead_time_h,
            "rush_threshold_h": self.rush_threshold_h,
            "retry_interval_h": self.retry_interval_h,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "PushPolicyConfig":
        """
        Build from a (possibly partial) dict, e.g. a frontend PATCH body —
        unspecified keys keep this class's normal defaults.
        """
        known = {f: d[f] for f in cls.__dataclass_fields__ if f in d}
        return cls(**known)


# ---------------------------------------------------------------------------
# Shared epoch — anchors both CustomerDemandKanban (has time-of-day) and
# CustomerDemand (date only, implicitly DAY_START_HOUR:00) onto one t=0.
# ---------------------------------------------------------------------------
_DATE_RE = re.compile(r"^(\d{1,2})\.(\d{1,2})\.(\d{4})")


def _parse_date_only(s: str) -> Optional[_dt.date]:
    """Parse a bare 'DD.MM.YYYY' (or 'DD.MM.YYYY ...') cell into a date."""
    if not isinstance(s, str):
        return None
    m = _DATE_RE.match(s.strip())
    if not m:
        return None
    d, mo, y = (int(x) for x in m.groups())
    try:
        return _dt.date(y, mo, d)
    except ValueError:
        return None


def _parse_kanban_datetime(s: str) -> Optional[_dt.datetime]:
    """Parse a full 'DD.MM.YYYY HH:MM:SS' kanban timestamp cell."""
    if not isinstance(s, str):
        return None
    s = s.strip()
    for fmt in ("%d.%m.%Y %H:%M:%S", "%d.%m.%Y %H:%M"):
        try:
            return _dt.datetime.strptime(s, fmt)
        except ValueError:
            continue
    return None


def _row_due_datetime(row) -> Optional[_dt.datetime]:
    """
    Combine a CustomerDemand row's separate `period_label` (Date) and
    `time_slot` (Time) cells into one real datetime — the row's due date.
    Mirrors kanban_process_logic._row_datetime_string's reasoning exactly
    (config_loader_v5 keeps both sheets' Date/Time as two plain-string
    columns; this is the one place that gets bridged back into a real
    datetime for THIS module's own scheduling arithmetic).
    """
    period = (row.period_label or "").strip()
    time_s = (getattr(row, "time_slot", "") or "").strip()
    return _parse_kanban_datetime(f"{period} {time_s}".strip())


def _compute_shared_epoch(cfg: SimConfig, day_start_hour: int) -> _dt.datetime:
    """
    t=0 on the shared clock = DAY_START_HOUR:00 on the EARLIEST date found
    across BOTH sheets (kanban_withdrawals rows with a parseable timestamp,
    and demand rows with a parseable date). Falls back to today's date at
    day_start_hour if neither sheet has a parseable date (legacy/bare
    sheets) — matches kanban_process_logic's own fallback spirit for
    single-day sheets, just computed independently here since this module
    must agree with kanban's epoch without importing its private helpers.
    """
    candidates: list[_dt.date] = []

    for evt in getattr(cfg, "kanban_withdrawals", []) or []:
        # v5 layout: KanbanWithdrawalEvent keeps Date + Time as two
        # separate columns (evt.date / evt.time), not one combined
        # `time_slot` cell — see config_loader_v5.KanbanWithdrawalEvent.
        dt = _parse_kanban_datetime(f"{evt.date} {evt.time}".strip())
        if dt is not None:
            candidates.append(dt.date())

    for d in getattr(cfg, "demand", []) or []:
        dte = _parse_date_only(d.period_label)
        if dte is not None:
            candidates.append(dte)

    if not candidates:
        base = _dt.date.today()
    else:
        base = min(candidates)

    return _dt.datetime.combine(base, _dt.time(hour=day_start_hour))


# ---------------------------------------------------------------------------
# LinePriorityGate — the per-line mutex crew_process() holds while
# running a turn on that line. Kept as a plain simpy.Resource(capacity=1)
# rather than a priority resource (see module docstring's v10 note): the
# old priority=0/1 split existed only to arbitrate two independent
# per-class drain loops fighting over one line — with crews as the sole
# consumer, there is only ever one kind of requester per line at a time,
# so nothing is left to prioritize. Class name kept unchanged (rather
# than renamed to e.g. LineLock) to limit blast radius for any external
# code (e.g. a frontend/API layer) already reading kenv.gates.
# ---------------------------------------------------------------------------

@dataclass
class LinePriorityGate:
    """
    Per-line mutex: whoever holds it currently owns the right to run one
    "unit" of production through this line's stations. Backed by
    simpy.Resource(capacity=1).

    last_activity_t : env.now() of the last time a crew finished a unit
        here. Not read by any control-flow logic — kept purely as a
        bookkeeping timestamp for anything that wants "when did this
        line last do something".
    current_rec : OrderRecord last executed on this line, across BOTH
        classes — the changeover() argument. Read/written by whichever
        crew is currently holding this line, regardless of whether it's
        running a push or pull turn — see _run_push_turn/_run_one_pull_card.
    """
    resource: simpy.Resource
    last_activity_t: float = 0.0
    current_rec: Optional[OrderRecord] = None

    def touch(self, t: float) -> None:
        self.last_activity_t = t


@dataclass
class PendingPullBatch:
    """
    One line's "not yet reached 4 cards" pull-batch memory — Rule 1.a's
    carryover. Created fresh (taken=0) whenever a pull turn starts on a
    product_number with no open record for this line (or a different one
    than what's now at the chute's front — see _run_pull_turn);
    persisted here, keyed by line_id, whenever a turn stops short of 4
    because the chute's front no longer offers more of that
    product_number. Consulted (and completed, never restarted) by
    whichever crew next works this line and finds the same
    product_number back at the front, however much of the chute filled
    with OTHER products in between. Cleared the instant taken reaches 4.

    Deliberately NOT scoped per-crew: which crew resumes an open batch
    is irrelevant — the chute (movement 1) decides ordering, not the
    crew, so this state belongs to the line, not to whichever crew
    happens to be holding it at any given moment.
    """
    product_type: str
    taken: int = 0


@dataclass
class GateActivityEntry:
    """
    One completed hold of a line's LinePriorityGate — i.e. one contiguous
    interval during which *something* (a pull card or a push chunk,
    whichever a crew was running) actually occupied the line's stations,
    start to finish. Appended once per hold, AFTER it ends, by the two
    call sites that already acquire/release gate.resource around real
    production: _run_one_pull_card (pull) and _run_push_turn (push) —
    see each site's own comment for exactly where t_start/t_end are
    captured.

    This is the movement/production-status counterpart to
    ExoticSlotSnapshot / PushChuteLogEntry / kanban_process_logic.
    SupermarketSnapshot: a caller-owned, append-only, timestamped log a
    frontend can replay to reconstruct "what was this line doing at any
    past instant t_s", not just its live value (gate.current_rec /
    gate.last_activity_t only ever reflect *right now*, same limitation
    PushChuteTracker.snapshot() has vs. push_chute_entries_at(), or
    ExoticSupermarketTracker's slots vs. exotic_snapshot_log).

    Since gate.resource is a simpy.Resource(capacity=1) — one crew at a
    time per line, regardless of class — entries for the same line_id
    never overlap — trivial to replay with a single linear scan (see
    production_status_at()).

    possible_changeover: True if this entry's sachnummer differs from
    whatever gate.current_rec was immediately before this hold started
    (i.e. a changeover() call was very likely made somewhere inside this
    interval). This is a coarse, whole-interval flag, NOT a resolved
    sub-boundary — changeover() itself runs inside run_one_kanban_batch /
    run_one_order (process_logic_sequential_v3.py), which isn't visible
    to either gate wrapper, so this log cannot currently say how much of
    [t_start, t_end) was setup (Rüstzeit) vs. actual piece production.
    Splitting that out would need process_logic_sequential_v3.changeover()
    itself to report its own elapsed delay back to the caller (or log a
    timestamped "setup" sub-event the same way this class does) — flagged
    here rather than silently approximated as a 50/50 split or similar.
    A consumer that only needs "producing vs. idle" can ignore this flag
    entirely; a consumer that wants to visually distinguish Rüstzeit
    should treat possible_changeover=True as "producing, likely including
    some setup time at the start of this interval" rather than a precise
    Rüstzeit-only sub-window.
    """
    t_start: float
    t_end: float
    line_id: int
    sachnummer: str
    sim_class: str              # "pull" | "push"
    crew_id: Optional[int] = None   # which crew_process(crew_id=...) ran
                                     # this unit — see crew_process(),
                                     # _run_one_pull_card(), _run_push_turn().
                                     # None only for entries from a kenv
                                     # produced before this field existed
                                     # (shouldn't occur going forward).
    possible_changeover: bool = False
    quantity: Optional[int] = None


def production_status_at(
    log: list["GateActivityEntry"],
    line_id: int,
    t_s: float,
    *,
    shift_calendar: Optional[ShiftCalendar] = None,
    line_name: Optional[str] = None,
    epoch: Optional[_dt.datetime] = None,
) -> dict:
    """
    Replay `log` to resolve what `line_id`'s gate was doing at time t_s —
    the "last reading at or before this instant" convention every other
    time-indexed log in this module/api_server_mixed.py already uses
    (_sm_state_at, _exotic_products_at, push_chute_entries_at,
    movement_trace.card_state_at).

    Returns one of:
      {"state": "producing", "sachnummer": str, "sim_class": "pull"|"push",
       "crew_id": int, "since_t": float, "possible_changeover": bool}
      {"state": "idle", "sachnummer": None, "sim_class": None,
       "crew_id": None, "since_t": float | None, "possible_changeover": False}
      {"state": "off_shift", "sachnummer": None, "sim_class": None,
       "crew_id": None, "since_t": float | None, "possible_changeover": False}

    "crew_id" (v10): which crew_process(crew_id=...) instance was
    holding this line's gate during the active entry — None whenever
    state isn't "producing" (a crew only exists in this log's record
    while actually holding a gate; there is no "idle crew" entry to
    attribute an idle/off_shift gap to).

    "idle" covers both a genuine gap between two holds (chute momentarily
    drained on both sides, but the line IS on-shift) and any time before
    the first hold / after the last one recorded so far — "since_t" is
    the end of the previous hold (None if there hasn't been one yet). It
    does NOT distinguish idle from Rüstzeit — see GateActivityEntry's
    docstring for why that sub-resolution isn't available from this log
    yet.

    v6 shift on/off (optional, additive) — pass ALL THREE of
    `shift_calendar`, `line_name`, and `epoch` to further split "idle"
    into "idle" (on-shift, genuinely nothing to do) vs. "off_shift" (this
    line simply isn't scheduled to run at t_s at all) — a distinction a
    Gantt-style frontend almost certainly wants to render differently
    (e.g. grey "off" bands vs. a thin "idle" gap). Typical call site,
    given a `kenv`/`ctx` from run_mixed():
        line_name = next(l.line_name for l in kenv.lines if l.line_id == line_id)
        production_status_at(log, line_id, t_s,
                              shift_calendar=kenv.cfg.shift_calendar,
                              line_name=line_name, epoch=ctx.epoch)
    Omit any of the three (or pass a `shift_calendar` with no shifts
    configured at all — see ShiftCalendar's own "empty calendar = feature
    off" convention) to get the exact pre-v6 two-state behaviour; this
    check is purely additive on top of it.

    This distinction is only ever applied to what would OTHERWISE have
    been "idle" — it never overrides "producing". A batch/chunk that
    started while on-shift and is still finishing after its shift's
    window closes is still, correctly, reported as "producing": neither
    _run_one_pull_card nor _run_push_turn (via crew_process) preempt a
    unit already in progress when its shift ends (see each's own v6
    comment) — a line's gate hold, once granted, is authoritative over
    the calendar for the remainder of that hold.
    """
    active: Optional["GateActivityEntry"] = None
    last_end: Optional[float] = None
    for e in log:
        if e.line_id != line_id or e.t_start > t_s:
            continue
        if e.t_start <= t_s < e.t_end:
            active = e
        elif e.t_end <= t_s and (last_end is None or e.t_end > last_end):
            last_end = e.t_end

    if active is not None:
        return {
            "state": "producing",
            "sachnummer": active.sachnummer,
            "sim_class": active.sim_class,
            "crew_id": active.crew_id,
            "since_t": active.t_start,
            "possible_changeover": active.possible_changeover,
        }

    if (
        shift_calendar is not None and shift_calendar.shifts
        and line_name is not None and epoch is not None
    ):
        at = epoch + _dt.timedelta(seconds=t_s)
        if not shift_calendar.is_line_on(line_name, at):
            return {
                "state": "off_shift",
                "sachnummer": None,
                "sim_class": None,
                "crew_id": None,
                "since_t": last_end,
                "possible_changeover": False,
            }

    return {
        "state": "idle",
        "sachnummer": None,
        "sim_class": None,
        "crew_id": None,
        "since_t": last_end,
        "possible_changeover": False,
    }


# ---------------------------------------------------------------------------
# Class-1/class-2 side — v10: both now drained exclusively by
# crew_process() (see this module's docstring). The old
# _install_kanban_gate_hook() monkeypatch is gone: crew_process() calls
# kanban_process_logic_v2.run_one_kanban_batch directly (via
# _run_one_pull_card, below), so there is no longer a separate
# production_trigger_process call path to intercept.
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Class-2 (push) side — rolling per-order dispatcher, dynamic line
# assignment, shared frozen-zone-aware chute. UNCHANGED by the v10
# crew refactor (movement 1) — only its drain side (movement 2,
# formerly push_drain_process) moved into crew_process().
#
# Replaces the old day-batch model (build_dispatch_plan() once per
# calendar day, one pre-sliced FIFO chunk list per line): that model
# assumed CustomerDemand had a per-line split to hand a chunk straight to
# a known line (config_loader_v5's Option 1 / "simple_einsetzer"), which
# no longer exists — CustomerDemand.line_id is now a DOWNSTREAM line, not
# one of ours (see that module's docstring). Line choice is therefore now
# made HERE, per order, at runtime — see push_dispatch_process().
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Exotic (push) Supermarket deposit tracking.
#
# CAVEAT (please read before relying on this for cross-consumption): this
# is a SELF-CONTAINED tracker living entirely in mixed_runner.py, built
# from cfg.supermarkets' SupermarketSlotConfig rows (config_loader_v5) —
# it is NOT wired into entities_resources_v4.py's real Supermarket
# resources (the ones kanban_process_logic.select_supermarket_for_withdrawal
# / kenv.supermarket_for() actually read for PULL withdrawal). Main-runner
# (pull) stock genuinely affects kanban's withdrawal logic; exotic stock
# tracked here does NOT yet feed back into that — it only tracks exotic
# occupancy for THIS module's own overflow-flagging and (per the "used
# for the optimization of supermarkets" instruction) reporting purposes.
# If entities_resources_v4.py already exposes real exotic-slot resources
# with a similar shape, ExoticSupermarketTracker.deposit_chunk()'s body is
# the only thing that needs to change to call into those instead — the
# call site (_run_push_turn (via crew_process)) doesn't need to change.
#
# Deposit/withdraw symmetry: deposit_chunk() is called the moment a push
# chunk finishes production (_run_push_turn (via crew_process), step (e)); withdraw_chunk()
# is called once that same chunk's due_date is actually reached
# (_withdraw_push_chunk_process, spawned right after step (e)) — modeling
# "produced early, sits in the exotic supermarket, customer takes it at/near
# the due date" rather than growing occupancy without bound.
# ---------------------------------------------------------------------------

@dataclass
class SupermarketOverflowFlag:
    """
    One "this exotic slot pool was already full" event — logged, not
    raised as an exception (per the "no problem, raise a flag and
    continue" rule). Intended for later supermarket-sizing analysis, not
    for stopping this simulation.
    """
    t: float
    line: str
    sachnummer: str
    reason: str


@dataclass
class PushDeliveryRecord:
    """
    One completed push chunk's delivery outcome — the flat, log-friendly
    artifact "due date vs. delivered date" KPI graphs and frontend
    summaries are meant to read, rather than reaching into
    OrderRecord/chunk internals directly. Appended by _run_push_turn() (via crew_process)
    the same instant chunk.delivered_date is stamped on the underlying
    OrderRecord (both places always agree — this is a mirror, not a
    second source of truth).

    Attributes
    ----------
    sachnummer      : product
    assigned_line   : which line actually produced this chunk
    quantity        : pieces in this chunk (<= PUSH_CHUNK_SIZE)
    due_date        : from the originating CustomerDemand row
    delivered_date  : when this chunk's production finished
    delivery_delta_h : hours late (positive) or early (negative) —
                       mirrors OrderRecord.delivery_delta_h
    rush            : True if this chunk was force-placed under the
                      rush_threshold_h override (chute.push_rush_entry())
                      rather than normally ranked (chute.push_chunk())
    """
    sachnummer: str
    assigned_line: str
    quantity: int
    due_date: Optional[_dt.datetime]
    delivered_date: _dt.datetime
    delivery_delta_h: Optional[float]
    rush: bool


def push_delivery_summary(log: list[PushDeliveryRecord]) -> dict:
    """
    Aggregate on-time-delivery stats from a push_delivery_log — a small,
    ready-to-render dict for a frontend KPI summary card. Not a
    replacement for graphing the raw log (a frontend should still plot
    delivery_delta_h over time / by product for a distribution or trend
    view) — this is just the "one glance" numbers.
    """
    deltas = [r.delivery_delta_h for r in log if r.delivery_delta_h is not None]
    n_late = sum(1 for d in deltas if d > 0)
    return {
        "n_delivered": len(log),
        "n_rush": sum(1 for r in log if r.rush),
        "n_with_due_date": len(deltas),
        "n_late": n_late,
        "n_on_time_or_early": len(deltas) - n_late,
        "avg_delta_h": (sum(deltas) / len(deltas)) if deltas else None,
        "max_late_h": max(deltas) if deltas else None,
        "max_early_h": min(deltas) if deltas else None,
    }


@dataclass
class ExoticSlotState:
    """
    One physical "Exotic" Supermarket row's current occupancy.

    A row that hasn't fully emptied before a different product starts
    filling it legitimately holds more than one product's cards at
    once — deposit_chunk()'s over-capacity fallback (pile onto the
    least-full slot) used to only ever grow a single `occupant`/`n_cards`
    pair, silently folding a second product's cards into whichever
    product got there first (and never updating `occupant` again once
    set, since it only overwrote a None occupant). `occupants` replaces
    that with a real per-product breakdown: sachnummer -> n_cards, for
    every product simultaneously sitting in the row.
    """
    line: str
    row_number: int
    capacity_cards: int
    occupants: dict[str, int] = field(default_factory=dict)

    @property
    def total_cards(self) -> int:
        """Cards of ALL products combined currently sitting in this row —
        what `capacity_cards` is actually checked against."""
        return sum(self.occupants.values())

    @property
    def occupant(self) -> Optional[str]:
        """Back-compat single-value view: the (deterministically) first
        product occupying the row, or None if empty. Prefer `occupants`
        for the full multi-product picture — this exists only for old
        call sites/consumers that haven't been updated yet."""
        if not self.occupants:
            return None
        return min(self.occupants)

    @property
    def n_cards(self) -> int:
        """Back-compat alias for total_cards."""
        return self.total_cards


@dataclass
class ExoticSlotSnapshot:
    """
    One timestamped reading of a single physical Exotic slot's occupancy —
    the Exotic-side counterpart to kanban_process_logic.SupermarketSnapshot
    (which only ever covers Main-runner (pull) product groups, never
    Exotic rows — see build_kanban_environment's docstring). Appended by
    ExoticSupermarketTracker.deposit_chunk() (via
    _deposit_push_chunk_to_supermarket, below) every time a push chunk is
    deposited, so callers can reconstruct "occupancy of row N over time"
    the same way kanban's snapshot_log lets them reconstruct "stock of
    product X over time" — see kanban_events.build_line_units_timeseries's
    _state_at()-style "last snapshot at or before t" convention, which
    api_server_mixed.py's row-level time series reuses for these too.

    `occupants` is the full multi-product breakdown at t — one
    {"sachnummer", "n_cards"} entry per product simultaneously occupying
    the row (a row that hasn't fully emptied before a different product
    starts filling it legitimately holds more than one at once).
    `occupant`/`n_cards` are kept alongside it purely for back-compat
    with readers that haven't been updated to consume `occupants` yet —
    `occupant` is the (deterministically) first product, or None if
    empty, and `n_cards` is the SUM across every product. New code should
    read `occupants` instead.
    """
    t: float
    line: str
    row_number: int
    capacity_cards: int
    occupant: Optional[str]
    n_cards: int
    occupants: list[dict] = field(default_factory=list)


class ExoticSupermarketTracker:
    """
    Per-line pool of "Exotic" Supermarket slots (config_loader_v5.
    SupermarketSlotConfig rows where is_exotic is True), tracking
    push-chunk deposits/withdrawals for overflow-flagging and reporting
    purposes. See this section's module-level caveat above re: not yet
    wired into the real per-product pull-side Supermarket resources.

    Two parallel bits of state, kept in sync by deposit_chunk()/
    withdraw_chunk():
      - `slots` (ExoticSlotState, per PHYSICAL row) — capacity/occupancy
        accounting, unchanged in shape from before. This is what
        snapshot()/ExoticSlotSnapshot reports on.
      - `_ready_stores` (one simpy.Store per (line, sachnummer), created
        lazily) — a FIFO queue of "which physical row number is this
        particular deposited-but-not-yet-withdrawn card sitting in".
        deposit_chunk() pushes the row_number it placed a card in;
        withdraw_chunk() pops the OLDEST one for that (line, sachnummer)
        and decrements that same row — so chunks of the same product are
        withdrawn in deposit order (FIFO), and a withdrawal for a product
        that hasn't been produced yet naturally BLOCKS (via `yield
        store.get()`) until a matching deposit arrives, instead of
        failing — mirroring the pull-side SupermarketResource.store's own
        "withdrawal blocks until production deposits" resolution of
        stockout behavior (see that class's docstring).

    Sizing simplification: every push chunk is treated as exactly ONE
    card occupying one unit of a slot's capacity_cards — matching how
    the deposit rule is phrased ("200pcs cards/chunks will be
    deposited"). A final chunk smaller than PUSH_CHUNK_SIZE (when an
    order's quantity isn't an exact multiple of it) is still counted as
    one full card for capacity-accounting purposes. This is a
    deliberate, documented simplification — piece-level partial-card
    tracking (mirroring SupermarketSlotConfig.initial_pcs_partial) can
    be added later if this proves inaccurate enough to matter; it isn't
    needed for the overflow-flag / reporting purpose this tracker serves
    today.

    snapshot() below is intentionally NOT called from inside
    deposit_chunk()/withdraw_chunk() themselves — this class stays a
    plain state holder (matching its original scope), and the CALLER
    (_deposit_push_chunk_to_supermarket / _withdraw_push_chunk_process)
    is responsible for appending a timestamped ExoticSlotSnapshot into an
    externally-owned log, exactly the same "caller owns the log, this
    class only mutates state" split kanban_process_logic uses for
    SupermarketSnapshot.
    """

    def __init__(self, cfg: SimConfig):
        self.slots: dict[str, list[ExoticSlotState]] = {}
        for line_name, slot_cfgs in (getattr(cfg, "supermarkets", None) or {}).items():
            self.slots[line_name] = [
                ExoticSlotState(line=line_name, row_number=s.row_number, capacity_cards=s.capacity)
                for s in slot_cfgs if s.is_exotic
            ]
        # Lazily created on first deposit/withdraw of a given (line,
        # sachnummer) pair — see class docstring. Needs a live
        # simpy.Environment, which isn't available at __init__ time (this
        # tracker is built from cfg alone, before/independently of env in
        # some call sites), so deposit_chunk()/withdraw_chunk() take `env`
        # as an explicit argument instead of storing it here.
        self._ready_stores: dict[tuple[str, str], simpy.Store] = {}

    def _store_for(self, line_name: str, sachnummer: str, env: "simpy.Environment") -> simpy.Store:
        key = (line_name, sachnummer)
        store = self._ready_stores.get(key)
        if store is None:
            store = simpy.Store(env)
            self._ready_stores[key] = store
        return store

    def available_count(self, line_name: str, sachnummer: str) -> int:
        """How many cards of `sachnummer` are currently sitting ready for
        withdrawal in `line_name`'s exotic pool. Purely a diagnostics/
        logging helper (e.g. to tell "withdrawing immediately" apart from
        "waiting on production" in _withdraw_push_chunk_process) — not
        used for placement or withdrawal decisions themselves, those go
        through the FIFO store directly."""
        store = self._ready_stores.get((line_name, sachnummer))
        return len(store.items) if store is not None else 0

    def snapshot(self, line_name: str, t: float) -> list[ExoticSlotSnapshot]:
        """One ExoticSlotSnapshot per physical Exotic slot on `line_name`,
        as of `t` — used both for the initial t=0 snapshot (run_mixed())
        and after every deposit/withdrawal."""
        out = []
        for slot in self.slots.get(line_name, []):
            occupants_list = [
                {"sachnummer": sachnr, "n_cards": n}
                for sachnr, n in sorted(slot.occupants.items())
            ]
            out.append(ExoticSlotSnapshot(
                t=t, line=line_name, row_number=slot.row_number,
                capacity_cards=slot.capacity_cards,
                occupant=slot.occupant, n_cards=slot.n_cards,
                occupants=occupants_list,
            ))
        return out

    def deposit_chunk(
        self, line_name: str, sachnummer: str, env: "simpy.Environment",
    ) -> tuple[bool, Optional[str]]:
        """
        Deposit one push chunk (one card) of `sachnummer` into
        `line_name`'s exotic slot pool. ALWAYS succeeds — the deposit
        always happens, per "no problem, raise a flag and continue" — a
        full Supermarket is a planning signal here, not a hard stop.

        Placement preference (mirrors the pull-side "consolidate before
        spreading" instinct): (1) an existing slot already holding this
        sachnummer, with room; (2) an empty slot; (3) if every slot is
        either full or holds only different products with no room, pile
        onto an existing same-product slot anyway (soft over-capacity)
        or, if this product has no slot anywhere on this line, the
        least-full slot overall. Piling onto a slot that already holds a
        DIFFERENT product does NOT evict or relabel that product — the
        slot's `occupants` breakdown tracks both side by side, since a
        real physical row that hasn't fully emptied keeps whatever was
        already in it while a new product starts filling the rest.

        Whichever row_number the card physically lands in (or None, if
        this line has no exotic slots configured at all — see below) is
        pushed onto this (line, sachnummer)'s FIFO ready-store, making it
        available to the next withdraw_chunk() call for that pair — this
        is what a blocked/waiting withdrawal is actually woken up by.

        If this line has NO exotic slots configured at all, there is
        nowhere to physically place the card — but the chunk still very
        much exists and a customer will still come to withdraw it at its
        due_date, so it's still pushed onto the FIFO store (with
        row_number=None) purely so that withdrawal can succeed instead of
        blocking forever; there's simply no physical slot to decrement on
        the withdrawal side in that case.

        Returns (fit_within_capacity, reason). reason is None when it
        fit; a short string describing the overflow when it didn't (the
        caller is expected to log a SupermarketOverflowFlag with it).
        """
        slots = self.slots.get(line_name, [])
        if not slots:
            self._store_for(line_name, sachnummer, env).put(None)
            return False, "no exotic slots configured for this line"

        for slot in slots:
            if slot.occupants.get(sachnummer, 0) > 0 and slot.total_cards < slot.capacity_cards:
                slot.occupants[sachnummer] = slot.occupants.get(sachnummer, 0) + 1
                self._store_for(line_name, sachnummer, env).put(slot.row_number)
                return True, None

        for slot in slots:
            if not slot.occupants and slot.capacity_cards > 0:
                slot.occupants[sachnummer] = 1
                self._store_for(line_name, sachnummer, env).put(slot.row_number)
                return True, None

        same_product = [s for s in slots if sachnummer in s.occupants]
        target = same_product[0] if same_product else min(slots, key=lambda s: s.total_cards)
        target.occupants[sachnummer] = target.occupants.get(sachnummer, 0) + 1
        self._store_for(line_name, sachnummer, env).put(target.row_number)
        other_products = sorted(p for p in target.occupants if p != sachnummer)
        return False, (
            f"exotic supermarket full on {line_name} — piled onto slot "
            f"row {target.row_number} (now {target.total_cards}/{target.capacity_cards} cards"
            + (f", alongside {', '.join(other_products)}" if other_products else "")
            + ")"
        )

    def withdraw_chunk(self, line_name: str, sachnummer: str, env: "simpy.Environment"):
        """
        SimPy generator — the customer-side counterpart to deposit_chunk().
        This is what was missing before: deposit_chunk() alone only ever
        grows occupancy, so without a matching release the exotic pool
        fills up and never drains (see this class's original module-level
        caveat).

        `yield`s on the (line, sachnummer) FIFO ready-store's get(), which:
          - returns immediately, FIFO (oldest-deposited-first), if a card
            of this product is already sitting in the exotic pool on this
            line — matching chunks to customer pickups in deposit order,
            not "whichever slot happens to be fullest" as an earlier
            version of this method did;
          - otherwise BLOCKS until the next matching deposit_chunk() call
            for this exact (line, sachnummer) — i.e. if the customer's
            due_date arrives before production has actually finished that
            chunk, this simply waits for it to show up (most likely it's
            still in production and becomes available within the hour),
            exactly mirroring SupermarketResource.store's own pull-side
            stockout resolution rather than failing/warning and moving on.

        Once a card is claimed, decrements the SAME physical row it was
        deposited into (tracked via the row_number carried through the
        FIFO store) — not "whichever row has the most cards" — for the
        SPECIFIC product being withdrawn (`slot.occupants[sachnummer]`),
        leaving any other product already sitting in that row untouched.
        Clears that product's entry out of `occupants` entirely once it
        hits zero, so deposit_chunk()'s "prefer an empty slot" branch can
        tell a row is genuinely empty again (i.e. `occupants` is empty),
        not just that THIS product is gone from it.
        A row_number of None (only possible when this line has no exotic
        slots configured at all — see deposit_chunk()) has nothing to
        decrement.

        This is a generator: call it as `yield from
        tracker.withdraw_chunk(line_name, sachnummer, env)` from within a
        SimPy process.
        """
        store = self._store_for(line_name, sachnummer, env)
        row_number = yield store.get()

        if row_number is None:
            return
        for slot in self.slots.get(line_name, []):
            if slot.row_number == row_number:
                remaining = slot.occupants.get(sachnummer, 0) - 1
                if remaining > 0:
                    slot.occupants[sachnummer] = remaining
                else:
                    slot.occupants.pop(sachnummer, None)
                break


# ---------------------------------------------------------------------------
# OEE loss (CombinedProductionLoss) — daily per-line draw.
#
# config_loader_v6's "OEE" sheet (OEEBinConfig, SimConfig.oee_distribution,
# _parse_oee_sheet — see that module) supplies, per line, a table of
# [Bin_down, Bin_up) ranges each with a selection Probability, in sheet
# order. Once a sim-day, sample_combined_production_loss() below draws one
# CombinedProductionLoss value per line from that table; OEELossTracker
# holds "today's value per line" (plus which day it was drawn for, so a
# mid-day order read never itself triggers a re-draw); the daily process
# that calls draw_for_day() owns and appends to an externally-visible
# OEELossDrawEntry log — same "state-holder only mutates state, caller
# owns the log" split ExoticSupermarketTracker uses for ExoticSlotSnapshot,
# above.
# ---------------------------------------------------------------------------

def sample_combined_production_loss(
    line_name: str,
    bins: list[OEEBinConfig],
    random_percentile: float,
    random_value: float,
) -> float:
    """
    Draw one CombinedProductionLoss value for `line_name` from its OEE bin
    table, via a two-stage random process.

    Stage 1 — bin selection: walk `bins` (config_loader_v6.OEEBinConfig
    rows for this line, in sheet order) building a running cumulative
    Probability sum; the first bin whose cumulative sum, normalized
    against the line's ACTUAL total (not assumed to be exactly 1.0 — the
    sample workbook's own per-line totals land at 0.9999/1.0001, not
    exactly 1.0), exceeds `random_percentile` is the selected bin. A
    probability-0 bin contributes a zero-width slice of that cumulative
    range, so it can never be the one `random_percentile` lands in — no
    special-case skip needed to keep a 0-probability row unselectable.

    Stage 2 — value selection: once a bin [Bin_down, Bin_up) is selected,
    `random_value` places a uniformly distributed value inside it:
    `Bin_down + random_value * (Bin_up - Bin_down)`. Probabilities only
    ever decide WHICH bin; there is no additional distribution inside one.

    `random_percentile`/`random_value` are two INDEPENDENT draws in
    [0, 1), passed in rather than generated here — callers own the RNG
    (OEELossTracker.draw_for_day, below), which keeps this function
    itself trivially deterministic/testable given fixed inputs.
    """
    if not bins:
        raise ValueError(f"no OEE bins configured for line {line_name!r}")

    total = sum(b.probability for b in bins)
    if total <= 0:
        raise ValueError(
            f"line {line_name!r}'s OEE bin probabilities sum to {total}; "
            "cannot select a bin"
        )

    cumulative = 0.0
    for b in bins:
        cumulative += b.probability
        if random_percentile < cumulative / total:
            selected = b
            break
    else:
        # Float-rounding safety net only (e.g. random_percentile very near
        # 1.0 landing past the last edge by epsilon) — the loop above
        # always selects a bin in the normal case.
        selected = bins[-1]

    return selected.bin_down + random_value * (selected.bin_up - selected.bin_down)


@dataclass
class OEELossDrawEntry:
    """
    One day's CombinedProductionLoss draw for one line — the OEE-loss
    counterpart to daily_log/snapshot_log/exotic_snapshot_log/
    gate_activity_log: a full history of every draw, not just "latest
    value", so a run can be inspected/reported on after the fact.
    Appended by the caller of OEELossTracker.draw_for_day() (the daily OEE
    process below) — never by OEELossTracker itself; see that class's
    docstring for why.
    """
    t: float
    day_index: int
    line: str
    combined_production_loss: float


class OEELossTracker:
    """
    Per-line "today's CombinedProductionLoss" state-holder.

    Plain state-holder, matching ExoticSupermarketTracker's own scope: it
    only ever mutates its own state (draw_for_day()) or reports it
    (get_current()/is_stale()) — it never appends to a log itself. The
    caller (the daily OEE process, below) owns and appends to an
    externally-visible OEELossDrawEntry log every time it actually draws
    a new value, mirroring the "caller owns the log" split
    ExoticSupermarketTracker uses for ExoticSlotSnapshot.

    Two bits of state per line, kept in sync by draw_for_day():
      - `current_value[line]`  — today's CombinedProductionLoss.
      - `current_day[line]`    — which sim-day index that value was drawn
        for, so a mid-day order (via get_current()) never itself
        triggers a re-draw, and so the daily process (via is_stale())
        knows whether today's line still needs rolling over.

    A line absent from `oee_distribution` (no rows on the "OEE" sheet for
    it) simply never gets an entry in either dict — get_current() returns
    None for it, exactly like "no OEE sheet at all" — purely additive,
    same fallback spirit as the Kanban/Shifts sheets elsewhere in this
    module.
    """

    def __init__(self, oee_distribution: dict[str, list[OEEBinConfig]]):
        self.oee_distribution: dict[str, list[OEEBinConfig]] = oee_distribution
        self.current_value: dict[str, float] = {}
        self.current_day: dict[str, int] = {}

    def is_stale(self, line_name: str, day_index: int) -> bool:
        """
        True if `line_name` has never been drawn for, or its stored value
        was drawn for a different day than `day_index` — the daily
        process's own rollover check.
        """
        return self.current_day.get(line_name) != day_index

    def draw_for_day(
        self, line_name: str, day_index: int, rng: random.Random,
    ) -> Optional[float]:
        """
        Draw and store `line_name`'s CombinedProductionLoss for
        `day_index`, via two independent draws off `rng`. Returns the
        drawn value, or None (no-op — nothing stored) if this line has no
        configured OEE bins, so callers can skip logging cleanly instead
        of special-casing an unconfigured line themselves.
        """
        bins = self.oee_distribution.get(line_name)
        if not bins:
            return None
        value = sample_combined_production_loss(
            line_name, bins, rng.random(), rng.random(),
        )
        self.current_value[line_name] = value
        self.current_day[line_name] = day_index
        return value

    def get_current(self, line_name: str) -> Optional[float]:
        """
        Read-only: `line_name`'s already-drawn CombinedProductionLoss for
        whatever day it was last drawn for, or None if it's never been
        drawn (no OEE sheet, or this line has no rows on it). Never
        triggers a draw itself — see class docstring.
        """
        return self.current_value.get(line_name)


def oee_daily_process(
    kenv: "KanbanSimEnvironment",
    tracker: OEELossTracker,
    log: list[OEELossDrawEntry],
    day_length_s: float,
    rng: random.Random,
):
    """
    SimPy process — rolls every configured line's OEELossTracker entry
    over once per sim-day (every `day_length_s`, starting at t=0 so a
    value is already in place before any order can run), appending one
    OEELossDrawEntry per line actually drawn into the externally-owned
    `log` this call (see OEELossTracker's own docstring for why the
    tracker itself never touches this log).

    Lines with no OEE bins configured are silently skipped each day
    (draw_for_day() returns None for them) rather than logged as
    no-op entries.
    """
    env = kenv.env
    day_index = 0
    while True:
        for line in kenv.lines:
            value = tracker.draw_for_day(line.line_name, day_index, rng)
            if value is not None:
                log.append(OEELossDrawEntry(
                    t=env.now, day_index=day_index, line=line.line_name,
                    combined_production_loss=value,
                ))
        day_index += 1
        yield env.timeout(day_length_s)


@dataclass
class PushChuteLogEntry:
    """
    One PUSH-side Chute admission ("deposit") or removal ("drain") event
    — the push-side analogue of ExoticSlotSnapshot, timestamped so the
    Movement Simulation can reconstruct which push chunks were actually
    sitting in a line's shared Chute at any past instant, rather than
    only ever reporting the run's current live total (the old
    "push_pending" approximation's documented limitation).

    A "drain" event's entry_id names exactly which earlier "deposit"
    left the queue — always whichever entry PushChuteTracker's own live
    view had at the front at that moment (see PushChuteTracker.
    drain_one()), since _run_push_turn (via crew_process) always drains "the next push
    entry" for a line, never a specific one by id.
    """
    t: float
    line: str
    kind: str                          # "deposit" | "drain"
    entry_id: int
    sachnummer: Optional[str] = None   # populated on deposit only
    rush: bool = False                 # populated on deposit only


class PushChuteTracker:
    """
    Best-effort mirror of what's sitting in each line's shared Chute on
    the PUSH side only — the push-side analogue of
    ExoticSupermarketTracker, with the SAME caveat: this is a
    self-contained bookkeeping structure fed entirely from
    push_dispatch_process()'s chute.push_chunk()/chute.push_rush_entry()
    calls and _run_push_turn() (via crew_process)'s chute.pop_next_of_class("push")
    calls — the ONLY push-side admission/removal call sites, both in
    this module — not wired into KanbanChuteResource's own internal
    queue. It approximates real ordering with the same rule
    push_dispatch_process itself follows: normal deposits append to the
    back, rush deposits jump ahead of every non-rush entry but queue up
    FIFO relative to any rush entries already pending (mirroring
    chute.push_rush_entry() semantics). It does NOT know about
    frozen-zone-driven reordering interactions with the PULL side —
    that logic lives entirely inside KanbanChuteResource — so this is
    an approximation of physical queue position, same spirit as
    ExoticSupermarketTracker's own "not the real resource" caveat.

    Deposits/drains are pushed onto `.log` (a PushChuteLogEntry list) as
    they happen, so push_chute_entries_at() can replay "what was pending
    as of time t" for any past t — the historical counterpart to
    `.pending`/`.snapshot()`, which only ever reflect the current
    simulated instant.
    """

    def __init__(self):
        self._next_id = 1
        self.pending: dict[str, list[dict]] = {}   # line -> [{"entry_id","sachnummer","t_entered","rush"}], oldest-first
        self.log: list[PushChuteLogEntry] = []

    def deposit(self, line_name: str, sachnummer: str, t: float, rush: bool = False) -> int:
        """Record one push chunk entering `line_name`'s Chute queue.
        Returns the entry_id assigned (unique across the whole run, not
        just this line) — not currently needed by callers, but handy for
        tests/debugging."""
        entry_id = self._next_id
        self._next_id += 1
        item = {"entry_id": entry_id, "sachnummer": sachnummer, "t_entered": t, "rush": rush}
        lst = self.pending.setdefault(line_name, [])
        if rush:
            # Insert after any rush entries already pending, ahead of every
            # non-rush one — NOT always index 0. Index 0 would let each new
            # rush deposit cut in front of an earlier rush deposit that's
            # still waiting, undoing its jump instead of queuing behind it
            # (see push_chute_entries_at's docstring for the matching replay
            # rule — both must agree, or scrubbing back to an earlier t_s
            # would show a different order than what actually happened live).
            idx = sum(1 for p in lst if p["rush"])
            lst.insert(idx, item)
        else:
            lst.append(item)
        self.log.append(PushChuteLogEntry(
            t=t, line=line_name, kind="deposit", entry_id=entry_id,
            sachnummer=sachnummer, rush=rush,
        ))
        return entry_id

    def drain_one(self, line_name: str, t: float) -> Optional[dict]:
        """Pop and return whichever entry is at the front of this line's
        tracked pending list (the one _run_push_turn (via crew_process)'s
        pop_next_of_class("push") call is assumed to have just drained —
        see class docstring), logging the removal. Returns None if this
        tracker's view is already empty for this line (shouldn't happen
        in normal operation — pop_next_of_class("push") only returns
        non-None when a push entry actually exists to drain — but
        guarded rather than raising, consistent with this module's
        "flag, don't block" stance elsewhere)."""
        lst = self.pending.get(line_name) or []
        if not lst:
            return None
        item = lst.pop(0)
        self.log.append(PushChuteLogEntry(
            t=t, line=line_name, kind="drain", entry_id=item["entry_id"],
        ))
        return item

    def snapshot(self, line_name: str) -> list[dict]:
        """Current (live, right-now) pending push entries for a line,
        oldest-first — i.e. index 0 is the next one _run_push_turn (via crew_process)
        will drain. For a specific PAST instant, use
        push_chute_entries_at(tracker.log, ...) instead; this only
        reflects the simulation's current moment."""
        return list(self.pending.get(line_name, []))


def push_chute_entries_at(log: list[PushChuteLogEntry], line_name: str, edge_s: float) -> list[dict]:
    """
    Replay `log` up to and including edge_s to reconstruct which push
    chunks were sitting in `line_name`'s Chute queue at that instant,
    oldest-first (index 0 = closest to production / next to drain — the
    same convention the pull side's card_ids/frozen_card_ids ordering
    uses). This is what movement_state reporting should call per-frame,
    rather than PushChuteTracker.snapshot() (which only ever reflects
    "right now"), so an animation scrubbed to an earlier t_s shows the
    push queue as it actually was then, not the run's final state
    repeated on every frame.

    Returns [{"entry_id", "sachnummer", "t_entered", "rush"}, ...].
    """
    pending: list[dict] = []
    for ev in log:
        if ev.line != line_name or ev.t > edge_s:
            continue
        if ev.kind == "deposit":
            item = {"entry_id": ev.entry_id, "sachnummer": ev.sachnummer, "t_entered": ev.t, "rush": ev.rush}
            if ev.rush:
                # Same rule as PushChuteTracker.deposit(): land after any
                # rush entries already pending, ahead of every non-rush one.
                idx = sum(1 for p in pending if p["rush"])
                pending.insert(idx, item)
            else:
                pending.append(item)
        elif ev.kind == "drain":
            pending = [p for p in pending if p["entry_id"] != ev.entry_id]
    return pending


@dataclass
class PushSchedulerContext:
    """
    Everything push_dispatch_process()/_run_push_turn() (via crew_process) need, bundled
    once in run_mixed() instead of threaded through as a long parameter
    list. Deliberately plain data + a couple of tiny read-only helpers —
    no SimPy control flow lives here.
    """
    kenv: KanbanSimEnvironment
    epoch: _dt.datetime
    policy: PushPolicyConfig
    gates: dict[int, LinePriorityGate]
    chutes: dict[int, KanbanChuteResource]
    activity_signal: simpy.Store                # ONE signal shared by every
                                                  # idle crew — see
                                                  # crew_process()/
                                                  # _install_crew_chute_hooks().
                                                  # Replaces the old per-line
                                                  # push_signals/pull_signals
                                                  # cross-wake pair: a crew
                                                  # cares about ANY line's
                                                  # front changing, not just
                                                  # "its own" line's, so one
                                                  # shared wake-up is both
                                                  # simpler and correct.
    name_to_id: dict[str, int]
    active_lines: list[str]
    verbose: bool = True
    unassigned_log: Optional[list[UnassignedOrder]] = None
    chunk_size: int = PUSH_CHUNK_SIZE
    exotic_tracker: Optional[ExoticSupermarketTracker] = None
    overflow_log: Optional[list[SupermarketOverflowFlag]] = None
    delivery_log: Optional[list[PushDeliveryRecord]] = None
    exotic_snapshot_log: Optional[list[ExoticSlotSnapshot]] = None
    chute_tracker: Optional[PushChuteTracker] = None
    gate_activity_log: Optional[list["GateActivityEntry"]] = None
    pending_batches: dict[int, PendingPullBatch] = field(default_factory=dict)
    rt: Optional[object] = None                  # KanbanRuntime — needed by
                                                  # _run_one_pull_card() for
                                                  # rt.record_supermarket() /
                                                  # the on_finish card-recycle
                                                  # wiring that used to live
                                                  # inside
                                                  # production_trigger_process.

    def line_id(self, line_name: str) -> int:
        return self.name_to_id[line_name]

    def notify(self) -> None:
        """Wake every idle crew so it re-runs Rule 1/2 — call after any
        change that could affect which line is most loaded (a chute
        insertion, or a crew finishing a turn)."""
        self.activity_signal.put(None)

    def is_busy(self, line_name: str) -> bool:
        """
        A line counts as "busy" for the normal (non-rush) placement
        search if either something is actively running on it right now,
        or it already has ANY work queued (pull or push — total_pending_cards
        counts both, see KanbanChuteResource) that a new order would have
        to wait behind. Deliberately coarse — "is it free right now", not
        "is it free for the next N minutes" — the wait-and-retry loop
        (rule 3) is what actually handles a line staying busy.
        """
        lid = self.line_id(line_name)
        return self.gates[lid].resource.count > 0 or self.chutes[lid].total_pending_cards > 0

    def load(self, line_name: str) -> int:
        """Queue depth in card-units — the "how busy" figure the 12h-rush
        override's least-busy-line comparison ranks candidate lines by."""
        return self.chutes[self.line_id(line_name)].total_pending_cards

    # --- v6 shift on/off -----------------------------------------------
    #
    # Thin conversions on top of SimEnvironment.is_line_on() /
    # next_line_on_transition() (entities_resources_v5.py) — those take a
    # real wall-clock datetime; everything in this module works in
    # sim-time seconds (env.now), so the epoch conversion is folded in
    # here once rather than repeated at every call site. Both inherit
    # SimEnvironment's own "no Shifts sheet loaded at all -> always on"
    # convention (see that method's docstring) — callers in this module
    # never need to special-case "does this workbook even have shifts".

    def wall_clock(self, t: Optional[float] = None) -> _dt.datetime:
        """Sim-time seconds (default: right now) -> shared wall-clock instant."""
        if t is None:
            t = self.kenv.env.now
        return self.epoch + _dt.timedelta(seconds=t)

    def is_line_on(self, line_name: str, t: Optional[float] = None) -> bool:
        """True iff `line_name` is on-shift at sim time `t` (default: now)."""
        return self.kenv.is_line_on(line_name, self.wall_clock(t))

    def seconds_until_on(self, line_name: str, t: Optional[float] = None) -> Optional[float]:
        """
        Seconds from sim time `t` (default: now) until `line_name` next
        turns on. Returns 0.0 if it's ALREADY on at `t` — a caller asking
        "how long do I need to sleep before this line is on" wants 0, not
        a wait until the FOLLOWING shift, when the answer is already yes.
        Returns None if the line never turns on within the configured
        calendar horizon (including "no Shifts sheet loaded at all", in
        which case a line is always on and this branch is unreachable) —
        callers must decide how to handle "never comes back on" rather
        than this method looping or guessing a horizon of its own.
        """
        if t is None:
            t = self.kenv.env.now
        if self.is_line_on(line_name, t):
            return 0.0
        now_dt = self.wall_clock(t)
        nxt = self.kenv.next_line_on_transition(line_name, now_dt)
        if nxt is None:
            return None
        return (nxt - now_dt).total_seconds()


def apply_push_policy(ctx: PushSchedulerContext, policy: Optional[PushPolicyConfig] = None) -> None:
    """
    Re-apply push policy settings that are CACHED elsewhere and don't
    take effect just by mutating a PushPolicyConfig object in place —
    call this after editing settings from a frontend/API layer.

    If `policy` is given, it REPLACES ctx.policy entirely before
    re-applying; otherwise the CURRENT ctx.policy object is re-applied
    as-is (useful if the caller already mutated its fields in place
    rather than swapping in a new object — a frontend PATCH-style update
    would typically do `ctx.policy.frozen_zone_cards = 10;
    apply_push_policy(ctx)`).

    What's live vs. needs this call
    --------------------------------
      - rush_threshold_h, retry_interval_h: read fresh on every loop
        iteration of push_dispatch_process's placement search — a
        change to ctx.policy's fields takes effect on the very next
        iteration for every order still in that loop, automatically.
        Nothing to do here for these.
      - frozen_zone_cards: cached on each KanbanChuteResource at setup
        time (chute.frozen_zone_cards) — THIS call is what re-syncs it.
      - push_visibility_days, max_lead_time_h: each order's dispatch
        process reads these ONCE, at two early `yield
        env.timeout(...)` points (see push_dispatch_process), to
        compute how long to sleep. A change only affects orders whose
        dispatch process hasn't reached that point yet (e.g. a new
        CustomerDemand row processed after the edit) — an order already
        mid-sleep keeps sleeping for the duration it originally
        computed; a plain SimPy timeout can't be retroactively
        shortened or extended. This call cannot fix that either — it's
        a structural limit of the current one-shot-sleep design, not
        something forgotten here.
      - ideal_lead_time_h: never read by the scheduler at all — pure
        reporting/target reference point (see delivery_delta_h /
        push_delivery_summary). Nothing to re-apply, ever.
    """
    if policy is not None:
        ctx.policy = policy
    for chute in ctx.chutes.values():
        chute.set_frozen_zone_cards(ctx.policy.frozen_zone_cards)


def _build_push_order_record(
    row,
    info: ProductLineInfo,
    assigned_line: str,
    due_dt: _dt.datetime,
    note: Optional[str] = None,
) -> OrderRecord:
    """
    Resolve one CustomerDemand row + its chosen production line into a
    full OrderRecord — due_date populated straight from the row's own
    (Date, Time); delivered_date stays None until _run_push_turn() (via crew_process)
    sets it on the specific chunk that actually finishes.
    """
    lc = info.lines[assigned_line]
    return OrderRecord(
        period_label=row.period_label,
        sachnummer=row.product_id,
        kunde=info.kunde,
        product_class=getattr(info.product_class, "name", str(info.product_class)),
        quantity=row.total_qty,
        assigned_line=assigned_line,
        freigabe=getattr(lc.freigabe, "name", str(lc.freigabe)),
        station_sequence=lc.station_names,
        feasible_lines=info.feasible_lines(),
        note=note,
        due_date=due_dt,
    )


def push_dispatch_process(ctx: PushSchedulerContext, row):
    """
    SimPy generator — the full lifecycle of ONE CustomerDemand (push) row,
    from "not yet visible" through "placed on a line's chute". One of
    these is spawned per row by run_mixed(); it does NOT wait for the
    order to actually finish producing (that's _run_push_turn() (via crew_process)'s
    job, decoupled via the chute) — it only decides WHERE the order goes
    and WHEN it becomes visible/placed.

    Assignment rules implemented here, in order:
      1. Resolve PRODUCT_MATRIX compatibility + priority ordering
         (MatrixPriorityStrategy — "the line with the highest priority /
         preference" first).
      2. Not visible yet (more than policy.push_visibility_days before
         its due date) -> sleep until it becomes visible. This is a
         planning-awareness window only (the order "exists" to the
         system) — nothing is placed yet even once it opens; see step 3.
      3. Still more than policy.max_lead_time_h before the due date ->
         keep sleeping (this is the "not before 24h" bound — production
         shouldn't start/occupy an exotic Supermarket slot any earlier
         than necessary). Once within that window, the busy/retry search
         begins.
      4. Try each compatible line in priority order; the first one that
         is NOT busy (ctx.is_busy) AND is on-shift right now (ctx.
         is_line_on) gets the order. An off-shift line is excluded from
         this search exactly like a busy one — it is simply not a valid
         candidate this iteration, not something to wait out mid-check.
      5. If every compatible line is busy or off-shift, and the order is
         not yet within policy.rush_threshold_h of its due date -> wait
         policy.retry_interval_h and retry from step 4 (re-checking rush
         eligibility each time round, since the clock keeps moving).
      6. Once within policy.rush_threshold_h of the due date: stop
         searching for "free", and force-assign to whichever compatible
         line is currently LEAST busy (ctx.load, min) among those that
         are ON-SHIFT right now — an off-shift line is NEVER force-
         assigned, no matter how urgent the order (an off line "cannot be
         used", full stop; there is no override for that). If every
         compatible line is off-shift at this point, this step falls
         back to the SAME wait-and-retry as step 5 (policy.
         retry_interval_h) rather than forcing an assignment — it simply
         re-checks the rush condition (and on-shift lines) again next
         iteration.

    policy.ideal_lead_time_h is NOT read anywhere in this function — it's
    a reporting/target reference point only, used when comparing
    delivered_date against due_date afterwards (see
    OrderRecord.delivery_delta_h / push_delivery_summary). There's no
    single moment in this rule set where "aim for exactly 5h early"
    would even mean anything operationally; it's what a KPI dashboard
    compares actual outcomes against.

    Live-editing note (see apply_push_policy()): rush_threshold_h and
    retry_interval_h are read fresh on every loop iteration below, so
    editing ctx.policy's fields in place takes effect immediately for
    every order still in the search loop. push_visibility_days and
    max_lead_time_h are each read ONCE, at the two `yield
    env.timeout(...)` points below, to compute a sleep duration — an
    edit only affects orders whose dispatch process hasn't reached that
    point yet; an order already mid-sleep keeps sleeping for the
    duration it originally computed (a plain SimPy timeout can't be
    retroactively shortened).

    Orders with no PRODUCT_MATRIX entry, no parseable due date, or no
    feasible/active line at all are recorded to ctx.unassigned_log (if
    given) and dropped — same "can't be placed" bucket UnassignedOrder
    already represents elsewhere in the codebase.
    """
    kenv = ctx.kenv
    env = kenv.env

    try:
        info = _plm_lookup(row.product_id)
    except ValueError:
        if ctx.unassigned_log is not None:
            ctx.unassigned_log.append(UnassignedOrder(
                row.period_label, row.product_id, row.total_qty,
                "product not found in PRODUCT_MATRIX",
            ))
        if ctx.verbose:
            print(f"  ⚠ push: {row.product_id!r} not in PRODUCT_MATRIX — dropping.")
        return

    due_dt = _row_due_datetime(row)
    if due_dt is None:
        if ctx.unassigned_log is not None:
            ctx.unassigned_log.append(UnassignedOrder(
                row.period_label, row.product_id, row.total_qty,
                "unparseable due date (Date/Time cell)",
            ))
        if ctx.verbose:
            print(f"  ⚠ push: {row.product_id!r} has an unparseable due date "
                  f"({row.period_label!r} {getattr(row, 'time_slot', '')!r}) — dropping.")
        return
    due_t = (due_dt - ctx.epoch).total_seconds()

    candidate_lines = MatrixPriorityStrategy().order(info, ctx.active_lines)
    if not candidate_lines:
        if ctx.unassigned_log is not None:
            ctx.unassigned_log.append(UnassignedOrder(
                row.period_label, row.product_id, row.total_qty,
                f"no feasible line among active lines {ctx.active_lines}",
            ))
        if ctx.verbose:
            print(f"  ⚠ push: {row.product_id!r} has no feasible line among "
                  f"{ctx.active_lines} — dropping.")
        return

    # --- visibility window (rule 2) -----------------------------------------
    visible_t = due_t - ctx.policy.push_visibility_days * 24 * 3600.0
    if env.now < visible_t:
        yield env.timeout(visible_t - env.now)

    # --- max-lead-time gate (rule 3): "not before 24h" ----------------------
    # Visible != actionable. The order enters the system's awareness at
    # push_visibility_days out, but nothing gets placed until we're within
    # max_lead_time_h of the due date — placing (and occupying an exotic
    # Supermarket slot) any earlier than necessary is exactly what this
    # bound exists to prevent.
    attempt_open_t = due_t - ctx.policy.max_lead_time_h * 3600.0
    if env.now < attempt_open_t:
        yield env.timeout(attempt_open_t - env.now)

    # --- placement search (rules 4-6) ---------------------------------------
    assigned_line: Optional[str] = None
    rush = False
    while True:
        hours_to_due = (due_t - env.now) / 3600.0
        if hours_to_due <= ctx.policy.rush_threshold_h:
            # Rush override: force-assign to the least-busy compatible
            # line, but ONLY among lines that are on-shift right now. An
            # off-shift line "cannot be used", full stop — rush urgency
            # does not override that; see the docstring above (confirmed
            # decision, not an oversight). If every compatible line
            # happens to be off-shift at this instant, fall through to
            # the exact same wait-and-retry as the non-rush branch below
            # and re-check both conditions (rush eligibility AND on-shift)
            # next iteration.
            on_lines = [ln for ln in candidate_lines if ctx.is_line_on(ln)]
            if on_lines:
                assigned_line = min(on_lines, key=ctx.load)
                rush = True
                break
            if ctx.verbose:
                print(f"  [t={env.now:10.1f}] push: all compatible lines "
                      f"OFF-shift for {row.product_id!r} (due in "
                      f"{hours_to_due:.1f}h, within rush window) — "
                      f"retrying in {ctx.policy.retry_interval_h}h.")
            yield env.timeout(ctx.policy.retry_interval_h * 3600.0)
            continue

        found = next(
            (ln for ln in candidate_lines if not ctx.is_busy(ln) and ctx.is_line_on(ln)),
            None,
        )
        if found is not None:
            assigned_line = found
            break

        if ctx.verbose:
            print(f"  [t={env.now:10.1f}] push: all compatible lines busy or "
                  f"off-shift for {row.product_id!r} (due in "
                  f"{hours_to_due:.1f}h) — retrying in "
                  f"{ctx.policy.retry_interval_h}h.")
        yield env.timeout(ctx.policy.retry_interval_h * 3600.0)

    # --- build + enqueue (rule 5 for rush; normal ranking otherwise) ------
    note = (
        f"RUSH placement (<= {ctx.policy.rush_threshold_h}h before due date)"
        if rush else None
    )
    order = _build_push_order_record(row, info, assigned_line, due_dt, note=note)

    line_id = ctx.line_id(assigned_line)
    chute = ctx.chutes[line_id]
    remaining = order.quantity
    n_chunks = 0
    while remaining > 0:
        qty = min(ctx.chunk_size, remaining)
        chunk = _dc_replace(order, quantity=qty)
        if rush:
            chute.push_rush_entry(order.sachnummer, payload=chunk)
            if ctx.chute_tracker is not None:
                ctx.chute_tracker.deposit(assigned_line, order.sachnummer, env.now, rush=True)
        else:
            chute.push_chunk(order.sachnummer, priority="M", payload=chunk)
            if ctx.chute_tracker is not None:
                ctx.chute_tracker.deposit(assigned_line, order.sachnummer, env.now, rush=False)
        # No explicit wake needed here — _install_crew_chute_hooks() wraps
        # push_chunk()/push_rush_entry() to notify ctx.activity_signal
        # after every insertion, so every idle crew re-checks automatically.
        remaining -= qty
        n_chunks += 1

    if ctx.verbose:
        print(f"  [t={env.now:10.1f}] push: {row.product_id!r} qty={order.quantity} "
              f"-> {assigned_line} ({n_chunks} chunk(s)){' [RUSH]' if rush else ''}, "
              f"due {due_dt.isoformat()}.")


def _deposit_push_chunk_to_supermarket(
    ctx: PushSchedulerContext, line_name: str, chunk: OrderRecord,
) -> None:
    """
    Deposit this finished push chunk (one card, PUSH_CHUNK_SIZE pieces of
    chunk.sachnummer by construction — see ExoticSupermarketTracker's
    sizing-simplification note for the one edge case) into `line_name`'s
    exotic Supermarket slot pool.

    Always succeeds (see ExoticSupermarketTracker.deposit_chunk) — if
    every exotic slot is already full, this raises a flag onto
    ctx.overflow_log and keeps going rather than blocking or discarding
    the chunk, per the "no problem, raise a flag and continue" rule (the
    flag is meant to feed a future supermarket-sizing optimization pass,
    not to stop this simulation). A no-op if ctx.exotic_tracker wasn't
    supplied (keeps this callable from ad-hoc tests without requiring a
    full cfg.supermarkets).
    """
    if ctx.exotic_tracker is None:
        return

    fit, reason = ctx.exotic_tracker.deposit_chunk(line_name, chunk.sachnummer, ctx.kenv.env)
    if ctx.exotic_snapshot_log is not None:
        # One reading per physical slot on this line, right after the
        # deposit — gives the row-level time series its next data point.
        # See ExoticSlotSnapshot's docstring for why this lives here
        # rather than inside deposit_chunk() itself.
        ctx.exotic_snapshot_log.extend(
            ctx.exotic_tracker.snapshot(line_name, ctx.kenv.env.now)
        )
    if not fit:
        flag = SupermarketOverflowFlag(
            t=ctx.kenv.env.now, line=line_name, sachnummer=chunk.sachnummer,
            reason=reason or "exotic supermarket full",
        )
        if ctx.overflow_log is not None:
            ctx.overflow_log.append(flag)
        if ctx.verbose:
            print(f"  ⚑ [t={ctx.kenv.env.now:10.1f}] exotic supermarket overflow: "
                  f"{line_name} {chunk.sachnummer!r} — {flag.reason}")


def _withdraw_push_chunk_process(ctx: PushSchedulerContext, line_name: str, chunk: OrderRecord):
    """
    SimPy generator — one spawned per delivered push chunk (see
    _run_push_turn (via crew_process), step (e)). Models "the customer withdraws this
    exotic-supermarket card when they actually need it", i.e. at
    chunk.due_date — the push-side counterpart to the pull-side
    withdrawal_process, and the missing half of ExoticSupermarketTracker
    (deposit_chunk() alone only ever grows occupancy; this is what drains
    it back down).

    Two separate waits happen here, in order:
      1. Sleeps until chunk.due_date (relative to ctx.epoch + env.now) —
         "the customer doesn't want it before they need it". If due_date
         is already in the past by the time this process starts (e.g. a
         chunk that finished production late), this step is skipped
         (delay clamped to 0) rather than sleeping a negative amount.
      2. `yield from ExoticSupermarketTracker.withdraw_chunk(...)` — FIFO
         (oldest-deposited-first) withdrawal of a card matching
         chunk.sachnummer from line_name's exotic pool. Because that
         method blocks on the underlying store's get() rather than
         failing when nothing is available yet, if THIS chunk itself is
         somehow still mid-production when its own due_date arrives (late
         push production), this simply waits until
         _deposit_push_chunk_to_supermarket() actually deposits it —
         "most likely it's still in production and becomes available
         within the hour" — instead of warning and moving on. (In normal
         operation this never actually waits here, since this process is
         only spawned once its OWN chunk has already been deposited by
         _run_push_turn (via crew_process); the block only matters for the general
         mechanism / any future caller of withdraw_chunk that isn't as
         tightly sequenced as this one.)

    Runs as a detached background process (fire-and-forget via
    env.process(), not `yield from`'d by _run_push_turn (via crew_process)) so it
    doesn't block that loop from moving on to the next chunk.

    If chunk.due_date is None (shouldn't happen for a
    CustomerDemand-sourced push row, but guards against ad-hoc/test
    chunks) or ctx.exotic_tracker wasn't supplied, this is a no-op —
    mirrors _deposit_push_chunk_to_supermarket's own no-op guard.
    """
    if ctx.exotic_tracker is None or chunk.due_date is None:
        return
    kenv = ctx.kenv
    env = kenv.env
    tracker = ctx.exotic_tracker

    now_dt = ctx.epoch + _dt.timedelta(seconds=env.now)
    delay_s = max(0.0, (chunk.due_date - now_dt).total_seconds())
    if delay_s:
        yield env.timeout(delay_s)

    waiting_on_production = tracker.available_count(line_name, chunk.sachnummer) == 0
    if waiting_on_production and ctx.verbose:
        print(f"  [t={env.now:10.1f}] {line_name}: due_date reached for exotic "
              f"push card {chunk.sachnummer!r} but none in stock yet — waiting "
              f"for production to deposit it.")

    yield from tracker.withdraw_chunk(line_name, chunk.sachnummer, env)

    if ctx.exotic_snapshot_log is not None:
        # Same "one reading per physical slot on this line, right after
        # the mutation" convention _deposit_push_chunk_to_supermarket uses
        # for deposits — keeps the row-level time series accurate through
        # withdrawals too, not just deposits.
        ctx.exotic_snapshot_log.extend(
            tracker.snapshot(line_name, env.now)
        )

    if ctx.verbose:
        waited_str = " (after waiting on production)" if waiting_on_production else ""
        print(f"  [t={env.now:10.1f}] {line_name}: customer withdrew exotic "
              f"push card {chunk.sachnummer!r}{waited_str}")


def _install_crew_chute_hooks(
    chutes: dict[int, KanbanChuteResource],
    activity_signal: simpy.Store,
) -> None:
    """
    Per-instance monkeypatch, called once before env.run() — same
    pattern/spirit as the old _install_pull_only_chute_view() /
    _install_kanban_gate_hook() it replaces. Two things, both required
    now that crew_process() is the ONLY drainer for every line's chute:

      1. Neutralise chute.pop_next(): kanban_process_logic_v2's
         production_trigger_process is still spawned, UNMODIFIED, by
         start_kanban_simulation() (needed for withdrawal_process /
         day_boundary_process / collection_box_emptying_process's sake —
         movement 1, chute filling), and it still calls this once per
         loop iteration. Since crews now own every pop (via
         pop_next_of_class(), called directly — see _run_pull_turn /
         _run_push_turn), pop_next() is patched to always return None.
         production_trigger_process's own
         `while batch is None: yield signal.get(); batch = chute.pop_next()`
         loop then simply blocks forever, harmlessly, never touching a
         station — this keeps kanban_process_logic_v2.py completely
         unmodified while guaranteeing it can never compete with a crew
         for the same chute entry.
      2. Notify `activity_signal` after every successful insertion —
         push_batch() (pull arrivals), push_chunk()/push_rush_entry()
         (push arrivals) — so an idle crew blocked on "nothing to do
         anywhere" wakes up and re-runs Rule 1/2 the instant ANY line's
         chute gains work. Replaces the old per-line, per-class
         push_signals/pull_signals cross-wake pair entirely: one shared
         signal, because a crew cares about ANY line's front changing,
         not just "its own" line's.

    Idempotent per chute instance (checked via a marker attribute), same
    as the two functions it replaces.
    """
    for chute in chutes.values():
        if getattr(chute, "_mixed_crew_hooked", False):
            continue  # already installed on this instance

        _orig_push_batch = chute.push_batch
        _orig_push_chunk = chute.push_chunk
        _orig_push_rush_entry = chute.push_rush_entry

        def _pop_next_disabled():
            return None

        def _push_batch(product_type, cards, _orig=_orig_push_batch):
            _orig(product_type, cards)
            activity_signal.put(None)

        def _push_chunk(product_type, priority, payload, _orig=_orig_push_chunk):
            _orig(product_type, priority, payload)
            activity_signal.put(None)

        def _push_rush_entry(product_type, payload, _orig=_orig_push_rush_entry):
            _orig(product_type, payload)
            activity_signal.put(None)

        chute.pop_next = _pop_next_disabled
        chute.push_batch = _push_batch
        chute.push_chunk = _push_chunk
        chute.push_rush_entry = _push_rush_entry
        chute._mixed_crew_hooked = True


def _run_one_pull_card(
    ctx: PushSchedulerContext,
    line_id: int,
    line_name: str,
    entry,  # ChuteEntry, sim_class == "pull", n_cards == 1 by construction
    n_workers: int,
    event_log: Optional[list[ScheduleEvent]],
    crew_id: int,
):
    """
    SimPy generator — run exactly ONE already-popped pull ChuteEntry (one
    physical KanbanCard) to completion.

    This is the crew-loop replacement for what used to be split across
    two places: _install_kanban_gate_hook's wrapper (gate admission,
    event_log tagging, GateActivityEntry) and one iteration of
    kanban_process_logic_v2.production_trigger_process's own loop body
    (order_rec construction via _make_kanban_order_record, the on_finish
    callback wiring card-recycling + Supermarket deposit). Both are
    inlined here since production_trigger_process is now permanently
    neutralised (see _install_crew_chute_hooks) and crew_process() calls
    kanban_process_logic_v2.run_one_kanban_batch directly.

    `crew_id` is recorded on the resulting GateActivityEntry — this is
    the "which crew produced this card" attribution the frontend reads
    (see api_server_mixed.py's kpi_by_crew / crew_activity endpoint).

    Caller (crew_process, via _run_pull_turn) already holds
    ctx.gates[line_id] for the duration of this call.
    """
    import kanban_process_logic_v2 as kpl

    kenv = ctx.kenv
    env = kenv.env
    rt = ctx.rt
    gate = ctx.gates[line_id]
    product_type, cards = entry.product_type, entry.cards
    batch_size = cards[0].batch_size
    quantity = batch_size * len(cards)

    order_rec = kpl._make_kanban_order_record(rt, line_name, product_type, quantity)
    if order_rec is None:
        if ctx.verbose:
            print(f"  ⚠ {line_name}: {product_type!r} not feasible here — "
                  f"re-queuing card on the Kanban Chute.")
        ctx.chutes[line_id].push_batch(product_type, cards)
        return

    for c in cards:
        c.record_transition("in_production", env.now)

    sm = kenv.supermarket_for(line_id, product_type)
    recycle_queue: list = list(cards)

    def on_finish(t: float, status: str, _sm=sm, _rq=recycle_queue,
                  _product=product_type, _line_id=line_id):
        if status != "passed" or _sm is None:
            return
        n_whole = _sm.deposit_finished_pcs(1)
        rt.record_supermarket(_line_id, _product, "deposit_partial", _sm)
        for _ in range(n_whole):
            if _rq:
                card = _rq.pop(0)
            else:
                # Fallback only — should be rare/never, mirrors the
                # original production_trigger_process comment: every
                # piece produced for this card has a matching card
                # already reserved in `cards` above.
                card = kenv.create_card(_product, _line_id, _sm.batch_size, priority="M")
            if ctx.verbose:
                print(f"  [t={t:10.1f}] {line_name}(L{_line_id}): package of "
                      f"{_sm.batch_size} × {_product!r} COMPLETED — "
                      f"delivering to Supermarket.")
            env.process(
                kpl._return_card_to_supermarket(rt, kenv, _sm, card, t, line_name,
                                                 _line_id, ctx.verbose)
            )

    t_start = env.now
    possible_changeover = (
        gate.current_rec is None
        or getattr(gate.current_rec, "sachnummer", None) != order_rec.sachnummer
    )
    # Tag events emitted by THIS call as "pull" — see module docstring's
    # "Event-log class tagging" section for why filtering by line_id (not
    # just index) is required. Also back-fills crew_id (v10) the same
    # way, from this call's own crew_id parameter — this is what lets
    # schedule_events.build_gantt_payload's "crewId" on each Gantt job
    # segment actually be populated instead of staying null (see that
    # module's ScheduleEvent.crew_id docstring for the contract this
    # fulfils).
    _log = kenv.event_log if event_log is None else event_log
    _start_idx = len(_log)
    result = yield from kpl.run_one_kanban_batch(
        kenv, line_id, gate.current_rec, order_rec, n_workers, on_finish, ctx.verbose,
        event_log=event_log,
    )
    for _ev in _log[_start_idx:]:
        if _ev.line_id == line_id and _ev.sim_class is None:
            _ev.sim_class = "pull"
        if _ev.line_id == line_id and _ev.crew_id is None:
            _ev.crew_id = crew_id
    if result is not None:
        gate.current_rec = result
    t_end = env.now
    gate.touch(t_end)
    if ctx.gate_activity_log is not None:
        ctx.gate_activity_log.append(GateActivityEntry(
            t_start=t_start, t_end=t_end, line_id=line_id,
            sachnummer=order_rec.sachnummer, sim_class="pull",
            crew_id=crew_id,
            possible_changeover=possible_changeover,
            quantity=order_rec.quantity,
        ))


def _run_pull_turn(
    ctx: PushSchedulerContext,
    line_id: int,
    line_name: str,
    n_workers: int,
    event_log: Optional[list[ScheduleEvent]],
    crew_id: int,
):
    """
    SimPy generator — one full pull "turn": drain up to 4 cards of one
    product_number from the front of this line's chute, respecting
    whatever order movement 1 has already established. NEVER looks past
    the front entry — if the front stops matching the batch in progress,
    the turn ends immediately and the partial count is persisted
    (ctx.pending_batches) for a future visit, never searched for out of
    order (chute order governs; the crew doesn't reorder it — see this
    module's docstring and Rule 1.a).

    Handles all of the spec's pull examples with the same loop, no
    special-casing:
      - 6 cards of A queued: takes 4 (pending cleared — batch complete),
        leaving 2 at the front for whatever turn visits this line next,
        which simply starts a fresh record from taken=0.
      - 2 cards of A queued, then 2 more of A arrive before the next
        visit: first visit takes 2 (pending persists {A, taken=2}); a
        later visit resumes and takes the remaining 2 to reach 4.
      - 3 taken, then 2 more of A arrive, then 4 of B, then 4 more of A:
        the turn that hits B persists {A, taken=3}; a later turn that
        finds A back at the front (once whatever's ahead of it has been
        dealt with by movement 1's own ordering) resumes with taken=3 —
        exactly Rule 1.a, needing only 1 more card to complete the batch.
    """
    chute = ctx.chutes[line_id]
    front = chute.peek_next_of_class("pull")
    if front is None:
        return

    pending = ctx.pending_batches.get(line_id)
    if pending is None or pending.product_type != front.product_type:
        pending = PendingPullBatch(product_type=front.product_type, taken=0)

    while pending.taken < 4:
        front = chute.peek_next_of_class("pull")
        if front is None or front.product_type != pending.product_type:
            break
        entry = chute.pop_next_of_class("pull")
        yield from _run_one_pull_card(ctx, line_id, line_name, entry, n_workers, event_log, crew_id)
        pending.taken += 1
        ctx.notify()

    if pending.taken >= 4:
        ctx.pending_batches.pop(line_id, None)
    else:
        ctx.pending_batches[line_id] = pending


def _run_push_turn(
    ctx: PushSchedulerContext,
    line_id: int,
    line_name: str,
    n_workers: int,
    event_log: Optional[list[ScheduleEvent]],
    crew_id: int,
):
    """
    SimPy generator — one full push "turn": pop and run consecutive push
    ChuteEntry objects off the front of this line's chute, one at a time,
    for as long as the front stays a push entry of the SAME product_type
    as the first one taken this turn — "take all the cards of the push
    order". An order's chunks are dispatched back-to-back under the same
    sachnummer (see push_dispatch_process), so a same-product run of push
    entries is, in practice, one order; this is a stated assumption, not
    verified against a scenario where two DIFFERENT push orders of the
    same product happen to sit adjacent in one line's chute.

    Per popped chunk, unchanged from the old push_drain_process (gate
    admission is already held by the caller — crew_process — for the
    whole turn, so there's no separate per-chunk gate request here):
    execute via run_one_order(), tag event_log entries "push", append a
    GateActivityEntry, stamp delivered_date + PushDeliveryRecord, deposit
    into the exotic Supermarket, and spawn the detached due-date
    withdrawal process.
    """
    kenv = ctx.kenv
    env = kenv.env
    gate = ctx.gates[line_id]
    chute = ctx.chutes[line_id]

    front = chute.peek_next_of_class("push")
    if front is None:
        return
    product_type = front.product_type

    while True:
        front = chute.peek_next_of_class("push")
        if front is None or front.product_type != product_type:
            break

        t_start = env.now
        entry = chute.pop_next_of_class("push")
        if ctx.chute_tracker is not None:
            ctx.chute_tracker.drain_one(line_name, env.now)
        chunk: OrderRecord = entry.payload
        possible_changeover = (
            gate.current_rec is None
            or getattr(gate.current_rec, "sachnummer", None) != chunk.sachnummer
        )

        _log = kenv.event_log if event_log is None else event_log
        _start_idx = len(_log)
        ran = yield from run_one_order(
            kenv, line_id, gate.current_rec, chunk, n_workers, ctx.verbose
        )
        # Same "pull" tagging as _run_one_pull_card above, plus the same
        # crew_id back-fill (v10) — see that function's comment for why.
        for _ev in _log[_start_idx:]:
            if _ev.line_id == line_id and _ev.sim_class is None:
                _ev.sim_class = "push"
            if _ev.line_id == line_id and _ev.crew_id is None:
                _ev.crew_id = crew_id
        if ran:
            gate.current_rec = chunk
        t_end = env.now
        gate.touch(t_end)
        if ctx.gate_activity_log is not None:
            ctx.gate_activity_log.append(GateActivityEntry(
                t_start=t_start, t_end=t_end, line_id=line_id,
                sachnummer=chunk.sachnummer, sim_class="push",
                crew_id=crew_id,
                possible_changeover=possible_changeover,
                quantity=chunk.quantity,
            ))

        chunk.delivered_date = ctx.epoch + _dt.timedelta(seconds=env.now)
        if ctx.delivery_log is not None:
            ctx.delivery_log.append(PushDeliveryRecord(
                sachnummer=chunk.sachnummer,
                assigned_line=chunk.assigned_line,
                quantity=chunk.quantity,
                due_date=chunk.due_date,
                delivered_date=chunk.delivered_date,
                delivery_delta_h=chunk.delivery_delta_h,
                rush=bool(chunk.note and "RUSH" in chunk.note),
            ))

        _deposit_push_chunk_to_supermarket(ctx, line_name, chunk)
        env.process(_withdraw_push_chunk_process(ctx, line_name, chunk))

        if ctx.verbose:
            delta = chunk.delivery_delta_h
            delta_str = f", delivery_delta={delta:+.2f}h" if delta is not None else ""
            print(f"  [t={env.now:10.1f}] {line_name}(L{line_id}): push chunk done "
                  f"{chunk.sachnummer} qty={chunk.quantity}{delta_str}")

        ctx.notify()


def _select_line_for_crew(
    ctx: PushSchedulerContext, held_line_id: Optional[int],
) -> Optional[int]:
    """
    Rule 1 (fresh pick — held_line_id=None) / Rule 2 (re-evaluation after
    a turn — held_line_id=the line this crew currently holds) line
    selection.

    Eligible lines: on-shift right now, with pending work
    (KanbanChuteResource.total_pending_cards > 0), and — unless it's the
    line we already hold — not currently locked by another crew
    (gate.resource.count == 0).

    Tie-breaking:
      - Among FRESH candidates (held_line_id=None), the max-load line
        wins; ties fall to iteration order (kenv.lines' own order, i.e.
        lowest line_id) — not specified by the spec beyond the
        crew-vs-crew timing case below, so this is a reasonable default.
      - When RE-EVALUATING a line already held, Rule 2's "equal amount
        stays on the same one" is implemented by requiring a candidate
        to be STRICTLY more loaded than the held line before switching.

    Crew-vs-crew arbitration: two crews becoming idle at the same
    simulated instant are NOT arbitrated by anything in this function —
    it's a pure (side-effect-free) read of current state. The
    arbitration happens naturally in crew_process(): requesting a line's
    gate.resource synchronously marks it busy before that crew's first
    yield, and SimPy runs same-instant processes in the order they were
    spawned (run_mixed() spawns crew_id=0 before crew_id=1, ...), so the
    lower-numbered crew's request is always visible to the next crew's
    call to this function — "crew 1 starts selecting, then crew 2",
    exactly as specified, with no extra bookkeeping needed here.
    """
    loads: dict[int, int] = {}
    for line in ctx.kenv.lines:
        lid = line.line_id
        if not ctx.is_line_on(line.line_name):
            continue
        if lid != held_line_id and ctx.gates[lid].resource.count > 0:
            continue
        load = ctx.chutes[lid].total_pending_cards
        if load > 0:
            loads[lid] = load

    if not loads:
        return None

    if held_line_id is not None and held_line_id in loads:
        held_load = loads[held_line_id]
        other_best_id, other_best_load = max(
            ((lid, ld) for lid, ld in loads.items() if lid != held_line_id),
            key=lambda kv: kv[1], default=(None, -1),
        )
        if other_best_load > held_load:
            return other_best_id
        return held_line_id

    return max(loads.items(), key=lambda kv: kv[1])[0]


def crew_process(
    ctx: PushSchedulerContext,
    crew_id: int,
    n_workers: int,
    event_log: Optional[list[ScheduleEvent]] = None,
):
    """
    SimPy generator — one instance per crew (run_mixed(n_crews=...)).
    Movement 2 (depletion): the ONLY consumer of every line's chute (see
    _install_crew_chute_hooks). Loop:

      1. Rule 1/2 (_select_line_for_crew): pick the most-loaded eligible
         line — None if nothing anywhere has work right now.
      2. Hold that line's LinePriorityGate for as long as this crew keeps
         working it (possibly several turns in a row — see step 3).
      3. Run ONE turn: the whole push order if a push entry is at the
         chute's front (_run_push_turn), or up to a 4-card
         same-product_number pull batch if a pull entry is at the front
         (_run_pull_turn, with Rule 1.a carryover via
         ctx.pending_batches). If neither is available (chute emptied
         since selection), release the gate and re-select from scratch.
      4. Rule 2: re-run _select_line_for_crew with this line as
         held_line_id. Strictly-more-loaded elsewhere -> release this
         gate and go back to step 1. Otherwise -> loop back to step 3 on
         the same line (still holding its gate).

    Idles (blocked on ctx.activity_signal) whenever step 1 finds nothing
    on-shift anywhere with pending work; woken by any chute insertion
    (_install_crew_chute_hooks) or any crew finishing a turn (ctx.notify,
    called from _run_pull_turn/_run_push_turn).

    v6 shift on/off: a line can go off-shift WHILE this crew holds it
    (between turns, or — more subtly — the calendar transitioning mid-
    turn is never checked, matching the "a turn already in progress is
    never preempted" rule from _gated_run_one_kanban_batch's original
    docstring). Before starting a NEW turn, this loop waits out any
    off-shift period on the currently-held line rather than starting one
    — "off lines cannot be used" applies to STARTING work, not to
    finishing what's already running.
    """
    kenv = ctx.kenv
    env = kenv.env

    while True:
        line_id = _select_line_for_crew(ctx, held_line_id=None)
        if line_id is None:
            yield ctx.activity_signal.get()
            continue

        gate = ctx.gates[line_id]
        with gate.resource.request() as req:
            yield req
            while True:
                line_name = next(l.line_name for l in kenv.lines if l.line_id == line_id)

                while not ctx.is_line_on(line_name):
                    wait_s = ctx.seconds_until_on(line_name)
                    yield env.timeout(wait_s if wait_s else _OFF_LINE_REPOLL_S)

                chute = ctx.chutes[line_id]
                if chute.peek_next_of_class("push") is not None:
                    yield from _run_push_turn(ctx, line_id, line_name, n_workers, event_log, crew_id)
                elif chute.peek_next_of_class("pull") is not None:
                    yield from _run_pull_turn(ctx, line_id, line_name, n_workers, event_log, crew_id)
                else:
                    break  # chute emptied since selection — release, re-select

                next_line_id = _select_line_for_crew(ctx, held_line_id=line_id)
                if next_line_id != line_id:
                    break  # Rule 2: a strictly more-loaded line exists
                           # (or nothing is left anywhere) — release and
                           # go back to a fresh selection.
                # else: strictly equal, or nothing more loaded -> stay
                # and run another turn on this same line.


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

def run_mixed(
    excel_path: str,
    setup_times_path: str,
    n_workers: int = N_WORKERS,
    n_crews: int = 2,
    seed: int = SEED,
    verbose: bool = True,
    day_start_hour: int = DAY_START_HOUR,
    day_length_s: float = DAY_LENGTH_S,
    drain_days: int = DRAIN_DAYS,
    push_chunk_size: int = PUSH_CHUNK_SIZE,
    horizon_s: Optional[float] = None,
    snapshot_log: Optional[list] = None,
    daily_log: Optional[list[dict]] = None,
    shortfall_log: Optional[list] = None,
    push_policy: Optional[PushPolicyConfig] = None,
):
    """
    Run the full mixed (class-1 Kanban + class-2 push) simulation on one
    continuous SimPy clock.

    Sequence
    --------
    1. load_config() — one workbook read, both CustomerDemandKanban and
       CustomerDemand sheets already parsed onto cfg.
    2. One shared simpy.Environment + KanbanSimEnvironment
       (build_kanban_environment — same station/buffer/chute objects push
       and kanban both execute against).
    3. Horizon: max of estimate_horizon_s() (kanban's own, sheet-driven
       estimate) and the last CustomerDemand due date + drain_days
       (kanban's estimator only looks at CustomerDemandKanban).
    4. One LinePriorityGate (plain per-line mutex) per line, plus each
       line's chute (from kenv.kanban_chutes) gets its frozen zone size
       set from `push_policy` and its insertion methods / pop_next()
       patched via _install_crew_chute_hooks (self-notifying inserts;
       pop_next() neutralised — see that function's docstring).
    5. start_kanban_simulation(kenv, ...) — registers withdrawal_process,
       day_boundary_process, per-line collection_box_emptying_process,
       and production_trigger_process EXACTLY as kanban_runner.py does
       (movement 1's pull side); production_trigger_process itself is
       harmless here because step 4 already neutralised chute.pop_next().
    6. One push_dispatch_process() per CustomerDemand row (movement 1's
       push side — visibility window, PRODUCT_MATRIX-priority line
       trial, busy/retry, 12h-rush override) + `n_crews` crew_process()
       instances (movement 2 — the only thing that ever drains a chute;
       see that function's docstring for Rule 1/1.a/2).
    7. env.run(until=horizon_s).

    `n_crews` : how many crew_process() workers share the `kenv.lines`
        pool — e.g. 2 crews across 3 lines. A crew can only ever hold one
        line's gate at a time, so at most `min(n_crews, len(kenv.lines))`
        lines are ever being worked simultaneously.

    `push_policy` : PushPolicyConfig, editable knobs for the frozen zone
        and the rolling push dispatcher (visibility window, lead-time
        bounds, rush threshold, retry interval — see that class's
        docstring). Defaults to PushPolicyConfig() if not given. Fully
        wired as of this version: frozen_zone_cards drives every line's
        chute, and the rest drive push_dispatch_process() directly.

    IMPORTANT — reads `kenv.kanban_chutes: dict[int, KanbanChuteResource]`,
    exposed by build_kanban_environment() (entities_resources_v5.py). Note
    this is a DIFFERENT dict from `kenv.chutes` — that one is the
    raw-material FIFO chute immediately upstream of Beladen, inherited
    unchanged from the push-model SimEnvironment; `kanban_chutes` is the
    per-line, priority-ordered, frozen-zone-aware admission queue this
    module and crew_process() actually share. This module never
    constructs a chute itself (that stays entities_resources_v5.py's
    job) — if the real attribute name ever changes again, run_mixed()
    raises immediately below rather than silently misbehaving.

    Live-editing from outside this call (e.g. a frontend/API layer): the
    returned kenv.push_ctx is the actual PushSchedulerContext every push
    process AND every crew reads from — mutate kenv.push_ctx.policy's
    fields directly (or call apply_push_policy(kenv.push_ctx, new_policy)
    to swap the whole object) while the simulation is running elsewhere
    (e.g. in a background thread advancing env.run() incrementally). See
    apply_push_policy()'s docstring for exactly which settings take
    effect immediately vs. only for orders that haven't started their
    placement search yet.

    Returns kenv (a KanbanSimEnvironment) with kenv.event_log,
    kenv.snapshot_log, kenv.daily_log, kenv.shortfall_log, kenv.rt,
    kenv.gates, kenv.push_policy, kenv.push_unassigned_log,
    kenv.supermarket_overflow_log, kenv.exotic_tracker,
    kenv.exotic_snapshot_log, kenv.push_delivery_log,
    kenv.push_chute_tracker, kenv.push_chute_log, kenv.gate_activity_log,
    kenv.push_ctx, kenv.oee_tracker, kenv.oee_draw_log attached — same
    attribute pattern as kanban_runner.run_kanban().

    kenv.oee_tracker (OEELossTracker) holds each line's current-day
    CombinedProductionLoss (get_current(line_name)); kenv.oee_draw_log
    (list[OEELossDrawEntry]) is the full day-by-day history of every draw
    for every line — see oee_daily_process()/OEELossTracker's own
    docstrings.

    kenv.gate_activity_log (list[GateActivityEntry]) is the time-indexed
    "what was this line's gate doing at any past instant" log — one
    entry per completed pull card (appended by _run_one_pull_card) or
    push chunk (appended by _run_push_turn), both funneled through the
    same LinePriorityGate, regardless of which crew ran them.
    See GateActivityEntry / production_status_at() for the replay
    convention and the known Rüstzeit-vs-producing sub-resolution
    limitation.
    """
    if push_policy is None:
        push_policy = PushPolicyConfig()

    cfg = load_config(excel_path, setup_times_path)
    epoch = _compute_shared_epoch(cfg, day_start_hour)

    env = simpy.Environment()
    kenv = build_kanban_environment(env, cfg, seed=seed)

    chutes: Optional[dict[int, KanbanChuteResource]] = getattr(kenv, "kanban_chutes", None)
    if chutes is None:
        raise RuntimeError(
            "kenv.kanban_chutes not found. build_kanban_environment() is "
            "expected to expose {line_id: KanbanChuteResource} under that "
            "name (NOT kenv.chutes — that dict is the raw-material FIFO "
            "chute upstream of Beladen, a different resource entirely) "
            "for mixed_runner's frozen zone / push dispatcher to work. "
            "If the real attribute is named differently, update this line."
        )

    if horizon_s is None:
        horizon_s = estimate_horizon_s(
            cfg, day_start_hour=day_start_hour, day_length_s=day_length_s,
            drain_days=drain_days, fallback_horizon_s=SIM_HORIZON_S,
        )
        # Also cover the case where push demand's last due date is later
        # than kanban's last withdrawal — estimate_horizon_s() (as
        # documented in kanban_runner.py) only looks at
        # CustomerDemandKanban, not CustomerDemand.
        last_due_t: Optional[float] = None
        for row in cfg.demand:
            due_dt = _row_due_datetime(row)
            if due_dt is not None:
                t = (due_dt - epoch).total_seconds()
                if last_due_t is None or t > last_due_t:
                    last_due_t = t
        if last_due_t is not None:
            horizon_s = max(horizon_s, last_due_t + (drain_days + 1) * day_length_s)
    horizon_s = max(horizon_s, SIM_HORIZON_S)

    gates: dict[int, LinePriorityGate] = {
        line.line_id: LinePriorityGate(resource=simpy.Resource(env, capacity=1))
        for line in kenv.lines
    }
    gate_activity_log: list[GateActivityEntry] = []

    # ONE shared wake-up signal for every idle crew — see
    # PushSchedulerContext.activity_signal / crew_process() / this
    # module's v10 docstring note for why a single Store replaces the
    # old per-line push_signals/pull_signals pair.
    activity_signal: simpy.Store = simpy.Store(env)

    for chute in chutes.values():
        chute.set_frozen_zone_cards(push_policy.frozen_zone_cards)
    # Patch BEFORE start_kanban_simulation registers any process (safe
    # either way — pop_next/push_batch/push_chunk/push_rush_entry are
    # only resolved when actually called, not at env.process()/
    # registration time — but doing it first keeps the ordering
    # obviously correct). See _install_crew_chute_hooks's docstring:
    # this both neutralises production_trigger_process's own pop_next()
    # calls and wires the activity_signal notify on every insertion.
    _install_crew_chute_hooks(chutes, activity_signal)

    event_log: list[ScheduleEvent] = []
    # run_one_order() (process_logic_sequential_v3.py) does NOT take an
    # event_log argument — it builds its PackageTracker from
    # `sim_env.event_log` directly. _run_push_turn() calls run_one_order()
    # with `kenv` as sim_env, so kenv.event_log must already be THIS
    # shared list before any process (kanban or crew) starts running, or
    # push chunks silently produce no Gantt/ScheduleEvent entries.
    # (Kanban's own side doesn't need this — run_one_kanban_batch takes
    # event_log as an explicit parameter, threaded through directly by
    # _run_one_pull_card.)
    kenv.event_log = event_log

    rt = start_kanban_simulation(
        kenv, n_workers=n_workers, verbose=verbose,
        event_log=event_log, snapshot_log=snapshot_log, daily_log=daily_log,
        shortfall_log=shortfall_log,
        day_start_hour=day_start_hour, day_length_s=day_length_s,
    )

    # Class-1's KanbanRuntime computes its own sim_epoch internally
    # (_compute_sim_epoch(cfg, day_start_hour), kanban-sheet-only). Sanity
    # check it against the shared epoch this module computed independently
    # (which also considers CustomerDemand) — they must agree, or push
    # chunks and kanban withdrawals are on two different clocks.
    if rt.sim_epoch is not None and rt.sim_epoch != epoch:
        print(f"  ⚠ mixed_runner: shared epoch {epoch} != KanbanRuntime.sim_epoch "
              f"{rt.sim_epoch} — push chunks and kanban withdrawals will be "
              f"misaligned in time. This means CustomerDemandKanban's own "
              f"earliest date differs from the earliest date across both "
              f"sheets; investigate before trusting this run's results.")

    # Movement 1 (push side): one dispatcher process per demand row.
    # Movement 2 (depletion, both classes): n_crews crew_process()
    # instances, sharing `ctx` — the only consumers of any line's chute.
    push_unassigned_log: list[UnassignedOrder] = []
    supermarket_overflow_log: list[SupermarketOverflowFlag] = []
    push_delivery_log: list[PushDeliveryRecord] = []
    exotic_tracker = ExoticSupermarketTracker(cfg)
    exotic_snapshot_log: list[ExoticSlotSnapshot] = []
    # Seed t=0 readings for every line's Exotic slots — same reasoning as
    # kanban_process_logic.start_kanban_simulation's initial
    # SupermarketSnapshot: without this, a row-level chart's very first
    # bin would show nothing rather than the (all-empty, at t=0) starting
    # state.
    for line in kenv.lines:
        exotic_snapshot_log.extend(exotic_tracker.snapshot(line.line_name, 0.0))
    chute_tracker = PushChuteTracker()
    # OEE loss (CombinedProductionLoss) — daily per-line draw. Purely
    # additive: an older workbook with no "OEE" sheet parses to an empty
    # oee_distribution (config_loader_v6._parse_oee_sheet), so
    # oee_daily_process below simply has nothing to draw for any line and
    # oee_tracker.get_current() reads None everywhere, same fallback
    # spirit as the Kanban/Shifts sheets. Attached onto kenv (not passed
    # as a new run_one_order() parameter) so run_one_order can read
    # sim_env.oee_tracker.get_current(line_name) directly — see
    # OEELossTracker's own module-level note.
    oee_tracker = OEELossTracker(getattr(cfg, "oee_distribution", None) or {})
    oee_draw_log: list[OEELossDrawEntry] = []
    env.process(oee_daily_process(
        kenv, oee_tracker, oee_draw_log, day_length_s, random.Random(seed),
    ))
    kenv.oee_tracker = oee_tracker
    kenv.oee_draw_log = oee_draw_log
    ctx = PushSchedulerContext(
        kenv=kenv, epoch=epoch, policy=push_policy, gates=gates, chutes=chutes,
        activity_signal=activity_signal,
        name_to_id={line.line_name: line.line_id for line in kenv.lines},
        active_lines=list(cfg.line_names), verbose=verbose,
        unassigned_log=push_unassigned_log, chunk_size=push_chunk_size,
        exotic_tracker=exotic_tracker, overflow_log=supermarket_overflow_log,
        delivery_log=push_delivery_log, exotic_snapshot_log=exotic_snapshot_log,
        chute_tracker=chute_tracker, gate_activity_log=gate_activity_log,
        rt=rt,
    )
    for row in cfg.demand:
        env.process(push_dispatch_process(ctx, row))
    # Spawned in crew_id order (0, 1, ...) — crew-vs-crew arbitration for
    # simultaneous idle selection relies on this registration order (see
    # _select_line_for_crew's docstring).
    for crew_id in range(n_crews):
        env.process(crew_process(ctx, crew_id, n_workers, event_log=event_log))

    env.run(until=horizon_s)

    if verbose:
        for line in kenv.lines:
            print_kpi(kenv, line.line_id, env.now)

    kenv.event_log = event_log
    kenv.snapshot_log = rt.snapshot_log
    kenv.daily_log = rt.daily_log
    kenv.shortfall_log = rt.shortfall_log
    kenv.rt = rt
    kenv.gates = gates
    kenv.n_crews = n_crews    # so a caller (e.g. api_server_mixed.py) can
                               # build a per-crew summary without having to
                               # infer crew count from which crew_ids
                               # happened to appear in gate_activity_log
                               # (a crew that never got any work — e.g.
                               # more crews than lines — wouldn't appear).
    kenv.push_policy = push_policy
    kenv.push_unassigned_log = push_unassigned_log
    kenv.supermarket_overflow_log = supermarket_overflow_log
    kenv.exotic_tracker = exotic_tracker
    kenv.exotic_snapshot_log = exotic_snapshot_log
    kenv.push_delivery_log = push_delivery_log
    kenv.push_chute_tracker = chute_tracker
    kenv.push_chute_log = chute_tracker.log
    kenv.gate_activity_log = gate_activity_log
    kenv.push_ctx = ctx
    return kenv


if __name__ == "__main__":
    import sys
    excel = sys.argv[1] if len(sys.argv) > 1 else "ProductionPlanning_v6.xlsx"
    setup = sys.argv[2] if len(sys.argv) > 2 else "HTL_setup_times.xlsx"
    workers = int(sys.argv[3]) if len(sys.argv) > 3 else N_WORKERS
    crews = int(sys.argv[4]) if len(sys.argv) > 4 else 2
    run_mixed(excel, setup, n_workers=workers, n_crews=crews)
