"""
dispatch_entities.py  (v3)
===========================
Shared data classes and line-priority strategies used by the dispatch
paths that turn CustomerDemand rows into a DispatchPlan:

    order_dispatcher_simple_adjuster.py  — replaces order_dispatcher_
                                            simple_batch.py; the
                                            "simple_einsetzer" / adjuster
                                            dispatcher (Option 1, below)
    order_optimizer_v1.py                — offline Step 1-4 order-sequencing
                                            optimizer (produces a DispatchPlan
                                            for the fixed-plan executor, see
                                            parallel_runner_v4.py's
                                            dispatch_mode="optimized",
                                            Option 2 below)

--- v3 context: CustomerDemand sheet is vertical, dispatch modes are now
just two, "per_order" is gone -----------------------------------------------
The "CustomerDemand" sheet (config_loader_v5.CustomerDemand) is one row
per (Date, Product) with a total_qty column plus an optional per-line
split (qty_by_line, e.g. "HTL3" | "HTL5" | "HTL6" columns). This feeds two
ways of arranging production orders (down from three — the old runtime
"per_order" SimPy dispatch mode, order_dispatcher_per_order_v3.py, is
discarded entirely):

    Option 1 — "simple_einsetzer" / simple_adjuster
        (order_dispatcher_simple_adjuster.py):
        Demand is taken straight from the sheet's per-line split
        (qty_by_line) — the line is already chosen by the sheet, so the
        dispatcher's only remaining job is deciding the running order
        (Reihenfolge) of the orders already sitting on each line, using
        basic rules (check-first-setup, then max→min quantity).
    Option 2 — "optimized" (mostly unchanged):
        Demand is taken from the sheet's total_qty column; the optimizer
        still decides which of the feasible lines (per PRODUCT_MATRIX)
        to run each product on and in what sequence, using the same
        Step 1-4 process as before, against full quantities.

Which option runs for a given order is decided by the calling dispatcher
/ parallel_runner — NOT by this file. This file only holds the shared
plain-data model (LineStatus, OrderRecord, UnassignedOrder, DispatchPlan)
and the LinePriorityStrategy hierarchy that Option 2 uses to pick a line
from PRODUCT_MATRIX; it stays agnostic to which option produced a given
OrderRecord, the same way it always has.

--- NOTE (v5 follow-up, not yet fully reconciled here) ----------------------
config_loader_v5.CustomerDemand no longer carries a per-line split
(qty_by_line) at all — its "LineId" column is now a downstream/customer
line, not one of ours (see that module's docstring). Option 1's
description above ("demand is taken straight from the sheet's per-line
split") is therefore stale; every CustomerDemand row now behaves like the
old Option 2. This module's docstring hasn't been fully rewritten for
that yet — flagging it here rather than doing a drive-by rewrite while
touching this file for an unrelated reason (see mixed_runner.py's
PushPolicyConfig / OrderRecord.due_date work).

Nothing in this file touches SimPy, Excel, or any config-loading
singleton — it is pure dataclasses + plain-Python priority logic, safe to
import from anywhere (including order_optimizer_v1.py, which deliberately
avoids importing config_loader_v5 at module scope — see that file's
docstring).

There is exactly ONE DispatchPlan implementation in the codebase, shared
by however many ways of arranging production orders exist at a given
time (currently: simple_adjuster, optimized). parallel_runner_v4.py's
dispatch modes consume this single DispatchPlan type directly (no
duck-typing / aliasing needed there).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

from product_line_matrix_v3 import ProductLineInfo, Freigabe


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Only used as a last-resort fallback (e.g. by FixedPriorityStrategy, or to
# seed line_status / print_summary ordering when nothing else is known).
DEFAULT_LINE_PREFERENCE: list[str] = ["HTL3", "HTL5", "HTL6"]


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class LineStatus:
    """
    Running totals accumulated during one dispatch pass.

    Attributes
    ----------
    line_name         : e.g. "HTL3" | "HTL5" | "HTL6"
    n_orders          : number of product types assigned
    total_qty         : total parts assigned across all orders
    assigned_products : sachnummern list (assignment order preserved)
    """
    line_name: str
    n_orders: int = 0
    total_qty: int = 0
    assigned_products: list[str] = field(default_factory=list)

    def assign(self, sachnummer: str, qty: int) -> None:
        self.n_orders += 1
        self.total_qty += qty
        self.assigned_products.append(sachnummer)


@dataclass
class OrderRecord:
    """
    One fully resolved order: a (product, quantity) pair from CustomerDemand
    mapped to a specific production line with routing and cycle-time info.

    Attributes
    ----------
    period_label     : planning period (e.g. "Week-01")
    sachnummer       : Bosch part number — primary product key
    kunde            : customer name / market designation
    product_class    : "Stift" | "DRS" | "Stab_Lochfilter" | etc.
    quantity         : parts to produce this period
    assigned_line    : line the order actually landed on
    freigabe         : approval status string on the assigned line
    station_sequence : ordered canonical station names for this product/line
    feasible_lines   : all lines that *could* run this product (audit trail)
    note             : warning text (conditional approval, …)
    due_date         : when the customer needs this order (wall-clock
                       datetime — NOT a simpy sim-time float; framework-
                       agnostic on purpose, same reasoning as everywhere
                       else in this file). For push/class-2 orders this is
                       the CustomerDemand row's own (Date, Time), combined —
                       see mixed_runner.py's PushPolicyConfig / new push
                       dispatcher, which is what actually populates this
                       field (this module never reads config_loader_v5, so
                       it can't compute it itself). None for orders where
                       no due date applies/is known (e.g. class-1 Kanban
                       orders, which are driven by supermarket triggers,
                       not a due date).
    delivered_date   : when this (possibly chunked) order actually
                       finished and was deposited — same wall-clock
                       representation as due_date, set post-hoc by the
                       caller once production of this specific
                       OrderRecord (or chunk of one, since mixed_runner
                       slices a push order into several same-sachnummer
                       OrderRecord copies) completes. None until then.
                       Compare against due_date (see delivery_delta_h)
                       for on-time-delivery KPIs / frontend graphs.
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
    due_date: Optional[datetime] = None
    delivered_date: Optional[datetime] = None

    def __repr__(self) -> str:
        return (
            f"OrderRecord({self.sachnummer} | {self.kunde} | "
            f"qty={self.quantity} | line={self.assigned_line} | "
            f"stations={self.station_sequence})"
        )

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


@dataclass
class DispatchPlan:
    """
    Complete output of any dispatch path (the simple_einsetzer dispatcher,
    build_dispatch_plan(), or order_optimizer_v1.schedule_to_dispatch_plan()).

    Attributes
    ----------
    period_label : planning period label (e.g. "Week-01")
    orders       : all successfully assigned OrderRecords (insertion order)
    unassigned   : orders that could not be placed on any line
    line_status  : per-line running totals  (line_name → LineStatus)
    """
    period_label: str
    orders: list[OrderRecord]
    unassigned: list[UnassignedOrder]
    line_status: dict[str, LineStatus]

    # ------------------------------------------------------------------
    # Query helpers — used by the parallel runner
    # ------------------------------------------------------------------

    def orders_for_line(self, line_name: str) -> list[OrderRecord]:
        """Return all assigned orders for a specific line (assignment order)."""
        return [r for r in self.orders if r.assigned_line == line_name]

    def total_qty_for_line(self, line_name: str) -> int:
        return self.line_status.get(line_name, LineStatus(line_name)).total_qty

    def is_fully_assigned(self) -> bool:
        return len(self.unassigned) == 0

    # ------------------------------------------------------------------
    # Reporting
    # ------------------------------------------------------------------

    def print_summary(self) -> None:
        """Print a human-readable dispatch report to stdout."""
        W = 72
        print("=" * W)
        print(f"  DISPATCH PLAN  —  Period: {self.period_label}")
        print("=" * W)

        # Iterate lines in the order they were first registered in
        # line_status (dicts preserve insertion order), rather than a
        # single hardcoded global preference.
        for line_name, ls in self.line_status.items():
            recs = self.orders_for_line(line_name)
            print(f"\n{'─' * W}")
            print(
                f"  {line_name}  │  {ls.n_orders} order(s)  │  "
                f"{ls.total_qty} parts total"
            )
            print(f"{'─' * W}")

            if not recs:
                print("    (no orders assigned)")
                continue

            for rec in recs:
                feasible_str = ", ".join(rec.feasible_lines)

                print(
                    f"    {rec.sachnummer:<14}  {rec.kunde:<28}  "
                    f"qty={rec.quantity:<6}  class={rec.product_class}"
                )
                print(
                    f"    {'':14}  stations  : "
                    f"{chr(32).join(rec.station_sequence)}"
                )
                print(
                    f"    {'':14}  freigabe  : {rec.freigabe:<32}"
                    f"feasible on: {feasible_str}"
                )
                if rec.note:
                    print(f"    {'':14}  ⚠ NOTE    : {rec.note}")

        # Unassigned block
        if self.unassigned:
            print(f"\n{'─' * W}")
            print(f"  UNASSIGNED ORDERS  ({len(self.unassigned)})")
            print(f"{'─' * W}")
            for u in self.unassigned:
                print(
                    f"    {u.product_id:<14}  qty={u.quantity:<6}  "
                    f"reason: {u.reason}"
                )
        else:
            print(
                f"\n  ✓ All {sum(r.quantity for r in self.orders)} parts "
                f"across {len(self.orders)} orders successfully assigned."
            )

        # Grand total
        total_assigned = sum(r.quantity for r in self.orders)
        total_unassigned = sum(u.quantity for u in self.unassigned)
        print(f"\n{'─' * W}")
        print(
            f"  Grand total:  {total_assigned} parts assigned  │  "
            f"{total_unassigned} parts unassigned"
        )
        print("=" * W)


def get_line_orders(plan: DispatchPlan, line_name: str) -> list[OrderRecord]:
    """Return the ordered work queue for one line (assignment/FIFO order)."""
    return plan.orders_for_line(line_name)


# ---------------------------------------------------------------------------
# Priority strategy — pluggable line-selection rule
# ---------------------------------------------------------------------------

class LinePriorityStrategy:
    """
    Base interface for "which lines, in what order, should this product
    be tried on" decisions.

    Subclass this (or duck-type it) to implement more complicated rules
    later without touching the calling dispatcher's own assignment
    machinery — only `order()` needs to change.
    """

    def order(self, info: ProductLineInfo, active_lines: list[str]) -> list[str]:
        """
        Return the lines this product may run on, restricted to
        `active_lines`, ordered from most- to least-preferred.
        An empty list means "no feasible line" (order goes unassigned).
        """
        raise NotImplementedError


class MatrixPriorityStrategy(LinePriorityStrategy):
    """
    Default rule: priority comes from PRODUCT_MATRIX itself, per product.

    - Only lines with Freigabe VORHANDEN carry a real priority
      (LineClass.priority, e.g. "1" / "2" / "3" — 1 = most preferred).
      They are tried first, sorted by that priority ascending.
    - Lines with Freigabe MOEGLICH have no priority by definition — they
      are always tried after every VORHANDEN line, in `active_lines`
      order (stable, deterministic, but not "prioritised" among
      themselves).
    - Lines with Freigabe NICHT_MOEGLICH (or not feasible at all) never
      appear.
    - A VORHANDEN line whose priority field is missing/unparseable is
      still tried before MOEGLICH lines, just after the ones that do
      have a numeric priority (keeps `order()` total even with
      incomplete data instead of silently dropping the line).
    """

    @staticmethod
    def _priority_rank(raw: Optional[str]) -> int:
        """Parse LineClass.priority into a sortable int; unparseable/None
        sorts after every real number but still before MOEGLICH lines."""
        if raw is None:
            return 10_000
        try:
            return int(str(raw).strip())
        except ValueError:
            return 10_000

    def order(self, info: ProductLineInfo, active_lines: list[str]) -> list[str]:
        vorhanden: list[str] = []
        moeglich: list[str] = []

        for ln in active_lines:
            lc = info.lines.get(ln)
            if lc is None or not lc.is_feasible:
                continue  # not present, or NICHT_MOEGLICH
            if lc.freigabe == Freigabe.VORHANDEN:
                vorhanden.append(ln)
            elif lc.freigabe == Freigabe.MOEGLICH:
                moeglich.append(ln)

        vorhanden.sort(key=lambda ln: self._priority_rank(info.lines[ln].priority))

        # moeglich keeps active_lines order (no priority concept applies)
        return vorhanden + moeglich


class FixedPriorityStrategy(LinePriorityStrategy):
    """
    Legacy rule: a single fixed preference order applied to every product,
    regardless of what PRODUCT_MATRIX says (e.g. HTL3 → HTL5 → HTL6).
    Kept for comparison / rollback purposes; no longer the default.
    """

    def __init__(self, preference: Optional[list[str]] = None):
        self.preference = preference or DEFAULT_LINE_PREFERENCE

    def order(self, info: ProductLineInfo, active_lines: list[str]) -> list[str]:
        feasible = set(info.feasible_lines())
        return [ln for ln in self.preference if ln in active_lines and ln in feasible]
