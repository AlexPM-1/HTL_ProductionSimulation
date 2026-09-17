"""
reports/movement/layout.py
=============================
Static Plant Layout shape for the Movement Simulation page:
active_kanban_products_by_line() and build_plant_structure_payload().

_BC_CAPACITY_SLACK / _CHUTE_CAPACITY_SLACK are the rendering-only slot-count
constants other movement/* modules also need (reports/movement/chute.py,
reports/movement/collector.py, reports/movement/frames.py) — this is their
one canonical home.
"""

from __future__ import annotations

from typing import Optional

from domain.config import SimConfig

# Rendering-only slot-count slack for the Plant Layout's Batch Collector /
# Chute boxes — neither BatchCollectorResource nor KanbanChuteResource has a
# real fixed capacity in the sim (BatchCollectorResource: unbounded bucket
# until pop_batch() drains it at threshold; KanbanChuteResource: `_entries`
# is a plain list, admission is priority-ordered, not capacity-bounded), so
# these give the frontend a finite, slightly-generous slot count to draw
# rather than an unbounded one.
_BC_CAPACITY_SLACK = 4
_CHUTE_CAPACITY_SLACK = 12


def active_kanban_products_by_line(kenv) -> dict[int, set[str]]:
    """
    product_type set per line_id that ACTUALLY has at least one KanbanCard
    ever created on that line (kenv.card_registry), as opposed to merely
    being config-eligible there (cfg.kanban_cards[...].eligible_lines).
    Used to trim the Batch Collector's drawn rows down to products that
    genuinely take part in this run — a product listed in the Kanban
    Cards setup sheet but never actually withdrawn/produced on a line
    would otherwise show up as a permanently-empty row, which is just
    noise on the Plant Layout.
    """
    out: dict[int, set[str]] = {}
    for card in getattr(kenv, "card_registry", {}).values():
        lid = getattr(card, "line_id", None)
        pt = getattr(card, "product_type", None)
        if lid is None or not pt:
            continue
        out.setdefault(lid, set()).add(pt)
    return out


