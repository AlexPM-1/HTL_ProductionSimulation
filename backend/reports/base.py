"""
reports/base.py
================
Tiny registry so GET /api/reports (manifest) and GET /api/reports/{id} can
list and dispatch to report builders without api/routes_reports.py having to
import every reports/* module by name and hand-maintain a dict.

reports/ modules may only import domain + telemetry; this module imports
neither.

Usage
-----
    from reports.base import register

    @register("gantt", tags=("all",))
    def build_gantt_report(kenv, cfg, **kwargs) -> dict:
        ...

Every registered builder must accept (kenv, cfg, **kwargs) and return a
JSON-serialisable dict — the same calling convention every report builder
in this package already follows, so existing call sites (api/legacy.py,
api/routes_movement.py) can keep calling the underlying function directly
without going through the registry; the registry is purely an additional,
optional lookup surface for /api/reports.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Optional


@dataclass(frozen=True)
class ReportEntry:
    report_id: str
    fn: Callable[..., dict]
    tags: tuple[str, ...] = field(default_factory=tuple)
    description: str = ""


_REGISTRY: dict[str, ReportEntry] = {}


def register(report_id: str, tags: tuple[str, ...] = (), description: str = ""):
    """Decorator: @register("gantt", tags=("all",), description="...")"""

    def _wrap(fn: Callable[..., dict]) -> Callable[..., dict]:
        if report_id in _REGISTRY:
            raise ValueError(f"report id already registered: {report_id!r}")
        _REGISTRY[report_id] = ReportEntry(report_id, fn, tags, description)
        return fn

    return _wrap


def get(report_id: str) -> Optional[ReportEntry]:
    return _REGISTRY.get(report_id)


def manifest() -> list[dict]:
    """[{"id", "tags", "description"}, ...] — the GET /api/reports payload."""
    return [
        {"id": e.report_id, "tags": list(e.tags), "description": e.description}
        for e in sorted(_REGISTRY.values(), key=lambda e: e.report_id)
    ]
