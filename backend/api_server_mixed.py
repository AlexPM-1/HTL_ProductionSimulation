"""
api_server_mixed.py
====================
Thin FastAPI wrapper around mixed_runner.run_mixed() ONLY — Option 3
(mixed push/pull: class-1 Kanban + class-2 push on one continuous SimPy
clock). This file deliberately does NOT expose /api/simulate,
/api/simulate_optimized or /api/simulate_multiday (those are push-only /
kanban-only and belong to api_server.py) — kept separate so this server
stays cheap to read and run for the mixed dashboard alone.

Endpoints
---------
GET  /api/health
GET  /api/parameters       -> same shape as api_server.py's, read live
                               from SimConfig (sidebar inputs)
POST /api/simulate_mixed   -> runs mixed_runner.run_mixed(), returns
                               { gantt, kpi_by_line, kpi_by_class,
                                 kpi_by_crew, card_flow, line_units_series,
                                 restmenge, gate_status, push_kpi,
                                 push_delivery_log,
                                 supermarket_overflow_log,
                                 push_unassigned_log, push_policy, summary }
GET  /api/mixed/push_policy    -> current push policy knobs (live ctx
                                   values if a run has happened, else bare
                                   PushPolicyConfig() defaults)
PATCH /api/mixed/push_policy   -> partial update of the ACTIVE run's push
                                   policy (frozen zone, visibility/lead
                                   times, rush threshold, retry interval).
                                   Requires a prior POST /api/simulate_mixed.
GET  /api/mixed/supermarket_state -> time-bucketed per-physical-row
                                   Supermarket series (Main runner +
                                   Exotic rows) for the "All Production"
                                   Supermarkets view. Requires a prior
                                   POST /api/simulate_mixed — see
                                   _build_supermarket_state_payload().
GET  /api/mixed/plant_structure   -> static per-line Plant Layout shape
                                   (Supermarket rows, Restmenge segment
                                   per Main-runner product, Batch
                                   Collector product rows + trigger
                                   amounts, Chute frozen-zone size) for
                                   the Movement Simulation page's spatial
                                   view. Requires a prior POST
                                   /api/simulate_mixed — see
                                   _build_plant_structure_payload().
GET  /api/mixed/movement_state    -> time-indexed per-card occupancy:
                                   which cards sit in which Supermarket
                                   row/slot, Batch Collector bucket,
                                   Collection Box, and Kanban Chute queue
                                   position, at each of a series of
                                   evenly spaced instants across a
                                   day/hour window (default one snapshot
                                   every 5 real-world minutes — see
                                   interval_min). The animated counterpart
                                   to plant_structure's static shape — see
                                   _build_movement_state_payload().
                                   include_push=true adds push-side
                                   presence (Exotic row push_count, Chute
                                   push_pending) for the "All Production"
                                   tab's single combined Plant Layout.
                                   Also includes, per line, a time-indexed
                                   "production_status" ({"state":
                                   "producing"|"idle"|"off_shift",
                                   "sachnummer", "sim_class", "crew_id",
                                   "since_t", "possible_changeover"})
                                   resolved from kenv.gate_activity_log —
                                   see mixed_runner.production_status_at().
                                   "crew_id" (v10) is which
                                   crew_process(crew_id=...) instance was
                                   holding the line's gate at that instant
                                   — None whenever state isn't "producing".
                                   "off_shift" (v6) means this line isn't
                                   scheduled to run at t_s at all, per the
                                   workbook's "Shifts" sheet — distinct
                                   from a genuine "idle" gap on an
                                   on-shift line. Only ever reported when
                                   the loaded workbook has shift data at
                                   all (cfg.shift_calendar.shifts
                                   non-empty); older/shift-less workbooks
                                   only ever report "producing"/"idle",
                                   exactly as before.
GET  /api/mixed/crew_activity     -> chronological per-crew production
                                   log — "production of crew 1",
                                   "production of crew 2", etc., the
                                   crew-based counterpart to filtering the
                                   Gantt/KPI views by line. One entry per
                                   completed unit (one pull card, or one
                                   push chunk), each carrying line_id/
                                   line_name/sachnummer/sim_class/
                                   quantity/t_start/t_end (in the
                                   requested time_unit) — straight off
                                   kenv.gate_activity_log, optionally
                                   filtered by crew_id and/or line_id.
                                   Requires a prior POST
                                   /api/simulate_mixed. See
                                   mixed_crew_activity()'s own docstring.

kpi_by_line vs kpi_by_class
----------------------------
kpi_by_line   — process_logic_sequential_v3.line_kpi_summary() per line,
                UNCHANGED / combined: passed, scrapped, reworked,
                total_created, station_utilisation, buffer_max_fill,
                mean_cycle_time_s. This counts BOTH class-1 (kanban) and
                class-2 (push) parts together, because entities_resources
                _v4.Part carries no class flag — station/buffer stats are
                genuinely shared physical resources anyway, so a
                per-class split of those two fields wouldn't mean
                anything different from the combined figure.

kpi_by_class  — passed/scrapped/reworked/mean_cycle_time_s split into
                "pull" and "push" buckets per line, computed here (not in
                process_logic_sequential_v3.py) by bucketing
                kenv.parts_out on Part.product_type membership in one of
                two sets derived from cfg:
                  - kanban products = { evt.product for evt in
                    cfg.kanban_withdrawals } — the v5 sheet is one row
                    per withdrawal event now (Date | Time | Product |
                    TotalQuantity | LineId), not the old wide per-slot
                    layout, so there's no more qty_by_product dict to
                    pull keys from; config_loader_v5.py's
                    _parse_customer_demand_kanban_sheet only keeps
                    rows with quantity > 0, so every event.product seen
                    here is a real (qty>0, at least once) class-1 product
                  - push products   = { d.product_id for d in cfg.demand }
                A product_type that ends up in neither set (shouldn't
                happen per the spec's disjoint-sheets assumption, but the
                sheets are user-edited) is silently excluded from both
                buckets rather than mis-attributed — see
                _build_class_product_sets()'s docstring.

restmenge / card_flow are class-1-only by construction (kanban_process_
logic.print_restmenge_report / kanban_events.build_card_flow_payload
only ever look at kenv.card_registry, which push never populates — see
that reasoning already validated against kanban_events.py's source).
There is no push-side restmenge equivalent in mixed_runner.py yet
(unfinished push chunks aren't tracked as a queryable list) — flagged
here, not silently invented.

push_kpi / push_delivery_log / supermarket_overflow_log /
push_unassigned_log / push_policy — straight pass-throughs of the
artifacts mixed_runner.run_mixed() already attaches to kenv
(push_delivery_log, supermarket_overflow_log, push_unassigned_log,
push_policy) plus push_delivery_summary(kenv.push_delivery_log) for
push_kpi. Dataclass logs are converted to plain JSON-safe dicts via
_to_jsonable() below (datetimes -> isoformat strings) rather than
FastAPI's default dataclass handling, since these lists can mix
dataclasses (PushDeliveryRecord, SupermarketOverflowFlag) with
whatever dispatch_entities.UnassignedOrder turns out to be — this
server was written without that file available, so the serializer
does not assume its exact shape.

gate_status[line].current_class — 'pull' | 'push' | 'unclassified' |
null, derived by checking gates[line].current_rec.sachnummer against
the same kanban_products/push_products sets _build_class_product_sets()
already computes for kpi_by_class. No new field on LinePriorityGate
was needed for this.

gate_status[line].current_crew_id (v10) — which crew_process(crew_id=...)
instance most recently held this line's gate, i.e. the crew_id of
whichever kenv.gate_activity_log entry for this line has the latest
t_end at the moment the run stopped. Same "last known, not literally
live" caveat as current_rec (the run has already ended by the time this
response is built) — None if this line never ran anything at all.

kpi_by_crew — per crew_id (n_crews from mixed_runner.run_mixed(n_crews=...)),
a pull/push unit-count + total-quantity breakdown plus which lines that
crew actually worked, built from kenv.gate_activity_log grouped by
crew_id (NOT from kenv.parts_out, unlike kpi_by_class — a GateActivityEntry
already IS one crew's one turn-unit, so no product-set membership lookup
is needed here). This is the "production of crew 1 / production of crew
2" summary, the crew-based counterpart to kpi_by_line. See
_build_kpi_by_crew()'s docstring for the exact shape.

gate_status[line].is_on_shift (v6) — live snapshot of whether this line
is on-shift at kenv.env.now (the instant the run stopped), via
kenv.is_line_on() (SimEnvironment.is_line_on(), entities_resources_v5.py
— the same query surface mixed_runner.py's PushSchedulerContext.
is_line_on() and _gated_run_one_kanban_batch already use directly, with
the "no Shifts sheet at all -> every line always on" fallback baked in
there). True unconditionally for a workbook with no "Shifts" sheet at
all. This is the /api/simulate_mixed-response counterpart to
/api/mixed/movement_state's time-indexed "off_shift" production_status
state (see that endpoint's own docstring) — this one only ever reflects
the single instant the run ended, not history.

card_flow.shortfall_by_line — per-line, per-bin count of Kanban
withdrawal requests that found their Supermarket empty at the instant
they asked ("customer wanted a card, none was on the shelf") and had to
wait. Sourced from kanban_process_logic.ShortfallEvent /
KanbanRuntime.record_shortfall (new), folded in here via
kenv.shortfall_log — see the shortfall_log comment at this endpoint's
call site below for the one piece still owed by mixed_runner.py.

Run
---
    pip install fastapi "uvicorn[standard]"
    export SIM_EXCEL_PATH=/path/to/ProductionPlanning_v6.xlsx
    export SIM_SETUP_XLSX_PATH=/path/to/HTL_setup_times.xlsx
    # run from backend/ so imports resolve (mixed_runner.py,
    # config_loader_v5.py, entities_resources_v4.py, dispatch_entities.py,
    # order_dispatcher_simple_adjuster.py, process_logic_sequential_v3.py,
    # kanban_process_logic.py, kanban_events.py, kanban_runner.py,
    # schedule_events.py)
    uvicorn api_server_mixed:app --reload --port 8000
"""

from __future__ import annotations

import dataclasses
import datetime as _dt
import os
import time
from functools import lru_cache
from typing import Optional

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from config_loader_v6 import load_config, SimConfig
from process_logic_sequential_v3 import line_kpi_summary
from schedule_events import build_gantt_payload
from kanban_events import build_card_flow_payload, build_line_units_timeseries
from kanban_process_logic_v2 import print_restmenge_report
from movement_trace import (
    build_part_trace_payload,
    build_part_ids_payload,
    build_card_trace_payload,
    build_movement_frame_cards,
)

from mixed_runner import (
    DEFAULT_N_WORKERS,
    run_mixed,
    DAY_START_HOUR, DAY_LENGTH_S, DRAIN_DAYS,
    PUSH_CHUNK_SIZE, SIM_HORIZON_S,
    PushPolicyConfig, apply_push_policy, push_delivery_summary,
    push_chute_entries_at, production_status_at,
)

