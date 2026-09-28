"""
reports/movement/status.py
============================
production_status_at() and build_production_occupants() — what a line's
gate is doing at a point in time, and the actual card/push card token(s)
sitting in the Production rectangle for it. Kept as one pair since
reports/movement/frames.py always calls them together; deliberately
NOT folded into reports.movement.transit.build_in_transit (see
build_production_occupants' docstring).

GateActivityEntry lives in telemetry/records.py; ShiftCalendar lives in
domain/config.
"""

from __future__ import annotations

import datetime as _dt
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from domain.config import ShiftCalendar
    from telemetry.records import GateActivityEntry


def production_status_at(
    log: list["GateActivityEntry"],
    line_id: int,
    t_s: float,
    *,
    shift_calendar: Optional["ShiftCalendar"] = None,
    line_name: Optional[str] = None,
    epoch: Optional[_dt.datetime] = None,
) -> dict:
    """
    Replay `log` to resolve what `line_id`'s gate was doing at time t_s —
    the "last reading at or before this instant" convention every other
    time-indexed report in this package uses (reports/movement/chute.py,
    reports/movement/supermarket.py, reports/movement/trace.card_state_at).

    Returns one of:
      {"state": "producing", "product_number": str, "production_type": "pull"|"push",
       "crew_id": int, "since_t": float, "possible_changeover": bool}
      {"state": "idle", "product_number": None, "production_type": None,
       "crew_id": None, "since_t": float | None, "possible_changeover": False}
      {"state": "off_shift", "product_number": None, "production_type": None,
       "crew_id": None, "since_t": float | None, "possible_changeover": False}

    "crew_id" — which crew_process(crew_id=...) instance was holding
    this line's gate during the active entry — None whenever state isn't
    "producing".

    "idle" covers both a genuine gap between two holds and any time
    before the first hold / after the last one recorded so far —
    "since_t" is the end of the previous hold (None if there hasn't been
    one yet). It does NOT distinguish idle from Rüstzeit (setup time) —
    see GateActivityEntry's docstring.

    shift_calendar/line_name/epoch (optional, additive) — pass all three
    to further split "idle" into "idle" (on-shift, genuinely nothing to
    do) vs. "off_shift" (this line simply isn't scheduled to run at t_s
    at all). Omit any of the three (or pass a `shift_calendar` with no
    shifts configured at all) to get the plain two-state behaviour; this
    check is purely additive on top of it.

    This distinction only ever applies to what would OTHERWISE have been
    "idle" — it never overrides "producing". A batch/push card that started
    while on-shift and is still finishing after its shift's window
    closes is still, correctly, reported as "producing".
    """
    active: Optional["GateActivityEntry"] = None
    last_end: Optional[float] = None
    for e in log:
        if e.line_id != line_id or e.t_start > t_s:
            continue
        if e.t_start <= t_s < e.t_end:
            active = e
        elif e.t_end <= t_s and (last_end is None or e.t_end > last_end):
            last_end = e.t_end

    if active is not None:
        return {
            "state": "producing",
            "product_number": active.product_number,
            "production_type": active.production_type,
            "crew_id": active.crew_id,
            "since_t": active.t_start,
            "possible_changeover": active.possible_changeover,
        }

    if (
        shift_calendar is not None and shift_calendar.shifts
        and line_name is not None and epoch is not None
    ):
        at = epoch + _dt.timedelta(seconds=t_s)
        if not shift_calendar.is_line_on(line_name, at):
            return {
                "state": "off_shift",
                "product_number": None,
                "production_type": None,
                "crew_id": None,
                "since_t": last_end,
                "possible_changeover": False,
            }

    return {
        "state": "idle",
        "product_number": None,
        "production_type": None,
        "crew_id": None,
        "since_t": last_end,
        "possible_changeover": False,
    }


def build_production_occupants(frame: dict, lid: int, production_status: dict) -> list[dict]:
    """
    The actual token(s) to draw in the Production rectangle right now
    (one line = one job at a time). Resolved from production_status,
    NOT from frame's raw "in_production" bucket directly — a PullCard
    is marked in_production for its WHOLE released batch, starting the
    instant the production-trigger process pops it off the chute, which
    can be before the batch has actually secured the line's gate if
    something else is still running. Filtering frame's "in_production"
    cards down to production_status's own product_number still isn't
    perfectly precise (a same-product batch queued behind the running
    one would also match), but it's a real improvement over drawing
    every "in_production" card regardless of whether it's actually on
    the line yet.

    Deliberately kept separate from reports.movement.transit.
    build_in_transit, which is scoped to states with no Step-1 box of
    their own ("withdrawn") — "in_production" is intentionally EXCLUDED
    there so a frontend can't double-draw the same card once from each
    list; this function is the sole authority for what's really in the
    Production rectangle.
    """
    production_occupants: list[dict] = []
    if production_status["state"] == "producing":
        if production_status["production_type"] == "pull":
            production_occupants = [
                {"card_id": c["card_id"], "product_type": production_status["product_number"], "kind": "pull"}
                for c in frame.get((lid, "in_production", production_status["product_number"]), [])
            ]
        elif production_status["production_type"] == "push":
            # Push cards never get a PullCard — no real per-piece id
            # exists, same situation as a push chute entry. Synthesize
            # one deterministic id from the gate hold's own start time so
            # repeated calls for the same instant return the same id.
            production_occupants = [{
                "card_id": f"push-{lid}-{production_status['since_t']:.3f}",
                "product_type": production_status["product_number"],
                "kind": "push",
            }]
    return production_occupants
