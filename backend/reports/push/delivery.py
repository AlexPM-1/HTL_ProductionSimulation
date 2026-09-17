"""
reports/push/delivery.py
==========================
Push delivery-performance summary — push_delivery_summary(). Reads
telemetry.records.PushDeliveryRecord (imported for the type hint only).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from telemetry.records import PushDeliveryRecord


def push_delivery_summary(log: list["PushDeliveryRecord"]) -> dict:
    """
    Aggregate on-time-delivery stats from a push_delivery_log — a small,
    ready-to-render dict for a frontend KPI summary card. Not a
    replacement for graphing the raw log (a frontend should still plot
    delivery_delta_h over time / by product for a distribution or trend
    view) — this is just the "one glance" numbers.
    """
    deltas = [r.delivery_delta_h for r in log if r.delivery_delta_h is not None]
    n_late = sum(1 for d in deltas if d > 0)
    return {
        "n_delivered": len(log),
        "n_rush": sum(1 for r in log if r.rush),
        "n_with_due_date": len(deltas),
        "n_late": n_late,
        "n_on_time_or_early": len(deltas) - n_late,
        "avg_delta_h": (sum(deltas) / len(deltas)) if deltas else None,
        "max_late_h": max(deltas) if deltas else None,
        "max_early_h": min(deltas) if deltas else None,
    }