# ---------------------------------------------------------------------------
# Paths — same env-var convention as api_server.py.
# ---------------------------------------------------------------------------
EXCEL_PATH = os.environ.get("SIM_EXCEL_PATH", "ProductionPlanning_v6.xlsx")
SETUP_XLSX_PATH = os.environ.get("SIM_SETUP_XLSX_PATH", "HTL_setup_times.xlsx")
PRODUCT_MASTER_PATH = os.environ.get("SIM_PRODUCT_MASTER_PATH", "product_master.xlsx")

CORS_ORIGINS = os.environ.get(
    "SIM_CORS_ORIGINS", "http://localhost:5173,http://localhost:3000"
).split(",")

app = FastAPI(title="Mixed Push/Pull Planner API", version="1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:5500",
        "http://127.0.0.1:5500",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ---------------------------------------------------------------------------
# Last-run cache — single global slot, same "last run wins" spirit as
# api_server.py's _LAST_KANBAN_ENV. Backs the /api/mixed/part_trace,
# /api/mixed/part_ids, /api/mixed/card_ids and /api/mixed/card_trace
# endpoints below (movement-simulation page), so a run doesn't need to be
# threaded through again. day_start_hour/day_length_s are cached
# alongside kenv/cfg because they're per-request inputs to
# /api/simulate_mixed (not fixed constants) and the day/hour windowing
# helper below needs the values that were ACTUALLY used for the cached
# run, not just mixed_runner's defaults.
# ---------------------------------------------------------------------------
_LAST_MIXED_KENV = None
_LAST_MIXED_CFG: Optional[SimConfig] = None
_LAST_MIXED_DAY_START_HOUR: Optional[int] = None
_LAST_MIXED_DAY_LENGTH_S: Optional[float] = None


@lru_cache(maxsize=1)
def _cfg() -> SimConfig:
    return load_config(EXCEL_PATH, SETUP_XLSX_PATH, PRODUCT_MASTER_PATH)


@app.get("/api/health")
def health():
    try:
        cfg = _cfg()
        return {"status": "ok", "lines": cfg.line_names}
    except FileNotFoundError as e:
        raise HTTPException(status_code=503, detail=str(e))


@app.get("/api/parameters")
def get_parameters(reload: bool = False):
    """Same shape as api_server.py's /api/parameters, for a shared sidebar."""
    if reload:
        _cfg.cache_clear()
    try:
        cfg = _cfg()
    except FileNotFoundError as e:
        raise HTTPException(status_code=503, detail=str(e))

    return {
        "lines": cfg.line_names,
        "n_kanban_products": len(_build_class_product_sets(cfg)[0]),
        "n_push_products": len(_build_class_product_sets(cfg)[1]),
        "worker_options": [1, 2],
        "default_workers": DEFAULT_N_WORKERS,
        # v10: crew count for the crew-based depletion model (mixed_runner
        # .crew_process) — a crew can hold at most one line at a time, so
        # more crews than lines just means some crews are always idle;
        # the sidebar can offer up to len(lines) as a sane upper bound.
        "crew_options": list(range(1, max(len(cfg.line_names), 1) + 1)),
        "default_n_crews": 2,
        "default_day_start_hour": DAY_START_HOUR,
        "default_push_chunk_size": PUSH_CHUNK_SIZE,
        # v6: whether the loaded workbook's "Shifts" sheet actually
        # configured any shift-time definitions at all — see
        # config_loader_v6.ShiftCalendar / _parse_shifts_sheet. False for
        # any pre-v6 (or v6-but-Shifts-less) workbook, in which case
        # every line/every time is treated as always-on and
        # production_status/movement_state will never report
        # "off_shift" — a frontend can use this to decide whether it's
        # even worth rendering an off-shift legend/toggle at all.
        "has_shift_calendar": bool(cfg.shift_calendar.shifts),
    }


# ---------------------------------------------------------------------------
# Push policy — read/edit the class-2 knobs (PushPolicyConfig).
# ---------------------------------------------------------------------------

@app.get("/api/mixed/push_policy")
def get_push_policy():
    """
    Current push policy. If a mixed run has already happened, this
    reflects the LIVE ctx.policy that push_dispatch_process/
    push_drain_process are actually reading from ("live": true) — the
    same object PATCH below edits. Otherwise it falls back to
    PushPolicyConfig()'s bare defaults ("live": false), purely so a
    settings panel has sensible starting values before the first run.
    """
    if _LAST_MIXED_KENV is not None and getattr(_LAST_MIXED_KENV, "push_policy", None) is not None:
        policy = _LAST_MIXED_KENV.push_policy
        live = True
    else:
        policy = PushPolicyConfig()
        live = False
    d = policy.to_dict()
    d["frozen_zone_hours"] = policy.frozen_zone_hours
    d["live"] = live
    return d


class PushPolicyPatchRequest(BaseModel):
    """All fields optional — unset ones keep the current policy's value.
    Mirrors PushPolicyConfig's own fields 1:1 (see that class for what
    each knob does and which are live-immediately vs. need this call vs.
    apply only to new orders — same caveats as apply_push_policy())."""
    frozen_zone_cards: Optional[int] = Field(default=None, ge=0)
    card_production_time_min: Optional[float] = Field(default=None, gt=0)
    push_visibility_days: Optional[int] = Field(default=None, ge=0)
    ideal_lead_time_h: Optional[float] = Field(default=None, ge=0)
    max_lead_time_h: Optional[float] = Field(default=None, ge=0)
    rush_threshold_h: Optional[float] = Field(default=None, ge=0)
    retry_interval_h: Optional[float] = Field(default=None, gt=0)


@app.patch("/api/mixed/push_policy")
def patch_push_policy(req: PushPolicyPatchRequest):
    """
    Partial update of the ACTIVE run's push policy. Requires a prior
    POST /api/simulate_mixed — there is no ctx to edit before that (use
    GET /api/mixed/push_policy beforehand if the UI just wants to know
    what values to preset a form with).

    Builds a full PushPolicyConfig from (current values + provided
    overrides) so the cross-field validation in
    PushPolicyConfig.__post_init__ (max_lead_time_h >= ideal_lead_time_h,
    frozen_zone_cards >= 0) runs against the RESULTING policy, not just
    the fields touched by this request. Then calls apply_push_policy()
    so frozen_zone_cards actually re-syncs onto every chute — see that
    function's docstring for exactly what's live-immediately vs. applies
    only to new orders.
    """
    kenv, _cfg_unused = _require_mixed_run()
    ctx = getattr(kenv, "push_ctx", None)
    if ctx is None:
        raise HTTPException(
            status_code=409,
            detail="Active run has no push_ctx — mixed_runner.py may be out of sync with this server.",
        )

    updates = req.model_dump(exclude_none=True) if hasattr(req, "model_dump") else req.dict(exclude_none=True)
    merged = ctx.policy.to_dict()
    merged.update(updates)
    try:
        new_policy = PushPolicyConfig.from_dict(merged)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    apply_push_policy(ctx, new_policy)
    kenv.push_policy = ctx.policy  # keep the kenv-level mirror in sync, same spot run_mixed() sets it

    d = ctx.policy.to_dict()
    d["frozen_zone_hours"] = ctx.policy.frozen_zone_hours
    d["live"] = True
    return d


# ---------------------------------------------------------------------------
# JSON-safety for the new push-side logs (see module docstring's
# "push_kpi / push_delivery_log / ..." section for why this exists
# instead of leaning on FastAPI's default dataclass encoding).
# ---------------------------------------------------------------------------

def _to_jsonable(obj):
    """
    Recursively turn a dataclass instance (or list/dict of them) into
    plain JSON-safe Python — datetimes become ISO-8601 strings, nested
    dataclasses become dicts. Deliberately does not import or assume the
    concrete type of dispatch_entities.UnassignedOrder (unavailable when
    this server was written) — dataclasses.is_dataclass() + asdict()
    handles any dataclass shape without knowing its fields up front.
    """
    if obj is None or isinstance(obj, (str, int, float, bool)):
        return obj
    if isinstance(obj, (_dt.datetime, _dt.date)):
        return obj.isoformat()
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        return {k: _to_jsonable(v) for k, v in dataclasses.asdict(obj).items()}
    if isinstance(obj, dict):
        return {k: _to_jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple, set)):
        return [_to_jsonable(v) for v in obj]
    # Fallback for a plain (non-dataclass) object — best-effort via __dict__
    # rather than raising, so one unexpected type doesn't 500 the endpoint.
    return getattr(obj, "__dict__", str(obj))


# ---------------------------------------------------------------------------
# Class-set derivation (see module docstring's "kpi_by_class" section).
# ---------------------------------------------------------------------------

def _build_class_product_sets(cfg: SimConfig) -> tuple[set[str], set[str]]:
    """
    (kanban_products, push_products) — the sachnummer/product_id sets that
    define class-1 vs class-2 for KPI bucketing.

    kanban_products: { evt.product for evt in cfg.kanban_withdrawals } —
    v5's CustomerDemandKanban sheet is a flat long table, one
    KanbanWithdrawalEvent per withdrawal row (Date | Time | Product |
    TotalQuantity | LineId), not the old wide per-15-min-slot layout with
    a qty_by_product/priority_by_product/line_by_product dict per row —
    see config_loader_v5.KanbanWithdrawalEvent's own docstring for that
    change. _parse_customer_demand_kanban_sheet only keeps rows with
    quantity > 0, so every event.product seen here is a real class-1
    product.

    push_products: { d.product_id for d in cfg.demand } (CustomerDemand
    rows — one row per (Date, Product), product_id is the per-row field
    per config_loader_v5.CustomerDemand).

    Per the spec these two sets should be disjoint (a product runs as
    EITHER pull or push, not both), but the sheets are user-edited by
    hand — callers should not assume disjointness; this function does not
    enforce or silently fix an overlap, it just returns what's there.
    """
    kanban_products: set[str] = {
        evt.product for evt in (getattr(cfg, "kanban_withdrawals", []) or [])
    }

    push_products: set[str] = {
        d.product_id for d in (getattr(cfg, "demand", []) or [])
    }
    return kanban_products, push_products


def _class_split_kpi(
    kenv, line, kanban_products: set[str], push_products: set[str],
) -> dict:
    """
    Per-line pull vs push KPI bucket, computed by filtering
    kenv.parts_out (already restricted to this line) on product_type
    membership. Mirrors the passed/scrapped/reworked/mean_cycle_time_s
    fields of process_logic_sequential_v3.line_kpi_summary() but does NOT
    duplicate station_utilisation / buffer_max_fill / total_created —
    those are combined-only, see module docstring.
    """
    parts_out = [p for p in kenv.parts_out if p.line_id == line.line_id]

    def _bucket(products: set[str]) -> dict:
        subset = [p for p in parts_out if p.product_type in products]
        passed = [p for p in subset if p.status == "passed"]
        scrapped = [p for p in subset if p.status == "scrapped"]
        reworked = [p for p in subset if p.rework_pass > 0]
        mean_ct = (
            sum(p.cycle_time for p in passed) / len(passed) if passed else None
        )
        return {
            "passed": len(passed),
            "scrapped": len(scrapped),
            "reworked": len(reworked),
            "mean_cycle_time_s": mean_ct,
        }

    unclassified = [
        p for p in parts_out
        if p.product_type not in kanban_products and p.product_type not in push_products
    ]

    return {
        "pull": _bucket(kanban_products),
        "push": _bucket(push_products),
        "unclassified_count": len(unclassified),  # flags sheet overlap/gaps, see docstring above
    }