def build_plant_structure_payload(kenv, cfg: SimConfig, line_id: Optional[int] = None) -> dict:
    """
    STATIC per-line shape for the Movement Simulation's "Plant Layout"
    spatial view (movement_layout.js renderPlantLayout()) — physical
    Supermarket rows, Batch Collector product rows + trigger amounts, and
    the Chute's current frozen-zone size. This answers "what does the
    diagram look like", never "what's on it right now" — per-card
    occupancy over time is derived separately by reports/movement/frames.py.

    Row/threshold sourcing:
      - supermarket rows: cfg.supermarkets[line_name], one entry per
        PHYSICAL row (Main runner AND Exotic — Exotic rows have no
        sachnummer of their own, labelled "Exotic"), same rows
        reports.all.supermarket_series reads. Several rows can share a
        sachnummer; each is still listed separately here since the plant
        diagram draws one row per physical lane, not one per logical
        (line, product) group.
      - batch collector rows: one per product ELIGIBLE on this line per
        KanbanCardsSetup (cfg.kanban_cards[sachnr].eligible_lines — empty
        list = eligible everywhere) AND ACTUALLY ACTIVE there — i.e. at
        least one KanbanCard of that product has been created on this
        line (see active_kanban_products_by_line()). Eligibility alone
        isn't enough: the setup sheet can list a product as eligible on a
        line that never actually withdraws/produces it, and drawing that
        as a permanently-empty row is just noise on the diagram, not
        signal. trigger_amount = cfg.kanban_cards[sachnr].cards_to_trigger.
        `capacity` here is a RENDERING-ONLY hint (trigger_amount +
        _BC_CAPACITY_SLACK), not a real sim constraint.
      - chute.capacity: likewise a RENDERING-ONLY hint —
        frozen_zone_cards + _CHUTE_CAPACITY_SLACK.
      - chute.frozen_zone_cards: read live off
        kenv.kanban_chutes[line_id].frozen_zone_cards (falls back to 0 if
        no run/line yet) rather than cfg, since it's a runtime-editable
        value (PATCH /api/mixed/push_policy) that gets synced onto EVERY
        line's KanbanChuteResource identically. Reported per-line anyway
        (not just once) so the frontend never has to assume that
        uniformity holds.
    """
    line_id_by_name = {line.line_name: line.line_id for line in kenv.lines}
    active_products = active_kanban_products_by_line(kenv)

    # Product -> trigger amount, per eligible line, trimmed to products
    # with real activity on that line (see active_kanban_products_by_line's
    # docstring for why eligible alone isn't enough).
    products_by_line: dict[int, list[tuple[str, int]]] = {}
    for sachnr, card_cfg in (cfg.kanban_cards or {}).items():
        eligible = getattr(card_cfg, "eligible_lines", None)
        for line in kenv.lines:
            if eligible and line.line_name not in eligible:
                continue
            if sachnr not in active_products.get(line.line_id, set()):
                continue
            products_by_line.setdefault(line.line_id, []).append(
                (sachnr, card_cfg.cards_to_trigger)
            )

    lines_out = []
    for line in kenv.lines:
        lid = line.line_id
        if line_id is not None and lid != line_id:
            continue

        sm_rows = []
        for slot in sorted((cfg.supermarkets or {}).get(line.line_name, []), key=lambda s: s.row_number):
            sm_rows.append({
                "row_number": slot.row_number,
                "label": "Exotic" if slot.is_exotic else (slot.sachnummer or f"Row {slot.row_number}"),
                "sachnummer": None if slot.is_exotic else slot.sachnummer,
                "is_exotic": slot.is_exotic,
                "capacity": slot.capacity,
            })

        bc_rows = [
            {
                "product_type": p,
                "trigger_amount": trig,
                "capacity": trig + _BC_CAPACITY_SLACK,  # rendering-only, see docstring
            }
            for p, trig in sorted(products_by_line.get(lid, []))
        ]

        # Restmenge: one entry per Main-runner sachnummer this line's
        # Supermarket has a physical row for (Exotic rows have no
        # sachnummer of their own and never carry pcs_partial). Reserves
        # a segment/shape for the frontend to draw even before any run
        # data exists at this instant — the live amount comes from
        # movement_state's "restmenge" field (pcs_partial, replayed per
        # t_s), this is only the static "what products get a Restmenge
        # segment at all, and what's a full card's worth" shape. Same
        # batch_size fallback (200) the sim uses when a product has no
        # KanbanCardsSetup row of its own.
        restmenge_sachnrs = sorted({r["sachnummer"] for r in sm_rows if not r["is_exotic"] and r["sachnummer"]})
        restmenge_rows = [
            {
                "sachnummer": sachnr,
                "batch_size": getattr(cfg.kanban_cards.get(sachnr), "batch_size", None) or 200,
            }
            for sachnr in restmenge_sachnrs
        ]

        chute = kenv.kanban_chutes.get(lid) if getattr(kenv, "kanban_chutes", None) else None
        frozen_zone_cards = getattr(chute, "frozen_zone_cards", 0) if chute is not None else 0

        lines_out.append({
            "line_id": lid,
            "line_name": line.line_name,
            "supermarket": {"rows": sm_rows},
            "restmenge": restmenge_rows,
            "batch_collector": {"rows": bc_rows},
            "chute": {
                "frozen_zone_cards": frozen_zone_cards,
                "capacity": frozen_zone_cards + _CHUTE_CAPACITY_SLACK,  # rendering-only, see docstring
            },
        })

    return {"lines": lines_out}


# Private aliases for call sites that import the underscore-prefixed names.
_active_kanban_products_by_line = active_kanban_products_by_line
_build_plant_structure_payload = build_plant_structure_payload
