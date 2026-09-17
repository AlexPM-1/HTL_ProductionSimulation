"""
reports/movement/collection_box.py
=====================================
Collection Box box logic for one movement-state frame. Called by
reports/movement/frames.py's build_movement_state_payload().
"""

from __future__ import annotations


def build_collection_box(frame: dict, lid: int) -> dict:
    """Merge every product's group for this line into one physical box
    (not per-product), oldest-first."""
    cb_cards = [
        {**c, "product_type": p}
        for (l, state, p), items in frame.items()
        if l == lid and state == "in_collection_box"
        for c in items
    ]
    cb_cards.sort(key=lambda c: (c["since_t"], c["card_id"]))
    return {"card_ids": [c["card_id"] for c in cb_cards]}
