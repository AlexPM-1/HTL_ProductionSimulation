"""
sim/fill/pull/withdrawal.py
============================
Drives CustomerDemandKanban: one independent, repeating withdrawal
stream per row. Started by
sim.fill.pull.bootstrap.start_kanban_simulation().
"""

from __future__ import annotations

from sim.context import RunContext
from sim.resources.cards import KanbanCard
from domain.timeparse import (
    parse_datetime as _parse_kanban_timestamp,
    row_datetime_string as _row_datetime_string,
)

from sim.fill.pull.assignment import select_supermarket_for_withdrawal


def withdrawal_process(rt: RunContext, verbose: bool = True):
    """
    SimPy generator - drives the whole (possibly multi-day)
    CustomerDemandKanban sheet (flat layout: one row = one demand
    event, Date | Time | Product | TotalQuantity | LineId).

    This doesn't fire one shared "tick" for the whole sheet. Every row
    launches its OWN independent, repeating withdrawal stream
    (_withdrawal_stream(), fire-and-forget SimPy process) at the moment
    this generator runs (t=0) — each stream then sleeps on its own until
    ITS row's (Date, Time) instant before doing any actual withdrawing.
    See _withdrawal_stream()'s docstring for the per-row mechanics.

    Row scheduling within a stream supports two modes:

      - Absolute mode: if the row's (Date, Time) parses as a real
        "DD.MM.YYYY HH:MM[:SS]" timestamp AND rt.sim_epoch was
        successfully derived (i.e. at least one row in the sheet has a
        date — see RunContext/domain.epoch.compute_epoch), the stream
        starts at the EXACT simpy-clock instant that timestamp maps to:
        `(dt - rt.sim_epoch).total_seconds()`.
      - Legacy mode: a row that doesn't parse as an absolute timestamp
        (bare "HH:MM" Time with no usable Date, or the whole sheet has
        no dates at all so rt.sim_epoch is None) starts its stream
        immediately at t=0 — there's no "whole sheet" fixed-cadence tick
        to anchor a delay to, since every row is independently scheduled.

    A sheet mixing both row shapes isn't the expected case, but nothing
    here requires uniformity — each row's stream is scheduled
    independently off its own cells.
    """
    kenv = rt.kenv
    env = kenv.env
    yield env.timeout(0)  # keep this a proper SimPy process even though
                           # all real scheduling now happens per-row,
                           # inside each _withdrawal_stream() below.

    for evt in kenv.cfg.kanban_withdrawals:
        dt = _parse_kanban_timestamp(_row_datetime_string(evt))
        if dt is not None and rt.sim_epoch is not None:
            start_t = (dt - rt.sim_epoch).total_seconds()
        else:
            start_t = env.now
        env.process(_withdrawal_stream(rt, evt, start_t, verbose))


def _n_cards_for_row(rt: RunContext, evt: "KanbanWithdrawalEvent") -> int:
    """
    Number of whole Kanban cards one CustomerDemandKanban row's
    TotalQuantity is worth: `quantity // batch_size` (e.g. "200 pieces =
    1 card"). `batch_size` comes from KanbanCardsSetup
    (cfg.kanban_cards[evt.product].batch_size), falling back to 200 if
    the product has no KanbanCardsSetup row at all.

    Any remainder (TotalQuantity not evenly divisible by batch_size) is
    dropped, with a warning — a partial card can't physically be
    withdrawn, same "no partial withdrawals" rule as
    SupermarketSlotConfig.initial_pcs_partial.
    """
    card_cfg = rt.kenv.cfg.kanban_cards.get(evt.product)
    batch_size = card_cfg.batch_size if card_cfg else 200
    if batch_size <= 0:
        return 0

    n_cards, remainder = divmod(evt.quantity, batch_size)
    if remainder:
        print(f"  ⚠ CustomerDemandKanban {evt.date} {evt.time}: "
              f"{evt.product!r} quantity={evt.quantity} isn't an exact "
              f"multiple of batch_size={batch_size} — {remainder} pcs "
              f"dropped (no partial-card withdrawal).")
    return n_cards


