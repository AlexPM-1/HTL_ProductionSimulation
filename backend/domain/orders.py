"""
domain/orders.py
=================
Shared order data classes: `OrderRecord` (a fully resolved, line-assigned
order) and `UnassignedOrder` (an order that couldn't be matched to any
line). Used throughout sim.fill.push and sim.produce for push/pull order
tracking, and by reports for delivery/KPI calculations.

Pure dataclasses, no SimPy, Excel, or config-loading dependency — safe to
import from anywhere.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional


@dataclass
class OrderRecord:
    """
    One fully resolved order: a (product, quantity) pair from CustomerDemand
    mapped to a specific production line with routing and cycle-time info.

    Attributes
    ----------
    order_id         : permanent, sequential (1, 2, 3, …) unique identifier,
                       assigned once by SimEnvironment.create_order() and
                       shared across BOTH push and pull orders (one counter,
                       one registry — see sim.resources.environment). For a
                       push order sliced into several same-sachnummer
                       OrderRecord chunks (sim.fill.push.chunking), every
                       chunk carries the SAME order_id — it identifies the
                       customer order, not the chunk. This is the join key
                       reports use to correlate an order against the
                       KanbanCard.history entries / GateActivityEntry rows
                       it produced.
    period_label     : planning period (e.g. "Week-01")
    sachnummer       : Bosch part number — primary product key
    kunde            : customer name / market designation
    product_class    : "Stift" | "DRS" | "Stab_Lochfilter" | etc.
    quantity         : parts to produce this period
    assigned_line    : line the order actually landed on. For a push order
                       this is the ONLY line it will ever run on (push
                       orders are single-line by construction). For a pull
                       order this is kept purely for backward-compatibility
                       with existing single-line callers/reports — the
                       authoritative, possibly-multi-line picture is
                       `assignments` below, since a Kanban withdrawal event
                       can be fulfilled by cards drawn from more than one
                       line's Supermarket (sim.fill.pull.assignment picks a
                       line fresh per card).
    freigabe         : approval status string on the assigned line
    station_sequence : ordered canonical station names for this product/line
    feasible_lines   : all lines that *could* run this product (audit trail)
    assignments      : every (line, cards) pairing that actually fulfilled
                       this order, appended to as production happens — NOT
                       populated at order-creation time. Each entry is
                       {"line": str, "card_ids": list[int]}. A push order
                       gets exactly one entry (its single assigned_line);
                       a pull order gets one entry per distinct line its
                       withdrawn cards ended up coming from, with
                       "card_ids" accumulating every KanbanCard.card_id
                       drawn from that line for this order_id. Empty list
                       until the first card/chunk is actually assigned.
    production_type  : "push" | "pull" — which side of the plant this
                       order belongs to. NOT meant to be set directly:
                       it's fixed per-subclass (OrderRecordPull /
                       OrderRecordPush below each hardcode their own
                       value), so a plain isinstance() check and this
                       string agree by construction. Kept as an explicit
                       field (rather than only relying on isinstance) so
                       JSON-serialising an OrderRecord for a report
                       doesn't need to special-case class names. The base
                       OrderRecord class is not meant to be instantiated
                       directly — always create an OrderRecordPull or
                       OrderRecordPush (see SimEnvironment.create_order()).
    note             : warning text (conditional approval, …)
    due_date         : when the customer needs this order (wall-clock
                       datetime — NOT a simpy sim-time float; framework-
                       agnostic on purpose). For push/class-2 orders this
                       is the CustomerDemand row's own (Date, Time),
                       combined via domain.timeparse.row_due_datetime and
                       populated by sim.fill.push.dispatch (this module
                       never reads any config-loading singleton, so it
                       can't compute it itself). None for orders where no
                       due date applies/is known (e.g. class-1 Kanban
                       orders, which are driven by supermarket triggers,
                       not a due date).
    delivered_date   : when this (possibly chunked) order actually
                       finished and was deposited — same wall-clock
                       representation as due_date, set post-hoc by the
                       caller once production of this specific
                       OrderRecord (or chunk of one, since a push order
                       can be sliced into several same-sachnummer
                       OrderRecord copies) completes. None until then.
                       Compare against due_date (see delivery_delta_h)
                       for on-time-delivery KPIs / frontend graphs.
    """
    order_id: int
    period_label: str
    sachnummer: str
    kunde: str
    product_class: str
    quantity: int
    assigned_line: str
    freigabe: str
    station_sequence: list[str]
    feasible_lines: list[str]
    assignments: list[dict] = field(default_factory=list)
    production_type: str = ""
    note: Optional[str] = None
    due_date: Optional[datetime] = None
    delivered_date: Optional[datetime] = None

    def __repr__(self) -> str:
        return (
            f"OrderRecord(id={self.order_id} | {self.sachnummer} | {self.kunde} | "
            f"qty={self.quantity} | line={self.assigned_line} | "
            f"stations={self.station_sequence})"
        )

    def record_assignment(self, line: str, card_id: Optional[int] = None) -> None:
        """
        Append one fulfilment fact to `assignments`: "a card/chunk of this
        order ran on `line`". If an entry for `line` already exists (e.g.
        a second card off the same line's Supermarket for this order),
        `card_id` is appended to that entry's "card_ids" list instead of
        creating a duplicate line entry. `card_id` is omitted (or None)
        for push orders, which have no card concept — the entry is then
        just {"line": line, "card_ids": []}.

        Called once per push dispatch (sim.fill.push.chunking /
        sim.fill.push.dispatch) and once per pull card withdrawal
        (sim.drain.pull_turn._run_one_pull_card).
        """
        for entry in self.assignments:
            if entry["line"] == line:
                if card_id is not None and card_id not in entry["card_ids"]:
                    entry["card_ids"].append(card_id)
                return
        self.assignments.append({
            "line": line,
            "card_ids": [card_id] if card_id is not None else [],
        })

    @property
    def delivery_delta_h(self) -> Optional[float]:
        """
        Hours between `delivered_date` and `due_date`: positive = delivered
        AFTER the due date (late), negative = delivered early. None if
        either timestamp isn't set (not a due-dated order, or not
        delivered/completed yet).
        """
        if self.due_date is None or self.delivered_date is None:
            return None
        return (self.delivered_date - self.due_date).total_seconds() / 3600.0


@dataclass
class OrderRecordPull(OrderRecord):
    """
    A class-1 Kanban (pull) order — one CustomerDemandKanban withdrawal
    event, possibly fulfilled by cards drawn from more than one line's
    Supermarket (see OrderRecord.assignments). `production_type` is fixed
    to "pull" and not settable from the constructor.

    Per-card, per-stage timing (withdrawal / collection box / batch
    collector / chute / production / reposition) is NOT duplicated here —
    it lives on the card itself, one CardHistoryEntry per order_id, in
    KanbanCard.history (sim.resources.cards). This class only tracks
    which cards, on which lines, fulfilled this order — `all_card_ids`
    below is the convenience view a report table's "Cards" column reads.

    Step 2 note: `assignments` (and therefore `all_card_ids`) is meant to
    be updated PER CARD, at the moment that specific card's production
    actually completes (sim.produce.run_kanban_batch / the on_finish
    callback in sim.drain.pull_turn._run_one_pull_card) — NOT once per
    batch/gate-hold, since a single hold can run cards belonging to
    several different orders (see run_pull_turn's up-to-4-cards loop).
    """
    production_type: str = field(default="pull", init=False, repr=False)

    def all_card_ids(self) -> list[int]:
        """Every KanbanCard.card_id that has fulfilled this order so far,
        across every line in `assignments`, flattened into one list."""
        return [cid for entry in self.assignments for cid in entry["card_ids"]]


@dataclass
class OrderRecordPush(OrderRecord):
    """
    A class-2 (push) order — one CustomerDemand row, possibly sliced into
    several same-sachnummer chunks (sim.fill.push.chunking), all sharing
    this same order_id, all running on the SAME single line
    (`assigned_line`; `assignments` here only ever holds that one entry).
    `production_type` is fixed to "push" and not settable from the
    constructor.

    Step 2 note: unlike pull, a push order's cards don't move through a
    Supermarket loop — instead this class needs its own production-start/
    production-end stamps, PLUS the moment the customer actually took
    delivery (which, per sim.fill.push.exotic, is a separate, LATER event
    than `delivered_date`/production-finish: production deposits into the
    exotic Supermarket at `delivered_date`, but the customer doesn't
    withdraw the full amount until `_withdraw_push_chunk_process` fires,
    at/after the order's own due_date).

    t_production_start / t_production_end : simpy sim-time floats
        (env.now), same convention as CardHistoryEntry — set by
        sim.produce.run_order.run_one_order() (via sim.drain.push_turn)
        around the station/buffer launch for this chunk.
    customer_withdrawal_date : wall-clock datetime — same representation
        as due_date/delivered_date; set by
        sim.fill.push.exotic._withdraw_push_chunk_process() the instant
        the customer actually withdraws this chunk (the "full amount
        delivered" moment), distinct from `delivered_date` (production
        finished) above.
    """
    production_type: str = field(default="push", init=False, repr=False)
    t_production_start: Optional[float] = None
    t_production_end: Optional[float] = None
    customer_withdrawal_date: Optional[datetime] = None


@dataclass
class KanbanBatchSpec:
    """
    The internal "what to produce on this line, right now" object for one
    Kanban batch — built once per batch by
    sim.produce.run_kanban_batch._make_kanban_order_record() and threaded
    through changeover() / part_lifecycle() / LinePriorityGate.current_rec
    purely by duck-typing (every call site along that path reads
    `.sachnummer` / `.quantity` / etc. off whatever object it's handed via
    plain attribute access or getattr(), never an isinstance() check
    against OrderRecord — see changeover.py's own "duck-typed" docstring).

    NOT an OrderRecord (or subclass): it is not a customer order, 
    is never registered in SimEnvironment.order_registry, and has
    no order_id / due_date / assignments / production_type — none of
    which mean anything for "the batch spec currently set up on this
    line's gate". Kept as its own small dataclass so future order-
    tracking-only fields added to OrderRecord (order_id, assignments,
    production_type, ...) never again require a throwaway value
    here just to satisfy an unrelated constructor.

    Field set mirrors exactly what _make_kanban_order_record() populates
    and what downstream readers (changeover._resolve_setup_time_s,
    sim.produce.part_lifecycle, sim.drain.pull_turn, api/legacy.py's
    gate_status) actually use: sachnummer, kunde, product_class, quantity,
    assigned_line, freigabe, station_sequence, feasible_lines, note.
    """
    period_label: str
    sachnummer: str
    kunde: str
    product_class: str
    quantity: int
    assigned_line: str
    freigabe: str
    station_sequence: list[str]
    feasible_lines: list[str]
    note: Optional[str] = None


@dataclass
class UnassignedOrder:
    """
    An order from CustomerDemand that could not be matched to any line.

    Possible reasons
    ----------------
    - product not found in PRODUCT_MATRIX
    - all lines are NICHT_MOEGLICH for this product
    - cfg.line_names / active_lines does not include any feasible line
    """
    period_label: str
    product_id: str
    quantity: int
    reason: str

    def __repr__(self) -> str:
        return (
            f"UnassignedOrder({self.product_id} | qty={self.quantity} "
            f"| reason={self.reason!r})"
        )
