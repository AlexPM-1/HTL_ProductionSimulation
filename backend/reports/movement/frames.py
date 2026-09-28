"""
reports/movement/frames.py
=============================
build_movement_state_payload() — the per-instant, per-line assembly loop
for the Movement Simulation page, composing the box builders in this
package (reports/movement/supermarket.py, collector.py, collection_box.py,
chute.py, status.py, transit.py) plus
reports/movement/layout.active_pull_products_by_line() and
reports/movement/trace.build_movement_frame_cards().

_MAX_MOVEMENT_FRAMES is the frame-count cap for this assembler, read by
api/routes_movement.py when resolving how many frames to render.
"""

from __future__ import annotations

from typing import Optional

from domain.config import SimConfig
from reports.movement.layout import active_pull_products_by_line
from reports.movement.trace import build_movement_frame_cards
from reports.movement.supermarket import build_supermarket_rows
from reports.movement.collector import build_batch_collector_rows
from reports.movement.collection_box import build_collection_box
from reports.movement.chute import build_chute_box
from reports.movement.transit import build_in_transit
from reports.movement.status import production_status_at, build_production_occupants

_MAX_MOVEMENT_FRAMES = 1000  # cap on GET /api/mixed/movement_state's frame count, either way it's derived


def build_movement_state_payload(
    menv, cfg: SimConfig, t_s: float, line_id: Optional[int] = None, include_push: bool = False,
) -> list[dict]:
    """
    Per-card occupancy snapshot at raw sim-clock time t_s, resolved down to
    physical Supermarket row/slot, Batch Collector bucket position,
    Collection Box queue position, and Kanban Chute queue position — the
    config-aware step reports.movement.trace.build_movement_frame_cards()
    deliberately leaves undone (see that function's docstring). This is
    the third sibling to reports.movement.layout.build_plant_structure_
    payload (STATIC row/bucket shape) and reports.all.supermarket_series.
    build_supermarket_state_payload (per-row/bucket COUNTS over time):
    same rows, same first-fill-by-index convention, but resolved to
    actual card_ids so the Movement Simulation page can animate
    individual cards sliding into slots instead of just filling a bar.

    include_push: for the "All Production" movement view (both push and
    pull traffic, not just Kanban cards). Push-produced pieces never get
    a PullCard, so they can't be resolved to card_ids the way Kanban
    cards are — instead, when True: Exotic Supermarket rows are ADDED
    (see reports.movement.supermarket), and the Chute's "entries" list
    interleaves real push entries alongside pull cards (see
    reports.movement.chute).

    See the individual box-builder modules for the approximation notes
    that used to live in this function's own docstring (Exotic rows never
    holding card_ids without include_push, the chute-queue replay rules,
    frozen_zone_cards reading the run's CURRENT live value, "in_transit"
    covering only states with no Step-1 box of their own).
    """
    frame = build_movement_frame_cards(menv, t_s, line_id=line_id)

    active_products = active_pull_products_by_line(menv)
    products_by_line: dict[int, list[tuple[str, int]]] = {}
    for product_number, card_cfg in (cfg.pull_cards or {}).items():
        eligible = getattr(card_cfg, "eligible_lines", None)
        for line in menv.lines:
            if eligible and line.line_name not in eligible:
                continue
            if product_number not in active_products.get(line.line_id, set()):
                continue
            products_by_line.setdefault(line.line_id, []).append(
                (product_number, card_cfg.cards_to_trigger)
            )

    lines_out = []
    for line in menv.lines:
        lid = line.line_id
        if line_id is not None and lid != line_id:
            continue
        line_name = line.line_name

        sm_rows_out, restmenge_out, _main_runner_groups = build_supermarket_rows(
            frame, menv, cfg, lid, line_name, t_s, include_push,
        )
        bc_rows_out = build_batch_collector_rows(frame, lid, products_by_line)
        collection_box = build_collection_box(frame, lid)
        chute_box = build_chute_box(menv, lid, line_name, t_s, include_push)

        # ---- Production status: what the line's gate is doing right now ----
        # Time-indexed (via GateActivityEntry replay), NOT the live-only
        # menv.gates[lid].current_rec the /simulate_mixed gate_status field
        # uses. shift_calendar/line_name/epoch are passed so an off-shift
        # line is reported distinctly from a genuinely idle one (v6) —
        # getattr'd defensively so this keeps working unchanged against a
        # menv from a run that predates push_ctx/shift_calendar ever
        # being set.
        gate_log = getattr(menv, "gate_activity_log", None) or []
        push_ctx = getattr(menv, "push_ctx", None)
        production_status = production_status_at(
            gate_log, lid, t_s,
            shift_calendar=cfg.shift_calendar,
            line_name=line_name,
            epoch=getattr(push_ctx, "epoch", None),
        )
        production_occupants = build_production_occupants(frame, lid, production_status)
        in_transit = build_in_transit(frame, lid)

        lines_out.append({
            "line_id": lid,
            "line_name": line_name,
            "supermarket_rows": sm_rows_out,
            "restmenge": restmenge_out,
            "batch_collector_rows": bc_rows_out,
            "collection_box": collection_box,
            "production_status": production_status,
            "production_occupants": production_occupants,
            "chute": chute_box,
            "in_transit": in_transit,
        })

    return lines_out


# Private alias for call sites that import the underscore-prefixed name.
_build_movement_state_payload = build_movement_state_payload
