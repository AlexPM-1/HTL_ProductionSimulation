"""
sim/fill/push/chunking.py
===========================
build_push_order_record() and split_into_chunks() — both called by
sim.fill.push.dispatch.push_dispatch_process(). split_into_chunks() is
pure data slicing (no chute/env side effects); dispatch.py does its own
enqueueing (push_rush_entry/push_chunk + chute_tracker.deposit) over
the list it returns.
"""

from __future__ import annotations

import datetime as _dt
from dataclasses import replace as _dc_replace
from typing import Optional

from domain.orders import OrderRecord
from domain.products import ProductLineInfo


def build_push_order_record(
    row,
    info: ProductLineInfo,
    assigned_line: str,
    due_dt: _dt.datetime,
    note: Optional[str] = None,
) -> OrderRecord:
    """
    Resolve one CustomerDemand row + its chosen production line into a
    full OrderRecord — due_date populated straight from the row's own
    (Date, Time); delivered_date stays None until run_push_turn()
    (sim/drain/push_turn.py) sets it on the specific chunk that
    actually finishes.
    """
    lc = info.lines[assigned_line]
    return OrderRecord(
        period_label=row.period_label,
        sachnummer=row.product_id,
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


def split_into_chunks(order: OrderRecord, chunk_size: int) -> list[OrderRecord]:
    """
    Split `order.quantity` into consecutive OrderRecord copies of at
    most `chunk_size` pieces each (`dataclasses.replace(order,
    quantity=qty)` per chunk). Pure: no chute / chute_tracker / env
    side effects — the caller (push_dispatch_process) is responsible
    for actually enqueueing each returned chunk.
    """
    chunks: list[OrderRecord] = []
    remaining = order.quantity
    while remaining > 0:
        qty = min(chunk_size, remaining)
        chunks.append(_dc_replace(order, quantity=qty))
        remaining -= qty
    return chunks