def _withdrawal_stream(rt: RunContext, evt: "KanbanWithdrawalEvent",
                        start_t: float, verbose: bool):
    """
    One independent, repeating card-withdrawal stream for a single
    CustomerDemandKanban row — see withdrawal_process()'s docstring for
    the overall rule this implements.

    Sleeps until `start_t`, then withdraws one card every
    cfg.kanban_timing.withdrawal_cadence_min minutes (workbook default:
    50 min), `_n_cards_for_row(rt, evt)` times total. Two rows for the
    SAME product at the SAME timestamp but different (downstream)
    `line_id` each get their own stream and simply run in parallel — no
    summing needed here, since combining two 1-card/50min streams into
    an effective 2-cards/50min rate falls straight out of running them
    independently at the same start time.

    Each individual card withdrawal is itself launched as its own
    fire-and-forget process (_withdraw_one_card), so a stockout on one
    card never blocks the next tick of this same stream (or any other
    stream).
    """
    kenv = rt.kenv
    env = kenv.env
    cadence_s = kenv.cfg.kanban_timing.withdrawal_cadence_min * 60.0

    n_cards = _n_cards_for_row(rt, evt)
    if n_cards <= 0:
        return

    delay = start_t - env.now
    if delay > 0:
        yield env.timeout(delay)
    # delay <= 0 (duplicate/out-of-order timestamp, or legacy mode
    # already at t=0) -> start immediately rather than going negative.

    for i in range(n_cards):
        if i > 0:
            yield env.timeout(cadence_s)
        if verbose:
            print(f"  [t={env.now:10.1f}] Withdrawal tick — {evt.product!r} "
                  f"card {i + 1}/{n_cards} (downstream {evt.line_id!r}, "
                  f"row {evt.date} {evt.time})")
        env.process(_withdraw_one_card(rt, evt.product, verbose))


def _withdraw_one_card(rt: RunContext, product_type: str, verbose: bool):
    """
    Withdraw exactly ONE card's worth (one whole batch) of `product_type`.

    Which line's Supermarket fulfils this is decided fresh, right here,
    by select_supermarket_for_withdrawal() — the "most stock first"
    assignment rule; see that function's docstring for the exact policy
    and its one known blocking-side simplification.

    Card priority: CustomerDemandKanban carries no priority column, so a
    withdrawn card simply keeps whatever priority it was created/recycled
    with (see KanbanCard / create_card default).
    """
    kenv = rt.kenv
    env = kenv.env

    line_id, sm = select_supermarket_for_withdrawal(rt, product_type)
    if sm is None:
        # Defensive guard only — shouldn't normally happen given
        # build_kanban_environment only builds a (line, product)
        # Supermarket for eligible products — but kept in case none
        # were built for this product at all.
        if verbose:
            print(f"  ⚠ Withdrawal: no Supermarket configured anywhere for "
                  f"{product_type!r} — dropping this card request.")
        return
    line_name = kenv.lines[line_id - 1].line_name

    # TRANSPORT-TIME HOOK: logistics-personnel travel time to physically
    # pick a card+batch off the supermarket shelf would be simulated with
    # `yield env.timeout(pickup_time_s)` right here, before the get().

    # SHORTFALL CHECK: if the shelf is empty right now, this request is
    # about to block on sm.store.get() below — that's exactly "the
    # customer asked for a card and the supermarket had none available".
    # Checked here, synchronously, immediately before the yield — nothing
    # else in this module's cooperative SimPy processes can run between
    # this line and the get() two lines down, so the check can't race
    # against another withdrawal on the same Supermarket. See
    # ShortfallEvent's docstring for the full reasoning.
    shortfall_start = env.now if sm.n_available == 0 else None

    card: KanbanCard = yield sm.store.get()

    if shortfall_start is not None:
        rt.record_shortfall(line_id, product_type, shortfall_start, env.now)

    sm.record_withdrawal()
    rt.record_supermarket(line_id, product_type, "withdrawal", sm)
    card.record_transition("withdrawn", env.now)

    # TRANSPORT-TIME HOOK: time to physically carry the card+batch from
    # the supermarket to the collection box would be
    # `yield env.timeout(carry_time_s)` right here.
    cb = kenv.collection_box_for(line_id)
    cb.add(card)
    card.record_transition("in_collection_box", env.now)
