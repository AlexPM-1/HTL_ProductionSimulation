"""
reports/push/exotic_occupancy.py
===================================
exotic_products_at() — per-product occupancy for one Exotic Supermarket
row at a point in time. Called by reports/all/supermarket_series.py (the
Exotic row time-series) and reports/movement/supermarket.py (the Exotic
row in one movement-state frame).
"""

from __future__ import annotations

from typing import Optional


def exotic_products_at(snaps: list, edge_s: float) -> list[dict]:
    """
    Per-product breakdown for one Exotic Supermarket row, as of edge_s —
    same "last reading at or before edge_s" convention as
    reports.binning.state_at, but returns EVERY product currently sitting
    in the row at once instead of a single (occupant, n_cards) pair.

    Prefers ExoticSlotSnapshot.occupants, if present — a list of
    {"product_number": ..., "n_cards": ...} entries (or (product_number, n_cards)
    tuples) covering every product simultaneously occupying the row. This
    is the shape needed for a row that hasn't fully emptied before a
    different product starts filling it: a real physical row can (and
    per the current sim-side behavior, visibly does) hold more than one
    product's pieces at once, and that mix should stay visible rather
    than being reported as if the whole row belongs to whichever product
    happened to be deposited most recently.

    Falls back to the legacy single-occupant `occupant`/`n_cards`
    attributes (wrapped as a one-entry list) for snapshot logs that don't
    carry `occupants` yet. Read defensively via getattr so this function
    stays safe to call regardless of which sim-side revision produced the
    log; it will pick up real multi-product data automatically, no
    caller changes needed, once the exotic-row deposit logic in
    sim/fill/push/exotic.py is extended to record `occupants`.
    """
    products: Optional[list[dict]] = None
    occupant, n_cards = None, 0
    for s in snaps:
        if s.t > edge_s:
            break
        raw = getattr(s, "occupants", None)
        if raw:
            products = [
                {
                    "product_number": (o.get("product_number") if isinstance(o, dict) else o[0]),
                    "push_count": (o.get("n_cards") if isinstance(o, dict) else o[1]),
                }
                for o in raw
            ]
        else:
            products = None
        occupant, n_cards = getattr(s, "occupant", None), getattr(s, "n_cards", 0)
    if products is not None:
        return products
    if occupant is not None or n_cards:
        return [{"product_number": occupant, "push_count": n_cards}]
    return []


# Back-compat alias for the old private name.
_exotic_products_at = exotic_products_at
