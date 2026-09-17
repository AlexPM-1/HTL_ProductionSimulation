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

from dataclasses import dataclass
from datetime import datetime
from typing import Optional


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
