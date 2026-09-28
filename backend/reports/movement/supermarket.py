"""
reports/movement/supermarket.py
==================================
Supermarket-row and Restmenge box logic for one movement-state frame.
Called by reports/movement/frames.py's build_movement_state_payload(),
which reuses the returned main_runner_groups for the Batch Collector box
(reports/movement/collector.py) without recomputing it.
"""

from __future__ import annotations

from domain.config import SimConfig
from reports.binning import state_at
from reports.push.exotic_occupancy import exotic_products_at


def build_supermarket_rows(
    frame: dict, menv, cfg: SimConfig, lid: int, line_name: str, t_s: float, include_push: bool,
) -> tuple[list[dict], list[dict], dict[str, list]]:
    """
    Returns (sm_rows_out, restmenge_out, main_runner_groups) for one line
    at one instant t_s — main_runner_groups is handed back so
    reports/movement/frames.py can reuse it for the Batch Collector box
    without recomputing it (mirrors the original function's single pass).

    Approximations (documented once here rather than per-field, since
    they all stem from the same root cause: the sim doesn't track
    per-physical-slot occupancy directly, only aggregate counts + each
    card's own transition history):
      - Exotic Supermarket rows hold PUSH-produced pieces, which never
        get a PullCard at all — no card_ids are ever placed into an
        "Exotic" row here (they're simply absent from sm_rows_out for
        that line) UNLESS include_push=True, in which case a row is
        added with "is_exotic": true and a "products" list (see
        exotic_products_at's docstring: a row that hasn't fully emptied
        before a different product starts filling it legitimately holds
        more than one product at once). "product_number" (first product, or
        None) and "push_count" (sum across products) are included
        alongside "products" for back-compat with any consumer still
        reading the old single-value shape.
    """
    frame_cards = frame  # already the whole-run per-state grouping from build_movement_frame_cards

    main_runner_groups: dict[str, list] = {}
    for slot in (cfg.supermarkets or {}).get(line_name, []):
        if slot.is_exotic or not slot.product_number:
            continue
        main_runner_groups.setdefault(slot.product_number, []).append(slot)
    for rows in main_runner_groups.values():
        rows.sort(key=lambda s: s.row_number)

    sm_rows_out = []
    for product_number, rows in sorted(main_runner_groups.items()):
        cards = frame_cards.get((lid, "in_supermarket", product_number), [])  # oldest-first
        cursor = 0
        for slot in rows:
            take = cards[cursor: cursor + slot.capacity]
            cursor += slot.capacity
            sm_rows_out.append({
                "row_number": slot.row_number,
                "product_number": product_number,
                "capacity": slot.capacity,
                "card_ids": [c["card_id"] for c in take],
            })
        leftover = cards[cursor:]
        if leftover:
            # More cards than the group's summed physical capacity —
            # shouldn't happen if capacity enforcement upstream is
            # working, but surfaced explicitly rather than silently
            # truncated.
            sm_rows_out.append({
                "row_number": None,
                "product_number": product_number,
                "capacity": 0,
                "card_ids": [c["card_id"] for c in leftover],
                "overflow": True,
            })

    # ---- Restmenge: loose, not-yet-a-whole-card pieces per product ----
    # This is SupermarketSnapshot.pcs_partial — real PIECES already
    # produced and sitting in the Supermarket, not yet enough of them to
    # recycle a whole PullCard. Same "last reading at or before t_s"
    # replay as every other snapshot-log field in this package, grouped
    # by (line_name, product_number) same as
    # reports.all.supermarket_series's main_groups. Reported once per
    # product this line's Supermarket actually stocks (main_runner_groups,
    # computed above) — a product with pcs_partial==0 at t_s still gets
    # an entry (amount 0), so a frontend segment doesn't have to guess
    # whether a product exists here at all.
    restmenge_out = []
    for product_number in sorted(main_runner_groups):
        snaps = [
            s for s in (getattr(menv, "snapshot_log", None) or [])
            if s.line_name == line_name and s.product_type == product_number
        ]
        snaps.sort(key=lambda s: s.t)
        _, pcs_partial = state_at(snaps, t_s, ("n_available", "pcs_partial"))
        card_size = snaps[-1].card_size if snaps else None
        restmenge_out.append({
            "product_number": product_number,
            "pcs_partial": pcs_partial,
            "card_size": card_size,
        })

    if include_push:
        exotic_snaps: dict[int, list] = {}
        for snap in (getattr(menv, "exotic_snapshot_log", None) or []):
            if snap.line != line_name:
                continue
            exotic_snaps.setdefault(snap.row_number, []).append(snap)
        for snaps in exotic_snaps.values():
            snaps.sort(key=lambda s: s.t)
        for slot in sorted((cfg.supermarkets or {}).get(line_name, []), key=lambda s: s.row_number):
            if not slot.is_exotic:
                continue
            products = exotic_products_at(exotic_snaps.get(slot.row_number, []), t_s)
            sm_rows_out.append({
                "row_number": slot.row_number,
                "product_number": products[0]["product_number"] if products else None,
                "push_count": sum(p["push_count"] for p in products),
                "products": products,
                "capacity": slot.capacity,
                "card_ids": [],
                "is_exotic": True,
            })

    return sm_rows_out, restmenge_out, main_runner_groups
