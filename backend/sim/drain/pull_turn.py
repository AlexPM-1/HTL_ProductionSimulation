"""
sim/drain/pull_turn.py
========================
PendingPullBatch (Rule 1.a's per-line carryover state), the internal
_run_one_pull_card() helper, and run_pull_turn() — called by
sim/drain/crew.py.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from sim.context import RunContext
from sim.produce.run_pull_batch import run_one_pull_batch, _make_pull_order_record
from sim.fill.pull.cards import _return_card_to_supermarket
from telemetry.records import ScheduleEvent, GateActivityEntry


@dataclass
class PendingPullBatch:
    """
    One line's "not yet reached 4 cards" pull-batch memory — Rule 1.a's
    carryover. Created fresh (taken=0) whenever a pull turn starts on a
    product_number with no open record for this line (or a different one
    than what's now at the chute's front — see run_pull_turn);
    persisted here, keyed by line_id, whenever a turn stops short of 4
    because the chute's front no longer offers more of that
    product_number. Consulted (and completed, never restarted) by
    whichever crew next works this line and finds the same
    product_number back at the front, however much of the chute filled
    with OTHER products in between. Cleared the instant taken reaches 4.

    Deliberately NOT scoped per-crew: which crew resumes an open batch
    is irrelevant — the chute (movement 1) decides ordering, not the
    crew, so this state belongs to the line, not to whichever crew
    happens to be holding it at any given moment.
    """
    product_type: str
    taken: int = 0


def _run_one_pull_card(
    ctx: RunContext,
    line_id: int,
    line_name: str,
    entry,  # ChuteEntry, production_type == "pull", n_cards == 1 by construction
    n_workers: int,
    event_log: Optional[list[ScheduleEvent]],
    crew_id: int,
):
    """
    SimPy generator — run exactly ONE already-popped pull ChuteEntry (one
    physical PullCard) to completion. Builds the order record via
    sim.produce.run_pull_batch._make_pull_order_record(), runs it via
    run_one_pull_batch(), and deposits finished pieces back to the
    supermarket via sim.fill.pull.cards._return_card_to_supermarket().

    `crew_id` is recorded on the resulting GateActivityEntry — this is
    the "which crew produced this card" attribution the frontend reads
    (see reports.kpi.by_crew / the /api/mixed/crew_activity endpoint).

    Caller (crew_process, via run_pull_turn) already holds
    ctx.gates[line_id] for the duration of this call.
    """
    menv = ctx.menv
    env = menv.env
    gate = ctx.gates[line_id]
    product_type, cards = entry.product_type, entry.cards
    card_size = cards[0].card_size
    quantity = card_size * len(cards)

    order_rec = _make_pull_order_record(ctx, line_name, product_type, quantity)
    if order_rec is None:
        if ctx.verbose:
            print(f"  ⚠ {line_name}: {product_type!r} not feasible here — "
                  f"re-queuing card on the Kanban Chute.")
        ctx.chutes[line_id].enter_pull_cards(product_type, cards)
        return

    for c in cards:
        c.record_transition("in_production", env.now)
        if c.current_history is not None:
            c.current_history.t_chute_end = round(env.now, 3)
            c.current_history.t_production_start = round(env.now, 3)

    sm = menv.supermarket_for(line_id, product_type)
    recycle_queue: list = list(cards)

    def on_finish(t: float, status: str, _sm=sm, _rq=recycle_queue,
                  _product=product_type, _line_id=line_id):
        if status != "passed" or _sm is None:
            return
        n_whole = _sm.deposit_finished_pcs(1)
        ctx.record_supermarket(_line_id, _product, "deposit_partial", _sm)
        for _ in range(n_whole):
            if _rq:
                card = _rq.pop(0)
            else:
                # Fallback only — should be rare/never: every piece
                # produced for this card has a matching card already
                # reserved in `cards` above.
                card = menv.create_card(_product, _line_id, _sm.card_size, priority="M")
            if ctx.verbose:
                print(f"  [t={t:10.1f}] {line_name}(L{_line_id}): package of "
                      f"{_sm.card_size} × {_product!r} COMPLETED — "
                      f"delivering to Supermarket.")
            env.process(
                _return_card_to_supermarket(ctx, menv, _sm, card, t, line_name,
                                             _line_id, ctx.verbose)
            )

    t_start = env.now
    possible_changeover = (
        gate.current_rec is None
        or getattr(gate.current_rec, "product_number", None) != order_rec.product_number
    )
    # Tag events emitted by THIS call as "pull" (filtering by line_id, not
    # just index, since multiple crews can be mid-turn on different
    # lines at once) and back-fill crew_id from this call's own
    # parameter — this is what lets schedule_events.build_gantt_payload's
    # "crewId" on each Gantt job segment actually be populated instead of
    # staying null (see telemetry.records.ScheduleEvent.crew_id).
    _log = menv.event_log if event_log is None else event_log
    _start_idx = len(_log)
    result = yield from run_one_pull_batch(
        menv, line_id, gate.current_rec, order_rec, n_workers, on_finish, ctx.verbose,
        event_log=event_log,
    )
    for _ev in _log[_start_idx:]:
        if _ev.line_id == line_id and _ev.production_type is None:
            _ev.production_type = "pull"
        if _ev.line_id == line_id and _ev.crew_id is None:
            _ev.crew_id = crew_id
    if result is not None:
        gate.current_rec = result
    t_end = env.now
    gate.touch(t_end)
    if ctx.gate_activity_log is not None:
        ctx.gate_activity_log.append(GateActivityEntry(
            t_start=t_start, t_end=t_end, line_id=line_id,
            product_number=order_rec.product_number, production_type="pull",
            crew_id=crew_id,
            possible_changeover=possible_changeover,
            quantity=order_rec.quantity,
        ))


def run_pull_turn(
    ctx: RunContext,
    line_id: int,
    line_name: str,
    n_workers: int,
    event_log: Optional[list[ScheduleEvent]],
    crew_id: int,
):
    """
    SimPy generator — one full pull "turn": drain up to 4 cards of one
    product_number from the front of this line's chute, respecting
    whatever order sim/fill/pull has already established. NEVER looks
    past the front entry — if the front stops matching the batch in
    progress, the turn ends immediately and the partial count is
    persisted (ctx.pending_batches) for a future visit, never searched
    for out of order (chute order governs; the crew doesn't reorder it —
    Rule 1.a).

    Handles all of the spec's pull examples with the same loop, no
    special-casing:
      - 6 cards of A queued: takes 4 (pending cleared — batch complete),
        leaving 2 at the front for whatever turn visits this line next,
        which simply starts a fresh record from taken=0.
      - 2 cards of A queued, then 2 more of A arrive before the next
        visit: first visit takes 2 (pending persists {A, taken=2}); a
        later visit resumes and takes the remaining 2 to reach 4.
      - 3 taken, then 2 more of A arrive, then 4 of B, then 4 more of A:
        the turn that hits B persists {A, taken=3}; a later turn that
        finds A back at the front (once whatever's ahead of it has been
        dealt with by movement 1's own ordering) resumes with taken=3 —
        exactly Rule 1.a, needing only 1 more card to complete the batch.
    """
    chute = ctx.chutes[line_id]
    front = chute.peek_next_of_class("pull")
    if front is None:
        return

    pending = ctx.pending_batches.get(line_id)
    if pending is None or pending.product_type != front.product_type:
        pending = PendingPullBatch(product_type=front.product_type, taken=0)

    while pending.taken < 4:
        front = chute.peek_next_of_class("pull")
        if front is None or front.product_type != pending.product_type:
            break
        entry = chute.pop_next_of_class("pull")
        yield from _run_one_pull_card(ctx, line_id, line_name, entry, n_workers, event_log, crew_id)
        pending.taken += 1
        ctx.notify()

    if pending.taken >= 4:
        ctx.pending_batches.pop(line_id, None)
    else:
        ctx.pending_batches[line_id] = pending
