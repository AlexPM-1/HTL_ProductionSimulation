"""
sim/fill/pull/cards.py
========================
_return_card_to_supermarket() — deposits one completed package's card
back onto a Supermarket. Called from sim.drain.pull_turn.
"""

from __future__ import annotations

from sim.context import RunContext
from sim.resources.environment import KanbanSimEnvironment
from sim.resources.supermarket import SupermarketResource
from sim.resources.cards import KanbanCard


def _return_card_to_supermarket(rt: RunContext, kenv: KanbanSimEnvironment,
                                 sm: SupermarketResource,
                                 card: KanbanCard, t: float, line_name: str,
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
    is a bounded simpy.Store (capacity = KanbanTimingConfig.supermarket_capacity_cards,
    enforced in build_kanban_environment). Once a lane already holds that
    many cards, this `yield sm.store.put(card)` blocks until a withdrawal
    frees up a slot — no manual semaphore/capacity check needed here.
    """
    env = kenv.env
    # TRANSPORT-TIME HOOK: time to physically carry the finished batch+card
    # from Sichtpruefung back to the Supermarket shelf would be
    # `yield env.timeout(return_time_s)` right here, before the put().
    card.record_transition("in_supermarket", t)
    yield sm.store.put(card)
    rt.record_supermarket(line_id, card.product_type, "deposit_batch", sm)
    if verbose:
        print(f"  [t={t:10.1f}] {line_name}(L{line_id}): Supermarket state for "
              f"{card.product_type!r} -> {sm.n_available} package(s) available.")
