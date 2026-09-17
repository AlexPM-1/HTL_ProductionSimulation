"""
sim/fill/pull/bootstrap.py
============================
start_kanban_simulation() — the top-level launcher that wires up
Movement 1 (chute FILLING) on a KanbanSimEnvironment. Called by
sim.runner.run_mixed(), which wires up Movement 1 here and then
separately spawns the crew_process() pool (sim/drain/) that drains
every chute.

Calling this on its own, with no crews spawned alongside it, wires a
pull side that fills chutes forever and never drains them.
"""

from __future__ import annotations

from typing import Optional

from sim.resources.environment import KanbanSimEnvironment
from sim.context import RunContext
from telemetry.records import SupermarketSnapshot, ShortfallEvent

from sim.fill.pull.withdrawal import withdrawal_process
from sim.fill.pull.collection_box import collection_box_emptying_process
from sim.fill.pull.day_boundary import day_boundary_process


def start_kanban_simulation(kenv: KanbanSimEnvironment,
                             verbose: bool = True,
                             snapshot_log: Optional[list["SupermarketSnapshot"]] = None,
                             daily_log: Optional[list[dict]] = None,
                             shortfall_log: Optional[list["ShortfallEvent"]] = None,
                             day_start_hour: int = 6,
                             day_length_s: float = 24 * 3600.0,
                             ) -> RunContext:
    """
    Wire up Movement 1 (chute FILLING) on every line of `kenv`: one
    withdrawal_process (whole-plant, drives CustomerDemandKanban), one
    day_boundary_process (whole-plant, per-day KPI/Restmenge/backlog
    checkpoint — see that function), plus one
    collection_box_emptying_process per line.

    This wires filling only — draining every chute (Movement 2) is
    crew_process()'s job now (sim/drain/), spawned separately by
    run_mixed(). Call this once, right after build_kanban_environment(),
    then call env.run(until=...) yourself (a horizon is recommended as
    a safety net — see estimate_horizon_s() — since day_boundary_process
    and withdrawal_process never naturally terminate for a multi-day
    sheet).

    snapshot_log: pass a list in (caller-owned, same by-reference pattern
    as schedule_events.ScheduleEvent) to collect every SupermarketSnapshot
    emitted over the run — e.g. for build_line_units_timeseries() later.
    If omitted, RunContext keeps its own private list internally (the
    run still works, you just have no external handle on it).

    daily_log: pass a list in (caller-owned, same by-reference pattern as
    snapshot_log) to collect one dict per production day from
    day_boundary_process(). If omitted, RunContext keeps its own
    private list internally (recoverable via rt.daily_log).

    shortfall_log: pass a list in (caller-owned, same by-reference
    pattern as snapshot_log) to collect every ShortfallEvent emitted by
    _withdraw_one_card() — one per withdrawal request that found its
    Supermarket empty and had to wait. If omitted, RunContext keeps
    its own private list internally (recoverable via rt.shortfall_log).

    day_start_hour / day_length_s: the production-day boundary — see
    RunContext's docstring. Defaults to 06:00 / 24h; edit these at the
    call site rather than here, so this module has no hidden hardcoded
    shift-time assumption.

    Returns the RunContext, in case the caller wants to inspect/extend
    the wiring (e.g. to add transport-time processes later) — this is
    also where snapshot_log/daily_log/shortfall_log end up
    (rt.snapshot_log, rt.daily_log, rt.shortfall_log) if you didn't pass
    your own lists in.
    """
    # Double-filling fix.
    #
    # build_kanban_environment() deliberately reuses build_environment()'s
    # exact station/buffer/inventory/chute wiring, which still includes the
    # push model's terminal finished-goods inventory (Inv_nach_HTL). But
    # part_lifecycle() (sim.produce.part_lifecycle, reused HERE completely
    # unchanged) unconditionally does, on every passed part:
    #
    #     if finished_goods_inv is not None:
    #         yield finished_goods_inv.container.put(1)
    #         finished_goods_inv.record_deposit_product(part.product_type, 1)
    #     ...
    #     if on_finish is not None:
    #         on_finish(env.now, "passed")
    #
    # — where finished_goods_inv is resolved purely from kenv.inventories
    # (whichever lane's upstream_station matches the route's last station).
    # In the Kanban model, on_finish (the crew-loop's closure, see
    # sim/drain/pull_turn.py) ALREADY deposits the same passed piece into
    # the Supermarket via deposit_finished_pcs(). Leaving Inv_nach_HTL
    # wired into kenv.inventories means every passed piece is counted
    # twice: once into the Supermarket, once into Inv_nach_HTL.
    #
    # Fix: rebuild kenv.inventories here, right before the loop starts,
    # dropping any inventory lane that is a terminal sink (downstream_station
    # is None — the config-level trait that distinguishes Inv_nach_HTL from
    # Inv_nach_ECM/Inv_nach_Loch/Inv_nach_DRS, which all feed a real
    # downstream station and must stay). part_lifecycle's own lookup then
    # resolves finished_goods_inv=None and silently skips the deposit — no
    # change to that shared function, and no change needed to
    # sim.resources.build either, since build_kanban_environment()'s
    # output stays the generic "same wiring as push" its own docstring
    # promises; this is purely a Kanban-runtime-time filter.
    kenv.inventories = {
        inv_name: {
            lane_name: inv for lane_name, inv in lanes.items()
            if inv.inventory_cfg.downstream_station is not None
        }
        for inv_name, lanes in kenv.inventories.items()
    }
    kenv.inventories = {
        inv_name: lanes for inv_name, lanes in kenv.inventories.items() if lanes
    }

    env = kenv.env
    # `verbose` is threaded onto RunContext here (not just used locally)
    # so a later run_mixed() call can read it back off the same object
    # for the push side, instead of having to pass it again.
    rt = RunContext.for_kanban(
        kenv, snapshot_log=snapshot_log, daily_log=daily_log,
        shortfall_log=shortfall_log,
        day_start_hour=day_start_hour, day_length_s=day_length_s,
        verbose=verbose,
    )

    # Seed-state visibility fix.
    #
    # build_kanban_environment() seeds every Supermarket's starting cards
    # by mutating sm.store.items directly (see sim.resources.build),
    # since RunContext doesn't exist yet at that point in the build —
    # so none of that initial stock was ever captured in snapshot_log.
    # Every stock chart downstream (build_supermarket_timeseries /
    # build_line_units_timeseries) therefore silently started mid-story:
    # the first point it could ever show was whatever the first real
    # withdrawal/deposit had already changed the stock to, not the
    # actual starting quantity (e.g. HTL3 starting at 32 cards).
    #
    # Fix: now that rt exists and nothing else has run yet (env.now == 0),
    # take one "initial" snapshot of every (line, product) Supermarket
    # exactly as build_kanban_environment() left it.
    for line_id, lane in kenv.supermarkets.items():
        for product_type, sm in lane.items():
            rt.record_supermarket(line_id, product_type, "initial", sm)

    env.process(withdrawal_process(rt, verbose=verbose))
    env.process(day_boundary_process(rt, verbose=verbose))
    for line in kenv.lines:
        env.process(collection_box_emptying_process(rt, line.line_id, verbose=verbose))

    return rt
