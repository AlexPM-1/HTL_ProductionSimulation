"""
sim/runner.py
==============
Top-level orchestration: apply_push_policy(), run_mixed(), and
_install_crew_chute_hooks(). This is the one place everything else in
`sim` gets wired together and run.

    sim/resources/*.py   build_kanban_environment, gates, chutes,
                          KanbanSimEnvironment
    sim/produce/*.py     run_one_order, run_one_kanban_batch — reached
                          indirectly via sim.drain.push_turn/pull_turn
    sim/context.py,
    sim/clock.py         RunContext, wall-clock helpers
    sim/fill/*.py        start_kanban_simulation, push_dispatch_process,
                          exotic-slot tracking, push-chute tracking
    sim/drain/*.py       crew_process and the turn-runners
    sim/oee.py           OEELossTracker, oee_daily_process
"""

from __future__ import annotations

import random
from typing import Optional

import simpy

from domain.config import load_config
from domain.constants import (
    DAY_START_HOUR, DAY_LENGTH_S, DRAIN_DAYS, SIM_HORIZON_S,
    PUSH_CHUNK_SIZE, N_WORKERS, SEED,
)
from domain.policy import PushPolicyConfig
from domain.orders import UnassignedOrder
from domain.timeparse import row_due_datetime as _row_due_datetime
from domain.epoch import compute_epoch, estimate_horizon_s

from sim.resources.build import build_kanban_environment
from sim.resources.chute import KanbanChuteResource
from sim.resources.gate import LinePriorityGate

from sim.context import RunContext
from sim.fill.pull.bootstrap import start_kanban_simulation
from sim.fill.push.dispatch import push_dispatch_process
from sim.fill.push.exotic import ExoticSupermarketTracker
from sim.fill.push.chute_tracker import PushChuteTracker
from sim.drain.crew import crew_process

from sim.oee import OEELossTracker, oee_daily_process

from reports.kpi.by_line import print_kpi

from telemetry.records import (
    GateActivityEntry,
    SupermarketOverflowFlag,
    PushDeliveryRecord,
    ExoticSlotSnapshot,
    OEELossDrawEntry,
    ScheduleEvent,
)

# ---------------------------------------------------------------------------
# PushSchedulerContext — plain alias to RunContext, so every
# `ctx: PushSchedulerContext` type hint below resolves to the same
# object as `rt: RunContext` elsewhere. See sim/context.py's docstring
# for RunContext's full role.
# ---------------------------------------------------------------------------
PushSchedulerContext = RunContext


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


def _install_crew_chute_hooks(
    chutes: dict[int, KanbanChuteResource],
    activity_signal: simpy.Store,
) -> None:
    """
    Per-instance monkeypatch, called once before env.run().

    One job now that crew_process() is the ONLY drainer for every
    line's chute: notify `activity_signal` after every successful
    insertion — push_batch() (pull arrivals), push_chunk()/
    push_rush_entry() (push arrivals) — so an idle crew blocked on
    "nothing to do anywhere" wakes up and re-runs Rule 1/2 the instant
    ANY line's chute gains work. One shared signal, because a crew
    cares about ANY line's front changing, not just "its own" line's.

    Idempotent per chute instance (checked via a marker attribute).
    """
    for chute in chutes.values():
        if getattr(chute, "_mixed_crew_hooked", False):
            continue  # already installed on this instance

        _orig_push_batch = chute.push_batch
        _orig_push_chunk = chute.push_chunk
        _orig_push_rush_entry = chute.push_rush_entry

        def _push_batch(product_type, cards, _orig=_orig_push_batch):
            _orig(product_type, cards)
            activity_signal.put(None)

        def _push_chunk(product_type, priority, payload, _orig=_orig_push_chunk):
            _orig(product_type, priority, payload)
            activity_signal.put(None)

        def _push_rush_entry(product_type, payload, _orig=_orig_push_rush_entry):
            _orig(product_type, payload)
            activity_signal.put(None)

        chute.push_batch = _push_batch
        chute.push_chunk = _push_chunk
        chute.push_rush_entry = _push_rush_entry
        chute._mixed_crew_hooked = True


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

