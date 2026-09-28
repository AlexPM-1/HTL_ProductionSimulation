"""
sim/fill/pull/collection_box.py
=================================
collection_box_emptying_process() — Collection Box -> Batch-Size
Collector -> [threshold reached] -> Kanban Chute. Started by
sim.fill.pull.bootstrap.start_mixed_simulation(), one instance per
line.
"""

from __future__ import annotations

from sim.context import RunContext


def collection_box_emptying_process(ctx: RunContext, line_id: int, verbose: bool = True):
    """
    SimPy generator - one instance per line. Every
    cfg.pull_timing_config.collection_box_emptying_min minutes, empties that
    line's CollectionBoxResource into its BatchCollectorResource, then
    checks every product touched for release readiness (cards_to_trigger
    reached) and pushes any newly-ready batch onto the ChuteResource.

    A `while` (not `if`) on is_ready() so a bucket that has accumulated
    more than one threshold's worth of cards over several emptying
    cycles is fully drained in one go, releasing multiple batches.
    """
    menv = ctx.menv
    env = menv.env
    cadence_s = menv.cfg.pull_timing_config.collection_box_emptying_min * 60.0
    _ORDER_EPSILON_S = 1e-6  # Forces this process to always resolve AFTER
                              # withdrawal_process whenever their cadences
                              # land on the same simulated instant,
                              # regardless of env.process() registration
                              # order or cadence values. Only the first
                              # tick is nudged; since this is a relative
                              # `timeout(cadence_s)` loop, the offset
                              # propagates to every later tick with no drift.

    cb = menv.collection_box_for(line_id)
    bc = menv.batch_collector_for(line_id)
    chute = menv.pull_chute_for(line_id)
    line_name = menv.lines[line_id - 1].line_name

    first_tick = True
    while True:
        yield env.timeout(cadence_s + (_ORDER_EPSILON_S if first_tick else 0.0))
        first_tick = False

        # TRANSPORT-TIME HOOK: time for logistics personnel to physically
        # empty the box and walk the cards over to the batch collector
        # would be `yield env.timeout(empty_walk_time_s)` right here.
        emptied = cb.empty(env.now)
        if not emptied:
            continue

        touched_products: set[str] = set()
        for card in emptied:
            bc.add(card)
            card.record_transition("in_batch_collector", env.now)
            touched_products.add(card.product_type)
            if card.current_history is not None:
                card.current_history.t_collection_box_end = round(env.now, 3)
                card.current_history.t_batch_collector_start = round(env.now, 3)

        if verbose:
            print(f"  [t={env.now:10.1f}] {line_name}(L{line_id}): "
                  f"Collection Box emptied — {len(emptied)} card(s), "
                  f"products={sorted(touched_products)}")

        for product_type in touched_products:
            while bc.is_ready(product_type):
                released = bc.pop_batch(product_type)
                for c in released:
                    c.record_transition("released_to_chute", env.now)
                    if c.current_history is not None:
                        c.current_history.t_batch_collector_end = round(env.now, 3)
                        c.current_history.t_chute_start = round(env.now, 3)
                chute.enter_pull_cards(product_type, released)
                if verbose:
                    print(f"  [t={env.now:10.1f}] {line_name}(L{line_id}): "
                          f"Batch-Size Collector RELEASED {len(released)} "
                          f"card(s) of {product_type!r} -> Kanban Chute "
                          f"(priority={released[0].priority})")
                # NOTE: chute.enter_pull_cards() already wakes any idle crew —
                # sim.runner.run_mixed()'s _install_crew_chute_hooks()
                # wraps enter_pull_cards() to notify activity_signal on every
                # insertion, and that wrapper is installed before
                # start_mixed_simulation() ever runs, so the enter_pull_cards()
                # call two lines up already wakes any idle crew.
