"""
kanban_events.py
=================
Step 2 of the push-vs-pull dashboard work — payload builders for the
Kanban-specific views (Card Flow, Supermarkets) that sit alongside the
existing Gantt chart.

Sibling to schedule_events.py: same design goals apply here.

1.  Zero changes to kanban_process_logic.py / entities_resources_v4.py.
    Both functions below are pure — they only ever *read* off objects
    the simulation already produces (kenv.card_registry and a caller-owned
    snapshot_log: list[SupermarketSnapshot], see kanban_process_logic.py's
    KanbanRuntime / SupermarketSnapshot docstrings). Nothing here drives
    simpy or mutates simulation state.
2.  Dependency-free. Deliberately does NOT import kanban_process_logic at
    runtime (that module pulls in the full config/entities stack) — the
    SupermarketSnapshot type is only referenced under TYPE_CHECKING, and
    at call time this module just duck-types on the attributes documented
    in that dataclass (t, line_id, line_name, product_type, event_type,
    n_available, pcs_partial, batch_size) and on KanbanCard
    (product_type, transitions — see kanban_runner.py's
    _print_card_cycle_times() for the same duck-typed access pattern:
    ``for state, t in card.transitions``).
3.  Same time_unit convention as schedule_events.build_gantt_payload():
    "h" | "min" | "s", applied via the same divisor table, so a caller
    can render Gantt / Card Flow / Supermarkets on one shared x-axis.

Usage
-----
    from kanban_events import (
        build_card_flow_payload, build_line_units_timeseries,
    )

    # after env.run(until=horizon_s):
    card_flow = build_card_flow_payload(kenv, time_unit="h")
    line_units = build_line_units_timeseries(kenv.snapshot_log, kenv.env.now, time_unit="h")
"""

from __future__ import annotations

import json
from dataclasses import asdict
from typing import TYPE_CHECKING, Iterable, Literal

if TYPE_CHECKING:
    # Only for type hints — never imported at runtime (see module docstring
    # point 2). entities_resources_v4.KanbanSimEnvironment /
    # kanban_process_logic.SupermarketSnapshot / .ShortfallEvent.
    from backend.entities_resources_v5 import KanbanSimEnvironment
    from backend.kanban_process_logic_v2 import SupermarketSnapshot, ShortfallEvent

TimeUnit = Literal["h", "min", "s"]

# Same divisor table as schedule_events.build_gantt_payload — kept as its
# own copy rather than importing schedule_events, to preserve this
# module's zero-dependency goal.
_DIVISORS: dict[str, float] = {"h": 3600.0, "min": 60.0, "s": 1.0}

# Canonical card-state ordering (matches the sequence documented on
# SupermarketSnapshot / the record_transition() call sites in
# kanban_process_logic.py: withdrawal_process -> collection_box_emptying_process
# -> production_trigger_process -> _return_card_to_supermarket). Any state
# encountered that isn't in this list is appended afterwards, sorted, so
# the payload never silently drops a state the sim logic gains later.
_CANONICAL_CARD_STATES: list[str] = [
    "withdrawn",
    "in_collection_box",
    "in_batch_collector",
    "released_to_chute",
    "in_production",
    "in_supermarket",
]


# ---------------------------------------------------------------------------
# Card Flow — per-card transition history + a time-bucketed state census
# ---------------------------------------------------------------------------