def _build_kpi_by_crew(kenv) -> dict:
    """
    Per-crew production summary — the "production of crew 1 / production
    of crew 2" counterpart to kpi_by_line/kpi_by_class, built from
    kenv.gate_activity_log (v10: every GateActivityEntry now carries
    crew_id — see mixed_runner.GateActivityEntry) rather than from
    kenv.parts_out, since a GateActivityEntry already IS one crew's one
    completed turn-unit (one pull card, or one push chunk) — no
    product-set membership lookup needed, unlike kpi_by_class.

    Reads kenv.n_crews (set by run_mixed(n_crews=...) — see that
    function's tail bookkeeping) so a crew that happened to never get any
    work (e.g. more crews configured than lines exist) still gets a
    zeroed-out entry, instead of silently disappearing the way inferring
    crew count from "which crew_ids appear in the log" would.

    Shape, keyed "crew_1", "crew_2", ... (1-based label, matching how
    line_name keys already read to a frontend — crew_id itself, 0-based,
    is included inside each entry for anything that needs the raw index):
        {
          "crew_1": {
            "crew_id": 0,
            "pull": {"n_units": int, "total_quantity": int},
            "push": {"n_units": int, "total_quantity": int},
            "n_units_total": int,
            "total_quantity_total": int,
            "lines_worked": [line_name, ...],   # sorted, distinct
          },
          ...
        }
    """
    gate_log = getattr(kenv, "gate_activity_log", None) or []
    n_crews = getattr(kenv, "n_crews", None)
    if n_crews is None:
        # Defensive fallback for a kenv from a run predating kenv.n_crews
        # being set — infer from whatever crew_ids actually appear rather
        # than 500ing (same "degrade gracefully" spirit as the other
        # getattr-guarded push artifacts in this module).
        seen = {getattr(e, "crew_id", None) for e in gate_log}
        seen.discard(None)
        n_crews = (max(seen) + 1) if seen else 0

    id_to_name = {line.line_id: line.line_name for line in kenv.lines}

    result: dict = {}
    for crew_id in range(n_crews):
        entries = [e for e in gate_log if getattr(e, "crew_id", None) == crew_id]

        def _bucket(sim_class: str) -> dict:
            subset = [e for e in entries if e.sim_class == sim_class]
            return {
                "n_units": len(subset),
                "total_quantity": sum(e.quantity or 0 for e in subset),
            }

        result[f"crew_{crew_id + 1}"] = {
            "crew_id": crew_id,
            "pull": _bucket("pull"),
            "push": _bucket("push"),
            "n_units_total": len(entries),
            "total_quantity_total": sum(e.quantity or 0 for e in entries),
            "lines_worked": sorted({
                id_to_name.get(e.line_id, str(e.line_id)) for e in entries
            }),
        }
    return result


# ---------------------------------------------------------------------------
# Supermarket state — per physical Supermarket row (Main runner + Exotic),
# time-bucketed, for the "All Production" tab's Supermarkets view.
# ---------------------------------------------------------------------------

_SM_DIVISORS: dict[str, float] = {"h": 3600.0, "min": 60.0, "s": 1.0}

# Rendering-only slot-count slack for the Plant Layout's Batch Collector /
# Chute boxes (see _build_plant_structure_payload's docstring) — neither
# BatchCollectorResource nor KanbanChuteResource has a real fixed capacity
# in entities_resources_v5.py, so these give movement_layout.js.
# renderPlantLayout() a finite, slightly-generous slot count to draw
# rather than an unbounded one.
_BC_CAPACITY_SLACK = 4
_CHUTE_CAPACITY_SLACK = 12
_MAX_MOVEMENT_FRAMES = 1000  # cap on GET /api/mixed/movement_state's frame count either way it's derived


def _sm_state_at(snaps: list, edge_s: float, fields: tuple[str, ...]) -> tuple:
    """Generic 'last reading at or before edge_s' lookup, shared by both
    the Main-runner (SupermarketSnapshot: n_available, pcs_partial) and
    Exotic (ExoticSlotSnapshot: occupant, n_cards) snapshot shapes —
    `fields` names the attributes to read off whichever snapshot wins.
    Mirrors kanban_events.build_line_units_timeseries's private
    _state_at(), duplicated here (rather than imported) to keep
    kanban_events.py's zero-dependency-on-entities/config promise intact
    (see that module's docstring, point 2) — this file already imports
    the full config/entities stack anyway, so there's no such constraint
    here.
    """
    current = tuple(0 for _ in fields)
    if fields and fields[0] == "occupant":
        current = (None,) + tuple(0 for _ in fields[1:])
    for s in snaps:
        if s.t > edge_s:
            break
        current = tuple(getattr(s, f) for f in fields)
    return current


