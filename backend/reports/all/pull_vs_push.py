"""
reports/all/pull_vs_push.py
=============================
"Pull vs Push" report for the All Production tab: how much of the plant's
output is class-1 Kanban (pull) vs class-2 (push), both as a single
run-wide total and broken down per calendar production-day.

Three counts, per side (pull/push), both totalled and per-day:

  parts  : pieces actually produced — summed straight off
           menv.gate_activity_log's `quantity` (GateActivityEntry — see
           telemetry.records.GateActivityEntry's docstring: one entry per
           completed hold of a line's gate, i.e. one finished pull card
           or one finished push card).
  cards  : how many of those completed holds there were — literally
           len(gate_activity_log) split by production_type, since (per
           that same docstring) one GateActivityEntry IS one completed
           card, pull or push.
  orders : DISTINCT customer orders, pull or push — read off
           menv.order_registry, which is keyed by order_id (see
           SimEnvironment.order_registry's docstring), so this is
           already deduplicated: a pull order fulfilled by several cards
           across several lines is still exactly one entry, and a push
           order sliced into several OrderRecordPush "chunks"
           (sim.fill.push.chunking) still shares one order_id — chunking
           does not mint new order_ids, so order_registry never
           double-counts a split push order into several push "orders".
           This is deliberately NOT the same thing as `cards`, per this
           report's own brief: an order can have many cards and many
           parts.

Two different clocks are used on purpose:
  - parts/cards are counted on the instant they were actually produced
    (GateActivityEntry.t_start, a simpy sim-time float, converted to
    wall-clock via the run's shared epoch) — that's the only timestamp
    a completed card has.
  - orders are counted on the day the order was fully delivered
    (OrderRecord.delivered_date — already a wall-clock datetime, no
    epoch conversion needed; see domain.orders.OrderRecord.delivered_date's
    docstring). An order not yet delivered contributes to `total` but
    not to any daily bucket, same "not there yet" convention
    reports.all.order_routing uses for in-flight orders.

Every daily bucket is keyed by a calendar date in the run's wall-clock
epoch, shifted back by day_start_hour so a card produced at, say,
01:00 still counts toward the production day that started the evening/
morning before, the same "day starts at day_start_hour, not midnight"
convention domain.constants.DAY_START_HOUR encodes elsewhere. day_start_hour
is read off menv.push_ctx (the RunContext run_mixed built the run with),
falling back to domain.constants.DAY_START_HOUR if that run never
attached one.

reports/ modules may only import domain + telemetry; sim types are only
ever referenced under TYPE_CHECKING (menv/cfg are accepted as untyped,
duck-typed arguments at runtime) — same convention as
reports.all.order_routing / reports.serialization.
"""

from __future__ import annotations

import datetime as _dt
from typing import TYPE_CHECKING, Optional

from domain.constants import DAY_START_HOUR as _DEFAULT_DAY_START_HOUR

from reports.base import register

if TYPE_CHECKING:
    from domain.config import SimConfig
    from sim.resources.environment import MixedSimEnvironment


_SIDES = ("pull", "push")


def _empty_counts() -> dict:
    return {"parts": 0, "cards": 0, "orders": 0}


def _epoch(menv) -> Optional[_dt.datetime]:
    """The run's shared wall-clock epoch, or None if unavailable — same
    helper as reports.all.order_routing._epoch()."""
    return getattr(getattr(menv, "push_ctx", None), "epoch", None)


def _day_start_hour(menv) -> int:
    return getattr(
        getattr(menv, "push_ctx", None), "day_start_hour", None
    ) or _DEFAULT_DAY_START_HOUR


def _day_key(dt: Optional[_dt.datetime], day_start_hour: int) -> Optional[str]:
    """Calendar-date string (YYYY-MM-DD) for a wall-clock instant, shifted
    so the "production day" starts at day_start_hour rather than
    midnight. None if `dt` is None (no timestamp to bucket by yet)."""
    if dt is None:
        return None
    return (dt - _dt.timedelta(hours=day_start_hour)).date().isoformat()


def _bucket(daily: dict[str, dict], day: str) -> dict:
    return daily.setdefault(day, {side: _empty_counts() for side in _SIDES})


# ---------------------------------------------------------------------------
# Parts + cards — from the completed-card ledger (gate_activity_log)
# ---------------------------------------------------------------------------

def _add_parts_and_cards(
    menv: "MixedSimEnvironment",
    total: dict[str, dict],
    daily: dict[str, dict],
) -> None:
    epoch = _epoch(menv)
    day_start_hour = _day_start_hour(menv)

    for entry in getattr(menv, "gate_activity_log", None) or []:
        side = entry.production_type
        if side not in _SIDES:
            continue  # defensive — every real entry is "pull" or "push"

        qty = entry.quantity or 0
        total[side]["parts"] += qty
        total[side]["cards"] += 1

        wall = epoch + _dt.timedelta(seconds=entry.t_start) if epoch is not None else None
        day = _day_key(wall, day_start_hour)
        if day is None:
            continue  # no epoch on this run — totals still counted above
        row = _bucket(daily, day)
        row[side]["parts"] += qty
        row[side]["cards"] += 1


# ---------------------------------------------------------------------------
# Orders — deduplicated straight off order_registry
# ---------------------------------------------------------------------------

def _add_orders(
    menv: "MixedSimEnvironment",
    total: dict[str, dict],
    daily: dict[str, dict],
) -> None:
    day_start_hour = _day_start_hour(menv)

    for order in (getattr(menv, "order_registry", None) or {}).values():
        side = order.production_type
        if side not in _SIDES:
            continue

        total[side]["orders"] += 1

        day = _day_key(order.delivered_date, day_start_hour)
        if day is None:
            continue  # not delivered yet — counted in total, not in a day
        row = _bucket(daily, day)
        row[side]["orders"] += 1


# ---------------------------------------------------------------------------
# Combined report / registry entry
# ---------------------------------------------------------------------------

@register(
    "pull_vs_push",
    tags=("all",),
    description="Pull vs push totals and daily breakdown: quantity of "
                "parts, cards, and distinct orders on each side.",
)
def build_pull_vs_push_report(menv: "MixedSimEnvironment", cfg: "SimConfig" = None, **kwargs) -> dict:
    """
    {"total": {"pull": {...}, "push": {...}},
     "daily": [{"date": "YYYY-MM-DD", "pull": {...}, "push": {...}}, ...]}

    Each {...} is {"parts", "cards", "orders"} — see this module's
    docstring for exactly what each count means and which timestamp it's
    bucketed on. `daily` is sorted by date ascending. `cfg` is accepted
    (and unused) only to satisfy the (menv, cfg, **kwargs) calling
    convention every reports.base-registered builder follows.
    """
    total: dict[str, dict] = {side: _empty_counts() for side in _SIDES}
    daily: dict[str, dict] = {}

    _add_parts_and_cards(menv, total, daily)
    _add_orders(menv, total, daily)

    return {
        "total": total,
        "daily": [
            {"date": day, **daily[day]} for day in sorted(daily.keys())
        ],
    }
