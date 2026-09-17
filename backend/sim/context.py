"""
sim/context.py
================
RunContext — the single shared state object for a simulation run,
bundling everything the pull side and push side need instead of
threading it through as a long parameter list. One RunContext wraps one
KanbanSimEnvironment (`kenv`) and is passed around by every generator
in sim/fill/ and sim/drain/.

Lifecycle
---------
1. `sim.fill.pull.bootstrap.start_kanban_simulation()` builds a
   RunContext via `RunContext.for_kanban(...)`, populating the pull-side
   fields (kenv, day_start_hour/day_length_s, epoch, the three
   caller-owned logs, the product-info cache). Push-side fields are left
   at their defaults (None / empty).
2. `sim.runner.run_mixed()` takes that same object and fills in the
   push-side fields (policy, gates, chutes, activity_signal,
   active_lines, unassigned/overflow/delivery/exotic/chute logs) by
   plain attribute assignment.

A kanban-only run (no push side) works fine with the push-side fields
left at their defaults and never touched.

wall_clock() / is_line_on() / seconds_until_on() below are one-line
wrappers over the plain functions in sim/clock.py, kept as methods so
existing call sites can write `ctx.is_line_on(...)` etc.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

import simpy

from sim.resources.environment import KanbanSimEnvironment
from sim.resources.supermarket import SupermarketResource
from sim.resources.chute import KanbanChuteResource
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
    See module docstring for field lifecycle. `kenv` is the only field
    with no default — everything else is either computed by
    `for_kanban()` at construction time (pull-side) or filled in
    afterwards by `sim.runner.run_mixed()` (push-side), so a kanban-only
    caller gets a fully working RunContext with every push-side field
    simply left at None / empty and never touched.
    """

    kenv: KanbanSimEnvironment

    # --- shared clock ----------------------------------------------------
    epoch: Optional[datetime] = None

    # --- pull-side (kanban) wiring — set by for_kanban() ------------------
    day_start_hour: int = 6
    day_length_s: float = 24 * 3600.0
    line_name_to_id: dict = field(default_factory=dict)          # str -> int
    snapshot_log: list = field(default_factory=list)              # list[SupermarketSnapshot]
    daily_log: list = field(default_factory=list)                 # list[dict]
    shortfall_log: list = field(default_factory=list)             # list[ShortfallEvent]
    _product_info_cache: dict = field(default_factory=dict, repr=False)

    # --- push-side wiring — filled in by sim.runner.run_mixed() after
    #     construction; left at these defaults for a kanban-only run that
    #     never touches them. -----------------------------------------
    verbose: bool = True
    policy: Optional[PushPolicyConfig] = None
    gates: Optional[dict] = None                                  # int -> LinePriorityGate
    chutes: Optional[dict] = None                                 # int -> KanbanChuteResource
    activity_signal: Optional[simpy.Store] = None
    active_lines: list = field(default_factory=list)              # list[str]
    unassigned_log: Optional[list] = None                         # list[UnassignedOrder]
    chunk_size: int = 0
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
    def for_kanban(
        cls,
        kenv: KanbanSimEnvironment,
        snapshot_log: Optional[list] = None,
        daily_log: Optional[list] = None,
        shortfall_log: Optional[list] = None,
        day_start_hour: int = 6,
        day_length_s: float = 24 * 3600.0,
        verbose: bool = True,
    ) -> "RunContext":
        """
        Build the pull-side half of a RunContext. Called once, by
        `sim.fill.pull.bootstrap.start_kanban_simulation()`. Push-side
        fields are left at their dataclass defaults (None / empty) and
        are only populated afterwards by `sim.runner.run_mixed()`, for a
        run that actually has a push side.
        """
        ctx = cls(
            kenv=kenv,
            day_start_hour=day_start_hour,
            day_length_s=day_length_s,
            line_name_to_id={line.line_name: line.line_id for line in kenv.lines},
            snapshot_log=snapshot_log if snapshot_log is not None else [],
            daily_log=daily_log if daily_log is not None else [],
            shortfall_log=shortfall_log if shortfall_log is not None else [],
            verbose=verbose,
        )
        # domain.epoch.compute_epoch() is a fixed constant, independent
        # of kenv.cfg / day_start_hour, so this is always known and
        # never None.
        ctx.epoch = compute_epoch()
        return ctx

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
    def product_info(self, sachnummer: str):
        info = self._product_info_cache.get(sachnummer)
        if info is None:
            info = _lookup_product(sachnummer)
            self._product_info_cache[sachnummer] = info
        return info

    def record_supermarket(self, line_id: int, product_type: str,
                            event_type: str, sm: SupermarketResource) -> None:
        """Append one SupermarketSnapshot reflecting sm's state right now
        (post-event). Called from every mutation site listed on
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
        a withdrawal request sat blocked with nothing on the shelf. See
        ShortfallEvent's docstring."""
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
        total_pending_cards counts both, see KanbanChuteResource) that a
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
    # wall-clock datetime that KanbanSimEnvironment.is_line_on() /
    # next_line_on_transition() (sim/resources/environment.py) expect.
    # Inherits that method's "no Shifts sheet loaded -> always on"
    # fallback, so callers never need to special-case a missing calendar.

    def wall_clock(self, t: Optional[float] = None) -> datetime:
        """Sim-time seconds (default: right now) -> shared wall-clock instant."""
        return _wall_clock(self.kenv, self.epoch, t)

    def is_line_on(self, line_name: str, t: Optional[float] = None) -> bool:
        """True iff `line_name` is on-shift at sim time `t` (default: now)."""
        return _is_line_on(self.kenv, self.epoch, line_name, t)

    def seconds_until_on(self, line_name: str, t: Optional[float] = None) -> Optional[float]:
        """Seconds from sim time `t` (default: now) until `line_name` next
        turns on — see sim.clock.seconds_until_on()'s docstring for the
        exact contract (0.0 if already on, None if it never comes back
        on)."""
        return _seconds_until_on(self.kenv, self.epoch, line_name, t)
