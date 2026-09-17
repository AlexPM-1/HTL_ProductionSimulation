"""
api/legacy.py
===============
Houses the three endpoints kept under their original combined shape:
POST /api/simulate_mixed, GET /api/mixed/crew_activity, and
GET /api/mixed/supermarket_state.

/api/simulate_mixed is "composite": it runs the simulation
(sim.runner.run_mixed) and builds every report in one call, returning a
single JSON blob, rather than the thinner run-then-fetch-each-report
shape that api/routes_reports.py's registry-based GET /api/reports/{id}
supports for newer reports. Splitting this endpoint into a thin
run -> RunArtifacts -> per-report-fetch flow would need every report
builder used here to be migrated onto the reports.base registry first;
until that happens this endpoint stays as the single source of truth for
what a full mixed run returns.
"""

from __future__ import annotations

import datetime as _dt
import time
from typing import Optional

from fastapi import HTTPException

from api.app import app, EXCEL_PATH, SETUP_XLSX_PATH, _cfg
from api.runs import run_store
from api.schemas import SimulateMixedRequest
from domain.constants import SIM_HORIZON_S, DAY_LENGTH_S
from sim.runner import run_mixed
from reports.binning import _TIME_DIVISORS  # shared time-unit divisor table
from reports.all.gantt import build_gantt_payload
from reports.all.supermarket_series import (
    build_line_units_timeseries,
    build_supermarket_state_payload,
)
from reports.kpi.by_line import line_kpi_summary
from reports.kpi.by_class import _build_class_product_sets, _class_split_kpi
from reports.kpi.by_crew import _build_kpi_by_crew
from reports.pull.card_flow import build_card_flow_payload
from reports.pull.shortfall import build_shortfall_payload
from reports.pull.restmenge import build_restmenge_payload
from reports.pull.inventory_timeline import build_pull_inventory_timeline_report
from reports.push.delivery import push_delivery_summary
from reports.all.order_routing import build_order_routing_report
from reports.serialization import _to_jsonable


