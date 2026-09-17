"""
sim/drain/push_turn.py
========================
run_push_turn() — called by sim/drain/crew.py.
"""

from __future__ import annotations

import datetime as _dt
from typing import Optional

from domain.orders import OrderRecord
from sim.context import RunContext
from sim.produce.run_order import run_one_order
from sim.fill.push.exotic import (
    _deposit_push_chunk_to_supermarket,
    _withdraw_push_chunk_process,
)
from telemetry.records import ScheduleEvent, GateActivityEntry, PushDeliveryRecord


def run_push_turn(
    ctx: RunContext,
    line_id: int,
    line_name: str,
    n_workers: int,
    event_log: Optional[list[ScheduleEvent]],
    crew_id: int,
):
    """
    SimPy generator — one full push "turn": pop and run consecutive push
    ChuteEntry objects off the front of this line's chute, one at a time,
    for as long as the front stays a push entry of the SAME product_type
    as the first one taken this turn — "take all the cards of the push
    order". An order's chunks are dispatched back-to-back under the same
    sachnummer (see sim.fill.push.dispatch.push_dispatch_process), so a
    same-product run of push entries is, in practice, one order; this is
    a stated assumption, not verified against a scenario where two
    DIFFERENT push orders of the same product happen to sit adjacent in
    one line's chute.

    Per popped chunk (gate admission is already held by the caller —
    crew_process — for the whole turn, so there's no separate per-chunk
    gate request here): execute via run_one_order(), tag event_log
    entries "push", append a GateActivityEntry, stamp delivered_date +
    PushDeliveryRecord, deposit into the exotic Supermarket, and spawn
    the detached due-date withdrawal process.
    """
    kenv = ctx.kenv
    env = kenv.env
    gate = ctx.gates[line_id]
    chute = ctx.chutes[line_id]

    front = chute.peek_next_of_class("push")
    if front is None:
        return
    product_type = front.product_type

    while True:
        front = chute.peek_next_of_class("push")
        if front is None or front.product_type != product_type:
            break

        t_start = env.now
        entry = chute.pop_next_of_class("push")
        if ctx.chute_tracker is not None:
            ctx.chute_tracker.drain_one(line_name, env.now)
        chunk: OrderRecord = entry.payload
        possible_changeover = (
            gate.current_rec is None
            or getattr(gate.current_rec, "sachnummer", None) != chunk.sachnummer
        )

        chunk.t_production_start = t_start
        # `chunk` is a dataclasses.replace() copy made by
        # sim.fill.push.chunking.split_into_chunks() — a distinct object
        # from the OrderRecordPush kenv.order_registry[chunk.order_id]
        # actually holds (only `assignments`, a shared mutable list, is
        # kept in sync across copies automatically). Reports read the
        # registry entry, so mirror this chunk's timing onto it too —
        # min(start)/max(end) across every chunk so a split order still
        # reports one sensible overall production window.
        _reg_order = kenv.order_registry.get(chunk.order_id)
        if _reg_order is not None:
            _reg_order.t_production_start = (
                t_start if _reg_order.t_production_start is None
                else min(_reg_order.t_production_start, t_start)
            )

        _log = kenv.event_log if event_log is None else event_log
        _start_idx = len(_log)
        ran = yield from run_one_order(
            kenv, line_id, gate.current_rec, chunk, n_workers, ctx.verbose
        )
        # Same "push" tagging and crew_id back-fill as _run_one_pull_card
        # (sim/drain/pull_turn.py) — see that function's comment.
        for _ev in _log[_start_idx:]:
            if _ev.line_id == line_id and _ev.sim_class is None:
                _ev.sim_class = "push"
            if _ev.line_id == line_id and _ev.crew_id is None:
                _ev.crew_id = crew_id
        if ran:
            gate.current_rec = chunk
        t_end = env.now
        chunk.t_production_end = t_end
        if _reg_order is not None:
            _reg_order.t_production_end = (
                t_end if _reg_order.t_production_end is None
                else max(_reg_order.t_production_end, t_end)
            )
        gate.touch(t_end)
        if ctx.gate_activity_log is not None:
            ctx.gate_activity_log.append(GateActivityEntry(
                t_start=t_start, t_end=t_end, line_id=line_id,
                sachnummer=chunk.sachnummer, production_type="push",
                crew_id=crew_id,
                possible_changeover=possible_changeover,
                quantity=chunk.quantity,
            ))

        chunk.delivered_date = ctx.epoch + _dt.timedelta(seconds=env.now)
        if _reg_order is not None:
            _reg_order.delivered_date = (
                chunk.delivered_date if _reg_order.delivered_date is None
                else max(_reg_order.delivered_date, chunk.delivered_date)
            )
        if ctx.delivery_log is not None:
            ctx.delivery_log.append(PushDeliveryRecord(
                sachnummer=chunk.sachnummer,
                assigned_line=chunk.assigned_line,
                quantity=chunk.quantity,
                due_date=chunk.due_date,
                delivered_date=chunk.delivered_date,
                delivery_delta_h=chunk.delivery_delta_h,
                rush=bool(chunk.note and "RUSH" in chunk.note),
            ))

        _deposit_push_chunk_to_supermarket(ctx, line_name, chunk)
        env.process(_withdraw_push_chunk_process(ctx, line_name, chunk))

        if ctx.verbose:
            delta = chunk.delivery_delta_h
            delta_str = f", delivery_delta={delta:+.2f}h" if delta is not None else ""
            print(f"  [t={env.now:10.1f}] {line_name}(L{line_id}): push chunk done "
                  f"{chunk.sachnummer} qty={chunk.quantity}{delta_str}")

        ctx.notify()
