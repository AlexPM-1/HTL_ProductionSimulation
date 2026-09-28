"""
reports/serialization.py
==========================
JSON-safety helpers for dataclass-shaped telemetry (to_jsonable), plus
flat dumpers for the raw event/snapshot/shortfall logs
(event_log_to_dicts, dump_gantt_json, snapshot_log_to_dicts,
shortfall_log_to_dicts, dump_pull_events_json) used for audit trails,
debugging, and writing a run's payloads straight to a JSON file.

dump_pull_events_json builds its "card_flow" key from two separate
calls — reports.pull.card_flow.build_card_flow_payload() and
reports.pull.shortfall.build_shortfall_payload() — and merges the
results, since those two report builders are kept separate (single-
purpose) rather than one function returning both blocks. This mirrors
how api/legacy.py's simulate_mixed assembles the same payload.
"""

from __future__ import annotations

import dataclasses
import datetime as _dt
import json
from typing import TYPE_CHECKING, Iterable, Literal, Optional

from reports.binning import validate_time_unit
from reports.all.gantt import build_gantt_payload
from reports.pull.card_flow import build_card_flow_payload
from reports.pull.shortfall import build_shortfall_payload
from reports.all.supermarket_series import build_line_units_timeseries
from telemetry.records import ScheduleEvent

if TYPE_CHECKING:
    from sim.resources.environment import MixedSimEnvironment
    from telemetry.records import SupermarketSnapshot, ShortfallEvent


def to_jsonable(obj):
    """
    Recursively turn a dataclass instance (or list/dict of them) into
    plain JSON-safe Python — datetimes become ISO-8601 strings, nested
    dataclasses become dicts. Deliberately does not import or assume the
    concrete type of any specific dataclass (domain.orders.UnassignedOrder,
    telemetry.records.PushDeliveryRecord, etc.) — dataclasses.is_dataclass()
    + asdict() handles any dataclass shape without knowing its fields up
    front.
    """
    if obj is None or isinstance(obj, (str, int, float, bool)):
        return obj
    if isinstance(obj, (_dt.datetime, _dt.date)):
        return obj.isoformat()
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        return {k: to_jsonable(v) for k, v in dataclasses.asdict(obj).items()}
    if isinstance(obj, dict):
        return {k: to_jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple, set)):
        return [to_jsonable(v) for v in obj]
    # Fallback for a plain (non-dataclass) object — best-effort via __dict__
    # rather than raising, so one unexpected type doesn't 500 the endpoint.
    return getattr(obj, "__dict__", str(obj))


# Private alias for call sites that import _to_jsonable directly.
_to_jsonable = to_jsonable


def event_log_to_dicts(event_log: list[ScheduleEvent]) -> list[dict]:
    """Flat JSON-serialisable dump of the raw schedule event log (audit
    trail / debugging — includes both setup and job events, un-gapped)."""
    return [dataclasses.asdict(e) for e in event_log]


def dump_gantt_json(
    event_log: list[ScheduleEvent],
    sim_time_s: float,
    line_names: list[str],
    path: str,
    time_unit: Literal["h", "min", "s"] = "h",
) -> None:
    """Convenience: write reports.all.gantt.build_gantt_payload(...)
    straight to a JSON file that the frontend (or a quick `fetch()`) can
    consume as-is."""
    validate_time_unit(time_unit)
    payload = build_gantt_payload(event_log, sim_time_s, line_names, time_unit)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)


def snapshot_log_to_dicts(snapshot_log: "Iterable[SupermarketSnapshot]") -> list[dict]:
    """Flat JSON-serialisable dump of the raw snapshot log (audit trail /
    debugging), mirroring event_log_to_dicts()."""
    return [dataclasses.asdict(s) for s in snapshot_log]


def shortfall_log_to_dicts(shortfall_log: "Iterable[ShortfallEvent]") -> list[dict]:
    """Flat JSON-serialisable dump of the raw shortfall log (audit trail /
    debugging) — one row per withdrawal request that had to wait,
    mirroring snapshot_log_to_dicts()."""
    return [dataclasses.asdict(e) for e in shortfall_log]


def dump_pull_events_json(
    menv: "MixedSimEnvironment",
    snapshot_log: "Iterable[SupermarketSnapshot]",
    path: str,
    n_bins: int | None = None,
    time_unit: Literal["h", "min", "s"] = "h",
    shortfall_log: "Iterable[ShortfallEvent] | None" = None,
) -> None:
    """Convenience: write all payloads straight to one JSON file, keyed
    "card_flow" / "line_units" — mirrors dump_gantt_json. ("line_units" is
    the binned per-line stock timeseries behind the dashboard's
    "Supermarkets" tab.)

    Builds "card_flow" from reports.pull.card_flow.build_card_flow_payload()
    and reports.pull.shortfall.build_shortfall_payload(), called separately
    and merged into one dict — matching how api/legacy.py's simulate_mixed
    assembles the same payload.
    """
    line_ids = sorted({
        getattr(c, "line_id", None) for c in menv.card_registry.values()
        if getattr(c, "line_id", None) is not None
    })
    card_flow = build_card_flow_payload(menv, n_bins=n_bins, time_unit=time_unit)
    card_flow.update(build_shortfall_payload(
        line_ids=line_ids,
        sim_time_s=menv.env.now,
        n_bins=n_bins,
        time_unit=time_unit,
        shortfall_log=shortfall_log,
    ))
    payload = {
        "card_flow": card_flow,
        "line_units": build_line_units_timeseries(
            snapshot_log, menv.env.now, n_bins=n_bins, time_unit=time_unit
        ),
    }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)