@app.post("/api/simulate_mixed")
def simulate_mixed(req: SimulateMixedRequest):
    if req.time_unit not in ("h", "min", "s"):
        raise HTTPException(400, "time_unit must be one of: h, min, s")

    try:
        cfg = _cfg()
    except FileNotFoundError as exc:
        raise HTTPException(status_code=503, detail=str(exc))

    kanban_products, push_products = _build_class_product_sets(cfg)

    t0 = time.time()
    try:
        kenv = run_mixed(
            EXCEL_PATH,
            SETUP_XLSX_PATH,
            n_workers=req.n_workers,
            n_crews=req.n_crews,
            seed=req.seed,
            verbose=False,
            day_start_hour=req.day_start_hour,
            day_length_s=req.day_length_s,
            drain_days=req.drain_days,
            push_chunk_size=req.push_chunk_size,
            horizon_s=req.horizon_s,
        )
    except FileNotFoundError as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    except Exception as exc:  # noqa: BLE001 — surface real sim errors to the UI
        raise HTTPException(status_code=500, detail=f"Mixed simulation failed: {exc}")
    wall_time_s = time.time() - t0

    # --- Gantt --------------------------------------------------------
    event_log = sorted(kenv.event_log, key=lambda e: getattr(e, "start_s", 0.0))
    _push_ctx_for_gantt = getattr(kenv, "push_ctx", None)
    gantt = build_gantt_payload(
        event_log, kenv.env.now, cfg.line_names, time_unit=req.time_unit,
        shift_calendar=cfg.shift_calendar,
        epoch=getattr(_push_ctx_for_gantt, "epoch", None),
    )

    # --- KPI, combined per line + class split ---------------------------
    kpi_by_line = {}
    kpi_by_class = {}
    for line in kenv.lines:
        kpi_by_line[line.line_name] = line_kpi_summary(kenv, line.line_id, kenv.env.now)
        kpi_by_class[line.line_name] = _class_split_kpi(
            kenv, line, kanban_products, push_products,
        )

    kpi_by_crew = _build_kpi_by_crew(kenv)

    # --- Class-1-only diagnostics ---------------------------------------
    shortfall_log = getattr(kenv, "shortfall_log", None)
    card_flow = build_card_flow_payload(kenv, time_unit=req.time_unit)
    card_flow.update(
        build_shortfall_payload(
            line_ids=[
                lid for lid in {
                    getattr(c, "line_id", None) for c in kenv.card_registry.values()
                } if lid is not None
            ],
            sim_time_s=kenv.env.now,
            time_unit=req.time_unit,
            shortfall_log=shortfall_log,
        )
    )
    line_units_series = build_line_units_timeseries(
        kenv.snapshot_log, kenv.env.now, time_unit=req.time_unit,
    )
    restmenge = build_restmenge_payload(kenv)
    restmenge_json = [
        {"line": line_name, "product": product_type, "pcs_partial": pcs_partial}
        for (line_name, product_type), pcs_partial in restmenge.items()
    ]

    # Unfiltered (line_name=None, product_type=None) — the dashboard's
    # Inventory sub-tab filters the full ledger/KPI set client-side, same
    # convention as card_flow/line_units_series above.
    inventory_timeline = build_pull_inventory_timeline_report(kenv, cfg)

    # --- Order routing (line-assignment/cards summary + per-order
    # timeline) — same (kenv, cfg) -> dict shape as every other report
    # built above; included directly here for now, same as the rest of
    # this endpoint (see reports.base.register()'s registry for the
    # GET /api/reports/order_routing alternative, once/if this endpoint
    # migrates onto it).
    order_routing = build_order_routing_report(kenv, cfg)

    # --- Gate status — "which class is running now" panel --------------
    def _gate_current_class(sachnummer: Optional[str]) -> Optional[str]:
        if sachnummer is None:
            return None
        if sachnummer in kanban_products:
            return "pull"
        if sachnummer in push_products:
            return "push"
        return "unclassified"

    _push_ctx_for_status = getattr(kenv, "push_ctx", None)
    _epoch_for_status = getattr(_push_ctx_for_status, "epoch", None)
    _now_dt_for_status = (
        _epoch_for_status + _dt.timedelta(seconds=kenv.env.now)
        if _epoch_for_status is not None else None
    )

    _gate_log_for_status = getattr(kenv, "gate_activity_log", None) or []
    _last_entry_by_line: dict[int, "object"] = {}
    for _e in _gate_log_for_status:
        cur = _last_entry_by_line.get(_e.line_id)
        if cur is None or _e.t_end > cur.t_end:
            _last_entry_by_line[_e.line_id] = _e

    gate_status = {
        line.line_name: {
            "current_rec": getattr(kenv.gates[line.line_id].current_rec, "sachnummer", None),
            "current_class": _gate_current_class(
                getattr(kenv.gates[line.line_id].current_rec, "sachnummer", None)
            ),
            "current_crew_id": getattr(
                _last_entry_by_line.get(line.line_id), "crew_id", None
            ),
            "last_activity_h": kenv.gates[line.line_id].last_activity_t / 3600.0,
            "is_on_shift": (
                kenv.is_line_on(line.line_name, _now_dt_for_status)
                if _now_dt_for_status is not None else True
            ),
        }
        for line in kenv.lines
    }

    # --- Push-specific artifacts -----------------------------------------
    push_delivery_log = getattr(kenv, "push_delivery_log", None) or []
    supermarket_overflow_log = getattr(kenv, "supermarket_overflow_log", None) or []
    push_unassigned_log = getattr(kenv, "push_unassigned_log", None) or []
    push_policy_obj = getattr(kenv, "push_policy", None)

    push_kpi = push_delivery_summary(push_delivery_log)
    push_delivery_log_json = [_to_jsonable(r) for r in push_delivery_log]
    supermarket_overflow_json = [_to_jsonable(f) for f in supermarket_overflow_log]
    push_unassigned_json = [_to_jsonable(u) for u in push_unassigned_log]
    if push_policy_obj is not None:
        push_policy_json = push_policy_obj.to_dict()
        push_policy_json["frozen_zone_hours"] = push_policy_obj.frozen_zone_hours
    else:
        push_policy_json = None

    run_store.set(
        kenv=kenv,
        cfg=cfg,
        day_start_hour=req.day_start_hour,
        day_length_s=req.day_length_s,
    )

    return {
        "gantt": gantt,
        "kpi_by_line": kpi_by_line,
        "kpi_by_class": kpi_by_class,
        "kpi_by_crew": kpi_by_crew,
        "card_flow": card_flow,
        "line_units_series": line_units_series,
        "restmenge": restmenge_json,
        "inventory_timeline": inventory_timeline,
        "order_routing": order_routing,
        "gate_status": gate_status,
        "push_kpi": push_kpi,
        "push_delivery_log": push_delivery_log_json,
        "supermarket_overflow_log": supermarket_overflow_json,
        "push_unassigned_log": push_unassigned_json,
        "push_policy": push_policy_json,
        "summary": {
            "n_workers": req.n_workers,
            "n_crews": req.n_crews,
            "seed": req.seed,
            "day_start_hour": req.day_start_hour,
            "push_chunk_size": req.push_chunk_size,
            "horizon_h": (req.horizon_s or SIM_HORIZON_S) / 3600.0,
            "sim_end_h": kenv.env.now / 3600.0,
            "wall_time_s": wall_time_s,
            "cards_tracked": len(kenv.card_registry),
            "n_days": len(kenv.daily_log) if kenv.daily_log else None,
            "n_kanban_products": len(kanban_products),
            "n_push_products": len(push_products),
            "n_push_delivered": len(push_delivery_log),
            "n_push_unassigned": len(push_unassigned_log),
            "n_supermarket_overflows": len(supermarket_overflow_log),
        },
    }


