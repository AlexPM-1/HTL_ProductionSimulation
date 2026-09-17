"""
sim/fill/push/dispatch.py
============================
push_dispatch_process() — assigns a push (class-2) order to a line and
places it on that line's chute. One instance spawned per
CustomerDemand row by sim.runner.run_mixed(). Uses
sim.fill.push.chunking.build_push_order_record()/split_into_chunks().
"""

from __future__ import annotations

from typing import Optional

from domain.orders import UnassignedOrder
from domain.line_priority import MatrixPriorityStrategy
from domain.products import lookup as _plm_lookup
from domain.timeparse import row_due_datetime as _row_due_datetime
from sim.context import RunContext

from sim.fill.push.chunking import build_push_order_record, split_into_chunks


def push_dispatch_process(ctx: RunContext, row):
    """
    SimPy generator — the full lifecycle of ONE CustomerDemand (push) row,
    from "not yet visible" through "placed on a line's chute". One of
    these is spawned per row by sim.runner.run_mixed(); it does NOT wait
    for the order to actually finish producing (that's
    sim.drain.push_turn.run_push_turn() (via crew_process)'s job,
    decoupled via the chute) — it only decides WHERE the order goes and
    WHEN it becomes visible/placed.

    Assignment rules implemented here, in order:
      1. Resolve PRODUCT_MATRIX compatibility + priority ordering
         (MatrixPriorityStrategy — "the line with the highest priority /
         preference" first).
      2. Not visible yet (more than policy.push_visibility_days before
         its due date) -> sleep until it becomes visible. This is a
         planning-awareness window only (the order "exists" to the
         system) — nothing is placed yet even once it opens; see step 3.
      3. Still more than policy.max_lead_time_h before the due date ->
         keep sleeping (this is the "not before 24h" bound — production
         shouldn't start/occupy an exotic Supermarket slot any earlier
         than necessary). Once within that window, the busy/retry search
         begins.
      4. Try each compatible line in priority order; the first one that
         is NOT busy (ctx.is_busy) AND is on-shift right now (ctx.
         is_line_on) gets the order. An off-shift line is excluded from
         this search exactly like a busy one — it is simply not a valid
         candidate this iteration, not something to wait out mid-check.
      5. If every compatible line is busy or off-shift, and the order is
         not yet within policy.rush_threshold_h of its due date -> wait
         policy.retry_interval_h and retry from step 4 (re-checking rush
         eligibility each time round, since the clock keeps moving).
      6. Once within policy.rush_threshold_h of the due date: stop
         searching for "free", and force-assign to whichever compatible
         line is currently LEAST busy (ctx.load, min) among those that
         are ON-SHIFT right now — an off-shift line is NEVER force-
         assigned, no matter how urgent the order (an off line "cannot be
         used", full stop; there is no override for that). If every
         compatible line is off-shift at this point, this step falls
         back to the SAME wait-and-retry as step 5 (policy.
         retry_interval_h) rather than forcing an assignment — it simply
         re-checks the rush condition (and on-shift lines) again next
         iteration.

    policy.ideal_lead_time_h is NOT read anywhere in this function — it's
    a reporting/target reference point only, used when comparing
    delivered_date against due_date afterwards (see
    OrderRecord.delivery_delta_h / push_delivery_summary). There's no
    single moment in this rule set where "aim for exactly 5h early"
    would even mean anything operationally; it's what a KPI dashboard
    compares actual outcomes against.

    Live-editing note (see sim.runner.apply_push_policy()): rush_threshold_h and
    retry_interval_h are read fresh on every loop iteration below, so
    editing ctx.policy's fields in place takes effect immediately for
    every order still in the search loop. push_visibility_days and
    max_lead_time_h are each read ONCE, at the two `yield
    env.timeout(...)` points below, to compute a sleep duration — an
    edit only affects orders whose dispatch process hasn't reached that
    point yet; an order already mid-sleep keeps sleeping for the
    duration it originally computed (a plain SimPy timeout can't be
    retroactively shortened).

    Orders with no PRODUCT_MATRIX entry, no parseable due date, or no
    feasible/active line at all are recorded to ctx.unassigned_log (if
    given) and dropped — same "can't be placed" bucket UnassignedOrder
    already represents elsewhere in the codebase.
    """
    kenv = ctx.kenv
    env = kenv.env

    try:
        info = _plm_lookup(row.product_id)
    except ValueError:
        if ctx.unassigned_log is not None:
            ctx.unassigned_log.append(UnassignedOrder(
                row.period_label, row.product_id, row.total_qty,
                "product not found in PRODUCT_MATRIX",
            ))
        if ctx.verbose:
            print(f"  ⚠ push: {row.product_id!r} not in PRODUCT_MATRIX — dropping.")
        return

    due_dt = _row_due_datetime(row)
    if due_dt is None:
        if ctx.unassigned_log is not None:
            ctx.unassigned_log.append(UnassignedOrder(
                row.period_label, row.product_id, row.total_qty,
                "unparseable due date (Date/Time cell)",
            ))
        if ctx.verbose:
            print(f"  ⚠ push: {row.product_id!r} has an unparseable due date "
                  f"({row.period_label!r} {getattr(row, 'time_slot', '')!r}) — dropping.")
        return
    due_t = (due_dt - ctx.epoch).total_seconds()

    candidate_lines = MatrixPriorityStrategy().order(info, ctx.active_lines)
    if not candidate_lines:
        if ctx.unassigned_log is not None:
            ctx.unassigned_log.append(UnassignedOrder(
                row.period_label, row.product_id, row.total_qty,
                f"no feasible line among active lines {ctx.active_lines}",
            ))
        if ctx.verbose:
            print(f"  ⚠ push: {row.product_id!r} has no feasible line among "
                  f"{ctx.active_lines} — dropping.")
        return

    # --- visibility window (rule 2) -----------------------------------------
    visible_t = due_t - ctx.policy.push_visibility_days * 24 * 3600.0
    if env.now < visible_t:
        yield env.timeout(visible_t - env.now)

    # --- max-lead-time gate (rule 3): "not before 24h" ----------------------
    # Visible != actionable. The order enters the system's awareness at
    # push_visibility_days out, but nothing gets placed until we're within
    # max_lead_time_h of the due date — placing (and occupying an exotic
    # Supermarket slot) any earlier than necessary is exactly what this
    # bound exists to prevent.
    attempt_open_t = due_t - ctx.policy.max_lead_time_h * 3600.0
    if env.now < attempt_open_t:
        yield env.timeout(attempt_open_t - env.now)

    # --- placement search (rules 4-6) ---------------------------------------
    assigned_line: Optional[str] = None
    rush = False
    while True:
        hours_to_due = (due_t - env.now) / 3600.0
        if hours_to_due <= ctx.policy.rush_threshold_h:
            # Rush override: force-assign to the least-busy compatible
            # line, but ONLY among lines that are on-shift right now. An
            # off-shift line "cannot be used", full stop — rush urgency
            # does not override that; see the docstring above (confirmed
            # decision, not an oversight). If every compatible line
            # happens to be off-shift at this instant, fall through to
            # the exact same wait-and-retry as the non-rush branch below
            # and re-check both conditions (rush eligibility AND on-shift)
            # next iteration.
            on_lines = [ln for ln in candidate_lines if ctx.is_line_on(ln)]
            if on_lines:
                assigned_line = min(on_lines, key=ctx.load)
                rush = True
                break
            if ctx.verbose:
                print(f"  [t={env.now:10.1f}] push: all compatible lines "
                      f"OFF-shift for {row.product_id!r} (due in "
                      f"{hours_to_due:.1f}h, within rush window) — "
                      f"retrying in {ctx.policy.retry_interval_h}h.")
            yield env.timeout(ctx.policy.retry_interval_h * 3600.0)
            continue

        found = next(
            (ln for ln in candidate_lines if not ctx.is_busy(ln) and ctx.is_line_on(ln)),
            None,
        )
        if found is not None:
            assigned_line = found
            break

        if ctx.verbose:
            print(f"  [t={env.now:10.1f}] push: all compatible lines busy or "
                  f"off-shift for {row.product_id!r} (due in "
                  f"{hours_to_due:.1f}h) — retrying in "
                  f"{ctx.policy.retry_interval_h}h.")
        yield env.timeout(ctx.policy.retry_interval_h * 3600.0)

    # --- build + enqueue (rule 5 for rush; normal ranking otherwise) ------
    note = (
        f"RUSH placement (<= {ctx.policy.rush_threshold_h}h before due date)"
        if rush else None
    )
    order = build_push_order_record(kenv, row, info, assigned_line, due_dt, note=note)
    order.record_assignment(assigned_line)

    line_id = ctx.line_id(assigned_line)
    chute = ctx.chutes[line_id]
    chunks = split_into_chunks(order, ctx.chunk_size)
    for chunk in chunks:
        if rush:
            chute.push_rush_entry(order.sachnummer, payload=chunk)
            if ctx.chute_tracker is not None:
                ctx.chute_tracker.deposit(assigned_line, order.sachnummer, env.now, rush=True)
        else:
            chute.push_chunk(order.sachnummer, priority="M", payload=chunk)
            if ctx.chute_tracker is not None:
                ctx.chute_tracker.deposit(assigned_line, order.sachnummer, env.now, rush=False)
        # No explicit wake needed here — sim.runner._install_crew_chute_hooks()
        # wraps push_chunk()/push_rush_entry() to notify ctx.activity_signal
        # after every insertion, so every idle crew re-checks automatically.
    n_chunks = len(chunks)

    if ctx.verbose:
        print(f"  [t={env.now:10.1f}] push: {row.product_id!r} qty={order.quantity} "
              f"-> {assigned_line} ({n_chunks} chunk(s)){' [RUSH]' if rush else ''}, "
              f"due {due_dt.isoformat()}.")
