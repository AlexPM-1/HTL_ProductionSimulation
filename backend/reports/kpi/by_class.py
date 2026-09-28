"""
reports/kpi/by_class.py
=========================
Pull-vs-push (class-1/class-2) KPI split: build_class_product_sets(),
class_split_kpi() and class_filter_products(). Kept separate from
reports/kpi/by_line.py's line_kpi_summary() since station/buffer stats
are shared physical resources with no per-class meaning — only the
part-level counts (passed/scrapped/reworked/mean_cycle_time_s) are
worth splitting by class.
"""

from __future__ import annotations

from typing import Optional

from domain.config import SimConfig


def build_class_product_sets(cfg: SimConfig) -> tuple[set[str], set[str]]:
    """
    (pull_products, push_products) — the product-identifier sets that
    define class-1 vs class-2 for KPI bucketing.

    pull_products: { evt.product for evt in cfg.pull_customer_demand } —
    the PullCustomerDemand sheet is a flat long table, one
    PullCustomerDemand row per withdrawal row (Date | Time | Product |
    TotalQuantity | LineId). Only rows with quantity > 0 are kept upstream,
    so every event.product seen here is a real class-1 product.

    push_products: { d.product_id for d in cfg.demand } (PushCustomerDemand
    rows — one row per (Date, Product)).

    Per the spec these two sets should be disjoint (a product runs as
    EITHER pull or push, not both), but the sheets are user-edited by
    hand — callers should not assume disjointness; this function does not
    enforce or silently fix an overlap, it just returns what's there.
    """
    pull_products: set[str] = {
        evt.product for evt in (getattr(cfg, "pull_customer_demand", []) or [])
    }

    push_products: set[str] = {
        d.product_id for d in (getattr(cfg, "demand", []) or [])
    }
    return pull_products, push_products


def class_split_kpi(
    menv, line, pull_products: set[str], push_products: set[str],
) -> dict:
    """
    Per-line pull vs push KPI bucket, computed by filtering
    menv.parts_out (already restricted to this line) on product_type
    membership. Mirrors the passed/scrapped/reworked/mean_cycle_time_s
    fields of reports.kpi.by_line.line_kpi_summary() but does NOT
    duplicate station_utilisation / buffer_max_fill / total_created —
    those are combined-only (see reports/kpi/by_line.py).
    """
    parts_out = [p for p in menv.parts_out if p.line_id == line.line_id]

    def _bucket(products: set[str]) -> dict:
        subset = [p for p in parts_out if p.product_type in products]
        passed = [p for p in subset if p.status == "passed"]
        scrapped = [p for p in subset if p.status == "scrapped"]
        reworked = [p for p in subset if p.rework_pass > 0]
        mean_ct = (
            sum(p.cycle_time for p in passed) / len(passed) if passed else None
        )
        return {
            "passed": len(passed),
            "scrapped": len(scrapped),
            "reworked": len(reworked),
            "mean_cycle_time_s": mean_ct,
        }

    unclassified = [
        p for p in parts_out
        if p.product_type not in pull_products and p.product_type not in push_products
    ]

    return {
        "pull": _bucket(pull_products),
        "push": _bucket(push_products),
        "unclassified_count": len(unclassified),  # flags sheet overlap/gaps, see docstring above
    }


def class_filter_products(cfg: SimConfig, cls: Optional[str]) -> Optional[set[str]]:
    """
    cls: 'pull' | 'push' | None/'all'. Returns the product_type set to
    KEEP, or None for no filtering. Raises ValueError for anything else —
    callers in api/ translate that to HTTPException(400, ...), same
    behaviour as the old inline `raise HTTPException(...)` here, just
    moved to the caller since reports/ must not import fastapi.
    """
    if cls is None or cls == "all":
        return None
    pull_products, push_products = build_class_product_sets(cfg)
    if cls == "pull":
        return pull_products
    if cls == "push":
        return push_products
    raise ValueError("cls must be one of: pull, push, all")


# Private aliases for call sites that import the underscore-prefixed names.
_build_class_product_sets = build_class_product_sets
_class_split_kpi = class_split_kpi
_class_filter_products = class_filter_products
