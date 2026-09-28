"""
reports/all/order_routing.py
==============================
"Line assignment / routing" report for the All Production tab:

  1. build_order_routing_summary(menv)  — one flat row per (order, line)
     pairing, straight off menv.order_registry.values():
         order_id | due_date | production_type | product_number | line | cards
     A pull order that drew cards from N different lines gets N rows
     (one per line, per OrderRecord.assignments); a push order always
     gets exactly one row with cards=None (frontend renders "-").

  2. build_order_routing_timeline(menv) — one entry per order_id with
     the full per-order detail: due/delivered/withdrawal dates, which
     lines and cards fulfilled it, and — for pull orders — every card's
     production interval (joined against menv.card_registry[card_id]
     .history, filtered to this order_id), sorted chronologically; for
     push orders, the single production window straight off the
     OrderRecordPush's own t_production_start/end.

  3. build_order_routing_report(menv, cfg, **kwargs) — {"summary": [...],
     "timeline": [...]}, registered as "order_routing" (tags=("all",))
     for GET /api/reports/order_routing, and included directly in
     api/legacy.py's POST /api/simulate_mixed payload alongside the
     other "all" reports.

reports/ modules may only import domain + telemetry; sim types are only
ever referenced under TYPE_CHECKING (menv/cfg are accepted as untyped,
duck-typed arguments at runtime — same convention as
reports.serialization).

Wall-clock conversion: t_production_start/end (sim.resources.cards
.CardHistoryEntry) and OrderRecordPush.t_production_start/end are simpy
sim-time floats (env.now), not wall-clock datetimes — converted here via
menv.push_ctx.epoch, the single shared epoch every push AND pull process
in the run derives its wall-clock timestamps from (see sim.runner
.run_mixed / sim.context.RunContext.epoch). None if no run has an epoch
yet (menv.push_ctx missing) or the specific stage hasn't happened yet.
"""

from __future__ import annotations

import datetime as _dt
from typing import TYPE_CHECKING, Optional

from domain.orders import OrderRecordPull, OrderRecordPush

from reports.base import register

if TYPE_CHECKING:
    from domain.config import SimConfig
    from sim.resources.environment import MixedSimEnvironment


def _epoch(menv) -> Optional[_dt.datetime]:
    """The run's shared wall-clock epoch, or None if unavailable (e.g. a
    menv from a run that never started push scheduling)."""
    return getattr(getattr(menv, "push_ctx", None), "epoch", None)


def _wall(epoch: Optional[_dt.datetime], t: Optional[float]) -> Optional[_dt.datetime]:
    """sim-time float -> wall-clock datetime, or None if either input is
    missing (stage hasn't happened yet / no epoch)."""
    if epoch is None or t is None:
        return None
    return epoch + _dt.timedelta(seconds=t)


def _iso(dt: Optional[_dt.datetime]) -> Optional[str]:
    return dt.isoformat() if dt is not None else None


# ---------------------------------------------------------------------------
# 1. Flat summary table
# ---------------------------------------------------------------------------

def build_order_routing_summary(menv: "MixedSimEnvironment") -> list[dict]:
    """
    One row per (order, line) pairing, straight off
    menv.order_registry.values() — the "Order | Due | Type | Product |
    Line Assigned | Cards" table.

    A pull order with cards drawn from several lines' Supermarkets
    yields one row per line (per OrderRecord.assignments entry); a push
    order — single-line by construction — always yields exactly one
    row. `cards` is None for a push row (no card concept) or for a pull
    line-entry with nothing assigned to it yet; the frontend renders
    that as "-".

    An order with no assignments yet (nothing produced/delivered
    against it so far) still gets exactly one row, with
    `line`=order.assigned_line (empty string for a fresh pull order —
    see OrderRecord.assigned_line's docstring) and `cards`=None, so the
    order isn't silently missing from the table while it's in flight.
    """
    rows: list[dict] = []
    for order in sorted(menv.order_registry.values(), key=lambda o: o.order_id):
        if not order.assignments:
            rows.append({
                "order_id": order.order_id,
                "due_date": _iso(order.due_date),
                "production_type": order.production_type,
                "product_number": order.product_number,
                "line": order.assigned_line or None,
                "cards": None,
            })
            continue

        for entry in order.assignments:
            is_pull = isinstance(order, OrderRecordPull)
            card_ids = sorted(entry["card_ids"]) if is_pull else None
            rows.append({
                "order_id": order.order_id,
                "due_date": _iso(order.due_date),
                "production_type": order.production_type,
                "product_number": order.product_number,
                "line": entry["line"],
                "cards": card_ids if card_ids else None,
            })
    return rows


# ---------------------------------------------------------------------------
# 2. Per-order timeline
# ---------------------------------------------------------------------------

