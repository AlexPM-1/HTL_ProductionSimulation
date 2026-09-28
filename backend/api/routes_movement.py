"""
api/routes_movement.py
=========================
GET endpoints for the Movement Simulation page and its supporting piece/
card lookups: /plant_structure, /movement_state, /part_ids, /part_trace,
/card_ids, /card_trace. Each delegates to a builder in the reports/
package and reads the cached run from api.runs.run_store.

A mixed run keeps BOTH classes (pull and push) on one continuous menv/
clock, so there's a single set of endpoints here rather than separate
push/kanban variants. Instead:
  - "mode" (push vs kanban) on the movement page maps to a `cls` filter
    here (pull=kanban products, push=push products, all=no filter),
    reusing reports.kpi.by_class.build_class_product_sets — the same
    split already used for kpi_by_class.
  - card endpoints are naturally kanban-only regardless of `cls`, because
    menv.card_registry is only ever populated for class-1 parts — passing
    cls='push' to a card endpoint will just come back empty, not an
    error.
  - windowing uses day_index (0-based from sim start) + optional hour
    (0-23), via api.runs.run_store.day_window_s().
"""

from __future__ import annotations

from typing import Optional

from fastapi import HTTPException

from api.app import app
from api.runs import run_store
from reports.binning import TIME_UNIT_DIVISORS
from reports.kpi.by_class import build_class_product_sets
from reports.movement.layout import build_plant_structure_payload
from reports.movement.frames import build_movement_state_payload, _MAX_MOVEMENT_FRAMES
from reports.movement.trace import (
    build_part_trace_payload,
    build_part_ids_payload,
    build_card_trace_payload,
)


def _class_filter_products(cls: Optional[str], cfg) -> Optional[set[str]]:
    """Resolves the `cls` query param ("pull" | "push" | "all"/None) to a
    product-type set via reports.kpi.by_class.build_class_product_sets,
    raising HTTPException(400, ...) for anything else. Kept here (rather
    than in reports/) because reports/ must not import fastapi."""
    if cls is None or cls == "all":
        return None
    pull_products, push_products = build_class_product_sets(cfg)
    if cls == "pull":
        return pull_products
    if cls == "push":
        return push_products
    raise HTTPException(status_code=400, detail="cls must be one of: pull, push, all")


@app.get("/api/mixed/plant_structure")
def mixed_plant_structure(line_id: Optional[int] = None):
    """Static per-line Plant Layout shape (Supermarket rows, Batch
    Collector product rows + trigger amounts, Chute frozen-zone size) —
    see build_plant_structure_payload's docstring. Requires a prior
    POST /api/simulate_mixed, same as every other /api/mixed/* endpoint."""
    menv, cfg = run_store.require()
    return build_plant_structure_payload(menv, cfg, line_id=line_id)


@app.get("/api/mixed/movement_state")
def mixed_movement_state(
    line_id: Optional[int] = None,
    day_index: Optional[int] = None,
    hour: Optional[int] = None,
    interval_min: Optional[float] = 5.0,
    n_frames: Optional[int] = None,
    time_unit: str = "h",
    include_push: bool = False,
):
    """
    Time-indexed per-card occupancy for the Movement Simulation's animated
    Plant Layout view: evenly spaced snapshots across the requested
    day/hour window (or the whole cached run if day_index is omitted),
    each resolved down to actual card_ids sitting in Supermarket
    rows/slots, Batch Collector buckets, the Collection Box, and the
    Kanban Chute queue — see build_movement_state_payload's docstring for
    the per-box assignment rules and their approximations.

    Frame spacing — two ways to control it, n_frames wins if given:
      - interval_min (default 5.0): frame count is DERIVED as
        window_seconds / (interval_min * 60). Clamped to
        [1, _MAX_MOVEMENT_FRAMES].
      - n_frames: explicit override. Must be between 1 and
        _MAX_MOVEMENT_FRAMES.
    Each frame re-scans the ENTIRE menv.card_registry, so both knobs are
    capped — narrow the window via day_index/hour first if you need finer
    resolution than the cap allows over a wide window.
    """
    if time_unit not in TIME_UNIT_DIVISORS:
        raise HTTPException(status_code=400, detail="time_unit must be one of: h, min, s")
    menv, cfg = run_store.require()

    window = run_store.day_window_s(day_index, hour)
    start_s, end_s = window if window is not None else (0.0, menv.env.now)
    window_s = max(0.0, end_s - start_s)

    if n_frames is not None:
        if not (1 <= n_frames <= _MAX_MOVEMENT_FRAMES):
            raise HTTPException(status_code=400, detail=f"n_frames must be between 1 and {_MAX_MOVEMENT_FRAMES}")
        resolved_frames = n_frames
    else:
        if interval_min is None or interval_min <= 0:
            raise HTTPException(status_code=400, detail="interval_min must be > 0 (or pass n_frames instead)")
        resolved_frames = max(1, min(_MAX_MOVEMENT_FRAMES, round(window_s / (interval_min * 60.0)) or 1))

    divisor = TIME_UNIT_DIVISORS[time_unit]
    edges = [start_s + window_s * i / resolved_frames for i in range(resolved_frames + 1)]
    frames = [
        {
            "t": round(edge_s / divisor, 4),
            "lines": build_movement_state_payload(menv, cfg, edge_s, line_id=line_id, include_push=include_push),
        }
        for edge_s in edges
    ]
    return {"time_unit": time_unit, "day_start_hour": run_store.day_start_hour, "frames": frames}


