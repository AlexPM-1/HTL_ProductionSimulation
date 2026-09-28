"""
sim/context.py
================
RunContext — the single shared state object for a simulation run,
bundling everything the pull side and push side need instead of
threading it through as a long parameter list. One RunContext wraps one
MixedSimEnvironment (`menv`) and is passed around by every generator
in sim/fill/ and sim/drain/.

Lifecycle
---------
`sim.runner.run_mixed()` builds ONE complete RunContext via
`RunContext.for_mixed(...)`, right after `build_mixed_environment()`
and right after the push-side resources (gates, chutes, activity_signal,
push_policy, the caller-owned logs) are constructed — pull and push both
read the same Excel-derived SimConfig and run on the same shared clock,
so there is no point in the run's lifecycle where one side's wiring is
legitimately "not ready yet" while the other's is. The fully-populated
RunContext is then handed to `sim.fill.pull.bootstrap.start_mixed_simulation()`
(which only *registers processes* on it — it does not construct it) and
to every push-side process/crew.

wall_clock() / is_line_on() / seconds_until_on() below are one-line
wrappers over the plain functions in sim/clock.py, kept as methods so
existing call sites can write `ctx.is_line_on(...)` etc.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

import simpy

from sim.resources.environment import MixedSimEnvironment
from sim.resources.supermarket import SupermarketResource
from sim.resources.chute import ChuteResource
from sim.resources.gate import LinePriorityGate
from domain.products import lookup as _lookup_product
from domain.epoch import compute_epoch
from domain.policy import PushPolicyConfig
from telemetry.records import (
    SupermarketSnapshot, ShortfallEvent, GateActivityEntry,
    ExoticSlotSnapshot, SupermarketOverflowFlag, PushDeliveryRecord,
)

from sim.clock import (
    wall_clock as _wall_clock,
    is_line_on as _is_line_on,
    seconds_until_on as _seconds_until_on,
)

# domain.orders.UnassignedOrder, sim.fill.push.exotic.ExoticSupermarketTracker,
# sim.fill.push.chute_tracker.PushChuteTracker, and PendingPullBatch are only
# used here as type-hint comments, not imported, to avoid a circular import
# (those modules import RunContext from this one).


@dataclass
class RunContext:
    """
    See module docstring for field lifecycle. Every field below is
    populated in one shot by `for_mixed()` — there is no partially-built
    state to worry about once a RunContext exists. `menv` is the only
    field with no default (direct `RunContext(...)` construction is
    still available for tests/one-offs that don't need `for_mixed()`'s
    bookkeeping), but the intended entry point for a real run is
    `RunContext.for_mixed(...)`, called once by `sim.runner.run_mixed()`.
    """

    menv: MixedSimEnvironment

    # --- shared clock ----------------------------------------------------
    epoch: Optional[datetime] = None

    # --- pull-side (kanban) wiring ----------------------------------------
    day_start_hour: int = 6
    day_length_s: float = 24 * 3600.0
    line_name_to_id: dict = field(default_factory=dict)          # str -> int
    snapshot_log: list = field(default_factory=list)              # list[SupermarketSnapshot]
    daily_log: list = field(default_factory=list)                 # list[dict]
    shortfall_log: list = field(default_factory=list)             # list[ShortfallEvent]
    _product_info_cache: dict = field(default_factory=dict, repr=False)
    # (line_id, product_type) -> last n_available seen, used only to
    # derive SupermarketSnapshot.delta_qty automatically — same
    # bookkeeping telemetry.recorder.Recorder keeps, duplicated here
    # because pull-side call sites (sim/fill/pull/*) call
    # ctx.record_supermarket() directly on this RunContext rather than
    # going through a Recorder.
    _last_n_available: dict = field(default_factory=dict, repr=False)

    # --- push-side wiring ---------------------------------------------
    # These carry no dataclass-level default of their own significance
    # (None/empty only exists transiently before for_mixed() assembles
    # everything) — a mixed run always has a push side, so a RunContext
    # produced by for_mixed() always has every one of these set.
    verbose: bool = True
    policy: Optional[PushPolicyConfig] = None
    gates: Optional[dict] = None                                  # int -> LinePriorityGate
    chutes: Optional[dict] = None                                 # int -> ChuteResource
    activity_signal: Optional[simpy.Store] = None
    active_lines: list = field(default_factory=list)              # list[str]
    unassigned_log: Optional[list] = None                         # list[UnassignedOrder]
    push_card_size: int = 0
    exotic_tracker: Optional[object] = None                       # ExoticSupermarketTracker
    overflow_log: Optional[list] = None                           # list[SupermarketOverflowFlag]
    delivery_log: Optional[list] = None                           # list[PushDeliveryRecord]
    exotic_snapshot_log: Optional[list] = None                    # list[ExoticSlotSnapshot]
    chute_tracker: Optional[object] = None                        # PushChuteTracker
    gate_activity_log: Optional[list] = None                      # list[GateActivityEntry]
    pending_batches: dict = field(default_factory=dict)           # int -> PendingPullBatch

    # ----------------------------------------------------------------
    # Construction
    # ----------------------------------------------------------------
    @classmethod
    def for_mixed(
        cls,
        menv: MixedSimEnvironment,
        *,
        push_policy: PushPolicyConfig,
        gates: dict,
        chutes: dict,
        activity_signal: simpy.Store,
        active_lines: list,
        push_card_size: int = 0,
        snapshot_log: Optional[list] = None,
        daily_log: Optional[list] = None,
        shortfall_log: Optional[list] = None,
        unassigned_log: Optional[list] = None,
        overflow_log: Optional[list] = None,
        delivery_log: Optional[list] = None,
        exotic_snapshot_log: Optional[list] = None,
        gate_activity_log: Optional[list] = None,
        exotic_tracker: Optional[object] = None,
        chute_tracker: Optional[object] = None,
        day_start_hour: int = 6,
        day_length_s: float = 24 * 3600.0,
        verbose: bool = True,
    ) -> "RunContext":
        """
        Build a fully-populated RunContext — pull-side and push-side
        fields together, in one call. Called once, by
        `sim.runner.run_mixed()`, right after `build_mixed_environment()`
        and right after the push-side resources (gates/chutes/
        activity_signal/push_policy) are built, and BEFORE
        `sim.fill.pull.bootstrap.start_mixed_simulation()` registers any
        process. There is no intermediate "pull-only" RunContext state —
        every field a pull- or push-side call site might read is set
        here, up front.
        """
        return cls(
            menv=menv,
            # domain.epoch.compute_epoch() is a fixed constant,
            # independent of menv.cfg / day_start_hour, so this is
            # always known up front and never None.
            epoch=compute_epoch(),
            day_start_hour=day_start_hour,
            day_length_s=day_length_s,
            line_name_to_id={line.line_name: line.line_id for line in menv.lines},
            snapshot_log=snapshot_log if snapshot_log is not None else [],
            daily_log=daily_log if daily_log is not None else [],
            shortfall_log=shortfall_log if shortfall_log is not None else [],
            verbose=verbose,
            policy=push_policy,
            gates=gates,
            chutes=chutes,
            activity_signal=activity_signal,
            active_lines=active_lines,
            unassigned_log=unassigned_log if unassigned_log is not None else [],
            push_card_size=push_card_size,
            exotic_tracker=exotic_tracker,
            overflow_log=overflow_log if overflow_log is not None else [],
            delivery_log=delivery_log if delivery_log is not None else [],
            exotic_snapshot_log=exotic_snapshot_log if exotic_snapshot_log is not None else [],
            chute_tracker=chute_tracker,
            gate_activity_log=gate_activity_log if gate_activity_log is not None else [],
        )

    # ----------------------------------------------------------------
    # Aliases used by push-side / pull-side call sites respectively.
    # ----------------------------------------------------------------
    @property
    def name_to_id(self) -> dict:
        """Alias for line_name_to_id, used by sim/fill/push/ call sites."""
        return self.line_name_to_id

    @property
    def sim_epoch(self) -> Optional[datetime]:
        """Alias for epoch, used by sim/fill/pull/ call sites."""
        return self.epoch

    # ----------------------------------------------------------------
    # Pull-side helpers
    # ----------------------------------------------------------------
    def product_info(self, product_number: str):
        info = self._product_info_cache.get(product_number)
        if info is None:
            info = _lookup_product(product_number)
            self._product_info_cache[product_number] = info
        return info

    def record_supermarket(self, line_id: int, product_type: str,
                            event_type: str, sm: SupermarketResource,
                            kanban_card_id: Optional[str] = None) -> None:
        """Append one SupermarketSnapshot reflecting sm's state right now
        (post-event). Called from every mutation site listed on
        SupermarketSnapshot's docstring — never call this speculatively.

        delta_qty is derived here from the last n_available this
        RunContext saw for (line_id, product_type) — None only for that
        pair's very first snapshot ("initial"), since there's nothing
        yet to diff against. Same derivation telemetry.recorder.Recorder
        does; kept in sync here since sim/fill/pull/* call sites call
        ctx.record_supermarket() on this RunContext directly rather than
        through a Recorder. kanban_card_id is optional and passed
        straight through for callers that have one to give."""
        line_name = self.menv.lines[line_id - 1].line_name

        key = (line_id, product_type)
        prev_n_available = self._last_n_available.get(key)
        if prev_n_available is None:
            delta_qty = None
        else:
            delta_qty = sm.n_available - prev_n_available
        self._last_n_available[key] = sm.n_available

        self.snapshot_log.append(
            SupermarketSnapshot(
                t=self.menv.env.now,
                line_id=line_id,
                line_name=line_name,
                product_type=product_type,
                event_type=event_type,
                n_available=sm.n_available,
                pcs_partial=sm.pcs_partial,
                card_size=sm.card_size,
                delta_qty=delta_qty,
                kanban_card_id=kanban_card_id,
            )
        )

    def record_shortfall(self, line_id: int, product_type: str,
                          start_s: float, end_s: float) -> None:
        """Append one ShortfallEvent covering [start_s, end_s) — the span
        a withdrawal request sat blocked with nothing on the shelf. See
        ShortfallEvent's docstring."""
        line_name = self.menv.lines[line_id - 1].line_name
        self.shortfall_log.append(
            ShortfallEvent(
                line_id=line_id,
                line_name=line_name,
                product_type=product_type,
                start_s=start_s,
                end_s=end_s,
            )
        )

    # ----------------------------------------------------------------
    # Push-side helpers
    # ----------------------------------------------------------------
    def line_id(self, line_name: str) -> int:
        return self.line_name_to_id[line_name]

    def notify(self) -> None:
        """Wake every idle crew so it re-runs Rule 1/2 — call after any
        change that could affect which line is most loaded (a chute
        insertion, or a crew finishing a turn)."""
        self.activity_signal.put(None)

    def is_busy(self, line_name: str) -> bool:
        """
        A line counts as "busy" for the normal (non-rush) placement
        search if either something is actively running on it right now,
        or it already has ANY work queued (pull or push —
        total_pending_cards counts both, see ChuteResource) that a
        new order would have to wait behind. Deliberately coarse — "is
        it free right now", not "is it free for the next N minutes" —
        the wait-and-retry loop (rule 3) is what actually handles a line
        staying busy.
        """
        lid = self.line_id(line_name)
        return self.gates[lid].resource.count > 0 or self.chutes[lid].total_pending_cards > 0

    def load(self, line_name: str) -> int:
        """Queue depth in card-units — the "how busy" figure the 12h-rush
        override's least-busy-line comparison ranks candidate lines by."""
        return self.chutes[self.line_id(line_name)].total_pending_cards

    # --- shift on/off — bodies live in sim/clock.py -----------------------
    #
    # Thin wrappers converting sim-time seconds (env.now) to the
    # wall-clock datetime that MixedSimEnvironment.is_line_on() /
    # next_line_on_transition() (sim/resources/environment.py) expect.
    # Inherits that method's "no Shifts sheet loaded -> always on"
    # fallback, so callers never need to special-case a missing calendar.

    def wall_clock(self, t: Optional[float] = None) -> datetime:
        """Sim-time seconds (default: right now) -> shared wall-clock instant."""
        return _wall_clock(self.menv, self.epoch, t)

    def is_line_on(self, line_name: str, t: Optional[float] = None) -> bool:
        """True iff `line_name` is on-shift at sim time `t` (default: now)."""
        return _is_line_on(self.menv, self.epoch, line_name, t)

    def seconds_until_on(self, line_name: str, t: Optional[float] = None) -> Optional[float]:
        """Seconds from sim time `t` (default: now) until `line_name` next
        turns on — see sim.clock.seconds_until_on()'s docstring for the
        exact contract (0.0 if already on, None if it never comes back
        on)."""
        return _seconds_until_on(self.menv, self.epoch, line_name, t)
