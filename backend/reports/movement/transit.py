"""
reports/movement/transit.py
==============================
"In transit" box logic for one movement-state frame. Called by
reports/movement/frames.py's build_movement_state_payload().
"""

from __future__ import annotations


def build_in_transit(frame: dict, lid: int) -> list[dict]:
    """
    States with no Step-1 box of their own — "withdrawn" (between
    Supermarket and Collection Box). "in_production" is deliberately
    EXCLUDED here — see reports.movement.status.build_production_occupants,
    which is the authoritative "what's really in the Production
    rectangle" source. Keeping both would let a frontend double-draw the
    same card once from each list.
    """
    return [
        {"card_id": c["card_id"], "state": state, "product_type": product}
        for (l, state, product), items in frame.items()
        if l == lid and state == "withdrawn"
        for c in items
    ]