@app.get("/api/mixed/part_ids")
def mixed_part_ids(line_id: Optional[int] = None, cls: Optional[str] = None):
    """Cheap piece dropdown, not scoped to a day/hour."""
    menv, cfg = run_store.require()
    payload = build_part_ids_payload(menv, line_id=line_id)
    keep = _class_filter_products(cls, cfg)
    if keep is not None:
        payload["parts"] = [p for p in payload["parts"] if p["product_type"] in keep]
    return payload


@app.get("/api/mixed/part_trace")
def mixed_part_trace(
    line_id: Optional[int] = None,
    day_index: Optional[int] = None,
    hour: Optional[int] = None,
    time_unit: str = "s",
    cls: Optional[str] = None,
    include_wip: bool = False,
):
    """Per-piece station trace for the movement page's 'piece' entity type."""
    menv, cfg = run_store.require()
    if time_unit not in TIME_UNIT_DIVISORS:
        raise HTTPException(status_code=400, detail="time_unit must be one of: h, min, s")
    payload = build_part_trace_payload(
        menv,
        time_unit=time_unit,
        include_wip=include_wip,
        line_id=line_id,
        day_window_s=run_store.day_window_s(day_index, hour),
        time_offset_s=0.0,  # already one continuous clock
    )
    keep = _class_filter_products(cls, cfg)
    if keep is not None:
        payload["parts"] = [p for p in payload["parts"] if p["product_type"] in keep]
    payload["day_start_hour"] = run_store.day_start_hour
    return payload


@app.get("/api/mixed/card_ids")
def mixed_card_ids(
    line_id: Optional[int] = None,
    day_index: Optional[int] = None,
    hour: Optional[int] = None,
):
    """Cheap card dropdown for one day/hour window — cards are always class-1 (kanban) only."""
    menv, _cfg = run_store.require()
    payload = build_card_trace_payload(
        menv,
        time_unit="s",
        line_id=line_id,
        day_window_s=run_store.day_window_s(day_index, hour),
    )
    ids = [
        {"card_id": c["card_id"], "product_type": c["product_type"], "line_id": c["line_id"]}
        for c in payload["cards"]
    ]
    return {"cards": ids}


@app.get("/api/mixed/card_trace")
def mixed_card_trace(
    card_id: Optional[int] = None,
    line_id: Optional[int] = None,
    day_index: Optional[int] = None,
    hour: Optional[int] = None,
    time_unit: str = "s",
):
    """Single card's transition history for the movement page's 'card' entity type."""
    menv, _cfg = run_store.require()
    if time_unit not in TIME_UNIT_DIVISORS:
        raise HTTPException(status_code=400, detail="time_unit must be one of: h, min, s")
    payload = build_card_trace_payload(
        menv,
        time_unit=time_unit,
        card_id=card_id,
        line_id=line_id,
        day_window_s=run_store.day_window_s(day_index, hour),
        time_offset_s=0.0,
    )
    payload["day_start_hour"] = run_store.day_start_hour
    return payload
