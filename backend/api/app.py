"""
api/app.py
============
Owns the FastAPI() instance, CORS setup, and the workbook-path/config
loading plumbing. Every other route module (api/legacy.py,
api/routes_movement.py, api/routes_policy.py, api/routes_reports.py)
imports `app` from here and attaches its own @app.get/@app.post/@app.patch
decorators to it.
"""

from __future__ import annotations

import os
from functools import lru_cache

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from domain.config import load_config, SimConfig

# ---------------------------------------------------------------------------
# Paths — overridable via environment variables so different environments
# can point at different workbooks without code changes.
# ---------------------------------------------------------------------------
EXCEL_PATH = os.environ.get("SIM_EXCEL_PATH", "domain/ProductionPlanningConfig.xlsx")
SETUP_XLSX_PATH = os.environ.get("SIM_SETUP_XLSX_PATH", "domain/HTLSetupTimes.xlsx")
PRODUCT_MASTER_PATH = os.environ.get("SIM_PRODUCT_MASTER_PATH", "domain/ProductMaster.xlsx")

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


@lru_cache(maxsize=1)
def cfg() -> SimConfig:
    return load_config(EXCEL_PATH, SETUP_XLSX_PATH, PRODUCT_MASTER_PATH)


# Alias used by api.legacy (and other call sites) that import `_cfg`.
_cfg = cfg


@app.get("/api/health")
def health():
    from fastapi import HTTPException
    try:
        c = cfg()
        return {"status": "ok", "lines": c.line_names}
    except FileNotFoundError as e:
        raise HTTPException(status_code=503, detail=str(e))


@app.get("/api/parameters")
def get_parameters(reload: bool = False):
    """Parameters used to populate the frontend's settings sidebar."""
    from fastapi import HTTPException
    from domain.constants import DEFAULT_N_WORKERS, DAY_START_HOUR, PUSH_CARD_SIZE
    from reports.kpi.by_class import build_class_product_sets

    if reload:
        cfg.cache_clear()
    try:
        c = cfg()
    except FileNotFoundError as e:
        raise HTTPException(status_code=503, detail=str(e))

    pull_products, push_products = build_class_product_sets(c)

    return {
        "lines": c.line_names,
        "n_pull_products": len(pull_products),
        "n_push_products": len(push_products),
        "worker_options": [1, 2],
        "default_workers": DEFAULT_N_WORKERS,
        # Crew count for sim.drain.crew.crew_process's crew-based depletion
        # model — a crew can hold at most one line at a time, so more crews
        # than lines just means some crews are always idle; the sidebar can
        # offer up to len(lines) as a sane upper bound.
        "crew_options": list(range(1, max(len(c.line_names), 1) + 1)),
        "default_n_crews": 2,
        "default_day_start_hour": DAY_START_HOUR,
        "default_push_card_size": PUSH_CARD_SIZE,
        # Whether the loaded workbook's "Shifts" sheet actually configured
        # any shift-time definitions — see domain.config.ShiftCalendar. If
        # False, every line/every time is treated as always-on and
        # production_status/movement_state will never report "off_shift" —
        # a frontend can use this to decide whether it's worth rendering an
        # off-shift legend/toggle at all.
        "has_shift_calendar": bool(c.shift_calendar.shifts),
    }
