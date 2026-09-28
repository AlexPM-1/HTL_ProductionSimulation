"""
sim/fill/push/splitting.py
===========================
build_push_order_record() and split_into_push_cards() — both called by
sim.fill.push.dispatch.push_dispatch_process(). split_into_push_cards() is
pure data slicing (no chute/env side effects); dispatch.py does its own
enqueueing (push_rush_entry/enter_push_cards + chute_tracker.deposit) over
the list it returns.
"""

from __future__ import annotations

import datetime as _dt
from dataclasses import replace as _dc_replace
from typing import Optional

from domain.orders import OrderRecord
from domain.products import ProductLineInfo
from sim.resources.environment import SimEnvironment


def build_push_order_record(
    menv: SimEnvironment,
    row,
    info: ProductLineInfo,
    assigned_line: str,
    due_dt: _dt.datetime,
    note: Optional[str] = None,
) -> OrderRecord:
    """
    Resolve one PushCustomerDemand row + its chosen production line into a
    full OrderRecordPush — due_date populated straight from the row's
    own (Date, Time); delivered_date stays None until run_push_turn()
    (sim/drain/push_turn.py) sets it on the specific push card that
    actually finishes.

    Created via menv.create_order(kind="push", ...) (rather than
    constructing OrderRecord directly) so order_id comes from the one
    shared push/pull counter and the result is registered in
    menv.order_registry — see SimEnvironment.create_order().
    """
    lc = info.lines[assigned_line]
    return menv.create_order(
        kind="push",
        period_label=row.period_label,
        product_number=row.product_id,
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


# Alias kept for backward compatibility with any external caller still
# using the old private name.
_build_push_order_record = build_push_order_record


def split_into_push_cards(order: OrderRecord, push_card_size: int) -> list[OrderRecord]:
    """
    Split `order.quantity` into consecutive OrderRecord copies of at
    most `push_card_size` pieces each (`dataclasses.replace(order,
    quantity=qty)` per push card). Every push card carries the SAME order_id as
    `order` (dataclasses.replace() only overrides `quantity` here, so
    order_id — and every other field, including the shared
    `assignments` list object — passes through unchanged). Pure: no
    chute / chute_tracker / env side effects — the caller
    (push_dispatch_process) is responsible for actually enqueueing each
    returned push card.
    """
    push_cards: list[OrderRecord] = []
    remaining = order.quantity
    while remaining > 0:
        qty = min(push_card_size, remaining)
        push_cards.append(_dc_replace(order, quantity=qty))
        remaining -= qty
    return push_cards