def build_card_flow_payload(
    kenv: "KanbanSimEnvironment",
    n_bins: int | None = None,
    time_unit: TimeUnit = "h",
    shortfall_log: "Iterable[ShortfallEvent] | None" = None,
) -> dict:
    """
    Flatten kenv.card_registry into a JSON-ready payload for the
    dashboard's Card Flow tab.

    Four things come out of this:

    1.  "cards" — one entry per card with its full transition history
        (state, t), for drill-down / debugging (e.g. a tooltip on a
        single card in the UI).
    2.  "series" — a time-bucketed census, PLANT-WIDE: n_bins+1 evenly
        -spaced samples across [0, sim_time_s] (the extra sample is the
        t=0 point itself — see below), each giving the count of cards in
        every state as of that point in the run. This is what drives the
        stacked-area "cards per state over time" chart's "Total" view —
        plotting one line per card (there can be dozens+) is not useful,
        so the aggregation happens here rather than in the frontend.
    3.  "series_by_line" — the same census, split out per line, so the
        frontend can offer a "which line?" selector alongside "Total".
        Keyed by line_id (as a string, for JSON-object-key safety); each
        value has the same shape as "series". "lines" carries the
        (line_id, line_name) pairs actually present, sorted by line_id,
        so the frontend can label that selector without hardcoding line
        names.
    4.  "shortfall_by_line" / "shortfall_products_by_line" — per line, the
        same n_bins+1-point time grid as "series", giving the COUNT of
        withdrawal requests that were open-and-unmet ("customer asked,
        supermarket had 0") at that instant — broken down per product
        (sachnummer), plus a "total" key, exactly the same point shape as
        line_units_series' packages_series/restmenge_series
        ({t, <product>: n, ..., total: n}) so the frontend's existing
        per-line stacked-by-product chart code works unmodified. Sourced
        from shortfall_log (a caller-owned list[ShortfallEvent] — see
        kanban_process_logic.ShortfallEvent / KanbanRuntime.
        record_shortfall). A request counts toward a bin edge t if
        start_s <= t <= end_s, i.e. it was still waiting at that moment.
        Both keyed by line_id (string), same convention as
        series_by_line; shortfall_products_by_line lists just the
        products that ever had a shortfall on that line (mirrors
        line_units_series.lines[i].products). Empty (all-zero, no
        products) if shortfall_log is omitted or no request ever had to
        wait — pass it in via `build_card_flow_payload(kenv,
        shortfall_log=rt.shortfall_log)` (or kenv.shortfall_log, if the
        caller mirrors that attribute onto kenv the way it already does
        for snapshot_log/event_log).

    A card's "current state as of time t" is the state of its last
    transition at or before t (transitions are appended in chronological
    order as the sim runs, since record_transition() is always called
    with kenv.env.now — see kanban_process_logic.py). A card with no
    transition yet at or before t is simply not counted at that bin —
    in practice this never happens past t=0, since KanbanSimEnvironment.
    create_card() records an "in_supermarket" transition at creation
    time (see entities_resources_v4.py), so every card already has a
    transition at or before t=0.

    Bins run i = 0..n_bins inclusive (n_bins+1 points), so the series
    always includes an explicit t=0 sample — the count as the run started
    — rather than jumping straight to the first bin edge and silently
    omitting the starting state.

    n_bins: if None (default), it is derived from sim_time_s so the bin
    width is always ~15 real-world minutes, regardless of how long the
    run is (previously this was a hardcoded 60, which meant bin width was
    horizon/60 — e.g. a 15-day/360h run silently degraded to 6h-wide
    bins, and every downstream day-tab view inherited that same coarse
    resolution since it's just a slice of this one array). Pass an
    explicit n_bins to override (e.g. for a coarser "full span" overview
    if the per-15-min grid ever gets too large to render).

    sim_time_s is read off kenv.env.now, so call this AFTER env.run(...)
    returns — same assumption print_restmenge_report() makes.
    """
    divisor = _DIVISORS[time_unit]
    sim_time_s = kenv.env.now
    if n_bins is None:
        # ~1 bin per real-world 15 minutes, independent of time_unit and
        # of how long the horizon is.
        n_bins = max(1, round(sim_time_s / 600.0))

    # line_id -> line_name, duck-typed off kenv.lines the same way
    # KanbanRuntime.record_supermarket() resolves it in
    # kanban_process_logic.py (`kenv.lines[line_id - 1].line_name`).
    line_names: dict[int, str] = {
        line.line_id: line.line_name for line in kenv.lines
    }

    cards: list[dict] = []
    seen_states: set[str] = set()
    for card_id, card in kenv.card_registry.items():
        transitions = [
            {"state": state, "t": round(t / divisor, 4)}
            for state, t in card.transitions
        ]
        seen_states.update(state for state, _ in card.transitions)
        cards.append({
            "card_id": card_id,
            "product_type": getattr(card, "product_type", None),
            "line_id": getattr(card, "line_id", None),
            "transitions": transitions,
        })

    states = [s for s in _CANONICAL_CARD_STATES if s in seen_states]
    states += sorted(seen_states - set(_CANONICAL_CARD_STATES))

    line_ids = sorted({
        getattr(card, "line_id", None) for card in kenv.card_registry.values()
        if getattr(card, "line_id", None) is not None
    })

    series: list[dict] = []
    series_by_line: dict[str, list[dict]] = {str(lid): [] for lid in line_ids}
    if n_bins > 0:
        for i in range(0, n_bins + 1):
            edge_s = sim_time_s * i / n_bins
            t_out = round(edge_s / divisor, 4)
            counts = {s: 0 for s in states}
            counts_by_line = {lid: {s: 0 for s in states} for lid in line_ids}
            for card in kenv.card_registry.values():
                current_state = None
                for state, t in card.transitions:
                    if t > edge_s:
                        break
                    current_state = state
                if current_state is not None:
                    counts[current_state] = counts.get(current_state, 0) + 1
                    lid = getattr(card, "line_id", None)
                    if lid in counts_by_line:
                        counts_by_line[lid][current_state] += 1
            series.append({"t": t_out, **counts})
            for lid in line_ids:
                series_by_line[str(lid)].append({"t": t_out, **counts_by_line[lid]})

    # --- Shortfall: per-line, per-bin count of withdrawal requests that
    # were open-and-unmet ("customer asked, supermarket had 0") at that
    # instant, broken down per product (sachnummer) plus a "total" key —
    # same point shape as line_units_series' packages_series/
    # restmenge_series, so the frontend's existing per-line stacked-by-
    # product chart machinery (drawStackedQuantityChart /
    # participatingProducts) works unmodified. Falls back to an empty
    # (no-products, all-zero) series per line if shortfall_log is
    # omitted, so the frontend never has to special-case a missing key.
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
        "time_unit": time_unit,
        "horizon": round(sim_time_s / divisor, 4),
        "states": states,
        "cards": cards,
        "series": series,
        "series_by_line": series_by_line,
        "shortfall_by_line": shortfall_by_line,
        "shortfall_products_by_line": {
            str(lid): shortfall_products[lid] for lid in shortfall_line_ids
        },
        "lines": [
            {"line_id": lid, "line_name": line_names.get(lid, str(lid))}
            for lid in line_ids
        ],
    }


