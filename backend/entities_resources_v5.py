"""
entities_resources_v4.py
=========================
Step ii – Entities & Resources  (v4 — Kanban extension)
---------------------------------------------------------
Defines the Part entity (the moving object in the simulation) and the
ProductionLine / SimEnvironment classes that hold all SimPy resources
(machines, operators, buffers) for one complete simulation environment.

Nothing in this file runs the simulation; it only *instantiates* the
objects.  The process logic (step iii onward) will import these and add
behaviour.

--- v4 / Kanban additions ---------------------------------------------------
Everything above the "Kanban entities & resources" section is UNCHANGED
from entities_resources_v3.py (Part, StationResource, BufferResource,
InventoryResource, ChuteResource, ProductionLine, SimEnvironment,
build_environment() — the push-model path is untouched, byte-for-byte
identical logic, just re-pointed at config_loader_v5).

New in this file (see the "Kanban entities & resources" section below
for full docstrings):
    KanbanCard              — one physical card, permanent card_id, full
                              (state, timestamp) transition history
    SupermarketResource     — per (line, product) store of ready batches
    CollectionBoxResource   — per line, cards waiting to be emptied
    BatchCollectorResource  — per line, per-product bucket vs. CardsToTrigger
    KanbanChuteResource     — per line, priority-ordered (H>M>L) release queue
    KanbanSimEnvironment    — subclass of SimEnvironment, adds the 4 dicts
                              above + a card registry (open question #6:
                              subclass, not bolted onto SimEnvironment)
    build_kanban_environment() — sibling factory to build_environment();
                              calls build_environment() itself to build
                              the untouched push-model wiring, then layers
                              the Kanban resources on top and seeds them
                              from cfg.kanban_cards / cfg.supermarkets
                              (the "Main runner" rows — see that function's
                              docstring for how multiple physical rows for
                              the same (line, sachnummer) get combined)

Usage
-----
    import simpy
    from config_loader_v5 import load_config
    from entities_resources_v4 import Part, build_environment, build_kanban_environment

    cfg = load_config("ProductionPlanning_v6.xlsx", "HTL_setup_times.xlsx")

    # push model (unchanged):
    env = simpy.Environment()
    sim_env = build_environment(env, cfg, seed=42)
    sim_env.describe()

    # kanban model (new, v4):
    kenv = simpy.Environment()
    ksim = build_kanban_environment(kenv, cfg, seed=42)
"""

from __future__ import annotations

import random
import datetime as _dt
from dataclasses import dataclass, field
from typing import ClassVar, Optional

import simpy

from config_loader_v6 import (
    SimConfig, StationConfig, BufferConfig, InventoryConfig, ChuteConfig,
    SupermarketSlotConfig, ShiftCalendar,
)
from schedule_events import ScheduleEvent
from product_line_matrix_v3 import ProductClass


# ===========================================================================
# Entity – Part
# ===========================================================================

@dataclass
class Part:
    """
    A single coupling blank flowing through the production line.

    Attributes are set at creation and updated as the part progresses
    through stations, inspection, and packaging.
    """

    # ---- identity ----
    part_id: int            # unique sequential number (global counter)
    product_type: str       # e.g. "A", "B", … matches Kundenbedarf column
    product_class: ProductClass

    # ---- line assignment ----
    line_id: int            # which of the n lines this part is assigned to

    # ---- timing ----
    t_created: float        # simulation time when part entered the system
    t_cycle_start: float    = 0.0
    t_beladen_start: float  = 0.0   # time seizing Beladen
    t_beladen_end: float    = 0.0
    t_supfina_start: float  = 0.0
    t_supfina_end: float    = 0.0
    t_waschen_start: float  = 0.0
    t_waschen_end: float    = 0.0
    t_sichtpruefung_start: float  = 0.0
    t_sichtpruefung_end: float    = 0.0
    t_exit: float           = 0.0   # time leaving system (pass / scrap)

    # ---- quality / rework ----
    rework_pass: int        = 0     # number of times sent back to Beladen
    status: str             = "in_progress"
    # status values: "in_progress" | "passed" | "scrapped"

    # ---- packaging ----
    package_id: Optional[int] = None  # assigned when part joins a complete package

    # ---- flags ----
    is_restmenge: bool      = False   # True if this part is a carry-over from previous cycle

    @property
    def total_passes(self) -> int:
        """Number of complete passes through the line (1 = no rework)."""
        return self.rework_pass + 1

    @property
    def cycle_time(self) -> float:
        """Wall-clock residence time from creation to exit."""
        return self.t_exit - self.t_cycle_start if self.t_cycle_start > 0 else 0.0

    def __repr__(self) -> str:
        return (
            f"Part(id={self.part_id}, type={self.product_type}, "
            f"line={self.line_id}, rework={self.rework_pass}, "
            f"status={self.status})"
        )


# ===========================================================================
# Resource wrappers
# ===========================================================================

@dataclass
class StationResource:
    """
    Wraps a SimPy Resource (the machine) for one station on one line.

    One StationResource instance = one physical station on one line.
    Capacity is always 1 (single-server) per the model specification.
    """
    station_cfg: StationConfig          # parameters from SimConfig
    line_id: int                        # which line
    resource: simpy.Resource            # the SimPy resource (capacity=1)

    # Statistics – filled in during simulation
    n_processed: int   = 0
    total_busy_time: float = 0.0

    @property
    def name(self) -> str:
        return self.station_cfg.name

    @property
    def utilisation(self) -> float:
        """ρ = total_busy_time / sim_time  (call after simulation ends)."""
        return self.total_busy_time   # numerator only; caller divides by horizon

    def __repr__(self) -> str:
        return f"StationResource(line={self.line_id}, station={self.name})"


@dataclass
class BufferResource:
    """
    Wraps a SimPy Container for a finite-capacity inter-station buffer.

    SimPy Container is ideal here: put() adds parts, get() removes them.
    The BAS blocking logic (step v) will use these containers.

    One BufferResource instance = one physical buffer on one line.
    """
    buffer_cfg: BufferConfig        # parameters from SimConfig
    line_id: int                    # which line
    container: simpy.Container      # SimPy container (level = current fill)

    # Statistics
    max_observed_fill: int = 0

    @property
    def name(self) -> str:
        return self.buffer_cfg.buffer_id

    @property
    def capacity(self) -> int:
        return self.buffer_cfg.capacity

    @property
    def current_fill(self) -> int:
        return int(self.container.level)

    def __repr__(self) -> str:
        return (
            f"BufferResource(line={self.line_id}, id={self.name}, "
            f"fill={self.current_fill}/{self.capacity})"
        )
# ===========================================================================
# Inventory / FIFO Chute resources  (Lochfilter / DRS decoupling model)
# ===========================================================================

