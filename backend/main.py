"""
main.py
=========
Entry point for uvicorn. Every dashboard-facing endpoint
(/api/health, /api/parameters, /api/mixed/push_policy [GET+PATCH],
/api/simulate_mixed, /api/mixed/crew_activity,
/api/mixed/supermarket_state, /api/mixed/movement_state,
/api/mixed/plant_structure, /api/mixed/card_trace,
/api/mixed/part_trace, /api/reports, /api/reports/{id}) lives in
api/app.py plus the api/routes_*.py and api/legacy.py modules,
attached to the single shared `app = FastAPI()` instance api/app.py
creates.

Attaching a route with `@app.get(...)` / `@app.post(...)` only takes
effect once Python actually imports the module it's written in — so
this file's only job is to import every route-registering module once
(for that side effect) and then expose the resulting `app` for uvicorn.

Run
---
    uvicorn main:app --reload --port 8000

(same host:port the dashboard's "API Base" field points at — see
mixed_production_planner_dashboard.html's apiBaseInput default,
http://localhost:8000)
"""

from __future__ import annotations

from api.app import app  # creates FastAPI(), CORS, /api/health, /api/parameters — must import first

# Each import below is side-effect-only (registers @app.get/@app.post
# routes onto the shared `app` from api.app) — the "as _" / noqa is just
# to stop linters flagging them as unused.

import api.routes_policy as _routes_policy    # noqa: F401  GET/PATCH /api/mixed/push_policy
import api.legacy as _legacy                  # noqa: F401  POST /api/simulate_mixed,
                                               #             GET /api/mixed/crew_activity,
                                               #             GET /api/mixed/supermarket_state
import api.routes_movement as _routes_movement  # noqa: F401  movement_state, plant_structure,
                                                 #              card_trace, part_trace,
                                                 #              card_ids, part_ids
import api.routes_reports as _routes_reports    # noqa: F401  GET /reports (manifest), /reports/{id}

__all__ = ["app"]