# ---------------------------------------------------------------------------
# Line Units — total physical units in stock per line, time-bucketed and
# broken out by product so it can drive a stacked-area chart. This is the
# data behind the dashboard's "Supermarkets" tab (previously "Line Units";
# the old raw-event "Supermarkets" tab / build_supermarket_timeseries()
# was removed as redundant with this — same underlying snapshot_log, but
# binned onto a shared time grid instead of irregular raw event points).
# ---------------------------------------------------------------------------

def build_line_units_timeseries(
    snapshot_log: "Iterable[SupermarketSnapshot]",
    sim_time_s: float,
    n_bins: int | None = None,
    time_unit: TimeUnit = "h",
) -> dict:
    """
    Aggregate the same raw snapshot_log build_supermarket_timeseries()
    consumes into two per-LINE, time-bucketed views, so the dashboard's
    Line Units tab can plot "quantity vs time" per line, split out by
    product, without the caller doing any of this grouping/binning
    itself:

      "packages_series"  — n_available (whole packages on the shelf),
                            summed across every product on the line
                            into a "total" key, plus each product's own
                            count for a stacked breakdown.
      "restmenge_series"  — pcs_partial (loose Restmenge pieces not yet
                             a full package), same per-product + total
                             shape.

    Why two series instead of one converted-to-units total: packages
    and Restmenge pieces are different quantities (whole packages vs.
    loose pcs) that don't sum meaningfully into one number without a
    batch_size multiply — the dashboard wants to see them as two
    separate "quantity vs time" charts, each still per-line and
    stacked-by-product like build_supermarket_timeseries() already
    shows per (line, product) pair.

    Both series are sampled onto the same n_bins+1-point (i = 0..n_bins,
    inclusive of an explicit t=0 sample) evenly-spaced time grid
    build_card_flow_payload() uses, so products on one line are directly
    stackable/summable at each point in time (the raw snapshot_log's
    event timestamps are irregular per product and can't be summed
    as-is). "Current state as of time t" = the last snapshot at or
    before t, same rule as build_card_flow_payload().

    The t=0 sample is what makes the starting stock visible at all: as
    long as the caller's snapshot_log was collected via
    kanban_process_logic.start_kanban_simulation() (which now takes one
    "initial" SupermarketSnapshot of every (line, product) Supermarket
    at t=0, before any withdrawal/production runs — see that function's
    docstring), _state_at() picks up that initial snapshot for the very
    first bin and the chart genuinely starts at e.g. 32 cards on HTL3,
    not at whatever count the first withdrawal left behind.

    Unlike cards, a (line, product) with no snapshot yet at or before t
    reports 0 for both fields rather than being omitted — an
    as-yet-untouched supermarket genuinely holds nothing yet, and it
    needs to be a real 0 so it doesn't distort the "total" sum for that
    line.

    sim_time_s: pass kenv.env.now (same convention as
    build_card_flow_payload's kenv.env.now read) — this function takes
    it as a plain float instead of kenv itself so it can keep operating
    on just the snapshot_log, like build_supermarket_timeseries().

    n_bins: if None (default), derived from sim_time_s for a ~15
    real-world-minute bin width, same reasoning and same fix as in
    build_card_flow_payload — see that function's docstring. Keeping
    both builders' default bin width in sync also matters because the
    dashboard's day tabs pull the "full" horizon from the Gantt payload
    and slice both of these series against it.
    """
    divisor = _DIVISORS[time_unit]
    if n_bins is None:
        n_bins = max(1, round(sim_time_s / 600.0))

    grouped: dict[tuple[str, str], list["SupermarketSnapshot"]] = {}
    for snap in snapshot_log:
        grouped.setdefault((snap.line_name, snap.product_type), []).append(snap)
    for snaps in grouped.values():
        snaps.sort(key=lambda s: s.t)

    products_by_line: dict[str, list[str]] = {}
    for (line_name, product_type) in grouped:
        products_by_line.setdefault(line_name, []).append(product_type)
    for products in products_by_line.values():
        products.sort()

    def _state_at(snaps: list["SupermarketSnapshot"], edge_s: float) -> tuple[int, int]:
        """(n_available, pcs_partial) as of edge_s, using the last
        snapshot at or before edge_s; (0, 0) if none yet."""
        n_available = 0
        pcs_partial = 0
        for s in snaps:
            if s.t > edge_s:
                break
            n_available, pcs_partial = s.n_available, s.pcs_partial
        return n_available, pcs_partial

    lines: list[dict] = []
    for line_name in sorted(products_by_line):
        products = products_by_line[line_name]
        packages_series: list[dict] = []
        restmenge_series: list[dict] = []
        if n_bins > 0:
            for i in range(0, n_bins + 1):
                edge_s = sim_time_s * i / n_bins
                t = round(edge_s / divisor, 4)
                pkg_point: dict = {"t": t}
                rm_point: dict = {"t": t}
                pkg_total = 0
                rm_total = 0
                for product_type in products:
                    n_available, pcs_partial = _state_at(
                        grouped[(line_name, product_type)], edge_s
                    )
                    pkg_point[product_type] = n_available
                    rm_point[product_type] = pcs_partial
                    pkg_total += n_available
                    rm_total += pcs_partial
                pkg_point["total"] = pkg_total
                rm_point["total"] = rm_total
                packages_series.append(pkg_point)
                restmenge_series.append(rm_point)
        lines.append({
            "line": line_name,
            "products": products,
            "packages_series": packages_series,
            "restmenge_series": restmenge_series,
        })

    return {
        "time_unit": time_unit,
        "horizon": round(sim_time_s / divisor, 4),
        "lines": lines,
    }


