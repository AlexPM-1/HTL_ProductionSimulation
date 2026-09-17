"""
sim/resources/environment.py
=============================
Top-level simulation environment holders: SimEnvironment (push model)
and KanbanSimEnvironment (adds the 4 Kanban-loop resource dicts).

Built by sim.resources.build.build_environment()/build_kanban_environment();
read/written throughout sim.produce, sim.fill, and sim.drain.
"""

from __future__ import annotations

import datetime as _dt
import random
from dataclasses import dataclass, field
from typing import Optional

import simpy

from domain.config import SimConfig
from domain.orders import OrderRecord, OrderRecordPull, OrderRecordPush
from domain.products import ProductClass
from telemetry.records import ScheduleEvent

from sim.resources.part import Part
from sim.resources.line import ProductionLine
from sim.resources.inventory import InventoryResource, ChuteResource
from sim.resources.cards import KanbanCard
from sim.resources.supermarket import SupermarketResource
from sim.resources.collector import CollectionBoxResource, BatchCollectorResource
from sim.resources.chute import KanbanChuteResource


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
    order_registry : dict[order_id → OrderRecord] — every OrderRecordPull
                or OrderRecordPush ever created (both subclasses of
                OrderRecord — see domain.orders), for BOTH push and pull
                orders (deliberately kept here on the base class, not on
                KanbanSimEnvironment, so a mixed run shares ONE id
                sequence / ONE lookup table across both order types
                instead of two disjoint ones). Populated by
                create_order() below; read back by reports that need to
                join an order against the KanbanCard.history entries or
                GateActivityEntry rows it produced.
    order_counter : global incrementing ID, mirrors part_counter/
                KanbanSimEnvironment.card_counter
    event_log : list of ScheduleEvent (see telemetry/records.py) — every
                changeover and finished package, across all lines, in the
                order they occur. Populated by sim.produce (changeover()
                and run_line()'s PackageTracker) as the sim runs.
                Single-threaded/cooperative SimPy means all lines can
                safely append to this one list. Feed it into
                reports.all.gantt.build_gantt_payload() after env.run()
                to get the Gantt-ready payload.
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
    order_registry: dict[int, OrderRecord] = field(default_factory=dict)
    order_counter: int          = 0
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
        Thin passthrough to self.cfg.shift_calendar.is_line_on(), given
        here (rather than making every caller reach into
        self.cfg.shift_calendar directly) so this reads the same way as
        chute_for()/inventory_for() above, and so a future caching/
        logging layer has one call site to change.

        `at` is a real wall-clock datetime, NOT self.env.now (env.now is
        just an elapsed-seconds counter on this object's own SimPy clock;
        converting it to a wall-clock instant requires the shared epoch,
        which is computed and owned by the runner layer — see
        domain.epoch.compute_epoch() / sim.context.RunContext.epoch —
        not by this class). Callers typically pass
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
        Thin passthrough to self.cfg.shift_calendar.next_on_transition();
        see is_line_on() above for why this lives here instead of every
        caller reaching into self.cfg directly. Returns None immediately (rather than
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

    def next_order_id(self) -> int:
        """Thread-safe (single-thread SimPy) unique, sequential order ID —
        mirrors next_part_id() / KanbanSimEnvironment.next_card_id()."""
        self.order_counter += 1
        return self.order_counter

    def create_order(
        self,
        kind: str,
        sachnummer: str,
        kunde: str,
        product_class: str,
        quantity: int,
        assigned_line: str,
        freigabe: str,
        station_sequence: list[str],
        feasible_lines: list[str],
        period_label: str = "",
        note: Optional[str] = None,
        due_date=None,
    ) -> OrderRecord:
        """
        Factory method: create a new OrderRecordPull or OrderRecordPush
        (picked via `kind`, "pull" | "push"), register it in
        order_registry, and return it. order_id is permanent and
        sequential — callers should hold onto and reuse this same
        OrderRecord (or, for a chunked push order, copy its order_id onto
        every chunk) rather than minting a fresh id per chunk. Mirrors
        create_part()/create_card(); lives on the base class so ONE
        counter/registry is shared across push and pull orders alike.

        `kind` picks the concrete subclass — `production_type` on the
        result is fixed by that subclass, never passed in directly (see
        domain.orders.OrderRecordPull / OrderRecordPush).

        `assignments` is deliberately left at its default (empty list) —
        it's populated afterwards, as production actually happens, via
        OrderRecord.record_assignment(), not at creation time.
        """
        if kind == "pull":
            cls = OrderRecordPull
        elif kind == "push":
            cls = OrderRecordPush
        else:
            raise ValueError(f"kind must be 'pull' or 'push', got {kind!r}")

        order = cls(
            order_id=self.next_order_id(),
            period_label=period_label,
            sachnummer=sachnummer,
            kunde=kunde,
            product_class=product_class,
            quantity=quantity,
            assigned_line=assigned_line,
            freigabe=freigabe,
            station_sequence=station_sequence,
            feasible_lines=feasible_lines,
            note=note,
            due_date=due_date,
        )
        self.order_registry[order.order_id] = order
        return order

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
# Kanban entities & resources
# ===========================================================================
#
# Everything below is the Kanban pull-system layer. Nothing above this
# line is touched by any of it — the push-model path (Part /
# StationResource / BufferResource / InventoryResource / ChuteResource /
# ProductionLine / SimEnvironment / build_environment()) is reused
# completely unchanged.
#
# Key design points:
#   Stockout behavior     : SupermarketResource.store is a simpy.Store;
#                            a withdrawal that finds it empty naturally
#                            blocks on `yield store.get()` until
#                            production deposits a batch — no separate
#                            stockout/skip branch needed.
#   Card identity          : KanbanCard.card_id is permanent, assigned
#                            once by KanbanSimEnvironment.create_card()
#                            and reused every loop; the full transition
#                            history lives on the card itself.
#   Partial packs          : SupermarketResource.pcs_partial accumulates
#                            leftover pieces; only a *whole* batch_size
#                            chunk ever becomes a card in `store` — a
#                            partial pack is never withdrawable.
#   Independent process    : SupermarketResource / KanbanCard etc. stand
#                            entirely on their own — sim.produce's
#                            kanban process-logic module never needs to
#                            import anything from sim.produce's
#                            order-based (push) functions.
#   Subclass               : KanbanSimEnvironment(SimEnvironment) — see
#                            class docstring below.
#   Excel-driven timing    : cfg.kanban_timing (KanbanTimingConfig) is
#                            threaded through unchanged from
#                            domain.config; nothing here hardcodes the
#                            15/30-min cadence.


@dataclass
class KanbanSimEnvironment(SimEnvironment):
    """
    Subclass of SimEnvironment adding the 4 Kanban-loop resource dicts
    plus a permanent card registry (a subclass rather than fields
    bolted onto SimEnvironment directly, so build_environment()/
    SimEnvironment stay 100% untouched for the push-model path, and
    isinstance() can distinguish which model an environment belongs to).

    All 4 new dicts are keyed first by line_id (int, 1-based, matching
    ProductionLine.line_id) for O(1) per-line access from the Kanban
    process generators (sim.produce.run_kanban_batch), which always
    operate one line at a time.

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
        register it in card_registry, and return it. card_id is
        permanent — callers should hold onto and reuse this same
        KanbanCard instance for the rest of its circulating life, never
        creating a fresh one for the same physical card slot.
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
