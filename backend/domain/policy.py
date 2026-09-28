"""
domain/policy.py
=================
Editable push/frozen-zone policy knobs (`PushPolicyConfig`), consumed by
sim.fill.push.dispatch and exposed to the frontend via
api.routes_policy.get_push_policy / patch_push_policy.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class PushPolicyConfig:
    """
    Editable policy knobs for class-2 (push / "exoten") production.
    Plain dataclass, deliberately dependency-free (no SimPy/Excel types)
    so it's trivial to construct from — or serialize to — a frontend
    request (see to_dict/from_dict).

    Card/hour equivalence
    ----------------------
    One "card" = one push card = PUSH_CARD_SIZE pieces (200, today) =
    `card_production_time_min` minutes of line time (30 min, today) — the
    SAME unit both class-1 pull cards and class-2 push cards are
    measured in, which is what lets a frozen zone sized in cards mean the
    same thing to both classes on a shared line queue. See
    frozen_zone_hours for the derived, informational hour figure — the
    canonical/edited value is `frozen_zone_cards`, per the "it will be set
    according to cards" instruction; frozen_zone_hours is computed FROM
    it, not stored independently, so the two can never drift apart.

    Attributes
    ----------
    frozen_zone_cards       : how many cards'-worth of imminent work at
                              the front of a line's queue is locked
                              (unreorderable / unremovable) at any given
                              moment. Default 8 (≈ 4h at 30 min/card).
    card_production_time_min : minutes of line time one card
                              represents. Default 30 — used only to
                              derive frozen_zone_hours; NOT the same
                              thing as a station's own cycle time, which
                              can vary by product/line (this is a fixed
                              planning/queueing unit, not a physics
                              measurement).
    push_visibility_days    : how many days of PushCustomerDemand rows
                              are considered "visible" for scheduling at
                              any given moment (a rolling window, not a
                              one-shot read of the whole sheet). Default 3.
    ideal_lead_time_h       : target: finish/deliver a push order this
                              many hours BEFORE its due date. Default 5.
    max_lead_time_h         : outer bound: never start pursuing an order
                              earlier than this many hours before its due
                              date (delivering much earlier than needed
                              just occupies exotic supermarket slots for
                              no benefit). Default 24. Must be >=
                              ideal_lead_time_h.
    rush_threshold_h        : once a push order is within this many hours
                              of its due date and still not
                              placed/finished, the normal "try
                              highest-priority compatible line, else next
                              compatible line, else wait and retry" rule
                              (below) is overridden: force-assign to
                              whichever compatible line is currently
                              least busy, and insert it right before that
                              line's frozen zone (i.e. as the very next
                              thing to run once whatever's currently
                              frozen finishes). Default 12.
    retry_interval_h        : when every compatible line is busy (and the
                              order is not yet inside rush_threshold_h),
                              how long to wait before re-trying placement.
                              Default 1.
    """
    frozen_zone_cards: int = 8
    card_production_time_min: float = 30.0
    push_visibility_days: int = 3
    ideal_lead_time_h: float = 5.0
    max_lead_time_h: float = 24.0
    rush_threshold_h: float = 12.0
    retry_interval_h: float = 1.0

    def __post_init__(self) -> None:
        if self.max_lead_time_h < self.ideal_lead_time_h:
            raise ValueError(
                f"max_lead_time_h ({self.max_lead_time_h}) must be >= "
                f"ideal_lead_time_h ({self.ideal_lead_time_h})"
            )
        if self.frozen_zone_cards < 0:
            raise ValueError("frozen_zone_cards must be >= 0")

    @property
    def frozen_zone_hours(self) -> float:
        """Informational: frozen_zone_cards expressed in hours of line time."""
        return self.frozen_zone_cards * self.card_production_time_min / 60.0

    def to_dict(self) -> dict:
        """JSON-serializable form, for a frontend settings panel."""
        return {
            "frozen_zone_cards": self.frozen_zone_cards,
            "card_production_time_min": self.card_production_time_min,
            "push_visibility_days": self.push_visibility_days,
            "ideal_lead_time_h": self.ideal_lead_time_h,
            "max_lead_time_h": self.max_lead_time_h,
            "rush_threshold_h": self.rush_threshold_h,
            "retry_interval_h": self.retry_interval_h,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "PushPolicyConfig":
        """
        Build from a (possibly partial) dict, e.g. a frontend PATCH body —
        unspecified keys keep this class's normal defaults.
        """
        known = {f: d[f] for f in cls.__dataclass_fields__ if f in d}
        return cls(**known)
