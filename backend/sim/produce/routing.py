"""
sim/produce/routing.py
=======================
material_stored_types() — used by sim.produce.run_order and
sim.produce.run_kanban_batch. `_active_buffer_sequence` (a related
routing helper) lives in domain/products.py instead, and is imported
directly from there by both of those modules.
"""

from __future__ import annotations

from domain.products import ProductClass


def material_stored_types(product_class: "ProductClass") -> list[str]:
    """
    Map a product's ProductClass to the chute lane(s) (Stored_type
    values from the "Inventories" sheet) it must draw from at Beladen.

    Every product draws "Standard material" (the base housing material).
    Products additionally requiring a Lochfilter or DRS sub-assembly also
    draw from their dedicated lane — those lanes only exist on lines that
    have them configured (HTL3, per the current "Inventories" sheet); on
    lines without a matching lane, chute_for() simply returns None and the
    draw is skipped (see sim.produce.part_lifecycle.part_lifecycle).

    ASSUMPTION (flag if wrong): a STAB_LOCHFILTER/DRS part still needs the
    Standard housing material in addition to its special sub-assembly —
    i.e. Beladen draws from BOTH lanes, not just the special one.
    """
    if product_class == ProductClass.STAB_LOCHFILTER:
        return ["Standard material", "Lochfilter"]
    elif product_class == ProductClass.DRS:
        return ["Standard material", "DRS"]
    return ["Standard material"]