def _exotic_products_at(snaps: list, edge_s: float) -> list[dict]:
    """
    Per-product breakdown for one Exotic Supermarket row, as of edge_s —
    same "last reading at or before edge_s" convention as _sm_state_at,
    but returns EVERY product currently sitting in the row at once
    instead of a single (occupant, n_cards) pair.

    Prefers ExoticSlotSnapshot.occupants, if present — a list of
    {"sachnummer": ..., "n_cards": ...} entries (or (sachnummer, n_cards)
    tuples) covering every product simultaneously occupying the row. This
    is the shape needed for a row that hasn't fully emptied before a
    different product starts filling it: a real physical row can (and
    per the current sim-side behavior, visibly does) hold more than one
    product's pieces at once, and that mix should stay visible rather
    than being reported as if the whole row belongs to whichever product
    happened to be deposited most recently.

    Falls back to the legacy single-occupant `occupant`/`n_cards`
    attributes (wrapped as a one-entry list) for snapshot logs that don't
    carry `occupants` yet — i.e. every ExoticSlotSnapshot produced by
    mixed_runner.py today, where a new deposit onto a non-empty row
    overwrites the row's single occupant/n_cards fields instead of
    tracking the mix. Read defensively via getattr so this function is
    safe to call right now; it will pick up real multi-product data
    automatically, no caller changes needed, once mixed_runner.py's
    exotic-row deposit logic is extended to record `occupants` (that
    sim-side change is the actual fix for the "whole row shows one
    product number even while a second product fills it" behavior — this
    function and its callers can only report what the snapshot log
    contains).
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
                    "sachnummer": (o.get("sachnummer") if isinstance(o, dict) else o[0]),
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
        return [{"sachnummer": occupant, "push_count": n_cards}]
    return []


def _build_supermarket_state_payload(
    kenv, cfg: SimConfig, n_bins: Optional[int] = None, time_unit: str = "h",
) -> dict:
    """
    Time-bucketed, per-physical-row Supermarket occupancy for the "All
    Production" Supermarkets view — same n_bins+1-point (~15 real-world
    minutes per bin by default), same "last reading at or before this bin's
    edge" convention, and the same day-tab-friendly shape
    kanban_events.build_line_units_timeseries already uses for the
    Kanban (Pull) tab's Supermarkets view, so the two panels feel like
    the same chart, just sliced differently — see that function's
    docstring for the shared reasoning (t=0 seed point, ~hourly default
    bin width, etc).

    Two kinds of row come out of cfg.supermarkets[line_name], plus two
    synthetic extras:

    1. "Main runner" rows — one entry per PHYSICAL row from the sheet.
       Several physical rows can share the same (line, sachnummer) — see
       build_kanban_environment's docstring — and the simulation only
       ever tracks ONE combined logical store per (line, sachnummer)
       over time (kenv.snapshot_log, grouped by (line_name,
       product_type) — the same log kanban_events.
       build_line_units_timeseries reads), not per individual physical
       lane. So each row's "series" is the shared group's time series
       (n_available at each bin) run through a deterministic first-fill
       distribution across the group's rows in row_number order (row 1
       fills to its own capacity first, then row 2, etc.) at EVERY bin
       — a per-bin repeat of the same approximation the (now-retired)
       single-snapshot version of this endpoint used, not a separately
       tracked truth (the sim doesn't know which physical lane a card
       sits in). `shared_rows` lists every row_number in the group.
    2. "Exotic" rows — one entry per physical row, with a REAL per-row
       time series built from kenv.exotic_snapshot_log
       (mixed_runner.ExoticSlotSnapshot, one reading per slot per push
       deposit — see that dataclass's docstring) — genuinely tracked per
       physical slot over time, unlike Main runner rows above. Falls
       back to an all-empty series if exotic_snapshot_log isn't present
       (older mixed_runner.py), read defensively via getattr.
    3. "restmenge_series" (per line) — the Main-runner groups' pcs_partial
       (loose Restmenge pieces) over time, one key per product plus a
       "total", same shape as kanban_events.build_line_units_timeseries's
       restmenge_series — this is group-level by construction (loose
       pieces aren't attributable to one physical lane any more than
       n_available is), so it's reported once per line rather than
       repeated per row.
    4. "exotic_routed_products" (per line) — Kanban-eligible products
       (per KanbanCardsSetup) with NO dedicated Main-runner row on this
       line, intentionally routed through the line's shared Exotic pool
       instead (SupermarketResource.is_exotic_routed). These don't
       correspond to any cfg.supermarkets row, so they can't be listed
       under (1) — but kenv.snapshot_log still has entries for them (any
       (line, product) pull group gets logged, row or no row), so they
       still get a real "series" here, just not tied to a physical slot.
    """
    divisor = _SM_DIVISORS[time_unit]
    sim_time_s = kenv.env.now
    if n_bins is None:
        n_bins = max(1, round(sim_time_s / 600.0))  # ~15-real-world-min bins by default

    line_id_by_name: dict[str, int] = {line.line_name: line.line_id for line in kenv.lines}
    kenv_supermarkets: dict = getattr(kenv, "supermarkets", None) or {}

    # --- Main-runner (pull) group snapshots, grouped + sorted by t ------
    main_groups: dict[tuple[str, str], list] = {}
    for snap in (getattr(kenv, "snapshot_log", None) or []):
        main_groups.setdefault((snap.line_name, snap.product_type), []).append(snap)
    for snaps in main_groups.values():
        snaps.sort(key=lambda s: s.t)

    # --- Exotic slot snapshots, grouped + sorted by t --------------------
    exotic_groups: dict[tuple[str, int], list] = {}
    for snap in (getattr(kenv, "exotic_snapshot_log", None) or []):
        exotic_groups.setdefault((snap.line, snap.row_number), []).append(snap)
    for snaps in exotic_groups.values():
        snaps.sort(key=lambda s: s.t)

    edges = [sim_time_s * i / n_bins for i in range(n_bins + 1)] if n_bins > 0 else []

    lines_out = []
    for line_name, slot_cfgs in (cfg.supermarkets or {}).items():
        line_id = line_id_by_name.get(line_name)
        supermarket_lane = kenv_supermarkets.get(line_id, {}) if line_id is not None else {}

        main_runner_groups: dict[str, list] = {}
        for slot in slot_cfgs:
            if slot.is_exotic or not slot.sachnummer:
                continue
            main_runner_groups.setdefault(slot.sachnummer, []).append(slot)
        for rows in main_runner_groups.values():
            rows.sort(key=lambda s: s.row_number)

        rows_out = []
        for slot in sorted(slot_cfgs, key=lambda s: s.row_number):
            if slot.is_exotic:
                snaps = exotic_groups.get((line_name, slot.row_number), [])
                series = [
                    {
                        "t": round(edge_s / divisor, 4),
                        "sachnummer": products[0]["sachnummer"] if products else None,
                        "n_cards": sum(p["push_count"] for p in products),
                        "products": products,
                    }
                    for edge_s in edges
                    for products in [_exotic_products_at(snaps, edge_s)]
                ]
                rows_out.append({
                    "row_number": slot.row_number,
                    "type": "Exotic",
                    "is_exotic": True,
                    "capacity": slot.capacity,
                    "series": series,
                })
            else:
                group_rows = main_runner_groups.get(slot.sachnummer, [slot])
                snaps = main_groups.get((line_name, slot.sachnummer), [])
                series = []
                for edge_s in edges:
                    n_available, pcs_partial = _sm_state_at(snaps, edge_s, ("n_available", "pcs_partial"))
                    # First-fill this bin's group total across the group's
                    # physical rows, in row_number order, same rule as the
                    # docstring above.
                    remaining = n_available
                    row_available = 0
                    for s in group_rows:
                        take = min(s.capacity, remaining)
                        if s.row_number == slot.row_number:
                            row_available = take
                        remaining -= take
                    series.append({
                        "t": round(edge_s / divisor, 4),
                        "n_cards": row_available,
                        "group_n_available": n_available,
                        "pcs_partial": pcs_partial,
                    })
                rows_out.append({
                    "row_number": slot.row_number,
                    "type": slot.slot_type or "Main runner",
                    "is_exotic": False,
                    "sachnummer": slot.sachnummer,
                    "capacity": slot.capacity,
                    "group_capacity": sum(s.capacity for s in group_rows),
                    "shared_rows": [s.row_number for s in group_rows],
                    "batch_size": getattr(supermarket_lane.get(slot.sachnummer), "batch_size", None),
                    "series": series,
                })

        # --- Restmenge (pcs_partial), per line, per Main-runner product --
        line_products = sorted(main_runner_groups.keys())
        restmenge_series = []
        for edge_s in edges:
            pt = {"t": round(edge_s / divisor, 4)}
            total = 0
            for product_type in line_products:
                snaps = main_groups.get((line_name, product_type), [])
                _, pcs_partial = _sm_state_at(snaps, edge_s, ("n_available", "pcs_partial"))
                pt[product_type] = pcs_partial
                total += pcs_partial
            pt["total"] = total
            restmenge_series.append(pt)

        # --- Exotic-routed products (no physical row at all) ------------
        exotic_routed_products = []
        for sachnr, sm in sorted(supermarket_lane.items()):
            if not getattr(sm, "is_exotic_routed", False):
                continue
            snaps = main_groups.get((line_name, sachnr), [])
            series = []
            for edge_s in edges:
                n_available, pcs_partial = _sm_state_at(snaps, edge_s, ("n_available", "pcs_partial"))
                series.append({"t": round(edge_s / divisor, 4), "n_cards": n_available, "pcs_partial": pcs_partial})
            exotic_routed_products.append({
                "sachnummer": sachnr,
                "capacity": sm.capacity,
                "batch_size": sm.batch_size,
                "series": series,
            })

        lines_out.append({
            "line_name": line_name,
            "rows": rows_out,
            "restmenge_series": restmenge_series,
            "restmenge_products": line_products,
            "exotic_routed_products": exotic_routed_products,
        })

    return {
        "time_unit": time_unit,
        "horizon": round(sim_time_s / divisor, 4),
        "lines": lines_out,
    }


@app.get("/api/mixed/supermarket_state")
def mixed_supermarket_state(time_unit: str = "h", n_bins: Optional[int] = None):
    """Time-bucketed per-physical-row Supermarket series for the just-
    completed cached run — see _build_supermarket_state_payload's
    docstring. n_bins: default ~1 bin/real-world-15-minutes, same as
    kanban_events' build_line_units_timeseries/build_card_flow_payload."""
    if time_unit not in ("h", "min", "s"):
        raise HTTPException(400, "time_unit must be one of: h, min, s")
    kenv, cfg = _require_mixed_run()
    return _build_supermarket_state_payload(kenv, cfg, n_bins=n_bins, time_unit=time_unit)


def _active_kanban_products_by_line(kenv) -> dict[int, set[str]]:
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


def _build_plant_structure_payload(kenv, cfg: SimConfig, line_id: Optional[int] = None) -> dict:
    """
    STATIC per-line shape for the Movement Simulation's "Plant Layout"
    spatial view (movement_layout.js renderPlantLayout()) — physical
    Supermarket rows, Batch Collector product rows + trigger amounts, and
    the Chute's current frozen-zone size. This answers "what does the
    diagram look like", never "what's on it right now" — per-card
    occupancy over time is derived separately, client-side, from
    movement_trace.build_card_trace_payload's per-card transitions (see
    that function's docstring): a card's product_type + its
    in_supermarket / in_batch_collector / released_to_chute transitions
    are enough to place it into one of THIS payload's rows without any
    further backend endpoint, the same "first-fill by index, not real
    per-slot tracking" approximation _build_supermarket_state_payload
    above already uses for its own row-level series.

    Row/threshold sourcing:
      - supermarket rows: cfg.supermarkets[line_name], one entry per
        PHYSICAL row (Main runner AND Exotic — Exotic rows have no
        sachnummer of their own, labelled "Exotic"), same rows
        _build_supermarket_state_payload reads. Several rows can share a
        sachnummer (see that function's docstring); each is still listed
        separately here since the plant diagram draws one row per
        physical lane, not one per logical (line, product) group.
      - batch collector rows: one per product ELIGIBLE on this line per
        KanbanCardsSetup (cfg.kanban_cards[sachnr].eligible_lines — empty
        list = eligible everywhere, same fallback build_kanban_environment()
        uses) AND ACTUALLY ACTIVE there — i.e. at least one KanbanCard of
        that product has been created on this line (see
        _active_kanban_products_by_line()). Eligibility alone isn't
        enough: the setup sheet can list a product as eligible on a line
        that never actually withdraws/produces it, and drawing that as a
        permanently-empty row is just noise on the diagram, not signal.
        trigger_amount = cfg.kanban_cards[sachnr].cards_to_trigger.
        BatchCollectorResource itself has no fixed capacity (unbounded
        bucket until pop_batch() drains it at threshold — see that
        class's docstring), so `capacity` here is a RENDERING-ONLY hint,
        not a real sim constraint: trigger_amount plus a fixed slack of
        _BC_CAPACITY_SLACK slots, so movement_layout.js's renderPlantLayout()
        has room to draw a card or two past the trigger marker before a
        release empties the bucket (mirrors movement_simulation.html's
        own Step-1 placeholder heuristic, `Math.max(smSlots, bcTrigger +
        2)`, just sourced from real trigger amounts now instead of the
        UI's manual "Trigger amount" input).
      - chute.capacity: likewise a RENDERING-ONLY hint — KanbanChuteResource
        has no fixed size either (see its docstring: `_entries` is a plain
        list, admission is priority-ordered, not capacity-bounded).
        frozen_zone_cards plus a fixed slack of _CHUTE_CAPACITY_SLACK, so
        renderPlantLayout() always has at least that many movable slots
        drawn below the frozen zone. If the live pending queue
        (kenv.kanban_chutes[line_id].n_pending_batches, in ENTRIES not
        cards, and mixing pull batches with push chunks — see
        KanbanChuteResource's docstring) ever exceeds this, the extra
        cards simply won't have a drawn slot to sit in; callers rendering
        movement_state's per-frame chute.card_ids should treat a list
        longer than this capacity as overflow (same convention
        _build_movement_state_payload's Supermarket rows use).
      - chute.frozen_zone_cards: read live off
        kenv.kanban_chutes[line_id].frozen_zone_cards (falls back to 0 if
        no run/line yet) rather than cfg, since it's a runtime-editable
        value (PATCH /api/mixed/push_policy) that mixed_runner.py syncs
        onto EVERY line's KanbanChuteResource identically — see
        PushPolicyConfig.frozen_zone_cards and
        KanbanChuteResource.set_frozen_zone_cards. Reported per-line
        anyway (not just once) so the frontend never has to assume that
        uniformity holds.
    """
    line_id_by_name = {line.line_name: line.line_id for line in kenv.lines}
    active_products = _active_kanban_products_by_line(kenv)

    # Product -> trigger amount, per eligible line (mirrors
    # build_kanban_environment's own eligible_lines / empty-means-all
    # fallback exactly, so this payload's batch-collector rows always
    # match the products that line's BatchCollectorResource can actually
    # bucket) — then trimmed to products with real activity on that line
    # (see _active_kanban_products_by_line's docstring for why eligible
    # alone isn't enough).
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
        # sachnummer of their own and never carry pcs_partial — see
        # kanban_process_logic_v2.SupermarketSnapshot's docstring, which
        # only ever covers Main-runner (line, product) groups). Reserves
        # a segment/shape for the frontend to draw even before any run
        # data exists at this instant — the live amount comes from
        # movement_state's "restmenge" field (pcs_partial, replayed per
        # t_s), this is only the static "what products get a Restmenge
        # segment at all, and what's a full card's worth" shape. Same
        # batch_size fallback (200) kanban_process_logic_v2's
        # _make_kanban_withdrawal_batch uses when a product has no
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


@app.get("/api/mixed/plant_structure")
def mixed_plant_structure(line_id: Optional[int] = None):
    """Static per-line Plant Layout shape (Supermarket rows, Batch
    Collector product rows + trigger amounts, Chute frozen-zone size) —
    see _build_plant_structure_payload's docstring. Requires a prior
    POST /api/simulate_mixed, same as every other /api/mixed/* endpoint."""
    kenv, cfg = _require_mixed_run()
    return _build_plant_structure_payload(kenv, cfg, line_id=line_id)


def _pull_chute_events(kenv, lid: int) -> list[dict]:
    """
    True deposit/drain event history for pull cards passing through a
    line's Chute, derived straight from each KanbanCard's own
    .transitions log (kenv.card_registry) — there's no PushChuteLogEntry
    -style structure on the pull side, but transitions already record
    every state change with its raw sim-clock timestamp, so a
    "released_to_chute" entry IS the deposit event and the very next
    transition after it (whatever state it moves to) IS the drain
    event. A card that re-enters the Chute more than once (e.g. a
    rework loop) yields one deposit/drain pair per visit. A card still
    sitting in "released_to_chute" as of its LAST recorded transition
    (i.e. nothing after it yet) yields a deposit with no matching drain
    — correctly left "open" so it stays in the replayed queue below
    until a drain event for it actually appears in the log.

    Shape matches PushChuteLogEntry closely enough that
    _replay_chute_queue() can merge both sides' events and replay them
    against ONE persistent list, the same way push_chute_entries_at()
    already does for push-only.
    """
    events: list[dict] = []
    for card in getattr(kenv, "card_registry", {}).values():
        if getattr(card, "line_id", None) != lid:
            continue
        transitions = list(getattr(card, "transitions", []))
        for idx, (state, t) in enumerate(transitions):
            if state != "released_to_chute":
                continue
            events.append({
                "t": t, "kind": "deposit", "chute_kind": "pull",
                "id": card.card_id, "product_type": getattr(card, "product_type", None),
                "rush": False,  # rush is a push-only concept today
            })
            if idx + 1 < len(transitions):
                events.append({
                    "t": transitions[idx + 1][1], "kind": "drain",
                    "chute_kind": "pull", "id": card.card_id,
                })
    return events


def _push_chute_events(push_chute_log, line_name: str) -> list[dict]:
    """Push-side deposit/drain events for one line, reshaped from
    PushChuteLogEntry (mixed_runner.py) into the same dict shape
    _pull_chute_events() produces, so _replay_chute_queue() can treat
    both sides identically."""
    events: list[dict] = []
    for ev in push_chute_log or []:
        if ev.line != line_name:
            continue
        if ev.kind == "deposit":
            events.append({
                "t": ev.t, "kind": "deposit", "chute_kind": "push",
                "id": ev.entry_id, "product_type": ev.sachnummer, "rush": ev.rush,
            })
        elif ev.kind == "drain":
            events.append({
                "t": ev.t, "kind": "drain", "chute_kind": "push", "id": ev.entry_id,
            })
    return events


def _replay_chute_queue(events: list[dict], edge_s: float, frozen_zone_cards: int) -> list[dict]:
    """
    THE single physical Chute queue for a line, as of edge_s, built by
    replaying every deposit/drain event (pull AND push, merged) in true
    chronological order against one persistent list — index 0 = closest
    to production. This replaces computing the frozen prefix and the
    movable tail as two independent things (previously: frozen = oldest
    entries by raw arrival-time rank; tail = a separately-built
    rush-priority merge). That split was the actual bug: an older
    non-rush entry that a rush entry had already jumped ahead of (in the
    tail's own ordering) could still get selected directly into the
    frozen prefix by the independent time-rank check — since that check
    never looked at the tail's ordering at all — which visually showed
    up as the rush entry pinned at the frozen-zone line while entries it
    had already passed slipped underneath it into the frozen zone. Here
    there is only ONE ordering; an entry can reach "frozen" (queue[:n])
    only by genuinely being at the front of the exact same list the tail
    is read from, so nothing can leapfrog past a rush entry's already-
    earned position.

    Deposit rule (mirrors PushChuteTracker.deposit()'s live insertion
    rule, extended with the frozen-zone lock the tracker itself doesn't
    know about): a non-rush deposit appends to the back; a rush deposit
    inserts right after any rush entries already in the queue, ahead of
    every non-rush entry — capped so it can never land at an index below
    frozen_zone_cards, i.e. it can never displace an entry already
    inside the locked frozen prefix. Replaying real drain events (not
    just re-deriving from "who's currently present, sorted by arrival
    time") is what lets a rush entry's earned position survive frame to
    frame instead of being recomputed differently as unrelated older
    entries happen to drain out.

    A drain event removes its entry by (chute_kind, id) wherever it
    currently sits — normally at or near the front, but not asserted,
    consistent with this module's "flag, don't block" stance elsewhere.
    """
    ordered = sorted(
        (e for e in events if e["t"] <= edge_s),
        key=lambda e: (e["t"], e["kind"] != "deposit"),  # deposits before drains on an exact tie
    )
    queue: list[dict] = []
    for e in ordered:
        if e["kind"] == "deposit":
            item = {
                "kind": e["chute_kind"], "id": e["id"],
                "product_type": e.get("product_type"), "rush": e.get("rush", False),
            }
            if item["rush"]:
                # Only count rush entries still sitting in the TAIL
                # (index >= frozen_zone_cards) — not every rush entry
                # ever deposited. Once an earlier rush entry advances
                # into the frozen zone it's locked there; it's no longer
                # part of the movable rush cluster a later rush deposit
                # should queue behind. Counting it anyway (the previous
                # bug) overstates how far into the queue the new rush
                # entry belongs — enough to push it past frozen_zone_cards
                # + len(tail), i.e. clamped by min() to the very back of
                # the queue, landing it BEHIND non-rush tail entries it
                # should have jumped ahead of.
                tail_rush_count = sum(1 for q in queue[frozen_zone_cards:] if q["rush"])
                insert_idx = min(frozen_zone_cards + tail_rush_count, len(queue))
                queue.insert(insert_idx, item)
            else:
                queue.append(item)
        else:  # drain
            key = (e["chute_kind"], e["id"])
            queue = [q for q in queue if (q["kind"], q["id"]) != key]
    return queue


def _build_movement_state_payload(
    kenv, cfg: SimConfig, t_s: float, line_id: Optional[int] = None, include_push: bool = False,
) -> list[dict]:
    """
    Per-card occupancy snapshot at raw sim-clock time t_s, resolved down to
    physical Supermarket row/slot, Batch Collector bucket position,
    Collection Box queue position, and Kanban Chute queue position — the
    config-aware step movement_trace.build_movement_frame_cards()
    deliberately leaves undone (see that function's docstring). This is
    the third sibling to _build_plant_structure_payload (STATIC row/bucket
    shape) and _build_supermarket_state_payload (per-row/bucket COUNTS
    over time): same rows, same first-fill-by-index convention, but
    resolved to actual card_ids so the Movement Simulation page can
    animate individual cards sliding into slots instead of just filling a
    bar.

    include_push: for the "All Production" movement view (both push and
    pull traffic, not just Kanban cards). Push-produced pieces never get
    a KanbanCard (see kpi_by_class docstring), so they can't be resolved
    to card_ids the way Kanban cards are — instead, when True:
      - Exotic Supermarket rows are ADDED to supermarket_rows (normally
        omitted entirely — see the Approximations note below), each with
        "is_exotic": true and a "products" list — [{"sachnummer",
        "push_count"}, ...], one entry per product currently occupying
        the row at t_s (see _exotic_products_at's docstring: a row that
        hasn't fully emptied before a different product starts filling
        it legitimately holds more than one product at once). For
        back-compat with any consumer still reading the old single-value
        shape, "sachnummer" (the first product, or None if the row is
        empty) and "push_count" (the SUM across all products) are still
        included alongside "products". Sourced from
        kenv.exotic_snapshot_log at t_s via the same "last reading at or
        before" convention _build_supermarket_state_payload's per-bin
        series use. This IS real time-indexed data (one snapshot per push
        deposit), unlike the chute figure below — though see
        _exotic_products_at's docstring for the current limit on how many
        simultaneous products it can actually distinguish, which depends
        on what mixed_runner.py's snapshot log records.
      - chute now interleaves real push entries into "entries" alongside
        pull cards, instead of a single anonymous "push_pending" count.
        Sourced via push_chute_entries_at(kenv.push_chute_log, ...) — a
        replay of every push-side Chute deposit/drain event up to t_s
        (see that function's docstring), so — unlike the old live-only
        estimate — this IS real time-indexed data: an earlier t_s
        correctly shows the push queue as it stood then, not the run's
        current state repeated on every frame. Each entry in "entries"
        looks like {"kind": "pull"|"push", "id", "product_type",
        "frozen", "rush"}; a "push" entry's "id" is a synthetic per-chunk
        counter (PushChuteTracker's own bookkeeping id), not a real
        KanbanCard id — there's no individual-piece id for a push chunk
        (see kpi_by_class's docstring). False (default) omits both
        additions entirely, keeping the existing Kanban-only Push/Pull
        tabs' payload shape unchanged.

    Approximations (documented once here rather than per-field, since
    they all stem from the same root cause: the sim doesn't track
    per-physical-slot occupancy directly, only aggregate counts + each
    card's own transition history, or — for push chute entries — a
    parallel bookkeeping structure alongside the real KanbanChuteResource
    queue rather than the queue itself):
      - Exotic Supermarket rows hold PUSH-produced pieces, which never get
        a KanbanCard at all — no card_ids are ever placed into an "Exotic"
        row here (they're simply absent from supermarket_rows' output for
        that line) UNLESS include_push=True, per above. Keep using
        /api/mixed/supermarket_state's per-row occupant/n_cards series for
        Exotic rows when finer-grained history (not just this one instant)
        is needed.
      - Chute "entries" ordering merges pull cards' "released_to_chute"
        transition time with push chunks' PushChuteTracker deposit time
        (see push_chute_entries_at's docstring), then replays them
        oldest-to-newest to build the physical queue: each non-rush
        entry appends to the back, each rush entry (a push chunk
        force-placed via chute.push_rush_entry()) inserts at the current
        front of the movable tail — capped so it can never land ahead of
        an entry already at index < frozen_zone_cards. That cap is what
        keeps the frozen zone (index < frozen_zone_cards) locked and
        un-rearranged while still letting a rush entry's position
        genuinely advance toward it over time, rather than picking the
        frozen subset by raw arrival time (which pins a rush entry near
        the back forever, since its own deposit timestamp is always
        recent — the "stuck at the line" bug this replay fixes). Each
        entry carries a "rush" flag (currently always False for pull,
        since rush is a push-only concept today) so the frontend can
        flag it visually. PushChuteTracker — like ExoticSupermarketTracker
        — is a bookkeeping structure fed from mixed_runner.py's own call
        sites, not wired into KanbanChuteResource's actual internal queue
        (which also re-sorts its movable tail by H/M/L priority — see
        KanbanChuteResource._insert_ranked). Close enough for "roughly
        where is this entry in the queue, and is it jumping ahead"
        animation, not an exact replay of pop_next()'s order.
      - frozen_zone_cards uses the run's CURRENT (live) value off
        kenv.kanban_chutes[line_id] — historical frozen-zone size isn't
        logged per point in time, so a t_s from earlier in the run still
        reports whatever the frozen zone is set to NOW (usually fine,
        since PATCH /api/mixed/push_policy is the only thing that changes
        it and is rare mid-analysis).
      - Cards in "withdrawn" (between Supermarket and Collection Box) or
        "in_production" (between Chute release and the next
        "in_supermarket" transition) don't correspond to any Step-1 box;
        they come back under "in_transit" so the frontend can animate them
        moving between boxes / along the production rectangle rather than
        silently dropping them.

    production_status: which single sachnummer (if any) the line's shared
    LinePriorityGate was actually occupied by at t_s, resolved from
    kenv.gate_activity_log via mixed_runner.production_status_at() — see
    that function's docstring. Since class-1 and class-2 always go
    through the SAME gate (capacity=1 per line), this is authoritative
    for "what's really in the Production rectangle right now", unlike
    "in_transit"'s "in_production" entries above: those are per-card/
    per-chunk bookkeeping states (a card sits at "in_production" for its
    whole batch's duration, a push chunk gets no such state at all today
    — see this function's own module-level TODO in movement_trace.py) and
    can therefore show more than one card/chunk "in production" at once
    even though only ONE of them is physically on the line at t_s. A
    caller drawing a single-occupant Production rectangle should prefer
    production_status over filtering in_transit by state=="in_production".
    "possible_changeover" flags that a changeover was very likely run
    somewhere inside this hold (previous sachnummer on this line
    differed) — it does NOT mean the whole interval was Rüstzeit; see
    GateActivityEntry's docstring for why that finer split isn't
    resolvable yet. state="idle" means the gate was free at t_s (nothing
    queued/running for either class at that instant) AND the line is
    on-shift; state="off_shift" (v6) means the gate was ALSO free, but
    this line simply isn't scheduled to run at t_s at all, per the
    workbook's "Shifts" sheet (cfg.shift_calendar) — see
    production_status_at()'s own docstring for why this is never allowed
    to override "producing" (a unit already running when its shift ends
    keeps running; the gate hold is authoritative for its own duration).
    Only ever distinguished from "idle" when the loaded workbook actually
    has shift data (cfg.shift_calendar.shifts non-empty) — a workbook
    with no "Shifts" sheet at all only ever reports "producing"/"idle",
    unchanged from before v6.

    production_occupants: the actual token(s) to draw INSIDE the
    Production rectangle at t_s, derived from production_status —
    {"card_id", "product_type", "kind": "pull"|"push"} per entry, empty
    when production_status.state == "idle". For a pull batch this can be
    more than one entry (every card released together as part of the
    same batch legitimately runs concurrently), filtered down to cards
    whose product matches production_status's own sachnummer so a
    same-product batch still queued behind the running one isn't drawn
    as if it were already on the line (see the caveat in this function's
    body for the one remaining edge case that can't be filtered out this
    way). For a push chunk there is no real per-piece id (push chunks
    never get a KanbanCard), so a single synthetic id is generated from
    the gate hold's own start time. "in_transit" above no longer includes
    "in_production" at all — production_occupants replaces it as the
    single source of truth for the Production rectangle.

    restmenge: per line, one entry per Main-runner product this line's
    Supermarket stocks — {"sachnummer", "pcs_partial", "batch_size"} —
    the loose, not-yet-a-whole-card PIECE count from kanban_process_
    logic_v2.SupermarketSnapshot.pcs_partial, replayed the same
    "last reading at or before t_s" way as every other snapshot field
    here. This is genuinely separate from production_occupants above: a
    card can still be the current production_occupant (its batch hasn't
    finished) while pieces it already contributed are already counted
    here as Restmenge stock in the Supermarket — draw them as two
    different things, not one "still being produced" blob.

    Batch Collector and Chute rows/queues both carry a "capacity" field —
    the same rendering-only _BC_CAPACITY_SLACK / _CHUTE_CAPACITY_SLACK
    hint _build_plant_structure_payload's STATIC shape already reports
    (see that function's docstring for why neither resource has a real
    fixed size). "card_ids" is truncated to that capacity, in the same
    oldest-first order build_movement_frame_cards() establishes; anything
    past it comes back under "overflow_card_ids" (None if nothing
    overflowed) rather than being silently dropped — the frontend can
    choose to pile overflow cards visually outside the box, badge a
    count, or just log it.
    """
    frame = build_movement_frame_cards(kenv, t_s, line_id=line_id)

    # product -> trigger amount, per eligible line — identical fallback
    # rule to build_kanban_environment() / _build_plant_structure_payload,
    # then trimmed to products with real activity on that line (same
    # _active_kanban_products_by_line() filter plant_structure applies,
    # so the two payloads' Batch Collector rows always match).
    active_products = _active_kanban_products_by_line(kenv)
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
        line_name = line.line_name

        # ---- Supermarket: Main-runner rows only (Exotic excluded, see docstring) ----
        main_runner_groups: dict[str, list] = {}
        for slot in (cfg.supermarkets or {}).get(line_name, []):
            if slot.is_exotic or not slot.sachnummer:
                continue
            main_runner_groups.setdefault(slot.sachnummer, []).append(slot)
        for rows in main_runner_groups.values():
            rows.sort(key=lambda s: s.row_number)

        sm_rows_out = []
        for sachnr, rows in sorted(main_runner_groups.items()):
            cards = frame.get((lid, "in_supermarket", sachnr), [])  # oldest-first
            cursor = 0
            for slot in rows:
                take = cards[cursor: cursor + slot.capacity]
                cursor += slot.capacity
                sm_rows_out.append({
                    "row_number": slot.row_number,
                    "sachnummer": sachnr,
                    "capacity": slot.capacity,
                    "card_ids": [c["card_id"] for c in take],
                })
            leftover = cards[cursor:]
            if leftover:
                # More cards than the group's summed physical capacity —
                # shouldn't happen if capacity enforcement upstream is
                # working, but surfaced explicitly rather than silently
                # truncated, same spirit as build_kanban_environment's own
                # "exceeding capacity — clipping" warning at seed time.
                sm_rows_out.append({
                    "row_number": None,
                    "sachnummer": sachnr,
                    "capacity": 0,
                    "card_ids": [c["card_id"] for c in leftover],
                    "overflow": True,
                })

        # ---- Restmenge: loose, not-yet-a-whole-card pieces per product ----
        # This is SupermarketSnapshot.pcs_partial (kanban_process_logic_v2.py)
        # — real PIECES already produced and sitting in the Supermarket,
        # not yet enough of them to recycle a whole KanbanCard (see
        # production_trigger_process's on_finish closure: deposit_finished_
        # pcs() ticks pcs_partial on every passed part, independent of
        # whether that part's own card has finished its batch yet — a
        # card can still legitimately read "in_production"/be the current
        # production_status occupant while pieces it already contributed
        # show up here). Same "last reading at or before t_s" replay as
        # every other snapshot-log field in this module, grouped by
        # (line_name, sachnummer) same as _build_supermarket_state_payload's
        # main_groups. Reported once per product this line's Supermarket
        # actually stocks (main_runner_groups, computed above) — a product
        # with pcs_partial==0 at t_s still gets an entry (amount 0), so a
        # frontend segment doesn't have to guess whether a product exists
        # here at all.
        restmenge_out = []
        for sachnr in sorted(main_runner_groups):
            snaps = [
                s for s in (getattr(kenv, "snapshot_log", None) or [])
                if s.line_name == line_name and s.product_type == sachnr
            ]
            snaps.sort(key=lambda s: s.t)
            _, pcs_partial = _sm_state_at(snaps, t_s, ("n_available", "pcs_partial"))
            batch_size = snaps[-1].batch_size if snaps else None
            restmenge_out.append({
                "sachnummer": sachnr,
                "pcs_partial": pcs_partial,
                "batch_size": batch_size,
            })

        if include_push:
            # ---- Exotic rows: push-produced pieces, no card_id — see docstring ----
            exotic_snaps: dict[int, list] = {}
            for snap in (getattr(kenv, "exotic_snapshot_log", None) or []):
                if snap.line != line_name:
                    continue
                exotic_snaps.setdefault(snap.row_number, []).append(snap)
            for snaps in exotic_snaps.values():
                snaps.sort(key=lambda s: s.t)
            for slot in sorted((cfg.supermarkets or {}).get(line_name, []), key=lambda s: s.row_number):
                if not slot.is_exotic:
                    continue
                products = _exotic_products_at(exotic_snaps.get(slot.row_number, []), t_s)
                sm_rows_out.append({
                    "row_number": slot.row_number,
                    # Back-compat single-value fields (first product / total
                    # count) — new consumers should read "products" instead,
                    # which carries every product currently in the row.
                    "sachnummer": products[0]["sachnummer"] if products else None,
                    "push_count": sum(p["push_count"] for p in products),
                    "products": products,
                    "capacity": slot.capacity,
                    "card_ids": [],
                    "is_exotic": True,
                })

        # ---- Batch Collector: one bucket per eligible product ----
        # capacity here is the SAME rendering-only trigger_amount +
        # _BC_CAPACITY_SLACK hint _build_plant_structure_payload uses for
        # the static shape — kept in sync so a card list longer than what
        # the static layout drew slots for is flagged, not silently lost.
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

        # ---- Collection Box: merge every product's group for this line (one physical box, not per-product) ----
        cb_cards = [
            {**c, "product_type": p}
            for (l, state, p), items in frame.items()
            if l == lid and state == "in_collection_box"
            for c in items
        ]
        cb_cards.sort(key=lambda c: (c["since_t"], c["card_id"]))

        # ---- Kanban Chute — pull cards + push chunks, merged into one FIFO ----
        # (see docstring: both classes share ONE physical
        # KanbanChuteResource queue, so drawing them separately — pull
        # cards as real tokens, push chunks as an anonymous "+N" badge —
        # both under-counted the badge (a live snapshot, same value every
        # animation frame) and hid which product each push entry actually
        # was. `entries` merges both sides, oldest-first by the time each
        # actually entered the chute, each carrying its own product_type
        # so the frontend can color them the same way Supermarket/Batch
        # Collector rows already are.
        chute = kenv.kanban_chutes.get(lid) if getattr(kenv, "kanban_chutes", None) else None
        frozen_zone_cards = getattr(chute, "frozen_zone_cards", 0) if chute is not None else 0
        chute_capacity = frozen_zone_cards + _CHUTE_CAPACITY_SLACK  # matches plant_structure's chute.capacity

        # Single unified queue replay (see _replay_chute_queue's
        # docstring for why this replaced the old "frozen picked by
        # independent raw-arrival-time rank, tail built by a separate
        # rush-priority merge" approach — that split was the actual
        # source of the "rush stuck at the frozen-zone line while other
        # entries slide underneath it" bug). Pull events always feed in
        # (the Kanban-only tabs still need a plain FIFO chute); push
        # events only when include_push is set.
        chute_events = _pull_chute_events(kenv, lid)
        if include_push:
            chute_events += _push_chute_events(getattr(kenv, "push_chute_log", None), line_name)

        queue = _replay_chute_queue(chute_events, t_s, frozen_zone_cards)
        entries_raw = [
            {
                "kind": item["kind"], "id": item["id"], "product_type": item["product_type"],
                "rush": item["rush"], "frozen": i < frozen_zone_cards,
            }
            for i, item in enumerate(queue)
        ]

        chute_entries = entries_raw[:chute_capacity]
        overflow_entries = entries_raw[chute_capacity:] or None

        # ---- Production status: what the line's gate is doing right now ----
        # Time-indexed (via GateActivityEntry replay), NOT the live-only
        # kenv.gates[lid].current_rec the /simulate_mixed gate_status field
        # uses — see production_status_at()'s docstring for the
        # "producing" vs "idle" vs "off_shift" resolution and the known
        # Rüstzeit/changeover caveat. shift_calendar/line_name/epoch are
        # passed so an off-shift line is reported distinctly from a
        # genuinely idle one (v6) — getattr'd defensively (like
        # gate_activity_log above) so this keeps working unchanged
        # against a kenv from a run that predates push_ctx/shift_calendar
        # ever being set; production_status_at() itself already falls
        # back to the old two-state behaviour whenever any of the three
        # is missing or the workbook had no "Shifts" sheet at all.
        gate_log = getattr(kenv, "gate_activity_log", None) or []
        push_ctx = getattr(kenv, "push_ctx", None)
        production_status = production_status_at(
            gate_log, lid, t_s,
            shift_calendar=cfg.shift_calendar,
            line_name=line_name,
            epoch=getattr(push_ctx, "epoch", None),
        )

        # ---- Production occupant(s): the actual token(s) to draw in the ----
        # ---- Production rectangle right now (one line = one job at a time) ----
        # Resolved from production_status, NOT from frame's raw
        # "in_production" bucket (see below) — a KanbanCard is marked
        # in_production for its WHOLE released batch, starting the
        # instant production_trigger_process pops it off the chute, which
        # can be before the batch has actually secured the line's gate if
        # something else is still running. Filtering frame's
        # "in_production" cards down to production_status's own
        # sachnummer still isn't perfectly precise (a same-product batch
        # queued behind the running one would also match), but it's a
        # real improvement over drawing every "in_production" card
        # regardless of whether it's actually on the line yet.
        production_occupants: list[dict] = []
        if production_status["state"] == "producing":
            if production_status["sim_class"] == "pull":
                production_occupants = [
                    {"card_id": c["card_id"], "product_type": production_status["sachnummer"], "kind": "pull"}
                    for c in frame.get((lid, "in_production", production_status["sachnummer"]), [])
                ]
            elif production_status["sim_class"] == "push":
                # Push chunks never get a KanbanCard (see kpi_by_class's
                # docstring in this module) — no real per-piece id exists,
                # same situation as a push chute entry. Synthesize one
                # deterministic id from the gate hold's own start time so
                # repeated calls for the same instant return the same id.
                production_occupants = [{
                    "card_id": f"push-{lid}-{production_status['since_t']:.3f}",
                    "product_type": production_status["sachnummer"],
                    "kind": "push",
                }]

        # ---- In-transit: states with no Step-1 box of their own ----
        # "in_production" is deliberately EXCLUDED here now — see
        # production_occupants above, which is the authoritative "what's
        # really in the Production rectangle" source. Keeping both would
        # let a frontend double-draw the same card once from each list.
        in_transit = [
            {"card_id": c["card_id"], "state": state, "product_type": product}
            for (l, state, product), items in frame.items()
            if l == lid and state == "withdrawn"
            for c in items
        ]

        lines_out.append({
            "line_id": lid,
            "line_name": line_name,
            "supermarket_rows": sm_rows_out,
            "restmenge": restmenge_out,
            "batch_collector_rows": bc_rows_out,
            "collection_box": {"card_ids": [c["card_id"] for c in cb_cards]},
            "production_status": production_status,
            "production_occupants": production_occupants,
            "chute": {
                "capacity": chute_capacity,
                "frozen_zone_cards": frozen_zone_cards,
                # New: merged pull+push queue, oldest-first, each entry
                # carrying its own product_type — this is what
                # movement_layout.js should render now.
                "entries": chute_entries,
                "overflow_entries": overflow_entries,
                # Legacy pull-only fields, kept for back-compat with any
                # other consumer still reading them directly — derived
                # from the SAME unified queue "entries" above (just
                # filtered to kind=="pull"), not recomputed separately,
                # so they can't drift out of sync with it.
                "card_ids": [e["id"] for e in entries_raw if e["kind"] == "pull"][:chute_capacity],
                "overflow_card_ids": (
                    [e["id"] for e in entries_raw if e["kind"] == "pull"][chute_capacity:] or None
                ),
                "frozen_card_ids": [e["id"] for e in entries_raw if e["kind"] == "pull" and e["frozen"]],
            },
            "in_transit": in_transit,
        })

    return lines_out


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
    Kanban Chute queue — see _build_movement_state_payload's docstring for
    the per-box assignment rules and their approximations.

    This is the endpoint that closes the "time-indexed way to say 'at time
    t, these N cards are sitting in this component/row'" gap:
    /api/mixed/card_trace gives one card's own transition history;
    /api/mixed/plant_structure gives the static row/bucket shape; this
    combines both (for EVERY card at once, at each requested instant) so
    the frontend doesn't need to replay every card's transitions itself.

    include_push: for the "All Production" tab — adds push-side presence
    (Exotic Supermarket rows' "products" list, Chute's merged "entries")
    alongside the normal Kanban card_ids; see
    _build_movement_state_payload's docstring for exactly what each
    added field means — both are now real time-indexed reconstructions
    (via _exotic_products_at / push_chute_entries_at), not live-only
    estimates.

    Frame spacing — two ways to control it, n_frames wins if given:
      - interval_min (default 5.0): frame count is DERIVED as
        window_seconds / (interval_min * 60), so cards get a fresh
        snapshot every 5 real-world minutes by default (previously a
        fixed n_frames=24 meant ~hourly steps for a day-length window —
        too coarse to see a card actually move). Clamped to
        [1, _MAX_MOVEMENT_FRAMES].
      - n_frames: explicit override (e.g. for a caller that wants exactly
        N evenly-spaced frames regardless of window length) — when set,
        interval_min is ignored. Must be between 1 and
        _MAX_MOVEMENT_FRAMES.
    Each frame re-scans the ENTIRE kenv.card_registry, so both knobs are
    capped — narrow the window via day_index/hour first if you need finer
    resolution than the cap allows over a wide window.
    """
    if time_unit not in ("h", "min", "s"):
        raise HTTPException(status_code=400, detail="time_unit must be one of: h, min, s")
    kenv, cfg = _require_mixed_run()

    window = _day_window_s(day_index, hour)
    start_s, end_s = window if window is not None else (0.0, kenv.env.now)
    window_s = max(0.0, end_s - start_s)

    if n_frames is not None:
        if not (1 <= n_frames <= _MAX_MOVEMENT_FRAMES):
            raise HTTPException(status_code=400, detail=f"n_frames must be between 1 and {_MAX_MOVEMENT_FRAMES}")
        resolved_frames = n_frames
    else:
        if interval_min is None or interval_min <= 0:
            raise HTTPException(status_code=400, detail="interval_min must be > 0 (or pass n_frames instead)")
        resolved_frames = max(1, min(_MAX_MOVEMENT_FRAMES, round(window_s / (interval_min * 60.0)) or 1))

    divisor = _SM_DIVISORS[time_unit]
    edges = [start_s + window_s * i / resolved_frames for i in range(resolved_frames + 1)]
    frames = [
        {
            "t": round(edge_s / divisor, 4),
            "lines": _build_movement_state_payload(kenv, cfg, edge_s, line_id=line_id, include_push=include_push),
        }
        for edge_s in edges
    ]
    return {"time_unit": time_unit, "day_start_hour": _LAST_MIXED_DAY_START_HOUR, "frames": frames}


# ---------------------------------------------------------------------------
# /api/simulate_mixed
# ---------------------------------------------------------------------------

class SimulateMixedRequest(BaseModel):
    n_workers: int = Field(default=DEFAULT_N_WORKERS, ge=1, le=2)
    n_crews: int = Field(
        default=2, ge=1,
        description="How many crew_process() workers share the plant's "
                     "lines (v10 crew-based depletion model — see "
                     "mixed_runner.crew_process). A crew holds at most "
                     "one line at a time, so more crews than lines just "
                     "leaves some crews idle; see /api/parameters' "
                     "crew_options for a sane upper bound.",
    )
    seed: int = 42
    day_start_hour: int = Field(default=DAY_START_HOUR, ge=0, le=23)
    day_length_s: float = DAY_LENGTH_S
    drain_days: int = Field(default=DRAIN_DAYS, ge=0, le=7)
    push_chunk_size: int = Field(
        default=PUSH_CHUNK_SIZE, ge=1,
        description="Max pieces per push OrderRecord slice before class-1 "
                     "gets another chance to jump the gate queue.",
    )
    horizon_s: Optional[float] = Field(
        default=None,
        description="Override the simulation horizon. Default: "
                     "run_mixed()'s own estimate (max of kanban's "
                     "estimate_horizon_s() and last push date + "
                     "drain_days).",
    )
    time_unit: str = "h"  # for gantt / card_flow / line_units_series: "h" | "min" | "s"


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
    # v6: shift_calendar/epoch split idle gaps into "idle" vs "off_shift"
    # sub-segments (schedule_events._split_gap_by_shift) — the Gantt
    # counterpart to production_status_at()'s off_shift state used below
    # by gate_status/movement_state. Read defensively via getattr so this
    # keeps working unchanged against a kenv from a run that predates
    # push_ctx being set; build_gantt_payload() itself already falls back
    # to plain "idle" segments whenever epoch is missing or the workbook
    # has no "Shifts" sheet at all (bool(cfg.shift_calendar.shifts)).
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

    # v10: per-crew production summary — see _build_kpi_by_crew()'s
    # docstring. Read defensively (the helper itself already tolerates a
    # kenv without gate_activity_log/n_crews) so this can't 500 an
    # otherwise-successful run.
    kpi_by_crew = _build_kpi_by_crew(kenv)

    # --- Class-1-only diagnostics (see module docstring) ---------------
    # shortfall_log: mirrors the snapshot_log/event_log convention this
    # server already relies on — mixed_runner.py needs to pass a
    # shortfall_log list into kanban_process_logic.start_kanban_simulation
    # (or however it launches the pull side) and mirror it onto
    # kenv.shortfall_log the same way it already does for
    # kenv.snapshot_log / kenv.event_log. Read defensively via getattr so
    # this endpoint doesn't 500 before mixed_runner.py is updated — the
    # Shortfall chart just comes back all-zero (empty) until then.
    shortfall_log = getattr(kenv, "shortfall_log", None)
    card_flow = build_card_flow_payload(
        kenv, time_unit=req.time_unit, shortfall_log=shortfall_log,
    )
    line_units_series = build_line_units_timeseries(
        kenv.snapshot_log, kenv.env.now, time_unit=req.time_unit,
    )
    restmenge = print_restmenge_report(kenv, verbose=False)
    restmenge_json = [
        {"line": line_name, "product": product_type, "pcs_partial": pcs_partial}
        for (line_name, product_type), pcs_partial in restmenge.items()
    ]

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

    # v10: which crew most recently held each line's gate — the latest
    # (by t_end) gate_activity_log entry for that line_id. Same "last
    # known at the instant the run stopped, not literally live" caveat as
    # current_rec below (the run has already finished by the time this
    # response is built).
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
            # v6: is this line on-shift right NOW (i.e. at kenv.env.now,
            # the instant the run stopped) — a live-snapshot counterpart
            # to production_status_at()'s time-indexed "off_shift" state
            # used by /api/mixed/movement_state. Via kenv.is_line_on()
            # (SimEnvironment.is_line_on(), entities_resources_v5.py) —
            # the same query surface PushSchedulerContext.is_line_on() and
            # _gated_run_one_kanban_batch already wrap/call directly in
            # mixed_runner.py, which already bakes in the "no Shifts sheet
            # at all -> every line always on" fallback, so nothing here
            # needs to re-derive that from cfg.shift_calendar itself. True
            # unconditionally whenever no shared epoch is available at all
            # (a kenv from a run that predates push_ctx being set) — same
            # "can't evaluate -> don't claim it's off" fallback
            # production_status_at() itself uses.
            "is_on_shift": (
                kenv.is_line_on(line.line_name, _now_dt_for_status)
                if _now_dt_for_status is not None else True
            ),
        }
        for line in kenv.lines
    }

    # --- Push-specific artifacts (Phase A) ------------------------------
    # Straight pass-throughs of what run_mixed() already attaches to kenv
    # (see module docstring) — read defensively via getattr so this
    # endpoint degrades gracefully rather than 500ing if mixed_runner.py
    # ever falls behind this server again, matching the shortfall_log
    # convention already used above.
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

    global _LAST_MIXED_KENV, _LAST_MIXED_CFG, _LAST_MIXED_DAY_START_HOUR, _LAST_MIXED_DAY_LENGTH_S
    _LAST_MIXED_KENV = kenv
    _LAST_MIXED_CFG = cfg
    _LAST_MIXED_DAY_START_HOUR = req.day_start_hour
    _LAST_MIXED_DAY_LENGTH_S = req.day_length_s

    return {
        "gantt": gantt,
        "kpi_by_line": kpi_by_line,
        "kpi_by_class": kpi_by_class,
        "kpi_by_crew": kpi_by_crew,
        "card_flow": card_flow,
        "line_units_series": line_units_series,
        "restmenge": restmenge_json,
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


# ---------------------------------------------------------------------------
# Movement-simulation endpoints (movement_simulation.html / movement_layout.js)
# ---------------------------------------------------------------------------
# A mixed run keeps BOTH classes on one continuous kenv/clock (see module
# docstring), so there is no separate "push server" / "kanban server" to
# point the movement page at like there is for api_server.py. Instead:
#   - "mode" (push vs kanban) on the movement page maps to a `cls` filter
#     here (pull=kanban products, push=push products, all=no filter),
#     reusing _build_class_product_sets() — the same split already used
#     for kpi_by_class above.
#   - card endpoints are naturally kanban-only regardless of `cls`,
#     because kenv.card_registry is only ever populated for class-1 parts
#     (see kpi_by_class docstring) — passing cls='push' to a card endpoint
#     will just come back empty, not an error.
#   - windowing uses day_index (0-based from sim start) + optional hour
#     (0-23), NOT a calendar "date" string like api_server.py's kanban
#     endpoints — mixed_runner.py runs one continuous clock rather than
#     stitching per-day push runs, so there is no per-run calendar-date
#     table to look up here. The frontend must be updated to send
#     day_index/hour for ALL modes when pointed at these routes (today it
#     sends day_index/hour for push but a "date" string for kanban).
#   - day_start_hour/day_length_s used for windowing come from the CACHED
#     request (_LAST_MIXED_DAY_START_HOUR/_LAST_MIXED_DAY_LENGTH_S), i.e.
#     whatever was actually passed to the /api/simulate_mixed call that
#     populated _LAST_MIXED_KENV — not mixed_runner.py's bare defaults.
# ---------------------------------------------------------------------------

def _require_mixed_run() -> tuple:
    """Every endpoint below needs a prior POST /api/simulate_mixed call."""
    if _LAST_MIXED_KENV is None or _LAST_MIXED_CFG is None:
        raise HTTPException(
            status_code=409,
            detail="No mixed simulation has been run yet. Call POST /api/simulate_mixed first.",
        )
    return _LAST_MIXED_KENV, _LAST_MIXED_CFG


def _day_window_s(day_index: Optional[int], hour: Optional[int]) -> Optional[tuple[float, float]]:
    """
    Raw sim-clock (start_s, end_s) window for the CACHED mixed run, in
    the same single continuous clock run_mixed() itself uses (no
    per-day offset stitching, unlike the push-only multiday runner —
    see movement_trace.build_part_trace_payload's day_window_s /
    time_offset_s docstring for that other convention).

    day_index: 0-based day number from sim start (t=0). None -> no
    windowing (whole run).
    hour: optional 0-23, narrows the window to that single hour within
    day_index. Required by the frontend today whenever day_index is
    given, to keep single-hour part lists from being enormous.

    NOTE: assumes day length is constant across the cached run, which
    holds for the current mixed_runner.py (drain days included in the
    day count, not a different length).
    """
    if day_index is None:
        return None
    day_len = _LAST_MIXED_DAY_LENGTH_S or DAY_LENGTH_S
    start = day_index * day_len
    if hour is not None:
        start += hour * 3600.0
        end = start + 3600.0
    else:
        end = day_index * day_len + day_len
    return (start, end)


def _class_filter_products(cfg: SimConfig, cls: Optional[str]) -> Optional[set[str]]:
    """
    cls: 'pull' | 'push' | None/'all'. Returns the product_type set to
    KEEP, or None for no filtering. Reuses _build_class_product_sets()
    — same kanban-withdrawal-sheet / demand-sheet derivation already
    used for kpi_by_class above, so "pull"/"push" here means exactly
    what it means there.
    """
    if cls is None or cls == "all":
        return None
    kanban_products, push_products = _build_class_product_sets(cfg)
    if cls == "pull":
        return kanban_products
    if cls == "push":
        return push_products
    raise HTTPException(status_code=400, detail="cls must be one of: pull, push, all")


@app.get("/api/mixed/part_ids")
def mixed_part_ids(line_id: Optional[int] = None, cls: Optional[str] = None):
    """Cheap piece dropdown, not scoped to a day/hour (see build_part_ids_payload)."""
    kenv, cfg = _require_mixed_run()
    payload = build_part_ids_payload(kenv, line_id=line_id)
    keep = _class_filter_products(cfg, cls)
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
    kenv, cfg = _require_mixed_run()
    if time_unit not in ("h", "min", "s"):
        raise HTTPException(status_code=400, detail="time_unit must be one of: h, min, s")
    payload = build_part_trace_payload(
        kenv,
        time_unit=time_unit,
        include_wip=include_wip,
        line_id=line_id,
        day_window_s=_day_window_s(day_index, hour),
        time_offset_s=0.0,  # already one continuous clock, see module note above
    )
    keep = _class_filter_products(cfg, cls)
    if keep is not None:
        payload["parts"] = [p for p in payload["parts"] if p["product_type"] in keep]
    payload["day_start_hour"] = _LAST_MIXED_DAY_START_HOUR
    return payload


@app.get("/api/mixed/card_ids")
def mixed_card_ids(
    line_id: Optional[int] = None,
    day_index: Optional[int] = None,
    hour: Optional[int] = None,
):
    """Cheap card dropdown for one day/hour window — cards are always class-1 (kanban) only."""
    kenv, _cfg = _require_mixed_run()
    payload = build_card_trace_payload(
        kenv,
        time_unit="s",
        line_id=line_id,
        day_window_s=_day_window_s(day_index, hour),
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
    kenv, _cfg = _require_mixed_run()
    if time_unit not in ("h", "min", "s"):
        raise HTTPException(status_code=400, detail="time_unit must be one of: h, min, s")
    payload = build_card_trace_payload(
        kenv,
        time_unit=time_unit,
        card_id=card_id,
        line_id=line_id,
        day_window_s=_day_window_s(day_index, hour),
        time_offset_s=0.0,
    )
    payload["day_start_hour"] = _LAST_MIXED_DAY_START_HOUR
    return payload


# ---------------------------------------------------------------------------
# Crew activity (v10) — "production of crew 1", "production of crew 2",
# the crew-based counterpart to filtering by line_id elsewhere on this
# page. Built straight from kenv.gate_activity_log, which now carries
# crew_id on every entry (see mixed_runner.GateActivityEntry / crew_process).
# ---------------------------------------------------------------------------

_TIME_DIVISORS: dict[str, float] = {"h": 3600.0, "min": 60.0, "s": 1.0}


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
    one line — "show me what crew 1 produced" / "show me what crew 2
    produced on HTL5", mirroring how the rest of this page already lets
    the frontend scope a view to one line via `line_id`.

    Each entry is one completed unit of production (one pull card, or
    one push chunk) — the same granularity kenv.gate_activity_log already
    records, just filtered and time-unit-converted here rather than
    exposed raw. Fields:
        crew_id, line_id, line_name, sachnummer, sim_class ("pull"|"push"),
        quantity, possible_changeover, t_start, t_end   (t_start/t_end in
        the requested time_unit, matching the Gantt/KPI endpoints'
        own convention)

    `summary` alongside `entries` is the SAME per-crew breakdown
    /api/simulate_mixed's top-level `kpi_by_crew` returns (see
    _build_kpi_by_crew) — included here too so a frontend can render
    "crew 1: 34 pull cards, 12 push chunks across HTL3, HTL5" without a
    second round-trip, computed over the FULL run regardless of any
    day_index/hour/line_id/crew_id filter applied to `entries` below
    (filters only narrow the chronological list, not the summary
    figures — a caller wanting a filtered summary should aggregate
    `entries` client-side).

    day_index/hour: same windowing convention as every other /api/mixed/*
    endpoint on this page (see this module's own windowing note above
    _require_mixed_run()) — 0-based day from sim start, optional 0-23
    hour to narrow further. Omit both for the whole run.
    """
    kenv, _cfg = _require_mixed_run()
    if time_unit not in ("h", "min", "s"):
        raise HTTPException(status_code=400, detail="time_unit must be one of: h, min, s")
    divisor = _TIME_DIVISORS[time_unit]

    window = _day_window_s(day_index, hour)
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
            "sim_class": e.sim_class,
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
