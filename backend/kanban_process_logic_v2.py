"""
kanban_process_logic.py
========================
Kanban pull-system process logic (replaces kanban_process_logic_v1.py).

This module owns EVERYTHING upstream of physical production:

    Supermarket --(withdrawal)--> Collection Box --(emptying)-->
    Batch-Size Collector --(threshold release)--> Kanban Chute
    --(production trigger)--> [ existing production movement ] -->
    back to Supermarket (cards recycled)

It deliberately contains **no** station/buffer/BAS logic of its own.
Every part still moves from Beladen through Sichtpruefung exactly the
way it does in the push model, by calling straight into
process_logic_sequential_v3.py:

    changeover()                    - whole-line setup delay
    part_lifecycle()                - one part's full journey (BAS, rework,
                                       inspection, finished-goods deposit)
    run_lochfilter_drs_production() - parallel Lochfilter/DRS sub-assembly
    _material_stored_types()        - which FIFO chute lane(s) Beladen draws
    _active_buffer_sequence()       - resolves buffer routing for a product

The one thing process_logic_sequential_v3.run_one_order() could NOT be
reused for as-is is the per-part completion hook: run_one_order() hard-
wires a PackageTracker (package_size-based) as `on_finish`. The Kanban
loop needs its own on_finish (deposit into the Supermarket + recycle the
physical Kanban card), so run_one_kanban_batch() below reproduces
run_one_order()'s thin orchestration (changeover -> resolve routing ->
launch parts -> AllOf) with a pluggable on_finish, while still calling
the exact same underlying production primitives. No station/buffer/part
movement logic is duplicated here.

Raw-material FIFO chutes (Chu_vor_HTL3/5/6 etc.) are UNCHANGED and still
used for loading material into Beladen. KanbanChuteResource is a
different, new thing: it only sequences which released card-batch
(production ORDER) starts next, by priority; it never touches material.

Withdrawal & Assignment rules (v5)
-----------------------------------
CustomerDemandKanban is now a flat, long event table — one row per
(Date, Time, Product, TotalQuantity, LineId) — see
config_loader_v5.KanbanWithdrawalEvent. `LineId` on that sheet is a
DOWNSTREAM line (whoever is requesting the product), NOT one of our
production lines, so it plays no role in choosing which of OUR
Supermarkets fulfils a card. Two rules replace the old fixed-line
lookup:

  (Withdrawal) Every row spawns its own independent, repeating
      withdrawal STREAM: starting at that row's own (Date, Time), it
      withdraws one card (batch_size pieces, from KanbanCardsSetup —
      workbook default 200) every
      cfg.kanban_timing.withdrawal_cadence_min minutes (workbook
      default now 50 min), until it has withdrawn quantity // batch_size
      cards total. Two rows for the SAME product at the SAME timestamp
      but different (downstream) LineId — e.g. "Line 9" and "Line 11"
      both wanting 1500 pcs at 06:00 — simply run two streams in
      parallel, so together they combine into a higher effective card
      rate for that product (2 cards / 50 min instead of 1) with no
      extra summing logic needed; see withdrawal_process() /
      _withdrawal_stream().

  (Assignment) Each individual card withdrawal (fire-and-forget, same
      as before) picks which of OUR lines' Supermarkets to pull from at
      the moment it fires, via select_supermarket_for_withdrawal(): the
      line with the most stock of that product right now. Because this
      is re-evaluated fresh on every single withdrawal (never sticky),
      it naturally drains the biggest Supermarket first and only moves
      on to the next-biggest once the first runs dry — see that
      function's docstring, and build_supermarket_state_lookup() for how
      the cross-line stock picture is assembled. This is deliberately a
      small, dedicated, easily-swappable function, since the assignment
      rule itself is expected to change later.

If the line picked out is empty, the withdrawal simply blocks on that
line's Store.get() until that line produces a batch — this is what
makes it a PULL system (see SupermarketResource docstring, Q1). See
select_supermarket_for_withdrawal()'s docstring for the one known
simplification this implies (a request blocked on one line isn't woken
early if a DIFFERENT line restocks first).

Card movement / transportation time
------------------------------------
Per the current scope, transportation time for materials (AGV/forklift)
and for card movement (logistics personnel) is NOT simulated yet - every
hand-off below happens at simulated time 0 (immediate). The movement
itself (withdrawn -> collection box -> batch collector -> chute ->
production -> supermarket) IS modelled step by step, with a
`KanbanCard.record_transition()` call at every step, specifically so
that adding real transport/logistics delay later is a one-line change:
just insert `yield env.timeout(transport_time_s)` right before the
relevant `record_transition(...)` call. Every place where that hook
belongs is marked with a `# TRANSPORT-TIME HOOK` comment below.

Multi-day support
-----------------
CustomerDemandKanban's Date + Time columns (combined via
_row_datetime_string()) accept either a bare "HH:MM" Time with no usable
Date (legacy, single day, original fixed-cadence behaviour) or a full
"DD.MM.YYYY HH:MM[:SS]" combined timestamp (new — lets one sheet span
any number of days, at whatever real cadence the rows encode). See
_parse_kanban_timestamp / _compute_sim_epoch / withdrawal_process /
day_boundary_process / estimate_horizon_s below for the full mechanics.
In short: a "production day" runs day_start_hour:00 -> day_start_hour:00
the next calendar day (06:00 by default, edit at the kanban_runner.py
call site); day_boundary_process() takes a KPI/Restmenge/backlog
checkpoint at every such boundary; nothing pauses production AT the
boundary — a part mid-station keeps running straight through it, and
next-day production simply resumes on its own once next-day withdrawal
rows start firing (see day_boundary_process's docstring).

Usage
-----
    import simpy
    from config_loader_v5 import load_config
    from entities_resources_v4 import build_kanban_environment
    from kanban_process_logic import (
        start_kanban_simulation, print_restmenge_report, estimate_horizon_s,
    )

    cfg  = load_config("ProductionPlanning_v5.xlsx", "HTL_setup_times.xlsx")
    env  = simpy.Environment()
    kenv = build_kanban_environment(env, cfg, seed=42)

    horizon_s = estimate_horizon_s(cfg)   # sizes itself to however many
                                            # days the sheet spans (+1 drain day)
    start_kanban_simulation(kenv, n_workers=1, verbose=True)
    env.run(until=horizon_s)
    print_restmenge_report(kenv)   # Correction #3: end-of-run partial-pack report
                                    # (day_boundary_process already prints one of
                                    # these at every day boundary during the run)
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, time as dt_time
from typing import Optional

import simpy

from entities_resources_v5 import (
    KanbanSimEnvironment, KanbanCard, SupermarketResource,
    StationResource, BufferResource,
)
from dispatch_entities import OrderRecord
from product_line_matrix_v3 import lookup as lookup_product, ProductClass, _active_buffer_sequence
from schedule_events import PackageTracker
from process_logic_sequential_v3 import (
    changeover,
    part_lifecycle,
    run_lochfilter_drs_production,
    _material_stored_types,
    print_kpi,
)

N_WORKERS: int = 1
# ===========================================================================
# Multi-day timestamp support
# ===========================================================================
#
# CustomerDemandKanban's Time column now accepts two shapes, distinguished
# purely by whether a date is present:
#
#   - Absolute:  "01.09.2026 06:51:00" (seconds optional) - a real
#                calendar date + time. Enables multi-day sheets: rows can
#                span any number of days, in any (non-uniform) cadence.
#   - Legacy:    "08:00" - no date, single day, original fixed-cadence
#                behaviour (see withdrawal_process()).
#
# config_loader_v5.py keeps Date and Time as two separate plain-string
# columns (see KanbanWithdrawalEvent) — _row_datetime_string() below
# combines them into one string before either shape is interpreted here.

_TIMESTAMP_FORMATS: tuple[str, ...] = (
    "%d.%m.%Y %H:%M:%S",
    "%d.%m.%Y %H:%M",
)


def _parse_kanban_timestamp(time_slot: str) -> Optional[datetime]:
    """
    Parse one CustomerDemandKanban row's Time cell.

    Returns a real datetime for the new absolute "DD.MM.YYYY HH:MM[:SS]"
    shape, or None for anything else (legacy bare "HH:MM", empty cell, or
    an unparseable value) - callers treat None as "fall back to legacy,
    single-day, fixed-cadence behaviour for this row" (see
    withdrawal_process(), _compute_sim_epoch(), estimate_horizon_s()).

    Never raises: this reads directly off a hand-maintained Excel sheet,
    so one malformed cell degrading to legacy-mode-for-that-row is far
    preferable to crashing the whole run.
    """
    if time_slot is None:
        return None
    s = str(time_slot).strip()
    if not s:
        return None
    for fmt in _TIMESTAMP_FORMATS:
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    return None


def _row_datetime_string(evt) -> str:
    """
    Combine one KanbanWithdrawalEvent's separate `date` + `time` cells
    into the single "DD.MM.YYYY HH:MM[:SS]" string _parse_kanban_timestamp
    expects.

    v5 sheet-layout note: CustomerDemandKanban keeps Date and Time as two
    separate columns (see config_loader_v5.KanbanWithdrawalEvent), unlike
    the pre-v5 sheet's single combined "Time" cell — this is the one
    place that difference is bridged back into the rest of this module's
    (unchanged) timestamp-parsing machinery.
    """
    date = (evt.date or "").strip()
    time = (evt.time or "").strip()
    return f"{date} {time}".strip()


def _compute_sim_epoch(cfg, day_start_hour: int) -> Optional[datetime]:
    """
    Find the datetime that maps to simpy t=0.

    Scans cfg.kanban_withdrawals for the FIRST row whose (Date, Time)
    parses as an absolute timestamp (see _parse_kanban_timestamp /
    _row_datetime_string), then floors it to that production day's
    start: `day_start_hour:00:00` on that same calendar date, or the
    PREVIOUS calendar date if the row's own clock time is earlier than
    day_start_hour (since a production day runs day_start_hour ->
    day_start_hour the next day, e.g. an 03:00 row belongs to the day
    that *started* the calendar day before).

    Returns None if no row in the entire sheet has an absolute timestamp
    (a pure legacy "HH:MM"-only sheet) - every caller treats None as
    "legacy mode, no date information available, anchor everything the
    old way".
    """
    for evt in cfg.kanban_withdrawals:
        dt = _parse_kanban_timestamp(_row_datetime_string(evt))
        if dt is not None:
            day_date = dt.date()
            if dt.hour < day_start_hour:
                day_date -= timedelta(days=1)
            return datetime.combine(day_date, dt_time(hour=day_start_hour))
    return None


def estimate_horizon_s(cfg, day_start_hour: int = 6, day_length_s: float = 24 * 3600.0,
                        drain_days: int = 1, fallback_horizon_s: float = 24 * 3600.0) -> float:
    """
    Derive a safe env.run(until=...) horizon for a (possibly multi-day)
    CustomerDemandKanban sheet, without the caller needing to know up
    front how many days it spans. Called by kanban_runner.run_kanban()
    before start_kanban_simulation() — this is a pure function of `cfg`,
    no live env/KanbanRuntime needed.

    - Absolute-timestamp sheets: finds the LATEST parseable timestamp,
      converts it to a day index using the same epoch _compute_sim_epoch()
      derives, and returns `(last_day_index + 1 + drain_days) * day_length_s`
      — i.e. run through every day that actually has demand rows, plus
      drain_days extra day(s) of no-new-demand runway so backlog already
      released before the last demand day's end (sitting in the
      collection box / batch collector / chute / mid-production) has
      time to finish draining back into a Supermarket.
    - Legacy sheets (no row parses as an absolute timestamp at all):
      returns fallback_horizon_s unchanged — there's no date information
      to size a multi-day horizon from, so this preserves the original
      fixed-horizon behaviour exactly.
    """
    epoch = _compute_sim_epoch(cfg, day_start_hour)
    if epoch is None:
        return fallback_horizon_s

    latest_dt: Optional[datetime] = None
    for evt in cfg.kanban_withdrawals:
        dt = _parse_kanban_timestamp(_row_datetime_string(evt))
        if dt is not None and (latest_dt is None or dt > latest_dt):
            latest_dt = dt

    if latest_dt is None:
        # Shouldn't happen if epoch was found (epoch requires at least one
        # parseable row) — defensive only.
        return fallback_horizon_s

    latest_t = (latest_dt - epoch).total_seconds()
    last_day_index = int(latest_t // day_length_s)
    return (last_day_index + 1 + drain_days) * day_length_s


# ===========================================================================
# SupermarketSnapshot — one timestamped stock-level observation
# ===========================================================================

@dataclass
class SupermarketSnapshot:
    """
    One timestamped observation of a Supermarket's stock level, recorded
    every time that Supermarket's state actually changes. Three call
    sites in this module emit one each:

      "withdrawal"       - _withdraw_one_card(), right after
                            sm.record_withdrawal() (a card left the shelf)
      "deposit_partial"  - production_trigger_process()'s on_finish
                            closure, right after sm.deposit_finished_pcs()
                            (a passed part was counted into the running
                            partial-pack total; may or may not have just
                            completed a whole batch — n_available and
                            pcs_partial both reflect the post-call state,
                            so a completed-batch tick is visible as
                            pcs_partial dropping back near 0)
      "deposit_batch"    - _return_card_to_supermarket(), right after
                            sm.store.put(card) (a recycled card + its
                            finished batch physically landed back on the
                            shelf, is now withdrawable)
      "initial"          - start_kanban_simulation(), once per (line,
                            product) Supermarket, at t=0 before any other
                            process has run. build_kanban_environment()
                            (entities_resources_v4.py) seeds each
                            Supermarket's starting cards with a direct
                            `sm.store.items.append(card)` — it has no
                            KanbanRuntime to call record_supermarket()
                            through at that point in the build. Without
                            this snapshot, the true starting stock (e.g.
                            32 cards on HTL3) was never observable: the
                            earliest point any consumer ever saw was
                            whatever the FIRST real event had already
                            changed it to.

    Same "caller owns the list, pass by reference" pattern as
    schedule_events.ScheduleEvent — no locking needed since SimPy is
    single-threaded/cooperative. Consumed by
    kanban_events.build_line_units_timeseries() to plot stock level per
    (line, product) over the run.

    n_available / pcs_partial are read straight off SupermarketResource
    AFTER the triggering call, so this is always the authoritative
    post-event state, not a delta.
    """
    t:              float
    line_id:        int
    line_name:      str
    product_type:   str
    event_type:     str   # "withdrawal" | "deposit_partial" | "deposit_batch"
    n_available:    int
    pcs_partial:    int
    batch_size:     int


# ===========================================================================
# ShortfallEvent — one "customer asked, supermarket had none" interval
# ===========================================================================

@dataclass
class ShortfallEvent:
    """
    One interval during which a Kanban withdrawal request found its
    line's Supermarket for that product completely empty (n_available
    == 0 at the exact instant the request was made) and had to sit
    blocked on that Store.get() until the next batch was produced/
    deposited before it could be granted.

    start_s : env.now the instant the request checked stock and found
              none — i.e. "the customer asked for a card".
    end_s   : env.now the instant sm.store.get() actually resolved — a
              batch became available and was handed to this request.

    Recorded ONLY for requests that genuinely had to wait; a request
    that finds stock already on the shelf never produces one of these
    (see _withdraw_one_card(), the sole call site of
    KanbanRuntime.record_shortfall()). The check happens synchronously,
    immediately before the blocking `yield sm.store.get()` two lines
    below it, with no intervening yield — so nothing else in this
    module's cooperative SimPy processes can run between the check and
    the get() call, making the check race-free against concurrent
    withdrawals on the same Supermarket.

    Same caller-owned, append-only, by-reference list pattern as
    snapshot_log / daily_log (see KanbanRuntime's docstring) — pass one
    in via start_kanban_simulation(shortfall_log=...) if you want it,
    otherwise a private list is created and simply discarded at run end.

    Consumed by kanban_events.build_card_flow_payload()'s
    shortfall_by_line output, which buckets these intervals onto the
    same time grid as the card-state census so the dashboard's
    "Shortfall" chart can plot, per line, how many withdrawal requests
    were open-and-unmet at each point in time.
    """
    line_id:        int
    line_name:      str
    product_type:   str
    start_s:        float
    end_s:          float


# ===========================================================================
# Small per-run state (kept OUTSIDE KanbanSimEnvironment on purpose - it's
# pure wiring for this module's own generators, not part of the shared
# entity/resource model that entities_resources_v4.py owns).
# ===========================================================================

class KanbanRuntime:
    """
    Wiring created once by start_kanban_simulation() and threaded through
    every generator in this module.

    line_name_to_id   : "HTL3" -> 1, "HTL5" -> 2, "HTL6" -> 3 (from kenv.lines)
    chute_signal      : dict[line_id -> simpy.Store] - a pure wake-up
                        channel. check_and_release_batch() drops a token
                        in here every time it pushes a batch onto that
                        line's KanbanChuteResource; production_trigger_process
                        blocks on it only when the chute is empty, and
                        always re-checks the chute first - so it's
                        self-correcting even if token counts and pushed
                        batches don't line up 1:1.
    product_info_cache: memoises product_line_matrix_v3.lookup() calls,
                        since every batch on every line calls it.
    snapshot_log      : list[SupermarketSnapshot], caller-owned (same
                        by-reference pattern as schedule_events'
                        event_log). Pass one in via start_kanban_simulation
                        if you want it; otherwise a private list is
                        created and simply discarded at run end.
    daily_log         : list[dict], caller-owned, same by-reference
                        pattern as snapshot_log. One entry appended per
                        production day by day_boundary_process() — see
                        that function's docstring for the dict shape.
                        Pass one in via start_kanban_simulation if you
                        want it; otherwise a private list is created and
                        simply discarded at run end.
    shortfall_log     : list[ShortfallEvent], caller-owned, same by-
                        reference pattern as snapshot_log. One entry
                        appended by _withdraw_one_card() every time a
                        withdrawal request finds its Supermarket empty
                        and has to wait — see ShortfallEvent's docstring.
                        Pass one in via start_kanban_simulation if you
                        want it; otherwise a private list is created and
                        simply discarded at run end.
    day_start_hour    : production-day boundary (0-23), e.g. 6 means each
                        day runs 06:00 -> 05:59:59 the next calendar day.
                        Threaded through from start_kanban_simulation /
                        kanban_runner.DAY_START_HOUR — manually editable
                        there, not hardcoded in this module.
    day_length_s      : length of one production day in seconds (default
                        24h — only reason to change this is a non-24h
                        shift-day definition, which is unusual, hence
                        exposed rather than assumed).
    sim_epoch         : the real-world datetime that maps to simpy t=0,
                        derived once via _compute_sim_epoch() from the
                        FIRST absolute timestamp found anywhere in
                        cfg.kanban_withdrawals. None for a legacy
                        (all-"HH:MM", no dates) sheet — see
                        _compute_sim_epoch()/withdrawal_process() for how
                        that fallback is handled. Used to convert every
                        row's absolute Time cell into a simpy-clock
                        offset: `(dt - sim_epoch).total_seconds()`.
    """

    def __init__(self, kenv: KanbanSimEnvironment,
                 snapshot_log: Optional[list["SupermarketSnapshot"]] = None,
                 daily_log: Optional[list[dict]] = None,
                 shortfall_log: Optional[list["ShortfallEvent"]] = None,
                 day_start_hour: int = 6,
                 day_length_s: float = 24 * 3600.0):
        self.kenv = kenv
        self.line_name_to_id: dict[str, int] = {
            line.line_name: line.line_id for line in kenv.lines
        }
        self.chute_signal: dict[int, simpy.Store] = {
            line.line_id: simpy.Store(kenv.env) for line in kenv.lines
        }
        self._product_info_cache: dict[str, object] = {}
        self.snapshot_log: list[SupermarketSnapshot] = (
            snapshot_log if snapshot_log is not None else []
        )
        self.daily_log: list[dict] = daily_log if daily_log is not None else []
        self.shortfall_log: list[ShortfallEvent] = (
            shortfall_log if shortfall_log is not None else []
        )
        self.day_start_hour = day_start_hour
        self.day_length_s = day_length_s
        self.sim_epoch: Optional[datetime] = _compute_sim_epoch(kenv.cfg, day_start_hour)

    def product_info(self, sachnummer: str):
        info = self._product_info_cache.get(sachnummer)
        if info is None:
            info = lookup_product(sachnummer)
            self._product_info_cache[sachnummer] = info
        return info

    def wake_production(self, line_id: int) -> None:
        """Drop a wake-up token for that line's production_trigger_process."""
        self.chute_signal[line_id].put(None)

    def record_supermarket(self, line_id: int, product_type: str,
                            event_type: str, sm: SupermarketResource) -> None:
        """Append one SupermarketSnapshot reflecting sm's state right now
        (post-event). Called from the three mutation sites listed on
        SupermarketSnapshot's docstring — never call this speculatively."""
        line_name = self.kenv.lines[line_id - 1].line_name
        self.snapshot_log.append(
            SupermarketSnapshot(
                t=self.kenv.env.now,
                line_id=line_id,
                line_name=line_name,
                product_type=product_type,
                event_type=event_type,
                n_available=sm.n_available,
                pcs_partial=sm.pcs_partial,
                batch_size=sm.batch_size,
            )
        )

    def record_shortfall(self, line_id: int, product_type: str,
                          start_s: float, end_s: float) -> None:
        """Append one ShortfallEvent covering [start_s, end_s) — the span
        a withdrawal request sat blocked with nothing on the shelf. Called
        only from _withdraw_one_card(), only when that request found
        n_available == 0 at the instant it asked — see ShortfallEvent's
        docstring."""
        line_name = self.kenv.lines[line_id - 1].line_name
        self.shortfall_log.append(
            ShortfallEvent(
                line_id=line_id,
                line_name=line_name,
                product_type=product_type,
                start_s=start_s,
                end_s=end_s,
            )
        )


# ===========================================================================
# 1. Withdrawal  (Supermarket -> Collection Box)
# ===========================================================================

def withdrawal_process(rt: KanbanRuntime, verbose: bool = True):
    """
    SimPy generator - drives the whole (possibly multi-day)
    CustomerDemandKanban sheet (v5 flat layout: one row = one demand
    event, Date | Time | Product | TotalQuantity | LineId — see
    config_loader_v5.KanbanWithdrawalEvent).

    v5 change: this no longer fires one shared "tick" for the whole
    sheet. Every row now launches its OWN independent, repeating
    withdrawal stream (_withdrawal_stream(), fire-and-forget SimPy
    process) at the moment this generator runs (t=0) — each stream then
    sleeps on its own until ITS row's (Date, Time) instant before doing
    any actual withdrawing. See the module docstring's "Withdrawal &
    Assignment rules" note for the full rate rule this implements, and
    _withdrawal_stream()'s docstring for the per-row mechanics.

    Row scheduling within a stream still supports the same two modes as
    before:

      - Absolute mode (new): if the row's (Date, Time) parses as a real
        "DD.MM.YYYY HH:MM[:SS]" timestamp AND rt.sim_epoch was
        successfully derived (i.e. at least one row in the sheet has a
        date - see KanbanRuntime/_compute_sim_epoch), the stream starts
        at the EXACT simpy-clock instant that timestamp maps to:
        `(dt - rt.sim_epoch).total_seconds()`.
      - Legacy mode (unchanged in spirit): a row that doesn't parse as
        an absolute timestamp (bare "HH:MM" Time with no usable Date, or
        the whole sheet has no dates at all so rt.sim_epoch is None)
        starts its stream immediately at t=0 — there's no "whole sheet"
        fixed-cadence tick to anchor a delay to any more, since every
        row is now independently scheduled.

    A sheet mixing both row shapes isn't the expected case, but nothing
    here requires uniformity - each row's stream is scheduled
    independently off its own cells.
    """
    kenv = rt.kenv
    env = kenv.env
    yield env.timeout(0)  # keep this a proper SimPy process even though
                           # all real scheduling now happens per-row,
                           # inside each _withdrawal_stream() below.

    for evt in kenv.cfg.kanban_withdrawals:
        dt = _parse_kanban_timestamp(_row_datetime_string(evt))
        if dt is not None and rt.sim_epoch is not None:
            start_t = (dt - rt.sim_epoch).total_seconds()
        else:
            start_t = env.now
        env.process(_withdrawal_stream(rt, evt, start_t, verbose))


def _n_cards_for_row(rt: KanbanRuntime, evt: "KanbanWithdrawalEvent") -> int:
    """
    Number of whole Kanban cards one CustomerDemandKanban row's
    TotalQuantity is worth: `quantity // batch_size` (see the module
    docstring's "200 pieces = 1 card" rule). `batch_size` comes from
    KanbanCardsSetup (cfg.kanban_cards[evt.product].batch_size), falling
    back to 200 (the rule's own example figure) if the product has no
    KanbanCardsSetup row at all.

    Any remainder (TotalQuantity not evenly divisible by batch_size) is
    dropped, with a warning — a partial card can't physically be
    withdrawn, same "no partial withdrawals" rule as
    SupermarketSlotConfig.initial_pcs_partial.
    """
    card_cfg = rt.kenv.cfg.kanban_cards.get(evt.product)
    batch_size = card_cfg.batch_size if card_cfg else 200
    if batch_size <= 0:
        return 0

    n_cards, remainder = divmod(evt.quantity, batch_size)
    if remainder:
        print(f"  ⚠ CustomerDemandKanban {evt.date} {evt.time}: "
              f"{evt.product!r} quantity={evt.quantity} isn't an exact "
              f"multiple of batch_size={batch_size} — {remainder} pcs "
              f"dropped (no partial-card withdrawal).")
    return n_cards


def _withdrawal_stream(rt: KanbanRuntime, evt: "KanbanWithdrawalEvent",
                        start_t: float, verbose: bool):
    """
    One independent, repeating card-withdrawal stream for a single
    CustomerDemandKanban row — see withdrawal_process()'s and the module
    docstring's "Withdrawal & Assignment rules" note for the rule this
    implements.

    Sleeps until `start_t`, then withdraws one card every
    cfg.kanban_timing.withdrawal_cadence_min minutes (workbook default:
    50 min), `_n_cards_for_row(rt, evt)` times total. Two rows for the
    SAME product at the SAME timestamp but different (downstream)
    `line_id` each get their own stream and simply run in parallel — no
    summing needed here, since combining two 1-card/50min streams into
    an effective 2-cards/50min rate falls straight out of running them
    independently at the same start time.

    Each individual card withdrawal is itself launched as its own
    fire-and-forget process (_withdraw_one_card), exactly like the
    pre-v5 per-(product, line) loop did, so a stockout on one card never
    blocks the next tick of this same stream (or any other stream).
    """
    kenv = rt.kenv
    env = kenv.env
    cadence_s = kenv.cfg.kanban_timing.withdrawal_cadence_min * 60.0

    n_cards = _n_cards_for_row(rt, evt)
    if n_cards <= 0:
        return

    delay = start_t - env.now
    if delay > 0:
        yield env.timeout(delay)
    # delay <= 0 (duplicate/out-of-order timestamp, or legacy mode
    # already at t=0) -> start immediately rather than going negative.

    for i in range(n_cards):
        if i > 0:
            yield env.timeout(cadence_s)
        if verbose:
            print(f"  [t={env.now:10.1f}] Withdrawal tick — {evt.product!r} "
                  f"card {i + 1}/{n_cards} (downstream {evt.line_id!r}, "
                  f"row {evt.date} {evt.time})")
        env.process(_withdraw_one_card(rt, evt.product, verbose))


def build_supermarket_state_lookup(
    rt: KanbanRuntime,
    product_type: str,
) -> dict[int, tuple[str, SupermarketResource]]:
    """
    Snapshot every line's current Supermarket stock for `product_type`,
    across every line eligible to hold it — the "lookup for the
    Supermarket state" select_supermarket_for_withdrawal() ranks lines
    by. Deliberately not cached: stock changes on every
    withdrawal/deposit, so this is rebuilt fresh on every call.

    Only lines listed in this product's KanbanCardsSetup.eligible_lines
    (cfg.kanban_cards) AND that actually got a Supermarket resource
    built for it are included (falls back to every known line if the
    product has no KanbanCardsSetup row at all).

    Multi-row note (Supermarkets sheet / SupermarketSlotConfig): a line
    can have several physical slot ROWS for the same product (e.g. HTL3
    rows 1 & 2 both F00RJ02491 — "these are separate physical lanes...
    therefore it must be added", per the assignment rule).
    build_kanban_environment() (entities_resources_v4.py) is the layer
    responsible for folding every row of a given (line, sachnummer) into
    ONE combined SupermarketResource at construction time (summed
    Capacity + summed InitialState/InitialPcsPartial across rows) — a
    build-time concern, not a per-withdrawal one — so that by the time
    this function runs, `kenv.supermarket_for(line_id, product_type)`
    already reflects that line's TRUE total stock. This function itself
    only aggregates ACROSS LINES on top of that. If
    entities_resources_v4.py hasn't been updated for the new
    multi-row-per-line "Supermarkets" sheet yet, that folding step is
    the one still outstanding — flagged here since this function is the
    first consumer that depends on it being correct.

    Returns {line_id: (line_name, SupermarketResource)}.
    """
    kenv = rt.kenv
    card_cfg = kenv.cfg.kanban_cards.get(product_type)
    eligible_lines = (
        card_cfg.eligible_lines if card_cfg and card_cfg.eligible_lines
        else list(rt.line_name_to_id.keys())
    )

    lookup: dict[int, tuple[str, SupermarketResource]] = {}
    for line_name in eligible_lines:
        line_id = rt.line_name_to_id.get(line_name)
        if line_id is None:
            continue
        sm = kenv.supermarket_for(line_id, product_type)
        if sm is None:
            continue
        lookup[line_id] = (line_name, sm)

    return lookup


def select_supermarket_for_withdrawal(
    rt: KanbanRuntime,
    product_type: str,
) -> tuple[Optional[int], Optional[SupermarketResource]]:
    """
    Dedicated, swappable "assignment" function (see the module
    docstring's "Withdrawal & Assignment rules" note) — decide which
    line's Supermarket a withdrawal request for `product_type` should be
    fulfilled from. Kept isolated in its own small function on purpose:
    this rule is expected to change / be tried differently later.

    Current rule — "most stock first": among every eligible line (see
    build_supermarket_state_lookup()), pick the one with the highest
    n_available right now; ties broken by line name (alphabetical) for
    determinism.

    Evaluated fresh on EVERY single card withdrawal (never memoized/
    sticky) — since stock only decreases as cards are pulled, this
    naturally reproduces "keep pulling from the biggest until it runs
    dry, then move to the next-biggest" with no extra bookkeeping: once
    repeated withdrawals drain today's biggest line below another
    line's level, the very next call to this function switches to that
    other line on its own.

    If every eligible line is currently at 0 (system-wide stockout for
    this product), there's no genuinely "biggest" one to prefer — the
    first line in the deterministic (alphabetical) tie-break order is
    still returned, and the caller (_withdraw_one_card) blocks there.
    Known simplification: if a DIFFERENT eligible line restocks first
    while this request is waiting, it is not woken early — the request
    stays blocked on the line it was assigned to. This mirrors a real
    logistics run needing a person to notice and reroute the card, so
    it's an intentional simplification, not an oversight; swap this
    function out for a "wait on whichever restocks first" version later
    if that granularity ever matters.

    Returns (line_id, SupermarketResource), or (None, None) if
    `product_type` has no configured Supermarket anywhere.
    """
    lookup = build_supermarket_state_lookup(rt, product_type)
    if not lookup:
        return None, None

    best_line_id, (_best_line_name, best_sm) = min(
        lookup.items(),
        key=lambda item: (-item[1][1].n_available, item[1][0]),
    )
    return best_line_id, best_sm


def _withdraw_one_card(rt: KanbanRuntime, product_type: str, verbose: bool):
    """
    Withdraw exactly ONE card's worth (one whole batch) of `product_type`.

    v5 change: which line's Supermarket fulfils this is no longer fixed
    by a sheet cell (CustomerDemandKanban's LineId is a downstream line
    now, not one of ours — see module docstring). It's decided fresh,
    right here, by select_supermarket_for_withdrawal() — the "most stock
    first" assignment rule; see that function's docstring for the exact
    policy and its one known blocking-side simplification.

    Card priority: CustomerDemandKanban no longer carries a priority
    column, so a withdrawn card simply keeps whatever priority it was
    created/recycled with (see KanbanCard / create_card default) instead
    of being overwritten here as it was pre-v5.
    """
    kenv = rt.kenv
    env = kenv.env

    line_id, sm = select_supermarket_for_withdrawal(rt, product_type)
    if sm is None:
        # Defensive guard only — shouldn't normally happen given
        # build_kanban_environment only builds a (line, product)
        # Supermarket for eligible products — but kept in case none
        # were built for this product at all.
        if verbose:
            print(f"  ⚠ Withdrawal: no Supermarket configured anywhere for "
                  f"{product_type!r} — dropping this card request.")
        return
    line_name = kenv.lines[line_id - 1].line_name

    # TRANSPORT-TIME HOOK: logistics-personnel travel time to physically
    # pick a card+batch off the supermarket shelf would be simulated with
    # `yield env.timeout(pickup_time_s)` right here, before the get().

    # SHORTFALL CHECK: if the shelf is empty right now, this request is
    # about to block on sm.store.get() below — that's exactly "the
    # customer asked for a card and the supermarket had none available".
    # Checked here, synchronously, immediately before the yield — nothing
    # else in this module's cooperative SimPy processes can run between
    # this line and the get() two lines down, so the check can't race
    # against another withdrawal on the same Supermarket. See
    # ShortfallEvent's docstring for the full reasoning.
    shortfall_start = env.now if sm.n_available == 0 else None

    card: KanbanCard = yield sm.store.get()

    if shortfall_start is not None:
        rt.record_shortfall(line_id, product_type, shortfall_start, env.now)

    sm.record_withdrawal()
    rt.record_supermarket(line_id, product_type, "withdrawal", sm)
    card.record_transition("withdrawn", env.now)

    # TRANSPORT-TIME HOOK: time to physically carry the card+batch from
    # the supermarket to the collection box would be
    # `yield env.timeout(carry_time_s)` right here.
    cb = kenv.collection_box_for(line_id)
    cb.add(card)
    card.record_transition("in_collection_box", env.now)


# ===========================================================================
# 2. Collection-box emptying  (Collection Box -> Batch-Size Collector
#    -> [threshold reached] -> Kanban Chute)
# ===========================================================================

def collection_box_emptying_process(rt: KanbanRuntime, line_id: int, verbose: bool = True):
    """
    SimPy generator - one instance per line. Every
    cfg.kanban_timing.collection_box_emptying_min minutes, empties that
    line's CollectionBoxResource into its BatchCollectorResource, then
    checks every product touched for release readiness (cards_to_trigger
    reached) and pushes any newly-ready batch onto the KanbanChuteResource.

    A `while` (not `if`) on is_ready() so a bucket that has accumulated
    more than one threshold's worth of cards over several emptying
    cycles is fully drained in one go, releasing multiple batches.
    """
    kenv = rt.kenv
    env = kenv.env
    cadence_s = kenv.cfg.kanban_timing.collection_box_emptying_min * 60.0
    _ORDER_EPSILON_S = 1e-6  # Correction #1: forces this process to always resolve
                              # AFTER withdrawal_process whenever their cadences land
                              # on the same simulated instant, regardless of
                              # env.process() registration order or cadence values.
                              # Only the first tick is nudged; since this is a
                              # relative `timeout(cadence_s)` loop, the offset
                              # propagates to every later tick with no drift.

    cb = kenv.collection_box_for(line_id)
    bc = kenv.batch_collector_for(line_id)
    chute = kenv.kanban_chute_for(line_id)
    line_name = kenv.lines[line_id - 1].line_name

    first_tick = True
    while True:
        yield env.timeout(cadence_s + (_ORDER_EPSILON_S if first_tick else 0.0))
        first_tick = False

        # TRANSPORT-TIME HOOK: time for logistics personnel to physically
        # empty the box and walk the cards over to the batch collector
        # would be `yield env.timeout(empty_walk_time_s)` right here.
        emptied = cb.empty(env.now)
        if not emptied:
            continue

        touched_products: set[str] = set()
        for card in emptied:
            bc.add(card)
            card.record_transition("in_batch_collector", env.now)
            touched_products.add(card.product_type)

        if verbose:
            print(f"  [t={env.now:10.1f}] {line_name}(L{line_id}): "
                  f"Collection Box emptied — {len(emptied)} card(s), "
                  f"products={sorted(touched_products)}")

        for product_type in touched_products:
            while bc.is_ready(product_type):
                released = bc.pop_batch(product_type)
                for c in released:
                    c.record_transition("released_to_chute", env.now)
                chute.push_batch(product_type, released)
                if verbose:
                    print(f"  [t={env.now:10.1f}] {line_name}(L{line_id}): "
                          f"Batch-Size Collector RELEASED {len(released)} "
                          f"card(s) of {product_type!r} -> Kanban Chute "
                          f"(priority={released[0].priority})")
                rt.wake_production(line_id)


# ===========================================================================
# 3. Production trigger  (Kanban Chute -> existing production movement
#    -> back to Supermarket, cards recycled)
# ===========================================================================

class _ChangeoverEventLogProxy:
    """
    Adapter passed to process_logic_sequential_v3.changeover() in place of
    `kenv`, so its `sim_env.log_event(ScheduleEvent(...))` call for the
    "setup" segment appends straight into *this run's* caller-owned
    `event_log` list — the exact same list PackageTracker below writes
    "job" segments into (see run_one_kanban_batch's event_log parameter) —
    instead of going through KanbanSimEnvironment's own internal
    log_event()/event_log, whatever that happens to be wired to.

    Root cause this works around: changeover() is reused unchanged from
    process_logic_sequential_v3.py and only knows how to call
    `sim_env.log_event(...)`. Everywhere else in this module (PackageTracker,
    snapshot_log) is deliberately wired with an explicit, caller-owned list
    passed by reference — but changeover() was being called as
    `changeover(kenv, ...)`, so its ScheduleEvent was landing in whatever
    `kenv.log_event()` does internally, a completely different, never-read
    channel. That's why "setup" segments ran (the delay + line.current_product
    update both happened) but never appeared on the Gantt: nothing in this
    module, and nothing the caller (api_server.py) holds a reference to, was
    ever reading from that channel. Routing log_event() through this proxy
    into the real `event_log` list fixes that at the source, so callers no
    longer need to guess at / merge in kenv's own internal event store.

    Every other attribute changeover() touches (`.env`, `.lines`, `.cfg`) is
    forwarded straight through to the real KanbanSimEnvironment.
    """
    def __init__(self, kenv: KanbanSimEnvironment, event_log: Optional[list]):
        self._kenv = kenv
        self._event_log = event_log

    def __getattr__(self, name):
        # Anything changeover() reads that isn't log_event() itself
        # (env, lines, cfg, ...) — delegate to the real KanbanSimEnvironment.
        return getattr(self._kenv, name)

    def log_event(self, event) -> None:
        if self._event_log is not None:
            self._event_log.append(event)


def run_one_kanban_batch(
    kenv: KanbanSimEnvironment,
    line_id: int,
    current_rec: "OrderRecord | None",
    order_rec: OrderRecord,
    n_workers: int,
    on_finish,
    verbose: bool = True,
    event_log: Optional[list] = None,
):
    """
    SimPy generator - run ONE Kanban-released batch to completion on
    *line_id*, and return the OrderRecord to use as `current_rec` for the
    NEXT batch's changeover lookup.

    This mirrors process_logic_sequential_v3.run_one_order()'s thin
    orchestration (changeover -> resolve station/buffer lists -> launch
    parts -> AllOf) EXACTLY, calling the same underlying primitives —
    the only difference is a caller-supplied `on_finish` instead of a
    hardwired PackageTracker, so a part passing final inspection can
    deposit into the Kanban Supermarket / recycle its card instead of
    (or in addition to, if you also want package-level tracking) ticking
    a package counter.

    The normal "product not feasible on this line" case is already
    filtered out one level up, in production_trigger_process (via
    _make_kanban_order_record), which pushes the batch back onto the
    Kanban Chute for a retry before this function is ever called. What
    remains here is a stricter, should-never-happen guard: the product IS
    feasible per the catalogue, but this SimEnvironment's build didn't
    actually construct one of its named StationResource objects (e.g. an
    inconsistency between the product matrix and the loaded workbook). In
    that case the batch is logged and dropped (current_rec returned
    unchanged) rather than silently lost with no trace — this function
    has no access to the raw KanbanCard list to requeue them, only the
    caller (production_trigger_process) does.

    event_log: caller-owned list[schedule_events.ScheduleEvent], same
    by-reference pattern as snapshot_log. part_lifecycle() itself has no
    event_log parameter — in the push model, run_one_order() gets its
    Gantt segments by wiring a schedule_events.PackageTracker's
    .on_finish as part_lifecycle's `on_finish` callback (see that
    function). This function does the same thing here, composed with
    the caller's own on_finish (Kanban Supermarket deposit + card
    recycle) so both fire on every completed part: the PackageTracker's
    handler groups finished parts into package_size chunks and appends a
    ScheduleEvent to `event_log` each time one closes, exactly like a
    push-model job segment. Optional — omit it and the batch still runs,
    it just won't be represented on a Gantt chart.
    """
    env = kenv.env
    cfg = kenv.cfg
    line = kenv.lines[line_id - 1]
    line_name = line.line_name
    rework_limit = cfg.inspection[line_name].rework_loop_limit

    # ── 1. Changeover (reused, unchanged) ─────────────────────────────────
    # Pass a proxy, not kenv itself, so changeover()'s internal
    # `sim_env.log_event(...)` call for the "setup" ScheduleEvent lands in
    # *this* run's caller-owned `event_log` list (same one PackageTracker
    # writes "job" segments into below) instead of KanbanSimEnvironment's
    # own, disconnected internal event store — see _ChangeoverEventLogProxy.
    yield env.process(
        changeover(
            _ChangeoverEventLogProxy(kenv, event_log),
            line_id, current_rec, order_rec, n_workers, verbose,
        )
    )

    # ── 2. Resolve station / buffer lists for this product's routing ─────
    station_list: list[StationResource] = []
    missing: list[str] = []
    for sname in order_rec.station_sequence:
        sr = line.stations.get(sname)
        if sr is None:
            missing.append(sname)
        else:
            station_list.append(sr)

    if missing or not station_list:
        print(f"  ⚠ {line_name}: cannot resolve routing for Kanban batch "
              f"{order_rec.sachnummer!r} (missing stations: {missing}) — "
              f"re-queuing batch on the Kanban Chute for retry.")
        return current_rec

    active_buf_cfgs = _active_buffer_sequence(
        line_name, [sr.name for sr in station_list], cfg.buffers,
    )
    buf_by_name: dict[str, BufferResource] = {br.name: br for br in line.buffers}
    buffer_list: list[BufferResource] = [
        buf_by_name[bc.buffer_id] for bc in active_buf_cfgs if bc.buffer_id in buf_by_name
    ]

    if verbose:
        print(f"  [t={env.now:10.1f}] {line_name}(L{line_id}): "
              f"Kanban PRODUCTION START — {order_rec.quantity} × "
              f"{order_rec.sachnummer!r}  |  "
              f"route: {' → '.join(order_rec.station_sequence)}")

    # Einsteller command for Lochfilter/DRS sub-assembly — reused,
    # unchanged, fire-and-forget exactly as in run_one_order().
    if order_rec.product_class in (ProductClass.STAB_LOCHFILTER, ProductClass.DRS):
        env.process(
            run_lochfilter_drs_production(kenv, line_id, order_rec, verbose=verbose)
        )

    material_stored_types = _material_stored_types(order_rec.product_class)

    # ── 3. Launch all parts concurrently (reused, unchanged) ──────────────
    # Gantt segments: same mechanism run_one_order() uses (a PackageTracker
    # wired as part_lifecycle's on_finish), composed here with the caller's
    # own on_finish (Kanban Supermarket deposit + card recycle) so both
    # fire on every completed part. part_lifecycle() has no event_log
    # parameter of its own — see this function's docstring.
    pkg_tracker: Optional[PackageTracker] = None
    if event_log is not None:
        pkg_tracker = PackageTracker(
            line_name=line_name,
            line_id=line_id,
            sachnummer=order_rec.sachnummer,
            kunde=order_rec.kunde,
            product_class=order_rec.product_class,
            package_size=cfg.packaging.package_size,
            event_log=event_log,
        )

    def _on_finish(t: float, status: str):
        if pkg_tracker is not None:
            pkg_tracker.on_finish(t, status)
        on_finish(t, status)

    part_processes: list[simpy.Process] = []
    for _ in range(order_rec.quantity):
        part = kenv.create_part(
            product_type  = order_rec.sachnummer,
            product_class = order_rec.product_class,
            line_id       = line_id,
        )
        proc = env.process(
            part_lifecycle(
                kenv, part, station_list, buffer_list, rework_limit, line_name,
                material_stored_types=material_stored_types,
                verbose=False,
                on_finish=_on_finish,
            )
        )
        part_processes.append(proc)

    # ── 4. Wait for the full batch to clear the line ──────────────────────
    if part_processes:
        yield simpy.AllOf(env, part_processes)

    if pkg_tracker is not None:
        pkg_tracker.flush(env.now)

    if verbose:
        passed = sum(1 for p in kenv.parts_out
                     if p.product_type == order_rec.sachnummer and p.status == "passed"
                     and p.line_id == line_id)
        print(f"  [t={env.now:10.1f}] {line_name}(L{line_id}): "
              f"Kanban batch {order_rec.sachnummer!r} COMPLETE "
              f"(cumulative line passed so far: {passed})")

    return order_rec


def _make_kanban_order_record(rt: KanbanRuntime, line_name: str, product_type: str,
                               quantity: int) -> Optional[OrderRecord]:
    """
    Build the (duck-typed but real) OrderRecord run_one_kanban_batch()
    needs, from the product_line_matrix_v3 catalogue. Returns None if the
    product turns out not to be feasible on this line (shouldn't happen —
    build_kanban_environment only creates a (line, product) Supermarket
    for eligible products — but checked defensively).
    """
    info = rt.product_info(product_type)
    line_class = info.lines.get(line_name)
    if line_class is None or not line_class.station_names:
        return None
    return OrderRecord(
        period_label="Kanban",
        sachnummer=product_type,
        kunde=info.kunde,
        product_class=info.product_class,
        quantity=quantity,
        assigned_line=line_name,
        freigabe=line_class.freigabe.value,
        station_sequence=line_class.station_names,
        feasible_lines=info.feasible_lines(),
        note="kanban batch",
    )


def _return_card_to_supermarket(rt: "KanbanRuntime", kenv: KanbanSimEnvironment,
                                 sm: SupermarketResource,
                                 card: KanbanCard, t: float, line_name: str,
                                 line_id: int, verbose: bool):
    """
    Correction #2 — SimPy generator (fire-and-forget via env.process()).

    Deposits one completed package's card onto the Supermarket's Store via
    the REAL Store.put(), not a raw `store.items.append()`. This matters:
    appending straight to the backing list never triggers Store's
    internal wake-up of any pending `get()` — so a Kanban card already
    blocked waiting for this exact product (see _withdraw_one_card's
    `yield sm.store.get()`) would never be woken up by a bypassed
    append, even though `sm.n_available` would look correct in a print.
    Going through `.put()` fixes that: a waiting withdrawal immediately
    picks up the recent production, as intended.

    This is also the upstream-blocking mechanism for a full shelf: `sm.store`
    is a bounded simpy.Store (capacity = KanbanTimingConfig.supermarket_capacity_cards,
    enforced in build_kanban_environment). Once a lane already holds that
    many cards, this `yield sm.store.put(card)` blocks until a withdrawal
    frees up a slot — no manual semaphore/capacity check needed here.
    """
    env = kenv.env
    # TRANSPORT-TIME HOOK: time to physically carry the finished batch+card
    # from Sichtpruefung back to the Supermarket shelf would be
    # `yield env.timeout(return_time_s)` right here, before the put().
    card.record_transition("in_supermarket", t)
    yield sm.store.put(card)
    rt.record_supermarket(line_id, card.product_type, "deposit_batch", sm)
    if verbose:
        print(f"  [t={t:10.1f}] {line_name}(L{line_id}): Supermarket state for "
              f"{card.product_type!r} -> {sm.n_available} package(s) available.")


def production_trigger_process(rt: KanbanRuntime, line_id: int, n_workers: int = N_WORKERS,
                                verbose: bool = True,
                                event_log: Optional[list] = None):
    """
    SimPy generator - one instance per line. Continuously drains that
    line's KanbanChuteResource (highest priority first, FIFO within a
    priority tie), runs each released batch through the existing
    production movement (run_one_kanban_batch), and on every PASSED part
    deposits the finished piece back into the Supermarket, recycling one
    of the batch's own physical KanbanCard objects per whole batch_size
    completed (cards are never recreated — see KanbanCard docstring,
    open question #2 — a brand-new card is only minted as a last-resort
    fallback if the batch's own card list is somehow exhausted first).

    Blocks (via rt.chute_signal) whenever the chute is empty; always
    re-checks the chute before blocking, so it drains everything already
    queued before waiting on the next wake-up token.

    event_log: forwarded unchanged to every run_one_kanban_batch() call
    made from this loop — see that function's docstring.
    """
    kenv = rt.kenv
    env = kenv.env
    chute = kenv.kanban_chute_for(line_id)
    signal = rt.chute_signal[line_id]
    line_name = kenv.lines[line_id - 1].line_name

    current_rec: Optional[OrderRecord] = None

    while True:
        batch = chute.pop_next()
        while batch is None:
            yield signal.get()
            batch = chute.pop_next()

        product_type, cards = batch.product_type, batch.cards
        batch_size = cards[0].batch_size
        quantity = batch_size * len(cards)

        order_rec = _make_kanban_order_record(rt, line_name, product_type, quantity)
        if order_rec is None:
            print(f"  ⚠ {line_name}: {product_type!r} not feasible here — "
                  f"re-queuing batch on the Kanban Chute.")
            chute.push_batch(product_type, cards)
            continue

        # TRANSPORT-TIME HOOK: time to physically move the released
        # batch's material tag from the Kanban Chute to the station
        # (Beladen) would be `yield env.timeout(chute_to_line_time_s)`
        # right here, before recording "in_production".
        for c in cards:
            c.record_transition("in_production", env.now)

        sm: Optional[SupermarketResource] = kenv.supermarket_for(line_id, product_type)
        recycle_queue: list[KanbanCard] = list(cards)

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
                    # Fallback only — should be rare/never: every piece
                    # produced for a released batch has a matching card
                    # already reserved in `cards` above.
                    card = kenv.create_card(_product, _line_id, _sm.batch_size, priority="M")
                if verbose:
                    print(f"  [t={t:10.1f}] {line_name}(L{_line_id}): package of "
                          f"{_sm.batch_size} × {_product!r} COMPLETED — "
                          f"delivering to Supermarket.")
                # Correction #2: launched as its own fire-and-forget SimPy
                # process (was a synchronous `_sm.store.items.append(card)`
                # bypassing Store entirely — see _return_card_to_supermarket
                # docstring for why that was a bug, not just a style choice).
                env.process(
                    _return_card_to_supermarket(rt, kenv, _sm, card, t, line_name,
                                                 _line_id, verbose)
                )

        result = yield from run_one_kanban_batch(
            kenv, line_id, current_rec, order_rec, n_workers, on_finish, verbose,
            event_log=event_log,
        )
        if result is not None:
            current_rec = result


# ===========================================================================
# Correction #3 — end-of-day "Restmenge" report
# ===========================================================================

def print_restmenge_report(kenv: KanbanSimEnvironment, verbose: bool = True) -> dict:
    """
    Print, per (line, product), the count of finished pieces that PASSED
    inspection but hadn't yet accumulated to a full `batch_size` chunk when
    the run ended — i.e. SupermarketResource.pcs_partial (see its docstring:
    a partial pack can never be withdrawn on its own; it carries over and
    is topped up by the next production run's deposit_finished_pcs() calls).

    Enumeration: no new API is needed for this — kenv.supermarkets is
    already dict[line_id -> dict[product_type -> SupermarketResource]], so
    a plain nested iteration covers every (line, product) Supermarket that
    build_kanban_environment() built.

    End-of-day detection: this function itself still just reports
    whatever kenv's Supermarkets look like at the moment it's called —
    it doesn't know or care what "a day" is. For a single call at the
    very end of a run, that's exactly what kanban_runner.py's
    end-of-run dump wants (see this module's top-of-file usage example).
    For an ONCE-PER-DAY report on a multi-day sheet, see
    day_boundary_process() below: it's the "call this once per day
    boundary" driver process this docstring used to describe as future
    work — it now exists and calls this same function every
    rt.day_length_s seconds, labelling each call with the production
    day it ends.

    Returns the same data as a plain dict, keyed
    (line_name, product_type) -> pcs_partial, for a caller that wants it
    for a KPI/export instead of (or in addition to) the printed report.
    """
    restmenge: dict[tuple[str, str], int] = {}
    if verbose:
        print(f"=== Restmenge (partial packages) @ t={kenv.env.now:10.1f} ===")

    any_nonzero = False
    for line in kenv.lines:
        line_id = line.line_id
        lane = kenv.supermarkets.get(line_id, {})
        for product_type, sm in sorted(lane.items()):
            if sm.pcs_partial > 0:
                any_nonzero = True
                restmenge[(line.line_name, product_type)] = sm.pcs_partial
                if verbose:
                    print(f"  {line.line_name}(L{line_id}) {product_type!r}: "
                          f"{sm.pcs_partial}/{sm.batch_size} pcs carrying "
                          f"into next run")

    if verbose:
        if not any_nonzero:
            print("  (none — every product finished on a whole-batch boundary)")
        print("=" * 55)

    return restmenge


# ===========================================================================
# Correction #6 — per-day checkpoint (multi-day support)
# ===========================================================================

# States a KanbanCard passes through between leaving a Supermarket
# ("withdrawn") and landing back on one ("in_supermarket" — deliberately
# excluded here, that's the "at rest" state, not backlog). Mirrors exactly
# the record_transition() call sites in this module (see each site's
# "# TRANSPORT-TIME HOOK" neighbours above for the full list).
_PIPELINE_STATES: frozenset[str] = frozenset({
    "withdrawn", "in_collection_box", "in_batch_collector",
    "released_to_chute", "in_production",
})


def _pipeline_backlog_cards(kenv: KanbanSimEnvironment) -> int:
    """
    Count Kanban cards currently "in flight": already withdrawn from a
    Supermarket, not yet deposited back into one (sitting in the
    collection box, batch collector, Kanban chute, or mid-production).
    This is the "units remaining to be produced" figure for the
    end-of-day carry-over warning — reads straight off
    KanbanCard.transitions (the permanent per-card audit trail already
    relied on by kanban_runner._print_card_cycle_times), no extra
    bookkeeping needed anywhere else in the pipeline.

    A card's LATEST recorded transition tells you where it currently
    sits. A card with NO transitions yet is one of the initial seed
    cards build_kanban_environment() placed straight onto a Supermarket
    (see entities_resources_v4.py) — resting there, not backlog, hence
    the "in_supermarket" default below.

    Deliberately does NOT count scrapped parts: a scrapped part's card
    is still physically in flight (it re-enters the recycle queue via
    the normal on_finish path) rather than vanishing, so it's correctly
    included if and only if its card hasn't made it back to
    "in_supermarket" yet — same rule as everything else here. Per this
    project's scope, "scrapped parts are scrapped, no redoing" governs
    the PART (it never gets reworked into a passing piece), not the
    physical card, which still needs to physically cycle back.
    """
    n = 0
    for card in kenv.card_registry.values():
        state = card.transitions[-1][0] if card.transitions else "in_supermarket"
        if state in _PIPELINE_STATES:
            n += 1
    return n


def day_boundary_process(rt: KanbanRuntime, verbose: bool = True):
    """
    SimPy generator - ONE background process, started once by
    start_kanban_simulation() alongside withdrawal_process() and every
    line's collection_box_emptying_process()/production_trigger_process().
    Ticks every rt.day_length_s seconds (the production-day length — see
    KanbanRuntime / kanban_runner.DAY_LENGTH_S) and, on every tick, takes
    a full "end of day N" checkpoint:

      1. Per-line KPI dump — process_logic_sequential_v3.print_kpi(),
         reused completely unchanged (same call kanban_runner.py already
         makes at end-of-run). It's a CUMULATIVE snapshot (everything
         since t=0), taken once per day so the trend across days is
         visible, not a per-day delta — process_logic_sequential_v3
         owns what print_kpi computes and this module doesn't reach
         into it to diff two snapshots.
      2. Restmenge (partial-pack) report — print_restmenge_report()
         above, likewise reused unchanged and likewise an instantaneous
         snapshot of current Supermarket state, not a per-day delta.
      3. Pipeline-backlog count — _pipeline_backlog_cards() above: the
         "if there are units remaining to be produced, carry them over
         and throw a small warning" requirement. >0 means real work
         (already-withdrawn cards) is still mid-pipeline as this day
         ends. Nothing here pauses, resets, or otherwise interrupts the
         simulation for that — see this module's top-of-file "Card
         movement / transportation time" note and withdrawal_process()'s
         docstring: a part mid-station keeps running straight through
         the boundary exactly like a real line would, and no NEW demand
         appears until the next day's own withdrawal rows fire. So
         "production resumes next day at day_start_hour" isn't something
         this function has to force — it's simply what the withdrawal
         side does on its own once the clock reaches that point; this
         function only reports the fact.

    One dict is appended to rt.daily_log (caller-owned, same
    by-reference pattern as snapshot_log/event_log — see
    start_kanban_simulation) per day, for a caller that wants the
    numbers programmatically (e.g. kanban_runner._print_daily_log_summary)
    instead of / in addition to the printed report:

        {"day_index": int,       # 1, 2, 3, ... (1 = the first FULL day
                                  # that just ended)
         "day_label": str,       # "Day 2 (2026-09-02)" if rt.sim_epoch is
                                  # known, else "Day 2"
         "t": float,             # env.now at this checkpoint
         "backlog_cards": int,   # _pipeline_backlog_cards() result
         "restmenge": dict}      # print_restmenge_report()'s return value

    Runs for as long as the environment does — like
    production_trigger_process, this generator never naturally
    terminates; env.run(until=horizon_s) (see estimate_horizon_s() /
    kanban_runner.py) is what stops it, same safety-net pattern as every
    other long-lived process in this module.
    """
    kenv = rt.kenv
    env = kenv.env
    day_index = 0

    while True:
        yield env.timeout(rt.day_length_s)
        day_index += 1

        if rt.sim_epoch is not None:
            day_date = (rt.sim_epoch + timedelta(seconds=(day_index - 1) * rt.day_length_s)).date()
            day_label = f"Day {day_index} ({day_date.isoformat()})"
        else:
            day_label = f"Day {day_index}"

        if verbose:
            print(f"\n{'='*70}\n  END OF {day_label} — t={env.now:.1f}s "
                  f"({env.now/3600:.2f}h)\n{'='*70}")

        for line in kenv.lines:
            print_kpi(kenv, line.line_id, env.now)

        restmenge = print_restmenge_report(kenv, verbose=verbose)

        backlog = _pipeline_backlog_cards(kenv)
        if backlog > 0:
            print(f"  ⚠ {backlog} Kanban card(s) still in the pull pipeline "
                  f"(withdrawn, not yet back in a Supermarket) as {day_label} "
                  f"ends — carrying over into next day. (Scrapped pieces are "
                  f"not redone — this count is only cards still legitimately "
                  f"in flight, not lost scrap.)")
        elif verbose:
            print(f"  (no backlog — every withdrawn card is back in a "
                  f"Supermarket as {day_label} ends)")

        rt.daily_log.append({
            "day_index": day_index,
            "day_label": day_label,
            "t": env.now,
            "backlog_cards": backlog,
            "restmenge": restmenge,
        })


# ===========================================================================
# Top-level launcher
# ===========================================================================

def start_kanban_simulation(kenv: KanbanSimEnvironment, n_workers: int = N_WORKERS,
                             verbose: bool = True,
                             snapshot_log: Optional[list["SupermarketSnapshot"]] = None,
                             event_log: Optional[list] = None,
                             daily_log: Optional[list[dict]] = None,
                             shortfall_log: Optional[list["ShortfallEvent"]] = None,
                             day_start_hour: int = 6,
                             day_length_s: float = 24 * 3600.0,
                             ) -> KanbanRuntime:
    """
    Wire up and launch the full Kanban loop on every line of `kenv`:
    one withdrawal_process (whole-plant, drives CustomerDemandKanban),
    one day_boundary_process (whole-plant, per-day KPI/Restmenge/backlog
    checkpoint — see that function), plus one
    collection_box_emptying_process and one production_trigger_process
    per line.

    Call this once, right after build_kanban_environment(), then call
    env.run(until=...) yourself (a horizon is recommended as a safety net
    — see kanban_runner.py / estimate_horizon_s() — since
    production_trigger_process blocks indefinitely on a stockout with no
    upstream withdrawal left to wake it, which is correct pull-system
    behaviour but means the generator itself never naturally terminates,
    same as day_boundary_process and withdrawal_process for a multi-day
    sheet).

    snapshot_log: pass a list in (caller-owned, same by-reference pattern
    as schedule_events.ScheduleEvent) to collect every SupermarketSnapshot
    emitted over the run — e.g. for build_line_units_timeseries() later.
    If omitted, KanbanRuntime keeps its own private list internally (the
    run still works, you just have no external handle on it).

    event_log: pass a list in (caller-owned, same by-reference pattern as
    snapshot_log) to collect every schedule_events.ScheduleEvent emitted
    by every line's production — forwarded unchanged into each line's
    production_trigger_process(). Unlike snapshot_log, there's no private
    fallback list here (KanbanRuntime doesn't own event_log); if omitted,
    parts still run, they just never get logged for a Gantt chart.

    daily_log: pass a list in (caller-owned, same by-reference pattern as
    snapshot_log) to collect one dict per production day from
    day_boundary_process(). If omitted, KanbanRuntime keeps its own
    private list internally (recoverable via rt.daily_log).

    shortfall_log: pass a list in (caller-owned, same by-reference
    pattern as snapshot_log) to collect every ShortfallEvent emitted by
    _withdraw_one_card() — one per withdrawal request that found its
    Supermarket empty and had to wait. If omitted, KanbanRuntime keeps
    its own private list internally (recoverable via rt.shortfall_log).

    day_start_hour / day_length_s: the production-day boundary — see
    KanbanRuntime's docstring. Defaults to 06:00 / 24h; edit these at the
    call site (kanban_runner.DAY_START_HOUR / DAY_LENGTH_S) rather than
    here, so this module has no hidden hardcoded shift-time assumption.

    Returns the KanbanRuntime, in case the caller wants to inspect/extend
    the wiring (e.g. to add transport-time processes later) — this is
    also where snapshot_log/daily_log/shortfall_log end up
    (rt.snapshot_log, rt.daily_log, rt.shortfall_log) if you didn't pass
    your own lists in.
    """
    # Correction #4 — double-filling fix.
    #
    # build_kanban_environment() deliberately reuses build_environment()'s
    # exact station/buffer/inventory/chute wiring, which still includes the
    # push model's terminal finished-goods inventory (Inv_nach_HTL). But
    # part_lifecycle() (process_logic_sequential_v3.py, reused HERE
    # completely unchanged, per this module's own docstring) unconditionally
    # does, on every passed part:
    #
    #     if finished_goods_inv is not None:
    #         yield finished_goods_inv.container.put(1)
    #         finished_goods_inv.record_deposit_product(part.product_type, 1)
    #     ...
    #     if on_finish is not None:
    #         on_finish(env.now, "passed")
    #
    # — where finished_goods_inv is resolved purely from kenv.inventories
    # (whichever lane's upstream_station matches the route's last station).
    # In the Kanban model, on_finish (production_trigger_process's closure)
    # ALREADY deposits the same passed piece into the Supermarket via
    # deposit_finished_pcs(). Leaving Inv_nach_HTL wired into kenv.inventories
    # means every passed piece is counted twice: once into the Supermarket,
    # once into Inv_nach_HTL — the double-filling this handover note flags.
    #
    # Fix: rebuild kenv.inventories here, right before the loop starts,
    # dropping any inventory lane that is a terminal sink (downstream_station
    # is None — the config-level trait that distinguishes Inv_nach_HTL from
    # Inv_nach_ECM/Inv_nach_Loch/Inv_nach_DRS, which all feed a real
    # downstream station and must stay). part_lifecycle's own lookup then
    # resolves finished_goods_inv=None and silently skips the deposit — no
    # change to that shared function, and no change needed to
    # entities_resources_v4.py either, since build_kanban_environment()'s
    # output stays the generic "same wiring as push" the rest of its
    # docstring promises; this is purely a Kanban-runtime-time filter.
    kenv.inventories = {
        inv_name: {
            lane_name: inv for lane_name, inv in lanes.items()
            if inv.inventory_cfg.downstream_station is not None
        }
        for inv_name, lanes in kenv.inventories.items()
    }
    kenv.inventories = {
        inv_name: lanes for inv_name, lanes in kenv.inventories.items() if lanes
    }

    env = kenv.env
    rt = KanbanRuntime(kenv, snapshot_log=snapshot_log, daily_log=daily_log,
                        shortfall_log=shortfall_log,
                        day_start_hour=day_start_hour, day_length_s=day_length_s)

    # Correction #5 — seed-state visibility fix.
    #
    # build_kanban_environment() seeds every Supermarket's starting cards
    # by mutating sm.store.items directly (see entities_resources_v4.py),
    # since KanbanRuntime doesn't exist yet at that point in the build —
    # so none of that initial stock was ever captured in snapshot_log.
    # Every stock chart downstream (kanban_events.build_supermarket_
    # timeseries / build_line_units_timeseries) therefore silently started
    # mid-story: the first point it could ever show was whatever the
    # first real withdrawal/deposit had already changed the stock to, not
    # the actual starting quantity (e.g. HTL3 starting at 32 cards).
    #
    # Fix: now that rt exists and nothing else has run yet (env.now == 0),
    # take one "initial" snapshot of every (line, product) Supermarket
    # exactly as build_kanban_environment() left it.
    for line_id, lane in kenv.supermarkets.items():
        for product_type, sm in lane.items():
            rt.record_supermarket(line_id, product_type, "initial", sm)

    env.process(withdrawal_process(rt, verbose=verbose))
    env.process(day_boundary_process(rt, verbose=verbose))
    for line in kenv.lines:
        env.process(collection_box_emptying_process(rt, line.line_id, verbose=verbose))
        env.process(production_trigger_process(rt, line.line_id, n_workers=n_workers,
                                                 verbose=verbose, event_log=event_log))

    return rt