def _pull_production_entries(menv, order: "OrderRecordPull") -> list[dict]:
    """
    Every card's production interval for this pull order, sorted
    chronologically by start time — joins order.assignments (which line
    each card came from) against menv.card_registry[card_id].history
    (filtered to this order_id, since a permanent card accumulates one
    CardHistoryEntry per order it has ever fulfilled).
    """
    epoch = _epoch(menv)
    out: list[dict] = []
    for assignment in order.assignments:
        line = assignment["line"]
        for card_id in assignment["card_ids"]:
            card = menv.card_registry.get(card_id)
            if card is None:
                continue
            hist = next(
                (h for h in card.history if h.order_id == order.order_id), None,
            )
            if hist is None:
                continue
            out.append({
                "line": line,
                "card_id": card_id,
                "start": _iso(_wall(epoch, hist.t_production_start)),
                "end": _iso(_wall(epoch, hist.t_production_end)),
                "_sort_key": hist.t_production_start
                if hist.t_production_start is not None else float("inf"),
            })
    out.sort(key=lambda e: e["_sort_key"])
    for e in out:
        del e["_sort_key"]
    return out


def _pull_withdrawal_window(menv, order: "OrderRecordPull") -> tuple[Optional[str], Optional[str]]:
    """
    (start, end) ISO strings spanning this pull order's whole fulfilment
    — start = earliest card's t_withdrawn_time, end = the latest stage
    any of its cards has reached so far (t_reposition_supermarket if
    the card has been fully returned, else t_production_end, else
    t_chute_end, else t_withdrawn_time — whichever is the furthest-along
    timestamp actually set). None/None if no card has been withdrawn
    for this order yet.
    """
    epoch = _epoch(menv)
    starts: list[float] = []
    ends: list[float] = []
    for assignment in order.assignments:
        for card_id in assignment["card_ids"]:
            card = menv.card_registry.get(card_id)
            if card is None:
                continue
            hist = next(
                (h for h in card.history if h.order_id == order.order_id), None,
            )
            if hist is None:
                continue
            if hist.t_withdrawn_time is not None:
                starts.append(hist.t_withdrawn_time)
            latest = (
                hist.t_reposition_supermarket
                or hist.t_production_end
                or hist.t_chute_end
                or hist.t_withdrawn_time
            )
            if latest is not None:
                ends.append(latest)
    start = _iso(_wall(epoch, min(starts))) if starts else None
    end = _iso(_wall(epoch, max(ends))) if ends else None
    return start, end


def build_order_routing_timeline(menv: "MixedSimEnvironment") -> list[dict]:
    """
    One entry per order_id — the expandable "Order A / Order B / ..."
    detail view under the summary table.
    """
    epoch = _epoch(menv)
    out: list[dict] = []

    for order in sorted(menv.order_registry.values(), key=lambda o: o.order_id):
        lines_assigned = sorted({a["line"] for a in order.assignments if a["line"]})

        if isinstance(order, OrderRecordPull):
            cards_by_line = {
                a["line"]: sorted(a["card_ids"])
                for a in order.assignments if a["card_ids"]
            }
            withdrawal_start, withdrawal_end = _pull_withdrawal_window(menv, order)
            production = _pull_production_entries(menv, order)
            customer_withdrawal_date = None
        elif isinstance(order, OrderRecordPush):
            cards_by_line = {}
            # Push has a single instant, not a range — see
            # OrderRecordPush.customer_withdrawal_date's docstring.
            withdrawal_start = withdrawal_end = _iso(order.customer_withdrawal_date)
            customer_withdrawal_date = _iso(order.customer_withdrawal_date)
            prod_start = _iso(_wall(epoch, order.t_production_start))
            prod_end = _iso(_wall(epoch, order.t_production_end))
            production = (
                [{
                    "line": order.assigned_line,
                    "card_id": None,
                    "start": prod_start,
                    "end": prod_end,
                }]
                if (prod_start or prod_end) else []
            )
        else:
            # Base OrderRecord is never meant to be instantiated directly
            # (see domain.orders.OrderRecord's docstring) — defensive
            # fallback only, shouldn't be reachable in practice.
            cards_by_line = {}
            withdrawal_start = withdrawal_end = None
            customer_withdrawal_date = None
            production = []

        out.append({
            "order_id": order.order_id,
            "production_type": order.production_type,
            "product_number": order.product_number,
            "quantity": order.quantity,
            "due_date": _iso(order.due_date),
            "delivered_date": _iso(order.delivered_date),
            "delivery_delta_h": order.delivery_delta_h,
            "lines_assigned": lines_assigned,
            "cards_by_line": cards_by_line,
            "withdrawal_start": withdrawal_start,
            "withdrawal_end": withdrawal_end,
            "customer_withdrawal_date": customer_withdrawal_date,
            "production": production,
        })
    return out


# ---------------------------------------------------------------------------
# 3. Combined report / registry entry
# ---------------------------------------------------------------------------

@register(
    "order_routing",
    tags=("all",),
    description="Line assignment / routing: order->line->cards summary "
                 "table plus a per-order fulfilment timeline.",
)
def build_order_routing_report(menv, cfg: "SimConfig" = None, **kwargs) -> dict:
    """
    {"summary": [...], "timeline": [...]} — see
    build_order_routing_summary()/build_order_routing_timeline() above.
    `cfg` is accepted (and unused) only to satisfy the
    (menv, cfg, **kwargs) calling convention every reports.base-
    registered builder follows — this report reads everything it needs
    off menv.order_registry / menv.card_registry.
    """
    return {
        "summary": build_order_routing_summary(menv),
        "timeline": build_order_routing_timeline(menv),
    }
