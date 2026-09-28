"""
sim/fill/pull/cards.py
========================
_return_card_to_supermarket() — deposits one completed package's card
back onto a Supermarket. Called from sim.drain.pull_turn.
"""

from __future__ import annotations

from sim.context import RunContext
from sim.resources.environment import MixedSimEnvironment
from sim.resources.supermarket import SupermarketResource
from sim.resources.cards import PullCard


def _return_card_to_supermarket(ctx: RunContext, menv: MixedSimEnvironment,
                                 sm: SupermarketResource,
                                 card: PullCard, t: float, line_name: str,
                                 line_id: int, verbose: bool):
    """
    SimPy generator (fire-and-forget via env.process()).

    Deposits one completed package's card onto the Supermarket's Store via
    the REAL Store.put(), not a raw `store.items.append()`. This matters:
    appending straight to the backing list never triggers Store's
    internal wake-up of any pending `get()` — so a Kanban card already
    blocked waiting for this exact product (see _withdraw_one_card's
    `yield sm.store.get()`) would never be woken up by a bypassed
    append, even though `sm.n_available` would look correct in a print.
    Going through `.put()` fixes that: a waiting withdrawal immediately
    picks up the recent production, as intended.

    This is also the upstream-blocking mechanism for a full shelf: `sm.store`
    is a bounded simpy.Store (capacity = the summed "Capacity" of this
    product/line's "Main runner" row(s) on the "Supermarkets" sheet,
    enforced in build_mixed_environment). Once a lane already holds that
    many cards, this `yield sm.store.put(card)` blocks until a withdrawal
    frees up a slot — no manual semaphore/capacity check needed here.
    """
    env = menv.env

    # Per-card completion point: this is also where the card's owning
    # OrderRecordPull learns which line actually fulfilled it — done
    # PER CARD (not per batch/gate-hold), since one gate-hold can mix
    # cards belonging to several different orders (see
    # sim.drain.pull_turn.run_pull_turn's up-to-4-cards loop).
    entry = card.current_history
    if entry is not None:
        entry.t_production_end = round(t, 3)
        entry.t_reposition_supermarket = round(t, 3)
        order = menv.order_registry.get(entry.order_id)
        if order is not None:
            order.record_assignment(line_name, card.card_id)

    # TRANSPORT-TIME HOOK: time to physically carry the finished batch+card
    # from Sichtpruefung back to the Supermarket shelf would be
    # `yield env.timeout(return_time_s)` right here, before the put().
    card.record_transition("in_supermarket", t)
    yield sm.store.put(card)
    ctx.record_supermarket(
        line_id, card.product_type, "deposit_batch", sm,
        kanban_card_id=card.card_id,
    )
    if verbose:
        print(f"  [t={t:10.1f}] {line_name}(L{line_id}): Supermarket state for "
              f"{card.product_type!r} -> {sm.n_available} package(s) available.")
