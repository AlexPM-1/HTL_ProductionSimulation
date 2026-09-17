"""
reports/movement/collector.py
================================
Batch Collector box logic for one movement-state frame. Called by
reports/movement/frames.py's build_movement_state_payload().
"""

from __future__ import annotations

from reports.movement.layout import _BC_CAPACITY_SLACK


def build_batch_collector_rows(
    frame: dict, lid: int, products_by_line: dict,
) -> list[dict]:
    """
    One bucket per eligible product. capacity here is the SAME
    rendering-only trigger_amount + _BC_CAPACITY_SLACK hint
    reports.movement.layout.build_plant_structure_payload uses for the
    static shape — kept in sync so a card list longer than what the
    static layout drew slots for is flagged, not silently lost.
    """
    bc_rows_out = []
    for sachnr, trig in sorted(products_by_line.get(lid, [])):
        cards = frame.get((lid, "in_batch_collector", sachnr), [])
        capacity = trig + _BC_CAPACITY_SLACK
        bc_rows_out.append({
            "product_type": sachnr,
            "trigger_amount": trig,
            "capacity": capacity,
            "card_ids": [c["card_id"] for c in cards[:capacity]],
            "overflow_card_ids": [c["card_id"] for c in cards[capacity:]] or None,
        })
    return bc_rows_out