# ---------------------------------------------------------------------------
# Debug / export helpers — same convenience shape as schedule_events.py
# ---------------------------------------------------------------------------

def snapshot_log_to_dicts(snapshot_log: "Iterable[SupermarketSnapshot]") -> list[dict]:
    """Flat JSON-serialisable dump of the raw snapshot log (audit trail /
    debugging), mirroring schedule_events.event_log_to_dicts()."""
    return [asdict(s) for s in snapshot_log]


def shortfall_log_to_dicts(shortfall_log: "Iterable[ShortfallEvent]") -> list[dict]:
    """Flat JSON-serialisable dump of the raw shortfall log (audit trail /
    debugging) — one row per withdrawal request that had to wait,
    mirroring snapshot_log_to_dicts()."""
    return [asdict(e) for e in shortfall_log]


def dump_kanban_events_json(
    kenv: "KanbanSimEnvironment",
    snapshot_log: "Iterable[SupermarketSnapshot]",
    path: str,
    n_bins: int | None = None,
    time_unit: TimeUnit = "h",
    shortfall_log: "Iterable[ShortfallEvent] | None" = None,
) -> None:
    """Convenience: write all payloads straight to one JSON file, keyed
    "card_flow" / "line_units" — mirrors schedule_events.dump_gantt_json.
    ("line_units" is the binned per-line stock timeseries behind the
    dashboard's "Supermarkets" tab; the raw-event build_supermarket_
    timeseries() this used to also dump was removed as redundant.)
    shortfall_log, if passed, is folded into card_flow's
    "shortfall_by_line" key — see build_card_flow_payload's docstring."""
    payload = {
        "card_flow": build_card_flow_payload(
            kenv, n_bins=n_bins, time_unit=time_unit, shortfall_log=shortfall_log,
        ),
        "line_units": build_line_units_timeseries(
            snapshot_log, kenv.env.now, n_bins=n_bins, time_unit=time_unit
        ),
    }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)
