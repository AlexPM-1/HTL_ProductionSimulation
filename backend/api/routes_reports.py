"""
api/routes_reports.py
========================
GET /api/reports (manifest of registered report builders) and
GET /api/reports/{id} (fetch one, via reports.base's registry).

No report builder is registered here yet: every existing builder (used
directly by api/routes_movement.py and api/legacy.py) has its own bespoke
call signature (some take n_bins=..., some take line_id=..., some take
t_s=...), while reports.base's registry expects a uniform
(menv, cfg, **kwargs) -> dict call. To register a builder here, add a
small (menv, cfg, **kwargs) -> ... wrapper around it via
reports.base.register(), rather than reshaping the builder itself.
"""

from __future__ import annotations

from fastapi import HTTPException

from api.app import app
from api.runs import run_store
from reports.base import manifest, get


@app.get("/api/reports")
def list_reports():
    """{"reports": [{"id", "tags", "description"}, ...]} — empty until at
    least one report builder is registered, see this module's docstring."""
    return {"reports": manifest()}


@app.get("/api/reports/{report_id}")
def get_report(report_id: str, **kwargs):
    entry = get(report_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"No such report: {report_id!r}")
    menv, cfg = run_store.require()
    return entry.fn(menv, cfg, **kwargs)
