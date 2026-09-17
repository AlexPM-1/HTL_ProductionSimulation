"""
reports/all/supermarket_series.py
====================================
Two related time-bucketed Supermarket views, kept in one module because
they read the same two logs (kenv.snapshot_log grouped by (line_name,
product_type), kenv.exotic_snapshot_log) and share the same bin-edge
helpers from reports/binning.py:

  - build_line_units_timeseries()      per-line, per-product
    packages/restmenge over time — the Pull tab's Supermarkets view.
  - build_supermarket_state_payload()  per-physical-row occupancy
    (Main-runner + Exotic) — the "All Production" tab's Supermarkets
    view.

The two functions stay separate (different callers, different return
shapes): reports/pull/* and the dashboard's Pull tab use
build_line_units_timeseries(); the "All Production" page uses
build_supermarket_state_payload().
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Iterable, Literal, Optional

from reports.binning import TIME_UNIT_DIVISORS, state_at as _sm_state_at
from reports.push.exotic_occupancy import _exotic_products_at

if TYPE_CHECKING:
    from domain.config import SimConfig
    from telemetry.records import SupermarketSnapshot

TimeUnit = Literal["h", "min", "s"]

_DIVISORS = TIME_UNIT_DIVISORS


# ---------------------------------------------------------------------------
# Line Units — per-line, per-product packages/Restmenge over time (Pull tab)
# ---------------------------------------------------------------------------

def build_line_units_timeseries(
    snapshot_log: "Iterable[SupermarketSnapshot]",
    sim_time_s: float,
    n_bins: int | None = None,
    time_unit: TimeUnit = "h",
) -> dict:
    """
    Aggregate snapshot_log into two per-LINE, time-bucketed views:
      "packages_series"  — n_available (whole packages on the shelf),
                            summed into a "total" key, plus each
                            product's own count for a stacked breakdown.
      "restmenge_series"  — pcs_partial (loose Restmenge pieces), same
                             per-product + total shape.

    Both series are sampled onto an n_bins+1-point (i = 0..n_bins,
    inclusive of an explicit t=0 sample) evenly-spaced time grid.
    "Current state as of time t" = the last snapshot at or before t.

    Unlike cards (reports.pull.card_flow), a (line, product) with no
    snapshot yet at or before t reports 0 for both fields rather than
    being omitted.

    n_bins: if None (default), derived from sim_time_s for a ~15
    real-world-minute bin width — same formula as
    reports.pull.card_flow.build_card_flow_payload.
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
# Supermarket state — per physical Supermarket row (Main runner + Exotic),
# time-bucketed, for the "All Production" tab's Supermarkets view.
# ---------------------------------------------------------------------------