def run_mixed(
    excel_path: str,
    setup_times_path: str,
    product_master_path: str,
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
       set from `push_policy` and its insertion methods patched via
       _install_crew_chute_hooks (self-notifying inserts).
    5. start_kanban_simulation(kenv, ...) — registers withdrawal_process,
       day_boundary_process, and per-line collection_box_emptying_process
       (movement 1's pull side). crew_process() (step 6, below) is the
       only thing that ever drains a chute.
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
        docstring). Defaults to PushPolicyConfig() if not given.
        frozen_zone_cards drives every line's chute; the rest drive
        push_dispatch_process() directly.

    IMPORTANT — reads `kenv.kanban_chutes: dict[int, KanbanChuteResource]`,
    exposed by build_kanban_environment() (sim/resources/build.py). Note
    this is a DIFFERENT dict from `kenv.chutes` — that one is the
    raw-material FIFO chute immediately upstream of Beladen, inherited
    unchanged from the push-model SimEnvironment; `kanban_chutes` is the
    per-line, priority-ordered, frozen-zone-aware admission queue this
    module and crew_process() actually share. This module never
    constructs a chute itself (that stays sim/resources/build.py's
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
    kenv.push_ctx, kenv.oee_tracker, kenv.oee_draw_log attached.

    kenv.oee_tracker (OEELossTracker) holds each line's current-day
    CombinedProductionLoss (get_current(line_name)); kenv.oee_draw_log
    (list[OEELossDrawEntry]) is the full day-by-day history of every draw
    for every line — see oee_daily_process()/OEELossTracker's own
    docstrings (sim/oee.py).

    kenv.gate_activity_log (list[GateActivityEntry]) is the time-indexed
    "what was this line's gate doing at any past instant" log — one
    entry per completed pull card (appended by
    sim.drain.pull_turn._run_one_pull_card) or push chunk (appended by
    sim.drain.push_turn.run_push_turn), both funneled through the same
    LinePriorityGate, regardless of which crew ran them. See
    GateActivityEntry / reports.movement.status.production_status_at()
    for the replay convention and the known Rüstzeit-vs-producing
    sub-resolution limitation.
    """
    if push_policy is None:
        push_policy = PushPolicyConfig()

    cfg = load_config(excel_path, setup_times_path, product_master_path)
    # domain.epoch.compute_epoch() is the fixed domain.constants.SIM_START
    # constant — both this module and RunContext.for_kanban()
    # (sim/context.py) derive their epoch from the exact same call, so
    # the sanity check just below can never actually disagree (kept
    # anyway, harmless, in case that invariant is ever broken by a
    # future edit to either module).
    epoch = compute_epoch()

    env = simpy.Environment()
    kenv = build_kanban_environment(env, cfg, seed=seed)

    chutes: Optional[dict[int, KanbanChuteResource]] = getattr(kenv, "kanban_chutes", None)
    if chutes is None:
        raise RuntimeError(
            "kenv.kanban_chutes not found. build_kanban_environment() is "
            "expected to expose {line_id: KanbanChuteResource} under that "
            "name (NOT kenv.chutes — that dict is the raw-material FIFO "
            "chute upstream of Beladen, a different resource entirely) "
            "for run_mixed's frozen zone / push dispatcher to work. "
            "If the real attribute is named differently, update this line."
        )

    if horizon_s is None:
        # domain.epoch.estimate_horizon_s() takes no day_start_hour
        # argument — the epoch it sizes itself against is the fixed
        # domain.constants.SIM_START constant, not one derived from
        # `cfg`/day_start_hour.
        horizon_s = estimate_horizon_s(
            cfg, day_length_s=day_length_s,
            drain_days=drain_days, fallback_horizon_s=SIM_HORIZON_S,
        )
        # Also cover the case where push demand's last due date is later
        # than kanban's last withdrawal — estimate_horizon_s() only looks
        # at CustomerDemandKanban, not CustomerDemand.
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
    # RunContext.notify() / crew_process() for how every insertion and
    # every finished turn wakes an idle crew off this single Store.
    activity_signal: simpy.Store = simpy.Store(env)

    for chute in chutes.values():
        chute.set_frozen_zone_cards(push_policy.frozen_zone_cards)
    # Patch BEFORE start_kanban_simulation registers any process (safe
    # either way — push_batch/push_chunk/push_rush_entry are only
    # resolved when actually called, not at env.process()/registration
    # time — but doing it first keeps the ordering obviously correct).
    # See _install_crew_chute_hooks's docstring: wires the
    # activity_signal notify on every insertion.
    _install_crew_chute_hooks(chutes, activity_signal)

    event_log: list[ScheduleEvent] = []
    # run_one_order() (sim/produce/run_order.py) does NOT take an
    # event_log argument — it builds its PackageTracker from
    # `sim_env.event_log` directly. run_push_turn() (sim/drain/push_turn.py)
    # calls run_one_order() with `kenv` as sim_env, so kenv.event_log must
    # already be THIS shared list before any process (kanban or crew)
    # starts running, or push chunks silently produce no Gantt/
    # ScheduleEvent entries. (Kanban's own side doesn't need this —
    # run_one_kanban_batch takes event_log as an explicit parameter,
    # threaded through directly by run_pull_turn's internal
    # _run_one_pull_card().)
    kenv.event_log = event_log

    rt = start_kanban_simulation(
        kenv, verbose=verbose,
        snapshot_log=snapshot_log, daily_log=daily_log,
        shortfall_log=shortfall_log,
        day_start_hour=day_start_hour, day_length_s=day_length_s,
    )

    # Class-1's pull side (start_kanban_simulation -> RunContext.for_kanban)
    # computes its own epoch internally (domain.epoch.compute_epoch()).
    # Sanity check it against the shared epoch this module computed
    # independently above (which also considers CustomerDemand) — they
    # must agree, or push chunks and kanban withdrawals are on two
    # different clocks. Both calls resolve to the exact same fixed
    # constant (see sim/context.py's docstring), so this can never
    # actually fire in practice — kept anyway, harmless, in case that
    # invariant is ever broken by a future edit.
    if rt.sim_epoch is not None and rt.sim_epoch != epoch:
        print(f"  ⚠ run_mixed: shared epoch {epoch} != RunContext.sim_epoch "
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
    # start_kanban_simulation's initial SupermarketSnapshot: without
    # this, a row-level chart's very first bin would show nothing rather
    # than the (all-empty, at t=0) starting state.
    for line in kenv.lines:
        exotic_snapshot_log.extend(exotic_tracker.snapshot(line.line_name, 0.0))
    chute_tracker = PushChuteTracker()
    # OEE loss (CombinedProductionLoss) — daily per-line draw. Purely
    # additive: an older workbook with no "OEE" sheet parses to an empty
    # oee_distribution (domain.config's _parse_oee_sheet), so
    # oee_daily_process below simply has nothing to draw for any line and
    # oee_tracker.get_current() reads None everywhere, same fallback
    # spirit as the Kanban/Shifts sheets. Attached onto kenv (not passed
    # as a run_one_order() parameter) so run_one_order can read
    # sim_env.oee_tracker.get_current(line_name) directly — see
    # OEELossTracker's own module-level note (sim/oee.py).
    oee_tracker = OEELossTracker(getattr(cfg, "oee_distribution", None) or {})
    oee_draw_log: list[OEELossDrawEntry] = []
    env.process(oee_daily_process(
        kenv, oee_tracker, oee_draw_log, day_length_s, random.Random(seed),
    ))
    kenv.oee_tracker = oee_tracker
    kenv.oee_draw_log = oee_draw_log
    # `rt` (returned by start_kanban_simulation above) already IS the
    # RunContext both push and pull share — fill in the push-side fields
    # directly onto the same object (`ctx` is just a second name for it
    # from here on). Its line_name_to_id (built by RunContext.for_kanban()
    # from the same `kenv.lines`) is already correct for the push side too.
    ctx = rt
    ctx.policy = push_policy
    ctx.gates = gates
    ctx.chutes = chutes
    ctx.activity_signal = activity_signal
    ctx.active_lines = list(cfg.line_names)
    ctx.unassigned_log = push_unassigned_log
    ctx.chunk_size = push_chunk_size
    ctx.exotic_tracker = exotic_tracker
    ctx.overflow_log = supermarket_overflow_log
    ctx.delivery_log = push_delivery_log
    ctx.exotic_snapshot_log = exotic_snapshot_log
    ctx.chute_tracker = chute_tracker
    ctx.gate_activity_log = gate_activity_log
    for row in cfg.demand:
        env.process(push_dispatch_process(ctx, row))
    # Spawned in crew_id order (0, 1, ...) — crew-vs-crew arbitration for
    # simultaneous idle selection relies on this registration order (see
    # select_line_for_crew's docstring, sim/drain/selection.py).
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