@app.get("/api/mixed/supermarket_state")
def mixed_supermarket_state(time_unit: str = "h", n_bins: Optional[int] = None):
    """Time-bucketed per-physical-row Supermarket series for the just-
    completed cached run — see reports.all.supermarket_series.
    build_supermarket_state_payload's docstring."""
    if time_unit not in ("h", "min", "s"):
        raise HTTPException(400, "time_unit must be one of: h, min, s")
    kenv, cfg = run_store.require()
    return build_supermarket_state_payload(kenv, cfg, n_bins=n_bins, time_unit=time_unit)


@app.get("/api/mixed/crew_activity")
def mixed_crew_activity(
    crew_id: Optional[int] = None,
    line_id: Optional[int] = None,
    day_index: Optional[int] = None,
    hour: Optional[int] = None,
    time_unit: str = "h",
):
    """
    Chronological production log, optionally scoped to one crew and/or
    one line. Each entry is one completed unit of production (one pull
    card, or one push chunk) straight off kenv.gate_activity_log.
    `summary` is the same per-crew breakdown /api/simulate_mixed's
    top-level `kpi_by_crew` returns, computed over the FULL run
    regardless of any day_index/hour/line_id/crew_id filter applied to
    `entries`.
    """
    kenv, _cfg_obj = run_store.require()
    if time_unit not in ("h", "min", "s"):
        raise HTTPException(status_code=400, detail="time_unit must be one of: h, min, s")
    divisor = _TIME_DIVISORS[time_unit]

    window = run_store.day_window_s(day_index, hour)
    id_to_name = {line.line_id: line.line_name for line in kenv.lines}
    gate_log = getattr(kenv, "gate_activity_log", None) or []

    entries = []
    for e in gate_log:
        if crew_id is not None and getattr(e, "crew_id", None) != crew_id:
            continue
        if line_id is not None and e.line_id != line_id:
            continue
        if window is not None and not (window[0] <= e.t_start < window[1]):
            continue
        entries.append({
            "crew_id": getattr(e, "crew_id", None),
            "line_id": e.line_id,
            "line_name": id_to_name.get(e.line_id, str(e.line_id)),
            "sachnummer": e.sachnummer,
            "production_type": e.production_type,
            "quantity": e.quantity,
            "possible_changeover": e.possible_changeover,
            "t_start": e.t_start / divisor,
            "t_end": e.t_end / divisor,
        })
    entries.sort(key=lambda x: x["t_start"])

    return {
        "entries": entries,
        "summary": _build_kpi_by_crew(kenv),
        "time_unit": time_unit,
    }