def build_supermarket_state_payload(
    kenv, cfg: "SimConfig", n_bins: Optional[int] = None, time_unit: str = "h",
) -> dict:
    """
    Time-bucketed, per-physical-row Supermarket occupancy for the "All
    Production" Supermarkets view. Two kinds of row, plus two synthetic
    extras:

    1. "Main runner" rows — several physical rows can share the same
       (line, sachnummer); each row's series is the shared group's time
       series run through a deterministic first-fill distribution across
       the group's rows in row_number order, at every bin.
    2. "Exotic" rows — one entry per physical row, with a REAL per-row
       time series built from kenv.exotic_snapshot_log.
    3. "restmenge_series" (per line) — Main-runner groups' pcs_partial
       over time.
    4. "exotic_routed_products" (per line) — Kanban-eligible products
       with no dedicated Main-runner row on this line, routed through
       the line's shared Exotic pool instead.
    """
    divisor = _DIVISORS[time_unit]
    sim_time_s = kenv.env.now
    if n_bins is None:
        n_bins = max(1, round(sim_time_s / 600.0))

    line_id_by_name: dict[str, int] = {line.line_name: line.line_id for line in kenv.lines}
    kenv_supermarkets: dict = getattr(kenv, "supermarkets", None) or {}

    main_groups: dict[tuple[str, str], list] = {}
    for snap in (getattr(kenv, "snapshot_log", None) or []):
        main_groups.setdefault((snap.line_name, snap.product_type), []).append(snap)
    for snaps in main_groups.values():
        snaps.sort(key=lambda s: s.t)

    exotic_groups: dict[tuple[str, int], list] = {}
    for snap in (getattr(kenv, "exotic_snapshot_log", None) or []):
        exotic_groups.setdefault((snap.line, snap.row_number), []).append(snap)
    for snaps in exotic_groups.values():
        snaps.sort(key=lambda s: s.t)

    edges = [sim_time_s * i / n_bins for i in range(n_bins + 1)] if n_bins > 0 else []

    lines_out = []
    for line_name, slot_cfgs in (cfg.supermarkets or {}).items():
        line_id = line_id_by_name.get(line_name)
        supermarket_lane = kenv_supermarkets.get(line_id, {}) if line_id is not None else {}

        main_runner_groups: dict[str, list] = {}
        for slot in slot_cfgs:
            if slot.is_exotic or not slot.sachnummer:
                continue
            main_runner_groups.setdefault(slot.sachnummer, []).append(slot)
        for rows in main_runner_groups.values():
            rows.sort(key=lambda s: s.row_number)

        rows_out = []
        for slot in sorted(slot_cfgs, key=lambda s: s.row_number):
            if slot.is_exotic:
                snaps = exotic_groups.get((line_name, slot.row_number), [])
                series = [
                    {
                        "t": round(edge_s / divisor, 4),
                        "sachnummer": products[0]["sachnummer"] if products else None,
                        "n_cards": sum(p["push_count"] for p in products),
                        "products": products,
                    }
                    for edge_s in edges
                    for products in [_exotic_products_at(snaps, edge_s)]
                ]
                rows_out.append({
                    "row_number": slot.row_number,
                    "type": "Exotic",
                    "is_exotic": True,
                    "capacity": slot.capacity,
                    "series": series,
                })
            else:
                group_rows = main_runner_groups.get(slot.sachnummer, [slot])
                snaps = main_groups.get((line_name, slot.sachnummer), [])
                series = []
                for edge_s in edges:
                    n_available, pcs_partial = _sm_state_at(snaps, edge_s, ("n_available", "pcs_partial"))
                    remaining = n_available
                    row_available = 0
                    for s in group_rows:
                        take = min(s.capacity, remaining)
                        if s.row_number == slot.row_number:
                            row_available = take
                        remaining -= take
                    series.append({
                        "t": round(edge_s / divisor, 4),
                        "n_cards": row_available,
                        "group_n_available": n_available,
                        "pcs_partial": pcs_partial,
                    })
                rows_out.append({
                    "row_number": slot.row_number,
                    "type": slot.slot_type or "Main runner",
                    "is_exotic": False,
                    "sachnummer": slot.sachnummer,
                    "capacity": slot.capacity,
                    "group_capacity": sum(s.capacity for s in group_rows),
                    "shared_rows": [s.row_number for s in group_rows],
                    "batch_size": getattr(supermarket_lane.get(slot.sachnummer), "batch_size", None),
                    "series": series,
                })

        line_products = sorted(main_runner_groups.keys())
        restmenge_series = []
        for edge_s in edges:
            pt = {"t": round(edge_s / divisor, 4)}
            total = 0
            for product_type in line_products:
                snaps = main_groups.get((line_name, product_type), [])
                _, pcs_partial = _sm_state_at(snaps, edge_s, ("n_available", "pcs_partial"))
                pt[product_type] = pcs_partial
                total += pcs_partial
            pt["total"] = total
            restmenge_series.append(pt)

        exotic_routed_products = []
        for sachnr, sm in sorted(supermarket_lane.items()):
            if not getattr(sm, "is_exotic_routed", False):
                continue
            snaps = main_groups.get((line_name, sachnr), [])
            series = []
            for edge_s in edges:
                n_available, pcs_partial = _sm_state_at(snaps, edge_s, ("n_available", "pcs_partial"))
                series.append({"t": round(edge_s / divisor, 4), "n_cards": n_available, "pcs_partial": pcs_partial})
            exotic_routed_products.append({
                "sachnummer": sachnr,
                "capacity": sm.capacity,
                "batch_size": sm.batch_size,
                "series": series,
            })

        lines_out.append({
            "line_name": line_name,
            "rows": rows_out,
            "restmenge_series": restmenge_series,
            "restmenge_products": line_products,
            "exotic_routed_products": exotic_routed_products,
        })

    return {
        "time_unit": time_unit,
        "horizon": round(sim_time_s / divisor, 4),
        "lines": lines_out,
    }
