"""
sim/fill/pull/assignment.py
============================
"Which of our lines' Supermarkets fulfils a withdrawal request" — the
withdrawal-assignment policy. build_supermarket_state_lookup() and
select_supermarket_for_withdrawal() are called by
sim.fill.pull.withdrawal.
"""

from __future__ import annotations

from typing import Optional

from sim.context import RunContext
from sim.resources.supermarket import SupermarketResource


def build_supermarket_state_lookup(
    rt: RunContext,
    product_type: str,
) -> dict[int, tuple[str, SupermarketResource]]:
    """
    Snapshot every line's current Supermarket stock for `product_type`,
    across every line eligible to hold it — the "lookup for the
    Supermarket state" select_supermarket_for_withdrawal() ranks lines
    by. Deliberately not cached: stock changes on every
    withdrawal/deposit, so this is rebuilt fresh on every call.

    Only lines listed in this product's KanbanCardsSetup.eligible_lines
    (cfg.kanban_cards) AND that actually got a Supermarket resource
    built for it are included (falls back to every known line if the
    product has no KanbanCardsSetup row at all).

    Multi-row note (Supermarkets sheet / SupermarketSlotConfig): a line
    can have several physical slot ROWS for the same product (e.g. HTL3
    rows 1 & 2 both F00RJ02491 — "these are separate physical lanes...
    therefore it must be added", per the assignment rule).
    build_kanban_environment() (sim.resources.build) is the layer
    responsible for folding every row of a given (line, sachnummer) into
    ONE combined SupermarketResource at construction time (summed
    Capacity + summed InitialState/InitialPcsPartial across rows) — a
    build-time concern, not a per-withdrawal one — so that by the time
    this function runs, `kenv.supermarket_for(line_id, product_type)`
    already reflects that line's TRUE total stock. This function itself
    only aggregates ACROSS LINES on top of that.

    Returns {line_id: (line_name, SupermarketResource)}.
    """
    kenv = rt.kenv
    card_cfg = kenv.cfg.kanban_cards.get(product_type)
    eligible_lines = (
        card_cfg.eligible_lines if card_cfg and card_cfg.eligible_lines
        else list(rt.line_name_to_id.keys())
    )

    lookup: dict[int, tuple[str, SupermarketResource]] = {}
    for line_name in eligible_lines:
        line_id = rt.line_name_to_id.get(line_name)
        if line_id is None:
            continue
        sm = kenv.supermarket_for(line_id, product_type)
        if sm is None:
            continue
        lookup[line_id] = (line_name, sm)

    return lookup


def select_supermarket_for_withdrawal(
    rt: RunContext,
    product_type: str,
) -> tuple[Optional[int], Optional[SupermarketResource]]:
    """
    Dedicated, swappable "assignment" function — decide which line's
    Supermarket a withdrawal request for `product_type` should be
    fulfilled from. Kept isolated in its own small function on purpose:
    this rule is expected to change / be tried differently later.

    Current rule — "most stock first": among every eligible line (see
    build_supermarket_state_lookup()), pick the one with the highest
    n_available right now; ties broken by line name (alphabetical) for
    determinism.

    Evaluated fresh on EVERY single card withdrawal (never memoized/
    sticky) — since stock only decreases as cards are pulled, this
    naturally reproduces "keep pulling from the biggest until it runs
    dry, then move to the next-biggest" with no extra bookkeeping: once
    repeated withdrawals drain today's biggest line below another
    line's level, the very next call to this function switches to that
    other line on its own.

    If every eligible line is currently at 0 (system-wide stockout for
    this product), there's no genuinely "biggest" one to prefer — the
    first line in the deterministic (alphabetical) tie-break order is
    still returned, and the caller (_withdraw_one_card) blocks there.
    Known simplification: if a DIFFERENT eligible line restocks first
    while this request is waiting, it is not woken early — the request
    stays blocked on the line it was assigned to. This mirrors a real
    logistics run needing a person to notice and reroute the card, so
    it's an intentional simplification, not an oversight; swap this
    function out for a "wait on whichever restocks first" version later
    if that granularity ever matters.

    Returns (line_id, SupermarketResource), or (None, None) if
    `product_type` has no configured Supermarket anywhere.
    """
    lookup = build_supermarket_state_lookup(rt, product_type)
    if not lookup:
        return None, None

    best_line_id, (_best_line_name, best_sm) = min(
        lookup.items(),
        key=lambda item: (-item[1][1].n_available, item[1][0]),
    )
    return best_line_id, best_sm
