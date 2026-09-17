"""
reports/pull/shortfall.py
===========================
"shortfall_by_line" / "shortfall_products_by_line" payload —
build_shortfall_payload(). Kept separate from
reports/pull/card_flow.build_card_flow_payload() so each report module
stays single-purpose; callers that need both call both functions and
merge the results (see api/legacy.py, api/routes_simulate.py,
reports/serialization.py's dump_kanban_events_json).

Reads telemetry.records.ShortfallEvent (t, line_id, product_type,
start_s, end_s).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Iterable, Literal

from reports.binning import TIME_UNIT_DIVISORS

if TYPE_CHECKING:
    from telemetry.records import ShortfallEvent

TimeUnit = Literal["h", "min", "s"]

_DIVISORS = TIME_UNIT_DIVISORS


def build_shortfall_payload(
    line_ids: list[int],
    sim_time_s: float,
    n_bins: int | None = None,
    time_unit: TimeUnit = "h",
    shortfall_log: "Iterable[ShortfallEvent] | None" = None,
) -> dict:
    """
    Per-line, per-bin count of withdrawal requests that were
    open-and-unmet ("customer asked, supermarket had 0") at that
    instant, broken down per product (sachnummer), plus a "total" key —
    the same n_bins+1-point time grid and point shape
    (line_units_series' packages_series/restmenge_series style:
    {t, <product>: n, ..., total: n}) as
    reports.all.supermarket_series.build_line_units_timeseries, so the
    frontend's existing per-line stacked-by-product chart code works
    unmodified. A request counts toward a bin edge t if
    start_s <= t <= end_s, i.e. it was still waiting at that moment.

    line_ids: the set of line_ids known to the run (pass the same
    line_ids reports.pull.card_flow.build_card_flow_payload() derived,
    e.g. kenv.card_registry's line_id set, or all kenv.lines ids) — used
    so a line with zero shortfall events still gets an all-zero series
    entry rather than being silently absent.

    sim_time_s: pass kenv.env.now (same convention build_card_flow_payload
    uses) — taken as a plain float rather than kenv itself, so this
    function stays a pure reducer over shortfall_log like
    reports.all.supermarket_series.build_line_units_timeseries.

    n_bins: if None (default), derived from sim_time_s for a ~15
    real-world-minute bin width — MUST use the same formula as
    reports.pull.card_flow.build_card_flow_payload() (max(1,
    round(sim_time_s / 600.0))) if the two payloads are meant to share
    one time axis; pass the same explicit n_bins to both if you already
    computed it once.

    Empty (all-zero, no products) per line if shortfall_log is omitted
    or no request ever had to wait.
    """
    divisor = _DIVISORS[time_unit]
    if n_bins is None:
        n_bins = max(1, round(sim_time_s / 600.0))

    shortfall_events = list(shortfall_log) if shortfall_log else []
    shortfall_line_ids = sorted(
        {getattr(evt, "line_id", None) for evt in shortfall_events} - {None}
        | set(line_ids)
    )
    shortfall_products: dict[int, list[str]] = {
        lid: sorted({
            getattr(evt, "product_type", None) for evt in shortfall_events
            if getattr(evt, "line_id", None) == lid
            and getattr(evt, "product_type", None) is not None
        })
        for lid in shortfall_line_ids
    }
    shortfall_by_line: dict[str, list[dict]] = {str(lid): [] for lid in shortfall_line_ids}
    if n_bins > 0:
        events_by_line: dict[int, list] = {}
        for evt in shortfall_events:
            lid = getattr(evt, "line_id", None)
            if lid is not None:
                events_by_line.setdefault(lid, []).append(evt)
        for i in range(0, n_bins + 1):
            edge_s = sim_time_s * i / n_bins
            t_out = round(edge_s / divisor, 4)
            for lid in shortfall_line_ids:
                per_product = {p: 0 for p in shortfall_products[lid]}
                for evt in events_by_line.get(lid, []):
                    if evt.start_s <= edge_s + 1e-9 and edge_s <= evt.end_s + 1e-9:
                        p = getattr(evt, "product_type", None)
                        if p in per_product:
                            per_product[p] += 1
                pt = {"t": t_out, **per_product}
                pt["total"] = sum(per_product.values())
                shortfall_by_line[str(lid)].append(pt)

    return {
        "shortfall_by_line": shortfall_by_line,
        "shortfall_products_by_line": {
            str(lid): shortfall_products[lid] for lid in shortfall_line_ids
        },
    }