@dataclass
class InventoryResource:
    """
    Wraps a SimPy Container for ONE lane of ONE inventory (one row of the
    "Inventories" sheet with Type == "Inventory") — e.g. Inv_nach_ECM has
    three lanes: "All materials" (feeds Beladen directly, Standard chutes),
    "Lochfitler material" (feeds the Chu_vor_Lochfilter chute), "DRS
    material" (feeds the Chu_vor_DRS chute) — all backed by the same
    virtually-infinite raw-material source, but tracked as separate lanes
    so each chute pulls the right stored_type.

    Inventories are the *slow* stores in the model: either the (virtually
    infinite) raw-material buffer feeding every chute (Inv_nach_ECM), the
    finished-goods stores fed by the Lochfilter / DRS side-stations
    (Inv_nach_Loch, Inv_nach_DRS), or the terminal finished-parts store
    fed by Sichtpruefen (Inv_nach_HTL).

    FIFO chutes pull (get) from these when their trigger fires; the
    Lochfilter/DRS production process pushes (put) into Inv_nach_Loch /
    Inv_nach_DRS as it produces; Sichtpruefen pushes into Inv_nach_HTL.
    """
    inventory_cfg: InventoryConfig      # parameters from SimConfig (one lane)
    container: simpy.Container          # SimPy container (level = current fill)

    # Statistics
    n_withdrawals:   int = 0
    total_withdrawn: int = 0
    n_deposits:      int = 0
    total_deposited: int = 0

    # Per-product piece tally — used by finished-goods inventories
    # (Inv_nach_HTL, fed by Sichtpruefen) to report how many PACKS of
    # each product are physically stored, e.g. 120 packs of 25 units of
    # F00RC00419, 80 packs of 25 units of F00RC00638, etc. Populated via
    # record_deposit_product(); lanes that never call it (raw-material
    # Inv_nach_ECM lanes, generic Inv_nach_Loch / Inv_nach_DRS) simply
    # keep this empty and behave exactly as before.
    product_pieces: dict[str, int] = field(default_factory=dict)

    @property
    def name(self) -> str:
        return self.inventory_cfg.name

    @property
    def stored_type(self) -> str:
        return self.inventory_cfg.stored_type

    @property
    def upstream_station(self) -> Optional[str]:
        return self.inventory_cfg.upstream_station

    @property
    def capacity(self) -> "int | float":
        return float("inf") if self.inventory_cfg.is_unbounded else self.inventory_cfg.capacity

    @property
    def current_fill(self) -> "int | float":
        # int(float('inf')) raises OverflowError — keep it as float for
        # unbounded (is_unbounded) inventories instead of truncating.
        level = self.container.level
        return level if level == float("inf") else int(level)

    @property
    def pack_size(self) -> int:
        """Pieces per pack (from the sheet's PackSize column, e.g. 25)."""
        return self.inventory_cfg.pack_size

    def record_withdrawal(self, qty: int) -> None:
        """Bookkeeping only — caller performs the actual `yield container.get(qty)`."""
        self.n_withdrawals   += 1
        self.total_withdrawn += qty

    def record_deposit(self, qty: int) -> None:
        """Bookkeeping only — caller performs the actual `yield container.put(qty)`."""
        self.n_deposits      += 1
        self.total_deposited += qty

    def record_deposit_product(self, product_type: str, qty: int = 1) -> None:
        """
        Bookkeeping for a finished-goods deposit tied to a specific
        product (e.g. Inv_nach_HTL, fed by Sichtpruefen once a part
        passes final inspection). Updates the aggregate counters exactly
        like record_deposit(), plus the per-product piece tally that
        packs_by_product() turns into whole PackSize packs.
        Caller still performs the actual `yield container.put(qty)`.
        """
        self.record_deposit(qty)
        self.product_pieces[product_type] = self.product_pieces.get(product_type, 0) + qty

    @property
    def packs_by_product(self) -> dict[str, int]:
        """
        dict[product_type -> whole packs currently stored], e.g.
        {"F00RC00419": 120, "F00RC00638": 80, ...} for a 25-pcs PackSize.
        A product with fewer than pack_size pieces accumulated so far
        shows 0 whole packs (its pieces are a partial pack — see
        partial_pack_by_product). Falls back to raw piece counts if
        pack_size is 0/unset on this inventory row.
        """
        if not self.pack_size:
            return dict(self.product_pieces)
        return {p: pieces // self.pack_size for p, pieces in self.product_pieces.items()}

    @property
    def partial_pack_by_product(self) -> dict[str, int]:
        """dict[product_type -> leftover pieces not yet forming a whole pack]."""
        if not self.pack_size:
            return {p: 0 for p in self.product_pieces}
        return {p: pieces % self.pack_size for p, pieces in self.product_pieces.items()}

    def __repr__(self) -> str:
        return (
            f"InventoryResource(name={self.name}, lane={self.stored_type}, "
            f"fill={self.current_fill}/{self.capacity})"
        )


@dataclass
class ChuteResource:
    """
    Wraps a SimPy Container for ONE lane of ONE FIFO chute (one row of
    the "Inventories" sheet with Type == "FIFO_Chute") — e.g.
    Chu_vor_HTL3 has three lanes: Standard, Lochfilter, DRS, each its
    own ChuteResource with its own container.

    A chute is a small, pull-triggered buffer directly upstream of a
    station — NOT a kanban supermarket with a continuous arrival process.
    Replenishment (executed by process_logic_sequential_v3, not here —
    this class only tracks state) is purely LEVEL-triggered:
        Once the chute's own current_fill drops to (or below)
        trigger_amount_left, the caller pulls replenish_qty_pcs
        (= replenish_amount packs x pack_size) units from
        `upstream_inventory` and calls record_refill(qty).
    Withdrawal FROM the chute is driven by the consuming station's own
    throughput (its cycle time), not by any external arrival distribution.
    """
    chute_cfg:            ChuteConfig
    container:             simpy.Container
    upstream_inventory:    Optional[InventoryResource]   # resolved at build time
    refill_lock:           simpy.Resource                # capacity=1 — serializes
    # the "check trigger, then refill" critical section (see
    # process_logic_sequential_v3._draw_from_chute). Without this, multiple
    # parts that all see the chute at/below trigger_amount_left before the
    # first refill completes (get()/put() both yield) would each
    # independently see needs_refill == True and each perform a redundant
    # refill, over-pulling from upstream_inventory.

    # Statistics
    n_refills:         int = 0
    total_refilled:    int = 0
    total_consumed:    int = 0
    max_observed_fill: int = 0

    @property
    def name(self) -> str:
        return self.chute_cfg.name

    @property
    def station(self) -> str:
        return self.chute_cfg.station

    @property
    def stored_type(self) -> str:
        return self.chute_cfg.stored_type

    @property
    def capacity(self) -> int:
        return self.chute_cfg.capacity

    @property
    def trigger_amount_left(self) -> int:
        return self.chute_cfg.trigger_amount_left

    @property
    def pack_size(self) -> int:
        return self.chute_cfg.pack_size

    @property
    def replenish_amount(self) -> int:
        return self.chute_cfg.replenish_amount

    @property
    def replenish_qty_pcs(self) -> int:
        """Total pieces pulled per replenishment = packs x pieces/pack."""
        return self.chute_cfg.replenish_qty_pcs

    @property
    def current_fill(self) -> int:
        return int(self.container.level)

    @property
    def needs_refill(self) -> bool:
        """
        True once the chute's own remaining stock has dropped to (or below)
        trigger_amount_left. Unlike the old kanban model, this is a pure
        fill-level check — there is no "consumed since last refill" counter.
        A freshly-built chute starts at initial_fill (per the sheet, 0 by
        default), which is already <= trigger_amount_left, so the very
        first check will correctly request an initial replenishment.
        """
        return self.trigger_amount_left > 0 and self.current_fill <= self.trigger_amount_left

    def record_consumption(self, qty: int = 1) -> None:
        """Bookkeeping only — caller performs the actual `yield container.get(qty)`."""
        self.total_consumed += qty

    def record_refill(self, qty: int) -> None:
        """Bookkeeping only — caller performs the actual container get/put pair."""
        self.n_refills      += 1
        self.total_refilled += qty
        if self.current_fill > self.max_observed_fill:
            self.max_observed_fill = self.current_fill

    def __repr__(self) -> str:
        return (
            f"ChuteResource(name={self.name}, lane={self.stored_type}, "
            f"fill={self.current_fill}/{self.capacity}, "
            f"trigger_left={self.trigger_amount_left}, "
            f"replenish={self.replenish_amount}pk({self.replenish_qty_pcs}pcs))"
        )




@dataclass
class ProductionLine:
    """
    All SimPy resources for a single production line.

    Attributes
    ----------
    line_id  : 1-based line number
    stations : dict mapping station name → StationResource
               ordered as Beladen → Supfina → Waschen → Sichtpruefung
    buffers  : list of BufferResource in station order
               [Puffer1(Beladen→Supfina), Puffer2(Supfina→Waschen),
                Puffer3(Waschen→Sichtpruefung)]
    current_product : product type currently set up on this line ("" = none)

    Note on shift on/off (v6): this class deliberately does NOT hold a
    live on/off flag or a reference to the shift calendar — "is this line
    on right now" depends on wall-clock time, which this class has no
    notion of (SimEnvironment/KanbanSimEnvironment know env.now, but not
    the wall-clock epoch that maps env.now to a real datetime — that
    lives in mixed_runner.py's PushSchedulerContext). describe() below
    accepts an optional (at, shift_calendar) pair purely for on-demand
    reporting; the actual gating logic (waiting for a line to turn on
    before starting production) belongs in the process/runner layer, not
    here — see SimEnvironment.is_line_on()/next_line_on_transition() for
    the query surface that layer uses.
    """

    line_id: int
    line_name: str                     # canonical name e.g. "HTL3"
    stations: dict[str, StationResource]
    buffers: list[BufferResource]
    current_product: str = ""          # last product type set up on this line

    def describe(
        self,
        at: Optional[_dt.datetime] = None,
        shift_calendar: Optional[ShiftCalendar] = None,
    ) -> None:
        """
        Print this line's static resource layout, as before. If both `at`
        (a wall-clock instant) and `shift_calendar` are supplied, also
        prints that line's on/off status at that instant — purely
        informational, for a debug dump / status snapshot; omitting
        either argument reproduces the exact pre-v6 output.
        """
        status = ""
        if at is not None and shift_calendar is not None:
            on = shift_calendar.is_line_on(self.line_name, at)
            status = f"  [{'ON' if on else 'OFF'} @ {at.strftime('%d.%m.%Y %H:%M:%S')}]"
        print(f"  Line {self.line_id} ({self.line_name}){status}")
        for sr in self.stations.values():
            print(f"    {sr.name:<18} SimPy resource cap={sr.resource.capacity}")
        for br in self.buffers:
            print(f"    {br.name:<36} "
                  f"fill={br.current_fill}/{br.capacity}  "
                  f"protocol={br.buffer_cfg.blocking_protocol}")


# ===========================================================================
# Top-level simulation environment holder
# ===========================================================================

@dataclass
class SimEnvironment:
    """
    Holds the SimPy environment, all production lines, and the global
    output storage (infinite queue of finished parts).

    This is the object passed between all simulation phases.

    Attributes
    ----------
    env       : the SimPy Environment
    cfg       : the validated SimConfig (parameters)
    lines     : list of ProductionLine (indexed 0 … n_lines-1)
    rng       : seeded random.Random instance (use this everywhere so the
                simulation is fully reproducible given the same seed)
    parts_out : list of Part objects that left the system (passed or scrapped)
                → used for KPI calculation
    parts_in_wip : list of Part objects currently in the system
    part_counter : global incrementing ID
    event_log : list of ScheduleEvent (see schedule_events.py) — every
                changeover and finished package, across all lines, in the
                order they occur. Populated by process_logic_sequential_v2
                (changeover() and run_line()'s PackageTracker) as the sim
                runs. Single-threaded/cooperative SimPy means all lines
                can safely append to this one list. Feed it into
                schedule_events.build_gantt_payload() after env.run() to
                get the Gantt-ready payload.
    inventories  : dict[inventory_name → dict[stored_type → InventoryResource]]
                mirrors cfg.inventories structurally (one entry per Excel
                row group); e.g. inventories["Inv_nach_ECM"]["Lochfitler material"].
                Covers Inv_nach_ECM (3 lanes), Inv_nach_Loch, Inv_nach_DRS,
                Inv_nach_HTL, etc.
    chutes : dict[chute_name → dict[stored_type → ChuteResource]]
                mirrors cfg.chutes structurally (one entry per Excel
                row group); e.g. chutes["Chu_vor_HTL3"]["Lochfilter"].
    chutes_by_station : dict[station → dict[stored_type → ChuteResource]]
                convenience index for process logic — the station a
                chute feeds (e.g. "HTL3", "Lochfilter", "DRS") maps
                straight to its lane(s), without needing to know the Excel
                "Name" column.
    """

    env: simpy.Environment
    cfg: SimConfig
    lines: list[ProductionLine]
    rng: random.Random
    parts_out: list[Part]       = field(default_factory=list)
    parts_in_wip: list[Part]    = field(default_factory=list)
    part_counter: int           = 0
    event_log: list[ScheduleEvent] = field(default_factory=list)
    inventories: dict[str, dict[str, InventoryResource]] = field(default_factory=dict)
    chutes: dict[str, dict[str, ChuteResource]] = field(default_factory=dict)
    chutes_by_station: dict[str, dict[str, ChuteResource]] = field(default_factory=dict)

    def chute_for(self, station: str, stored_type: str) -> Optional[ChuteResource]:
        """
        Convenience lookup: the ChuteResource feeding *station* that
        stocks *stored_type* (e.g. chute_for("HTL3", "Lochfilter")).
        Returns None if no such lane exists (e.g. HTL5/HTL6 have no
        Lochfilter/DRS lanes — only "Standard").
        """
        return self.chutes_by_station.get(station, {}).get(stored_type)

    def inventory_for(self, name: str, stored_type: str) -> Optional[InventoryResource]:
        """
        Convenience lookup: the InventoryResource lane named *name* that
        stocks *stored_type* (e.g. inventory_for("Inv_nach_ECM", "DRS material")).
        """
        return self.inventories.get(name, {}).get(stored_type)

    def is_line_on(self, line_name: str, at: _dt.datetime) -> bool:
        """
        v6 shift on/off query — thin passthrough to
        self.cfg.shift_calendar.is_line_on(), given here (rather than
        making every caller reach into self.cfg.shift_calendar directly)
        so this reads the same way as chute_for()/inventory_for() above,
        and so a future caching/logging layer has one call site to change.

        `at` is a real wall-clock datetime, NOT self.env.now (env.now is
        just an elapsed-seconds counter on this object's own SimPy clock;
        converting it to a wall-clock instant requires the shared epoch,
        which is computed and owned by the runner layer — see
        mixed_runner._compute_shared_epoch() / PushSchedulerContext.epoch
        — not by this class). Callers typically pass
        `ctx.epoch + timedelta(seconds=self.env.now)`.

        If no "Shifts" sheet was ever loaded (cfg.shift_calendar.shifts is
        empty), this returns True for every line at every instant — shift
        gating is simply not in effect for that workbook, matching
        ShiftCalendar's own "empty calendar = feature off" contract rather
        than making every line permanently unusable.
        """
        if not self.cfg.shift_calendar.shifts:
            return True
        return self.cfg.shift_calendar.is_line_on(line_name, at)

    def next_line_on_transition(
        self, line_name: str, after: _dt.datetime,
    ) -> Optional[_dt.datetime]:
        """
        v6 shift on/off query — thin passthrough to
        self.cfg.shift_calendar.next_on_transition(); see is_line_on()
        above for why this lives here instead of every caller reaching
        into self.cfg directly. Returns None immediately (rather than
        scanning) if no shift calendar is configured at all, matching
        is_line_on()'s "empty calendar = feature off" convention — there
        is no future "on transition" to find if the line is always
        considered on.
        """
        if not self.cfg.shift_calendar.shifts:
            return None
        return self.cfg.shift_calendar.next_on_transition(line_name, after)

    def next_part_id(self) -> int:
        """Thread-safe (single-thread SimPy) unique part ID."""
        self.part_counter += 1
        return self.part_counter

    def create_part(
        self,
        product_type: str,
        product_class: ProductClass,
        line_id: int,
        is_restmenge: bool = False,
    ) -> Part:
        """
        Factory method: create a new Part entity, register it as WIP,
        and return it.
        """
        p = Part(
            part_id       = self.next_part_id(),
            product_type  = product_type,
            product_class = product_class,
            line_id       = line_id,
            t_created     = self.env.now,
            is_restmenge  = is_restmenge,
        )
        self.parts_in_wip.append(p)
        return p

    def finish_part(self, part: Part, status: str) -> None:
        """
        Move a part from WIP to the finished list.
        status must be 'passed' or 'scrapped'.
        """
        assert status in ("passed", "scrapped"), f"Unknown status: {status}"
        part.status  = status
        part.t_exit  = round(self.env.now,2)
        if part in self.parts_in_wip:
            self.parts_in_wip.remove(part)
        self.parts_out.append(part)

    def log_event(self, event: ScheduleEvent) -> None:
        """
        Append a ScheduleEvent (a changeover or a finished package) to the
        shared event log. Mirrors create_part()/finish_part() as the
        canonical way process logic reports what happened, rather than
        callers threading their own list through run_line()/changeover().
        """
        self.event_log.append(event)

    def describe(self, at: Optional[_dt.datetime] = None) -> None:
        print("=== SimEnvironment ===")
        print(f"SimPy time     : {self.env.now}")
        print(f"Lines          : {len(self.lines)}")
        print(f"Parts created  : {self.part_counter}")
        print(f"Parts out      : {len(self.parts_out)}")
        print(f"Parts in WIP   : {len(self.parts_in_wip)}")
        for line in self.lines:
            line.describe(at=at, shift_calendar=self.cfg.shift_calendar if at is not None else None)
        if self.inventories:
            print("  --- Inventories ---")
            for lanes in self.inventories.values():
                for inv in lanes.values():
                    print(f"    {inv}")
                    if inv.product_pieces:
                        for product, packs in inv.packs_by_product.items():
                            leftover = inv.partial_pack_by_product[product]
                            extra = f" + {leftover} pcs" if leftover else ""
                            print(f"        {product}: {packs} packs x {inv.pack_size}{extra}")
        if self.chutes:
            print("  --- FIFO Chutes ---")
            for lanes in self.chutes.values():
                for ch in lanes.values():
                    print(f"    {ch}  <- {ch.upstream_inventory.name if ch.upstream_inventory else 'NONE'}")
        print("=====================")


# ===========================================================================
# Factory function – build_environment
# ===========================================================================

def build_environment(
    env: simpy.Environment,
    cfg: SimConfig,
    seed: int = 42,
) -> SimEnvironment:
    """
    Instantiate all SimPy resources (machines + buffers) for every line
    and return a fully initialised SimEnvironment.

    Parameters
    ----------
    env  : a fresh simpy.Environment()
    cfg  : validated SimConfig from config_loader.load_config()
    seed : random seed for the RNG (int)

    Returns
    -------
    SimEnvironment  — ready for process logic to be attached (step iii+)
    """
    rng = random.Random(seed)
    lines: list[ProductionLine] = []

    for line_idx, line_name in enumerate(cfg.line_names):
        line_id = line_idx + 1  # 1-based

        # ---- Stations ----
        station_resources: dict[str, StationResource] = {}
        for station_cfg in cfg.stations.get(line_name, []):
            resource = simpy.Resource(env, capacity=1)
            sr = StationResource(
                station_cfg = station_cfg,
                line_id     = line_id,
                resource    = resource,
            )
            station_resources[station_cfg.name] = sr

        # ---- Buffers ----
        buffer_resources: list[BufferResource] = []
        for buf_cfg in cfg.buffers.get(line_name, []):
            # SimPy Container: capacity = K, initial level = initial_fill
            container = simpy.Container(
                env,
                capacity = buf_cfg.capacity,
                init     = buf_cfg.initial_fill,
            )
            br = BufferResource(
                buffer_cfg = buf_cfg,
                line_id    = line_id,
                container  = container,
            )
            buffer_resources.append(br)

        line = ProductionLine(
            line_id         = line_id,
            line_name       = line_name,
            stations        = station_resources,
            buffers         = buffer_resources,
            current_product = "",
        )
        lines.append(line)

    # ---- Inventories ("Inventories" sheet, Type == Inventory) ----
    # Each named inventory may have several lanes (one per Stored_type),
    # e.g. Inv_nach_ECM has "All materials" / "Lochfitler material" /
    # "DRS material" — all backed by the same virtually-infinite raw
    # source, tracked as independent containers so chutes can pull the
    # right stored_type.
    inventories: dict[str, dict[str, InventoryResource]] = {}
    all_inventory_lanes: list[InventoryResource] = []
    for inv_name, lanes in cfg.inventories.items():
        lane_dict: dict[str, InventoryResource] = {}
        for inv_cfg in lanes:
            if inv_cfg.is_unbounded:
                # capacity=inf so put() never blocks a sink-type inventory
                # (e.g. Inv_nach_HTL). init stays FINITE (the literal
                # initial_fill from the sheet, e.g. 1000000) — NOT inf:
                # SimPy's Container._do_put checks
                # `capacity - level >= amount`, and inf - inf is nan,
                # which is never >= anything, so an inf/inf container
                # deadlocks on the very first put(). A source-type
                # inventory (e.g. Inv_nach_ECM) just keeps its large
                # literal initial_fill as "virtually infinite" supply.
                container = simpy.Container(env, capacity=float("inf"), init=inv_cfg.initial_fill)
            else:
                container = simpy.Container(env, capacity=inv_cfg.capacity, init=inv_cfg.initial_fill)
            inv_res = InventoryResource(inventory_cfg=inv_cfg, container=container)
            lane_dict[inv_cfg.stored_type] = inv_res
            all_inventory_lanes.append(inv_res)
        inventories[inv_name] = lane_dict

    # ---- FIFO Chutes ("Inventories" sheet, Type == FIFO_Chute) ----
    # Each chute lane must resolve which InventoryResource lane it draws
    # from on a level-triggered pull. Matching is done in two steps:
    #
    #   1. Finished-goods match: an inventory lane whose `upstream_station`
    #      (the station that FILLS it, e.g. "Lochfilter"/"DRS") equals the
    #      chute lane's `stored_type` (e.g. "Lochfilter"/"DRS") — i.e. the
    #      HTL3 chute's Lochfilter lane draws from Inv_nach_Loch, the
    #      inventory that the Lochfilter station itself fills.
    #
    #   2. Raw-material match: a "source" inventory lane (upstream_station
    #      is None — e.g. any of Inv_nach_ECM's 3 lanes) whose own
    #      `stored_type` matches the chute lane's `stored_type` exactly
    #      (e.g. Chu_vor_Lochfilter's "Lochfitler material" lane <->
    #      Inv_nach_ECM's "Lochfitler material" lane). A chute lane with no
    #      exact stored_type match among source lanes (e.g. any "Standard"
    #      lane) falls back to the general "All materials" source lane —
    #      the raw-material store at the head of the whole flow.
    def _resolve_upstream_inventory(chute_cfg: "ChuteConfig") -> Optional[InventoryResource]:
        target = chute_cfg.stored_type.strip().lower()

        for inv in all_inventory_lanes:
            up = inv.upstream_station
            if up and up.strip().lower() == target:
                return inv

        source_lanes = [inv for inv in all_inventory_lanes if inv.upstream_station is None]
        for inv in source_lanes:
            if inv.stored_type.strip().lower() == target:
                return inv

        generic = [inv for inv in source_lanes if "all" in inv.stored_type.strip().lower()]
        if generic:
            return generic[0]
        return source_lanes[0] if source_lanes else None

    chutes: dict[str, dict[str, ChuteResource]] = {}
    chutes_by_station: dict[str, dict[str, ChuteResource]] = {}
    for chute_name, lanes in cfg.chutes.items():
        lane_dict: dict[str, ChuteResource] = {}
        for chute_cfg in lanes:
            container = simpy.Container(env, capacity=chute_cfg.capacity, init=chute_cfg.initial_fill)
            ch = ChuteResource(
                chute_cfg           = chute_cfg,
                container           = container,
                upstream_inventory  = _resolve_upstream_inventory(chute_cfg),
                refill_lock         = simpy.Resource(env, capacity=1),
            )
            lane_dict[chute_cfg.stored_type] = ch
            chutes_by_station.setdefault(chute_cfg.station, {})[chute_cfg.stored_type] = ch
        chutes[chute_name] = lane_dict

    return SimEnvironment(
        env   = env,
        cfg   = cfg,
        lines = lines,
        rng   = rng,
        inventories       = inventories,
        chutes            = chutes,
        chutes_by_station = chutes_by_station,
    )


# ===========================================================================
# Kanban entities & resources  (v4 — new, additive)
# ===========================================================================
#
# Everything below is new for the Kanban pull-system (see the HTL Kanban
# Pull-System handoff notes, esp. §3, §5.2, §7.2, and the resolved open
# questions in §8). Nothing above this line is touched by any of it — the
# push-model path (Part / StationResource / BufferResource /
# InventoryResource / ChuteResource / ProductionLine / SimEnvironment /
# build_environment()) is reused completely unchanged.
#
# Design decisions baked in here (see §8 of the handoff notes for the
# full resolution of each open question):
#   Q1 (stockout behavior)   : SupermarketResource.store is a simpy.Store;
#                              a withdrawal that finds it empty naturally
#                              blocks on `yield store.get()` until
#                              production deposits a batch — no separate
#                              stockout/skip branch needed.
#   Q2 (card identity)       : KanbanCard.card_id is permanent, assigned
#                              once by KanbanSimEnvironment.create_card()
#                              and reused every loop; the full transition
#                              history lives on the card itself.
#   Q3 (partial packs)       : SupermarketResource.pcs_partial accumulates
#                              leftover pieces; only a *whole* batch_size
#                              chunk ever becomes a card in `store` — a
#                              partial pack is never withdrawable.
#   Q5 (independent process) : NOT implemented in this file, but this file
#                              is built so the future kanban process-logic
#                              module never needs to import anything from
#                              process_logic_sequential_v3.py's order-based
#                              functions — SupermarketResource / KanbanCard
#                              etc. stand entirely on their own.
#   Q6 (subclass)            : KanbanSimEnvironment(SimEnvironment) — see
#                              class docstring below.
#   Q7 (Excel-driven timing) : cfg.kanban_timing (KanbanTimingConfig) is
#                              threaded through unchanged from config_loader_v5;
#                              nothing here hardcodes the 15/30-min cadence.


@dataclass
class KanbanCard:
    """
    A physical Kanban card — one per circulating batch-slot for a given
    (line, product). Per handoff-notes open question #2, card_id is
    PERMANENT: the same KanbanCard instance is reused every loop
    (in_supermarket -> withdrawn -> in_collection_box ->
    in_batch_collector -> released_to_chute -> in_production ->
    in_supermarket), never recreated. That makes `transitions` the card's
    complete lifetime history, so cycle-time-per-card is a simple diff
    between two of its own timestamps (e.g. successive 'withdrawn'
    entries, or 'withdrawn' -> the next 'in_supermarket').

    Attributes
    ----------
    card_id      : permanent unique identifier, assigned by
                   KanbanSimEnvironment.create_card()
    product_type : sachnummer this card is dedicated to for its whole
                   life (no product-switching on a card in this design)
    line_id      : 1-based line this card circulates on
    batch_size   : pieces represented by this card (from KanbanCardConfig
                   for this product — fixed, so fixed per card)
    priority     : "H" | "M" | "L" — (re)set at each withdrawal from the
                   CustomerDemandKanban row that triggered it; carried
                   through collection box / batch collector / chute so
                   KanbanChuteResource can order releases on it
    state        : current position in the card state machine (handoff
                   notes §5.2); one of the six state-machine values above
    transitions  : list of (state, sim_time) tuples, one appended per
                   record_transition() call — the full audit trail
    """
    card_id: int
    product_type: str
    line_id: int
    batch_size: int
    priority: str = "M"
    state: str = "in_supermarket"
    transitions: list[tuple[str, float]] = field(default_factory=list)

    def record_transition(self, new_state: str, t: float) -> None:
        """Advance the card's state machine and timestamp the move."""
        self.state = new_state
        self.transitions.append((new_state, round(t, 3)))

    def __repr__(self) -> str:
        return (
            f"KanbanCard(id={self.card_id}, product={self.product_type}, "
            f"line={self.line_id}, state={self.state}, priority={self.priority})"
        )


@dataclass
class SupermarketResource:
    """
    Ready-batch store for ONE (line, product) pair — the "Supermarket" box
    at the head of the Kanban loop (handoff notes §5.1).

    `store` holds one KanbanCard per whole, ready batch of `batch_size`
    finished parts, card still attached. It is a plain simpy.Store
    (unbounded capacity) rather than a Container, precisely so a
    withdrawal that finds it empty can `yield store.get()` and block
    until production deposits the next batch — this is the entire
    resolution of open question #1 (stockout behavior): no separate
    "stockout" branch is needed, the withdrawing process simply waits.

    `pcs_partial` tracks leftover finished pieces that haven't yet
    accumulated to a full `batch_size` chunk (open question #3: a
    partial pack can NEVER be withdrawn on its own — only
    deposit_finished_pcs() converting it into one or more whole cards via
    `store` makes it available). This mirrors
    InventoryResource.partial_pack_by_product's bookkeeping-only style
    elsewhere in this module: the caller still performs the actual
    `yield store.get()` / card creation; this class only does the
    piece-counting arithmetic and simple stats.
    """
    line_id: int
    product_type: str
    batch_size: int
    store: simpy.Store
    capacity: int = field(default=25)
    pcs_partial: int = 0
    is_exotic_routed: bool = False
    """
    True when this product has NO dedicated "Main runner" row on this line
    in the "Supermarkets" sheet, but the line DOES have "Exotic" rows —
    i.e. this is a deliberately-exotic product per the current
    Supermarkets layout (not a missing/incomplete-workbook case). The
    `store`/`capacity` here are still a synthetic fallback
    (kanban_timing.supermarket_capacity_cards, zero seed) because this
    class always needs *some* Store to hand kanban_process_logic a place
    to withdraw from; the real physical storage for this product's cards
    is one of the line's shared "Exotic" slots, tracked separately by
    mixed_runner.ExoticSupermarketTracker. Frontend/reporting code should
    use this flag to render such lanes under the line's Exotic pool
    rather than as their own dedicated Main-runner row.
    """
    n_withdrawals: int = 0
    n_deposits: int = 0

    @property
    def n_available(self) -> int:
        """Whole batches (cards) currently sitting ready in the supermarket."""
        return len(self.store.items)

    @property
    def is_stocked(self) -> bool:
        return self.n_available > 0

    @property
    def is_full(self) -> bool:
        """True once the lane holds `capacity` whole batches. `store` is a
        bounded simpy.Store (capacity=self.capacity, set in
        build_kanban_environment) so store.put() already blocks on its own
        once this is True — this property is for reporting/pre-checks
        only, not itself an enforcement mechanism."""
        return self.n_available >= self.capacity

    def record_withdrawal(self) -> None:
        """Bookkeeping only — caller performs the actual `yield store.get()`."""
        self.n_withdrawals += 1

    def deposit_finished_pcs(self, qty: int) -> int:
        """
        Accumulate `qty` freshly finished pieces of this product into
        pcs_partial and return how many WHOLE batch_size chunks that
        completes. The caller (kanban process logic) is responsible for
        creating/reusing exactly that many KanbanCard objects — flipping
        each to 'in_supermarket' via record_transition() — and pushing
        them into `store` (e.g. `store.items.append(card)` then, if
        inside a running process, `yield store.put(card)` semantics are
        not required since items are just appended directly, mirroring
        how build_kanban_environment seeds initial cards below).
        Any leftover < batch_size stays in pcs_partial, unwithdrawable,
        per open question #3.
        """
        self.pcs_partial += qty
        n_whole_batches, self.pcs_partial = divmod(self.pcs_partial, self.batch_size)
        if n_whole_batches:
            self.n_deposits += n_whole_batches
        return n_whole_batches

    def __repr__(self) -> str:
        return (
            f"SupermarketResource(line={self.line_id}, product={self.product_type}, "
            f"batches_ready={self.n_available}/{self.capacity}, "
            f"partial_pcs={self.pcs_partial}/{self.batch_size})"
        )


@dataclass
class CollectionBoxResource:
    """
    Per-line box where withdrawn cards simply accumulate as they arrive
    (handoff notes §5.1) until the next periodic emptying (cadence =
    cfg.kanban_timing.collection_box_emptying_min, default 30 min —
    resolves open question #7, no hardcoded interval in this class).

    Emptying is driven by kanban_process_logic_v1's
    collection_box_emptying_process, which calls empty() and redistributes
    the returned cards into the line's BatchCollectorResource.
    """
    line_id: int
    cards: list[KanbanCard] = field(default_factory=list)
    last_emptied_at: float = 0.0

    def add(self, card: KanbanCard) -> None:
        self.cards.append(card)

    def empty(self, t: float) -> list[KanbanCard]:
        """Remove and return every card currently in the box, in arrival order."""
        emptied, self.cards = self.cards, []
        self.last_emptied_at = t
        return emptied

    @property
    def n_waiting(self) -> int:
        return len(self.cards)

    def __repr__(self) -> str:
        return f"CollectionBoxResource(line={self.line_id}, waiting={self.n_waiting})"


@dataclass
class BatchCollectorResource:
    """
    Per-line, per-product accumulation buckets that sit between the
    Collection Box and the Kanban Chute (handoff notes §5.1). A bucket's
    cards are released together, as one batch, once its count reaches
    that product's CardsToTrigger threshold (from cfg.kanban_cards —
    resolved as a per-PRODUCT setting, not per-line, per §5.5 of the
    handoff notes).

    `cards_to_trigger` is shared (read-only) across all 3 lines' collector
    instances — it's the same {sachnummer: threshold} dict built once in
    build_kanban_environment() from cfg.kanban_cards.
    """
    line_id: int
    cards_to_trigger: dict[str, int]
    buckets: dict[str, list[KanbanCard]] = field(default_factory=dict)

    def add(self, card: KanbanCard) -> None:
        self.buckets.setdefault(card.product_type, []).append(card)

    def is_ready(self, product_type: str) -> bool:
        """True once this product's bucket has reached its CardsToTrigger."""
        threshold = self.cards_to_trigger.get(product_type)
        if not threshold:
            return False
        return len(self.buckets.get(product_type, [])) >= threshold

    def pop_batch(self, product_type: str) -> list[KanbanCard]:
        """
        Pop exactly `cards_to_trigger[product_type]` cards off the front
        of that product's bucket (FIFO) and return them, leaving any
        surplus behind for the next trigger. Caller (kanban process
        logic) should check is_ready() first; popping from a bucket that
        hasn't reached threshold yet is allowed but returns fewer cards
        than the threshold, which the caller should not treat as a
        valid release.
        """
        threshold = self.cards_to_trigger.get(product_type, 0)
        bucket = self.buckets.get(product_type, [])
        released, remaining = bucket[:threshold], bucket[threshold:]
        self.buckets[product_type] = remaining
        return released

    def __repr__(self) -> str:
        counts = {p: len(c) for p, c in self.buckets.items() if c}
        return f"BatchCollectorResource(line={self.line_id}, buckets={counts})"


@dataclass
class ChuteEntry:
    """
    One unit of work waiting for admission to a line's stations — either
    a released Kanban batch (class-1/pull, from
    kanban_process_logic_v1.check_and_release_batch()) or a push chunk
    (class-2, from mixed_runner's push dispatcher). Both classes now
    share ONE queue per line (see KanbanChuteResource) because the
    frozen zone (mixed_runner.PushPolicyConfig.frozen_zone_cards)
    freezes the front of that ONE combined queue — a rushed push chunk
    needs to see, and insert itself right before, the exact same
    boundary the pull side's releases are subject to.

    Attributes
    ----------
    sim_class    : "pull" | "push"
    product_type : sachnummer
    n_cards      : how many 200pcs/30min "card units" this entry is
                   worth — the shared sizing unit the frozen zone counts
                   in. For a pull entry this is len(cards) (a released
                   batch can bundle several cards at once, per
                   CardsToTrigger). For a push entry this is always 1 by
                   construction — push chunks are pre-sliced to
                   <= PUSH_CHUNK_SIZE, i.e. exactly one card.
    cards        : the KanbanCard objects (pull entries only; None for push)
    payload      : opaque handle for push entries — mixed_runner's own
                   chunk/order object, round-tripped without this module
                   needing to import dispatch_entities.OrderRecord (keeps
                   entities_resources_v4.py free of any push-specific
                   type dependency, same separation kanban_process_logic.py
                   already keeps). None for pull entries.
    """
    sim_class: str
    product_type: str
    n_cards: int
    cards: Optional[list[KanbanCard]] = None
    payload: Optional[object] = None


@dataclass
class KanbanChuteResource:
    """
    Per-line priority-ordered admission queue — shared by BOTH class-1
    (Kanban, via push_batch()) and class-2 (push, via push_chunk() /
    push_rush_entry()) production. Populated on the pull side by
    kanban_process_logic_v1.check_and_release_batch() once a product's
    BatchCollectorResource bucket reaches its CardsToTrigger threshold,
    and on the push side by mixed_runner's rolling push dispatcher;
    drained by the line's production-trigger process, which pops the
    highest-priority pending entry (respecting the frozen zone — see
    below), resolves the station_list, and drives it through the
    existing process_part_at_station / part_lifecycle chain.

    NOT the same thing as ChuteResource (the raw-material FIFO chute
    immediately upstream of Beladen) — unchanged, reused as-is once
    production of a popped entry actually starts. This class only
    decides WHAT starts next.

    --- v6 change: frozen zone + shared push/pull queue -------------------
    Ordering used to be priority H before M before L, FIFO within a tier,
    with the WHOLE list re-sorted on every push_batch() call. That still
    holds for the MOVABLE part of the queue, but the front
    `frozen_zone_cards` worth of entries (summed n_cards, from the
    current front) are now FROZEN:

      - pop_next() still drains them first, in their existing order —
        freezing doesn't delay them, it protects them.
      - No insertion (push_batch / push_chunk / push_rush_entry) may
        land at or before the frozen boundary, REGARDLESS of priority —
        a same-tick "H" pull release can no longer resort itself ahead
        of an already-frozen entry the way the old single-list-sort
        design would have allowed. This is enforced structurally: every
        insertion recomputes the frozen boundary, splits `_entries` into
        an untouched frozen prefix + a movable tail, inserts/sorts only
        within the tail, then recombines — the frozen prefix's slice is
        never part of any sort() call.
      - `frozen_zone_cards` is a plain mutable field (see
        set_frozen_zone_cards()) — deliberately NOT sourced from
        mixed_runner.PushPolicyConfig directly (this module doesn't
        import mixed_runner, to avoid a dependency cycle); mixed_runner
        is expected to call set_frozen_zone_cards() on each line's chute
        once at startup and again whenever the policy is edited (e.g.
        from a frontend). Defaults to 0 (no freezing) until set, so
        class-1-only callers (kanban_runner.py, no mixed_runner in the
        loop at all) are unaffected.

    push_rush_entry() implements the "12h-before-due-date: force-assign
    to least-busy line, stick right before the frozen zone" rule —
    inserting AT the frozen boundary (not through the normal rank/seq
    sort), so it becomes the very next thing this line runs once
    whatever's currently frozen finishes, without disturbing the frozen
    prefix or needing to out-rank it. See that method's docstring for
    the ordering behaviour when multiple rush entries land close together.

    pop_next() now returns a ChuteEntry (not a (product_type, cards)
    tuple) — callers that unpack it as a tuple need a one-line update:
    `entry = chute.pop_next(); product_type, cards = entry.product_type, entry.cards`.

    Naming: kept as "KanbanChuteResource" rather than renamed (e.g. to
    LineChuteResource) to limit the blast radius of this change — every
    existing construction site / class-1 reference keeps working
    unchanged. Consider a rename in a dedicated follow-up pass once
    push's own call sites (mixed_runner) are wired up, since the name no
    longer accurately reflects that push uses this too.
    """
    line_id: int
    frozen_zone_cards: int = 0
    _entries: list[tuple[int, int, ChuteEntry]] = field(default_factory=list)
    _sequence: int = field(default=0, init=False, repr=False)

    _PRIORITY_RANK: ClassVar[dict[str, int]] = {"H": 0, "M": 1, "L": 2}

    # ------------------------------------------------------------------
    # Frozen-zone configuration & accounting
    # ------------------------------------------------------------------

    def set_frozen_zone_cards(self, n_cards: int) -> None:
        """
        Update the frozen-zone size in place. Safe to call at any time,
        including mid-simulation (e.g. a frontend edit) — the very next
        insertion/pop call picks up the new value; nothing is
        retroactively frozen or unfrozen for entries already popped.
        """
        if n_cards < 0:
            raise ValueError("frozen_zone_cards must be >= 0")
        self.frozen_zone_cards = n_cards

    def first_unfrozen_index(self) -> int:
        """
        Index of the first entry NOT in the frozen zone, walking from the
        front of the current pop order — i.e. the smallest k such that
        the first k entries' n_cards sum to >= frozen_zone_cards. Equals
        len(_entries) if the whole queue is smaller than the frozen zone
        (everything currently queued is frozen), or 0 if
        frozen_zone_cards is 0 (nothing is frozen).
        """
        remaining = self.frozen_zone_cards
        for i, (_, _, entry) in enumerate(self._entries):
            if remaining <= 0:
                return i
            remaining -= entry.n_cards
        return len(self._entries)

    @property
    def total_pending_cards(self) -> int:
        """Sum of n_cards across every pending entry — a cheap 'how busy
        is this line's queue right now' figure for the push dispatcher's
        least-busy-line comparison (see mixed_runner)."""
        return sum(entry.n_cards for _, _, entry in self._entries)

    # ------------------------------------------------------------------
    # Enqueue
    # ------------------------------------------------------------------

    def push_batch(self, product_type: str, cards: list[KanbanCard]) -> None:
        """
        Enqueue one already-released Kanban batch (class-1/pull) — SAME
        call signature as before, so kanban_process_logic.py's
        collection_box_emptying_process needs no changes. `cards` is
        everything the Batch-Size Collector released together (its
        `CardsToTrigger` worth, e.g. 4 physical cards) — gathering that
        many cards' worth of stock into one release EVENT is correct
        Kanban behaviour; admitting them onto the shared line as one
        atomic, indivisible production job is not.

        v8 chute-correction fix: this used to wrap the WHOLE released
        list into a single ChuteEntry (n_cards=len(cards)), so
        production_trigger_process popped it as ONE job and
        run_one_kanban_batch launched all of it concurrently — every
        card in the release got the same "in_production" timestamp,
        i.e. the chute let go `CardsToTrigger` cards at once instead of
        one at a time. Now each card gets its OWN ChuteEntry (n_cards=1),
        so each waits its own turn on the chute: only the front one can
        ever be popped (pop_next / pop_next_of_class are already
        front-only — see those methods), production_trigger_process
        runs it to completion, and only then does the next card's entry
        become poppable. This is also what makes the frozen zone's
        card-unit counting exact rather than only precise to the size of
        whatever bundle happened to release together.

        Priority is taken from the first card (same as before — the
        whole release happened together at the same priority context,
        so every card in it gets that priority), and each card keeps
        its own insertion-order tie-break via `_insert_ranked`'s
        `_sequence` counter, so splitting doesn't reorder them relative
        to each other or to anything already queued.
        """
        if not cards:
            return
        priority = cards[0].priority
        for card in cards:
            entry = ChuteEntry(
                sim_class="pull", product_type=product_type,
                n_cards=1, cards=[card],
            )
            self._insert_ranked(entry, priority=priority)

    def push_chunk(self, product_type: str, priority: str, payload: object) -> None:
        """
        Enqueue one push chunk (class-2), NOT within
        PushPolicyConfig.rush_threshold_h of its due date. `priority` is
        accepted for call-site/logging compatibility (mixed_runner still
        calls this as `chute.push_chunk(sachnummer, priority="M",
        payload=chunk)`) but no longer drives placement — see the v7
        push-production-correction note below.

        v7 push-production-correction fix: this used to go through
        _insert_ranked(), which rank-sorts into the movable tail — so an
        "M" push chunk could land behind any number of "H" pull batches
        already queued. That's wrong: a push chunk must be placed
        "before all cards on the chute" — i.e. at the front of the
        movable (non-frozen) segment, ahead of every other pending
        entry, pull or push, regardless of rank. That's exactly the
        position push_rush_entry() already used, so both now share
        _insert_at_boundary(). The only thing that still distinguishes
        rush from non-rush is upstream, in mixed_runner's
        push_dispatch_process (which line gets picked, and whether the
        busy/retry search is skipped) — not chute placement.
        """
        entry = ChuteEntry(
            sim_class="push", product_type=product_type,
            n_cards=1, payload=payload,
        )
        self._insert_at_boundary(entry)

    def push_rush_entry(self, product_type: str, payload: object) -> None:
        """
        Force-insert a rushed push chunk (class-2, within
        PushPolicyConfig.rush_threshold_h of its due date) immediately
        after the frozen zone — ahead of every normally-ranked entry
        currently waiting, bypassing H/M/L ranking entirely. This
        implements the placement half of "12h before due date: assign to
        the least-busy compatible line, and stick them right before the
        frozen zone" — which LINE to target is mixed_runner's job (its
        own least-busy comparison, using total_pending_cards across
        candidate lines' chutes); this method only handles placement
        WITHIN the one already-chosen line's queue.

        Ordering among multiple rush entries: each call recomputes the
        frozen boundary fresh and inserts exactly there, so a second
        rush call arriving before the first has been absorbed into the
        (dynamically growing) frozen span lands AHEAD of it — i.e. LIFO
        among rush entries, but still strictly FIFO/immovable against
        the frozen prefix itself. This is a deliberate choice ("the
        newest emergency gets the front-most open slot") rather than an
        oversight — rush placement should be rare enough (only
        orders inside rush_threshold_h) that a stricter "oldest rush
        entry first" tie-break isn't worth extra bookkeeping unless it
        turns out to matter in practice.

        v7: now a thin wrapper over _insert_at_boundary() — the same
        helper push_chunk() uses (see that method's docstring for why
        the two are no longer distinguished at the chute-placement
        level). Behaviour is otherwise unchanged from before.
        """
        entry = ChuteEntry(
            sim_class="push", product_type=product_type,
            n_cards=1, payload=payload,
        )
        self._insert_at_boundary(entry)

    def _insert_at_boundary(self, entry: ChuteEntry) -> None:
        """
        Shared insertion path for push_chunk()/push_rush_entry(): inserts
        `entry` exactly at first_unfrozen_index() — the front of the
        movable segment — bypassing rank/seq sorting entirely. Every
        push chunk, rush or not, preempts to the front of whatever's
        still movable; if the whole queue is currently smaller than the
        frozen zone, first_unfrozen_index() returns len(_entries), so
        this becomes a same-position append (no jump possible) —
        matching "if there are less than 8 cards, priority is useless."

        Deliberately NOT going through _insert_ranked(): that method's
        rank-based sort is still correct for pull's push_batch()
        (Kanban H/M/L tiers should keep sorting against each other), but
        push entries always preempt regardless of rank, so this method
        never risks touching the frozen prefix's slice — the one
        invariant that must never break.
        """
        self._sequence += 1
        boundary = self.first_unfrozen_index()
        self._entries.insert(boundary, (self._PRIORITY_RANK["H"], self._sequence, entry))

    def _insert_ranked(self, entry: ChuteEntry, priority: str) -> None:
        """
        Shared insertion path for push_batch()/push_chunk(): re-sorts
        ONLY the movable tail of the queue (by rank, then insertion
        order) and leaves the frozen prefix's slice completely untouched
        — see the class docstring's "frozen zone" note for why this is
        the one thing that changed from the original single-list-sort
        behaviour.
        """
        rank = self._PRIORITY_RANK.get(priority, 1)
        self._sequence += 1
        boundary = self.first_unfrozen_index()
        frozen_part = self._entries[:boundary]
        movable_part = self._entries[boundary:]
        movable_part.append((rank, self._sequence, entry))
        movable_part.sort(key=lambda e: (e[0], e[1]))
        self._entries = frozen_part + movable_part

    # ------------------------------------------------------------------
    # Drain
    # ------------------------------------------------------------------

    @property
    def n_pending_batches(self) -> int:
        return len(self._entries)

    def pop_next(self) -> Optional[ChuteEntry]:
        """
        Pop the highest-priority (frozen-first, then ranked) pending
        entry, or None if empty. Since frozen entries always occupy the
        front of `_entries` (see _insert_ranked / push_rush_entry, both
        of which refuse to insert before the frozen boundary), this
        naturally drains the frozen span first, in the order it froze —
        the "committed, about to run, guaranteed not preempted" property
        the frozen zone exists to provide.

        BREAKING CHANGE from the pre-frozen-zone version: returns a
        ChuteEntry, not a (product_type, cards) tuple (push entries have
        no `cards` list, so the old tuple shape can't represent them).
        Existing callers that do `product_type, cards = chute.pop_next()`
        need updating to
        `entry = chute.pop_next(); product_type, cards = entry.product_type, entry.cards`.

        v6 note: mixed_runner.py replaces THIS method with a per-instance
        pull-only wrapper (see _install_pull_only_chute_view()) once push
        chunks start sharing this chute, so in practice
        kanban_process_logic.py's calls to pop_next() only ever see
        sim_class == "pull" entries — see pop_next_of_class() below for
        the general-purpose version that wrapper is built on.
        """
        if not self._entries:
            return None
        _, _, entry = self._entries.pop(0)
        return entry

    def pop_next_of_class(self, sim_class: str) -> Optional[ChuteEntry]:
        """
        Pop the front entry ONLY if it belongs to `sim_class`; otherwise
        return None without touching the queue.

        v7 chute-correction fix: this used to scan past entries of the
        OTHER class to extract the first match wherever it sat in the
        queue — i.e. it let a push entry sitting behind still-untouched
        frozen pull entries get popped (and its production started)
        while those pull entries were still ahead of it in the queue.
        That let two independent per-class drain loops (pull's
        production_trigger_process, push's push_drain_process) both pop
        successfully at the same simulated instant regardless of which
        entry was actually at the front — effectively releasing more
        than one entry from the chute at once. That's backwards for a
        physical chute: it's ONE mechanical admission slot for a shared
        line, so only one entry may ever be "in flight" (popped, and
        therefore in production) at a time, and it has to be whichever
        entry is currently at the front — not whichever entry happens
        to match the calling process's class. This is also what the
        frozen zone actually requires: nothing behind a frozen entry may
        leave the chute before that frozen entry does, regardless of
        class.

        Now: only self._entries[0] is ever eligible. If it belongs to
        the other class, this is a genuine "nothing for me right now,
        wait your turn" — the caller re-blocks on its own wake signal
        exactly as it already does for "queue empty" (see
        production_trigger_process / push_drain_process), and must be
        woken again once the front changes — i.e. whenever EITHER class
        successfully pops, not just when its own class's entries are
        enqueued. (mixed_runner.py wires this cross-class wake: a
        successful pull pop wakes push's signal, and a successful push
        pop wakes pull's signal — see push_drain_process and
        _install_pull_only_chute_view.)
        """
        if not self._entries:
            return None
        _, _, front_entry = self._entries[0]
        if front_entry.sim_class != sim_class:
            return None
        self._entries.pop(0)
        return front_entry

    def peek_next_of_class(self, sim_class: str) -> Optional[ChuteEntry]:
        """
        Read-only counterpart to pop_next_of_class(): returns the front
        entry if (and only if) it belongs to `sim_class`, WITHOUT removing
        it from the queue. Returns None if the queue is empty or the front
        entry belongs to the other class.

        Added for push_drain_process's "peek before securing the gate" fix
        (v9 chute-early-pop-correction, mixed_runner.py): the chute's
        front-of-queue / frozen-zone protection only protects an entry while
        it is still IN the queue. Popping it before it has actually secured
        the LinePriorityGate hands that protection away one step too early.
        peek_next_of_class() lets a caller check "is there an entry ready
        for me" without giving up the chute's ordering guarantee; only
        pop_next_of_class() once actually about to execute.
        """
        if not self._entries:
            return None
        _, _, front_entry = self._entries[0]
        if front_entry.sim_class != sim_class:
            return None
        return front_entry

    def __repr__(self) -> str:
        return (
            f"KanbanChuteResource(line={self.line_id}, "
            f"pending_batches={self.n_pending_batches}, "
            f"frozen_zone_cards={self.frozen_zone_cards})"
        )


@dataclass
class KanbanSimEnvironment(SimEnvironment):
    """
    Subclass of SimEnvironment adding the 4 Kanban-loop resource dicts
    plus a permanent card registry (handoff-notes open question #6:
    subclass, not fields bolted onto SimEnvironment directly — this keeps
    build_environment()/SimEnvironment 100% untouched for the push-model
    path, and lets isinstance() distinguish which model an environment
    belongs to).

    All 4 new dicts are keyed first by line_id (int, 1-based, matching
    ProductionLine.line_id) for O(1) per-line access from the Kanban
    process generators (kanban_process_logic_v1.py — not yet written),
    which always operate one line at a time.

    supermarkets      : dict[line_id -> dict[product_type -> SupermarketResource]]
    collection_boxes  : dict[line_id -> CollectionBoxResource]
    batch_collectors  : dict[line_id -> BatchCollectorResource]
    kanban_chutes     : dict[line_id -> KanbanChuteResource]
    card_registry     : dict[card_id -> KanbanCard] — every card ever
                        created, for O(1) lookup/traceability (a future
                        cycle-time-per-card KPI reads a card's
                        `transitions` straight off here)
    card_counter      : global incrementing ID, mirrors
                        SimEnvironment.part_counter
    """
    supermarkets: dict[int, dict[str, SupermarketResource]] = field(default_factory=dict)
    collection_boxes: dict[int, CollectionBoxResource] = field(default_factory=dict)
    batch_collectors: dict[int, BatchCollectorResource] = field(default_factory=dict)
    kanban_chutes: dict[int, KanbanChuteResource] = field(default_factory=dict)
    card_registry: dict[int, KanbanCard] = field(default_factory=dict)
    card_counter: int = 0

    def next_card_id(self) -> int:
        """Thread-safe (single-thread SimPy) unique, PERMANENT card ID."""
        self.card_counter += 1
        return self.card_counter

    def create_card(
        self,
        product_type: str,
        line_id: int,
        batch_size: int,
        priority: str = "M",
    ) -> KanbanCard:
        """
        Factory method: create a new KanbanCard (starts 'in_supermarket'),
        register it in card_registry, and return it. Per open question #2
        this card_id is permanent — callers should hold onto and reuse
        this same KanbanCard instance for the rest of its circulating
        life, never creating a fresh one for the same physical card slot.
        """
        card = KanbanCard(
            card_id=self.next_card_id(),
            product_type=product_type,
            line_id=line_id,
            batch_size=batch_size,
            priority=priority,
        )
        card.record_transition("in_supermarket", self.env.now)
        self.card_registry[card.card_id] = card
        return card

    def supermarket_for(self, line_id: int, product_type: str) -> Optional[SupermarketResource]:
        return self.supermarkets.get(line_id, {}).get(product_type)

    def collection_box_for(self, line_id: int) -> Optional[CollectionBoxResource]:
        return self.collection_boxes.get(line_id)

    def batch_collector_for(self, line_id: int) -> Optional[BatchCollectorResource]:
        return self.batch_collectors.get(line_id)

    def kanban_chute_for(self, line_id: int) -> Optional[KanbanChuteResource]:
        return self.kanban_chutes.get(line_id)

    def describe_kanban(self) -> None:
        print("=== KanbanSimEnvironment (Kanban loop) ===")
        print(f"Cards created total : {self.card_counter}")
        for line in self.lines:
            lid = line.line_id
            print(f"  --- Line {lid} ({line.line_name}) ---")
            sm = self.supermarkets.get(lid, {})
            nonzero = {
                p: f"{r.n_available}/{r.capacity}" for p, r in sm.items()
                if r.n_available or r.pcs_partial
            }
            print(f"    Supermarket (nonzero only, available/capacity): {nonzero}")
            print(f"    {self.collection_boxes.get(lid)}")
            print(f"    {self.batch_collectors.get(lid)}")
            print(f"    {self.kanban_chutes.get(lid)}")
        print("===========================================")


def build_kanban_environment(
    env: simpy.Environment,
    cfg: SimConfig,
    seed: int = 42,
) -> KanbanSimEnvironment:
    """
    Sibling factory to build_environment() — builds the Kanban-loop
    resources on top of the exact same station/buffer/inventory/chute
    wiring, by calling build_environment() itself (so that wiring stays
    completely unchanged/unduplicated) and copying its fields into a new
    KanbanSimEnvironment, then layering the 4 new resource dicts and
    seeding them from cfg.kanban_cards / cfg.supermarkets.

    A SupermarketResource is created for each (line, product) pair drawn
    from cfg.kanban_cards, but restricted to that product's
    KanbanCardConfig.eligible_lines (the HTL3/HTL5/HTL6 columns on the
    KanbanCardsSetup sheet — see config_loader_v6._parse_kanban_cards_sheet).
    This module still has no import-time dependency on product_line_matrix;
    eligible_lines is read purely off cfg.kanban_cards, which config_loader_v6
    already resolved from the sheet, so the independence from
    product_line_matrix noted elsewhere in this module is preserved.

    Backward-compat fallback: if a product's eligible_lines comes back empty
    (e.g. an older workbook without the HTL3/HTL5/HTL6 columns — see the
    config_loader_v6 docstring note that eligible_lines is simply empty for
    every row in that case), it is treated as "eligible on every line" rather
    than "eligible on none", so older workbooks keep building exactly the
    unfiltered (line, product) matrix this function used to build
    unconditionally.

    --- v6 change: capacity/seed stock now come from the "Supermarkets"
    sheet, not a flat global constant ------------------------------------
    Previously every (line, product) SupermarketResource got the SAME
    capacity (cfg.kanban_timing.supermarket_capacity_cards) and its seed
    stock came from a SPARSE cfg.supermarket_init dict keyed by
    (line_name, sachnummer). Both of those are gone: the "Supermarkets"
    sheet (config_loader_v6.SupermarketSlotConfig) now gives a full
    per-slot layout — Line | RowNumber | Type | Capacity | Sachnummer |
    InitialState | InitialPcsPartial — and, importantly, a line can have
    SEVERAL physical "Main runner" rows for the SAME product (e.g. HTL3
    rows 1 & 2 both F00RJ02491 — separate physical lanes, not a
    duplicate/error; see that dataclass's docstring). Since ONE
    SupermarketResource models a (line, product) pair as a single
    logical store, every "Main runner" row matching a given (line,
    sachnummer) is summed together here: capacity = sum(row.capacity),
    seed cards = sum(row.initial_state), seed partial pieces =
    sum(row.initial_pcs_partial) — with the summed partial pieces folded
    into extra whole seed cards if they cross a batch_size boundary
    (mirrors SupermarketResource.deposit_finished_pcs()'s own divmod
    logic; two lanes each sitting on a partial pack can genuinely add up
    to a full card once combined into one logical store).

    "Exotic" rows (push/generic slots, no fixed Sachnummer — see
    SupermarketSlotConfig.is_exotic) are NOT touched by this function at
    all: no SupermarketResource is built for them here. They currently
    exist only as raw SupermarketSlotConfig rows on cfg.supermarkets;
    mixed_runner.py's ExoticSupermarketTracker reads those directly and
    tracks push-chunk deposits independently (see that class's
    module-level caveat about not yet being wired into a resource object
    from this module).

    If a product is eligible (per KanbanCardsSetup) on a line but the
    "Supermarkets" sheet has NO "Main runner" row at all for that exact
    (line, sachnummer), there are two possible reasons, distinguished by
    whether that line has any "Exotic" rows at all:
      - Line HAS Exotic rows: expected/by-design — this product is
        intentionally routed through the line's shared Exotic pool
        instead of getting a private Main-runner lane. A synthetic
        fallback SupermarketResource is still built (capacity =
        kanban_timing.supermarket_capacity_cards, zero seed stock, so
        kanban_process_logic always has somewhere to withdraw from) but
        it's flagged via `SupermarketResource.is_exotic_routed = True`,
        and only a quiet informational (ℹ) note is printed — this is NOT
        a workbook problem.
      - Line has NO Exotic rows either: genuinely under-specified — same
        synthetic fallback, but this prints a ⚠ warning, since there's
        nowhere (dedicated or shared) for this product's cards to
        physically live on that line. Likely an incomplete/not-yet-
        migrated workbook.

    Parameters
    ----------
    env  : a fresh simpy.Environment()
    cfg  : validated SimConfig from config_loader_v6.load_config()
           (must have been loaded with a workbook containing the 4 Kanban
           sheets for kanban_cards/kanban_withdrawals/supermarkets/
           kanban_timing to be non-empty/non-default; if absent, this
           still returns a valid but empty KanbanSimEnvironment)
    seed : random seed for the RNG (int) — passed straight through to
           build_environment(), so a push and a kanban run built from the
           same seed share identical station/buffer RNG behavior

    Returns
    -------
    KanbanSimEnvironment — ready for kanban_process_logic_v1 (not yet
    written) to attach withdrawal_process / collection_box_emptying_process
    / check_and_release_batch / the production-trigger process.
    """
    base = build_environment(env, cfg, seed=seed)

    kenv = KanbanSimEnvironment(
        env=base.env,
        cfg=base.cfg,
        lines=base.lines,
        rng=base.rng,
        parts_out=base.parts_out,
        parts_in_wip=base.parts_in_wip,
        part_counter=base.part_counter,
        event_log=base.event_log,
        inventories=base.inventories,
        chutes=base.chutes,
        chutes_by_station=base.chutes_by_station,
    )

    # Shared, read-only across all 3 lines' BatchCollectorResource instances.
    cards_to_trigger: dict[str, int] = {
        sachnr: card_cfg.cards_to_trigger for sachnr, card_cfg in cfg.kanban_cards.items()
    }

    # {(line_name, sachnummer): [SupermarketSlotConfig, ...]} — every
    # "Main runner" (pull) row from the "Supermarkets" sheet, grouped for
    # the aggregation described above. "Exotic" rows are deliberately
    # excluded here (see the docstring note on why this function doesn't
    # build resources for them).
    main_runner_rows: dict[tuple[str, str], list[SupermarketSlotConfig]] = {}
    # {line_name: bool} — whether this line has ANY "Exotic" row at all in
    # the "Supermarkets" sheet. Used below to tell "this product is
    # intentionally routed through the shared Exotic pool" (line has
    # Exotic rows) apart from "this workbook is genuinely
    # incomplete/not-yet-migrated" (line has neither a Main-runner row for
    # this product NOR any Exotic rows to fall back on).
    line_has_exotic_rows: dict[str, bool] = {}
    for line_name, slot_cfgs in (cfg.supermarkets or {}).items():
        line_has_exotic_rows[line_name] = any(s.is_exotic for s in slot_cfgs)
        for slot in slot_cfgs:
            if slot.is_exotic or not slot.sachnummer:
                continue
            main_runner_rows.setdefault((line_name, slot.sachnummer), []).append(slot)

    for line in kenv.lines:
        line_id = line.line_id
        line_name = line.line_name

        # ---- Supermarket (one SupermarketResource per eligible product) ----
        supermarket_lane: dict[str, SupermarketResource] = {}
        for sachnr, card_cfg in cfg.kanban_cards.items():
            # Empty eligible_lines means the sheet had no HTL3/HTL5/HTL6
            # columns at all (older workbook) — fall back to "eligible on
            # every line" so those workbooks still build the full matrix
            # this function used to build unconditionally. A non-empty
            # eligible_lines list is honored literally: skip this line if
            # it isn't listed.
            if card_cfg.eligible_lines and line_name not in card_cfg.eligible_lines:
                continue

            rows = main_runner_rows.get((line_name, sachnr), [])
            exotic_routed = False
            if rows:
                capacity = sum(r.capacity for r in rows)
                n_seed_cards = sum(r.initial_state for r in rows)
                pcs_partial = sum(r.initial_pcs_partial for r in rows)
                if pcs_partial >= card_cfg.batch_size:
                    # Summing leftover pieces from several physical lanes
                    # can itself complete one or more whole cards (e.g.
                    # two lanes each sitting on 150/200pcs sum to 300 =
                    # 1 full card + 100 leftover) — fold that in exactly
                    # like SupermarketResource.deposit_finished_pcs() does.
                    extra_cards, pcs_partial = divmod(pcs_partial, card_cfg.batch_size)
                    n_seed_cards += extra_cards
            elif line_has_exotic_rows.get(line_name, False):
                # No dedicated Main-runner row for this (line, sachnr), but
                # the line DOES have Exotic rows — this is the expected,
                # by-design case for a product that's routed through the
                # shared Exotic pool rather than a private lane, not a
                # workbook problem. Quiet, informational only.
                print(f"  ℹ {sachnr!r} on {line_name} has no dedicated 'Main "
                      f"runner' Supermarkets row — routed via the shared "
                      f"'Exotic' pool instead (per KanbanCardsSetup eligibility).")
                capacity = cfg.kanban_timing.supermarket_capacity_cards
                n_seed_cards = 0
                pcs_partial = 0
                exotic_routed = True
            else:
                print(f"  ⚠ No 'Main runner' Supermarkets row found for {sachnr!r} "
                      f"on {line_name} (eligible per KanbanCardsSetup), and this "
                      f"line has no 'Exotic' rows either to fall back on — falling "
                      f"back to kanban_timing.supermarket_capacity_cards="
                      f"{cfg.kanban_timing.supermarket_capacity_cards}, zero seed "
                      f"stock. Likely an incomplete/not-yet-migrated workbook.")
                capacity = cfg.kanban_timing.supermarket_capacity_cards
                n_seed_cards = 0
                pcs_partial = 0

            sm = SupermarketResource(
                line_id         = line_id,
                product_type    = sachnr,
                batch_size      = card_cfg.batch_size,
                store           = simpy.Store(env, capacity=capacity),
                capacity        = capacity,
                is_exotic_routed= exotic_routed,
            )
            sm.pcs_partial = pcs_partial
            if n_seed_cards > capacity:
                print(f"  ⚠ Supermarket seed for {sachnr!r} on {line_name}: "
                      f"initial_state totals {n_seed_cards} card(s) across "
                      f"{len(rows)} row(s), exceeding capacity={capacity} "
                      f"— clipping to {capacity}.")
                n_seed_cards = capacity
            for _ in range(n_seed_cards):
                card = kenv.create_card(
                    product_type = sachnr,
                    line_id      = line_id,
                    batch_size   = card_cfg.batch_size,
                    priority     = "M",
                )
                # Direct list append (not `yield store.put(...)`): this
                # runs at build time, before env.run() starts, exactly
                # mirroring how build_environment() seeds Container
                # levels via the `init=` constructor argument above.
                # Safe to bypass capacity enforcement here since we've
                # already clipped n_seed_cards to `capacity` above.
                sm.store.items.append(card)
            supermarket_lane[sachnr] = sm
        kenv.supermarkets[line_id] = supermarket_lane

        # ---- Collection Box / Batch Collector / Kanban Chute (one each per line) ----
        kenv.collection_boxes[line_id] = CollectionBoxResource(line_id=line_id)
        kenv.batch_collectors[line_id] = BatchCollectorResource(
            line_id=line_id, cards_to_trigger=cards_to_trigger,
        )
        kenv.kanban_chutes[line_id] = KanbanChuteResource(line_id=line_id)

    return kenv


# ===========================================================================
# Quick self-test
# ===========================================================================
if __name__ == "__main__":
    import sys
    from backend.config_loader_v6 import load_config

    excel = sys.argv[1] if len(sys.argv) > 1 else "ProductionPlanning_v6.xlsx"
    csv   = sys.argv[2] if len(sys.argv) > 2 else "HTL_setup_times.xlsx"
    cfg  = load_config(excel, csv)
    env  = simpy.Environment()
    sim  = build_environment(env, cfg, seed=42)
    sim.describe()

    # Create a few test parts and verify the entity structure
    print("\n--- Test Part Creation ---")
    for product, qty in list(cfg.demand[0].quantities.items())[:3]:
        p = sim.create_part(product_type=product, line_id=1)
        print(f"  Created: {p}")

    # Simulate a part completing the line
    p_test = sim.parts_in_wip[0]
    sim.finish_part(p_test, status="passed")
    print(f"\n  Finished (passed): {p_test}")
    print(f"  WIP count : {len(sim.parts_in_wip)}")
    print(f"  Out count : {len(sim.parts_out)}")

    # ---- Kanban environment self-test ----
    print("\n\n=== build_kanban_environment() self-test ===")
    kenv_raw = simpy.Environment()
    ksim = build_kanban_environment(kenv_raw, cfg, seed=42)
    ksim.describe_kanban()

    if cfg.kanban_cards:
        sachnr = next(iter(cfg.kanban_cards))
        sm = ksim.supermarket_for(line_id=1, product_type=sachnr)
        print(f"\n  Supermarket lane for {sachnr!r} on line 1: {sm}")

        # Simulate production finishing a batch's worth of pieces
        if sm is not None:
            n_new = sm.deposit_finished_pcs(sm.batch_size)
            print(f"  deposit_finished_pcs({sm.batch_size}) -> {n_new} whole batch(es)")
            for _ in range(n_new):
                card = ksim.create_card(sachnr, line_id=1, batch_size=sm.batch_size)
                sm.store.items.append(card)
            print(f"  Supermarket lane after deposit: {sm}")

        # Exercise the priority chute ordering (H before M before L, FIFO within tie)
        chute = ksim.kanban_chute_for(line_id=1)
        c1 = ksim.create_card(sachnr, line_id=1, batch_size=50, priority="L")
        c2 = ksim.create_card(sachnr, line_id=1, batch_size=50, priority="H")
        c3 = ksim.create_card(sachnr, line_id=1, batch_size=50, priority="M")
        chute.push_batch(sachnr, [c1])
        chute.push_batch(sachnr, [c2])
        chute.push_batch(sachnr, [c3])
        print(f"\n  Chute pending before pops: {chute.n_pending_batches}")
        order = []
        while chute.n_pending_batches:
            entry = chute.pop_next()
            product_type, cards = entry.product_type, entry.cards
            order.append(cards[0].priority)
        print(f"  Pop order (expect H, M, L): {order}")
