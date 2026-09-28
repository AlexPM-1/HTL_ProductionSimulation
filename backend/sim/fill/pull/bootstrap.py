"""
sim/fill/pull/bootstrap.py
============================
start_mixed_simulation() — the top-level launcher that wires up
Movement 1 (chute FILLING) on a MixedSimEnvironment. Called by
sim.runner.run_mixed(), which builds ONE complete RunContext up front
(RunContext.for_mixed() — see sim/context.py) and passes it in here;
this function only *registers processes* on that RunContext, it does
not construct one. run_mixed() then separately spawns the
crew_process() pool (sim/drain/) that drains every chute.

Calling this on its own, with no crews spawned alongside it, wires a
pull side that fills chutes forever and never drains them.
"""

from __future__ import annotations

from sim.context import RunContext

from sim.fill.pull.withdrawal import withdrawal_process
from sim.fill.pull.collection_box import collection_box_emptying_process
from sim.fill.pull.day_boundary import day_boundary_process


def start_mixed_simulation(ctx: RunContext, verbose: bool = True) -> RunContext:
    """
    Wire up Movement 1 (chute FILLING) on every line of `ctx.menv`: one
    withdrawal_process (whole-plant, drives PullCustomerDemand), one
    day_boundary_process (whole-plant, per-day KPI/Restmenge/backlog
    checkpoint — see that function), plus one
    collection_box_emptying_process per line.

    This wires filling only — draining every chute (Movement 2) is
    crew_process()'s job now (sim/drain/), spawned separately by
    run_mixed(). Call this once, right after
    `RunContext.for_mixed(...)` has built a complete `ctx` (pull-side
    AND push-side fields already populated — see sim/context.py), then
    call env.run(until=...) yourself (a horizon is recommended as a
    safety net — see estimate_horizon_s() — since day_boundary_process
    and withdrawal_process never naturally terminate for a multi-day
    sheet).

    This function does NOT construct a RunContext — it only registers
    processes on the one it's given. `ctx.snapshot_log` / `ctx.daily_log`
    / `ctx.shortfall_log` / `ctx.day_start_hour` / `ctx.day_length_s`
    are expected to already be set by `RunContext.for_mixed(...)`.

    Returns `ctx` unchanged, in case the caller wants to inspect/extend
    the wiring (e.g. to add transport-time processes later).
    """
    menv = ctx.menv
    env = menv.env

    # Double-filling fix.
    #
    # build_mixed_environment() deliberately reuses build_environment()'s
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
    # — where finished_goods_inv is resolved purely from menv.inventories
    # (whichever lane's upstream_station matches the route's last station).
    # In the Kanban model, on_finish (the crew-loop's closure, see
    # sim/drain/pull_turn.py) ALREADY deposits the same passed piece into
    # the Supermarket via deposit_finished_pcs(). Leaving Inv_nach_HTL
    # wired into menv.inventories means every passed piece is counted
    # twice: once into the Supermarket, once into Inv_nach_HTL.
    #
    # Fix: rebuild menv.inventories here, right before the loop starts,
    # dropping any inventory lane that is a terminal sink (downstream_station
    # is None — the config-level trait that distinguishes Inv_nach_HTL from
    # Inv_nach_ECM/Inv_nach_Loch/Inv_nach_DRS, which all feed a real
    # downstream station and must stay). part_lifecycle's own lookup then
    # resolves finished_goods_inv=None and silently skips the deposit — no
    # change to that shared function, and no change needed to
    # sim.resources.build either, since build_mixed_environment()'s
    # output stays the generic "same wiring as push" its own docstring
    # promises; this is purely a Kanban-runtime-time filter.
    menv.inventories = {
        inv_name: {
            lane_name: inv for lane_name, inv in lanes.items()
            if inv.inventory_cfg.downstream_station is not None
        }
        for inv_name, lanes in menv.inventories.items()
    }
    menv.inventories = {
        inv_name: lanes for inv_name, lanes in menv.inventories.items() if lanes
    }

    # Seed-state visibility.
    #
    # build_mixed_environment() seeds every Supermarket's starting cards
    # by mutating sm.store.items directly (see sim.resources.build),
    # so none of that initial stock is captured in ctx.snapshot_log yet.
    # Every stock chart downstream (build_supermarket_timeseries /
    # build_line_units_timeseries) would otherwise silently start
    # mid-story: the first point it could show would be whatever the
    # first real withdrawal/deposit had already changed the stock to,
    # not the actual starting quantity (e.g. HTL3 starting at 32 cards).
    #
    # This runs here (nothing has executed yet — env.now == 0) rather
    # than inside build_mixed_environment() only because `ctx` isn't
    # threaded into the build layer; it is no longer a workaround for
    # `ctx` not existing (it always exists by the time this function is
    # called — see RunContext.for_mixed()).
    for line_id, lane in menv.supermarkets.items():
        for product_type, sm in lane.items():
            ctx.record_supermarket(line_id, product_type, "initial", sm)

    env.process(withdrawal_process(ctx, verbose=verbose))
    env.process(day_boundary_process(ctx, verbose=verbose))
    for line in menv.lines:
        env.process(collection_box_emptying_process(ctx, line.line_id, verbose=verbose))

    return ctx
