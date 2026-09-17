"""
telemetry/artifacts.py
=======================
RunArtifacts: a typed container gathering every log a mixed run produces
(event_log, snapshot_log, daily_log, shortfall_log, gate_activity_log,
exotic_snapshot_log, push_chute_log, oee_draw_log,
supermarket_overflow_log, push_delivery_log) into one object, instead of
each living as a separate loose attribute on kenv.

Two ways to use it:
  - Construct empty at the start of a run and hand its lists to whatever
    populates them (telemetry.recorder.Recorder, telemetry.packaging
    .PackageTracker, the various sim.fill trackers) — a caller-owned-list
    pattern, gathered in one place instead of scattered variables.
  - `RunArtifacts.from_kenv(kenv)` after a run, to pull the same logs
    back off a kenv whose attributes were populated directly, for call
    sites not yet migrated to constructing RunArtifacts up front. This is
    the "adapter over kenv" role.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from telemetry.records import (
    ExoticSlotSnapshot,
    GateActivityEntry,
    OEELossDrawEntry,
    PushChuteLogEntry,
    PushDeliveryRecord,
    ScheduleEvent,
    ShortfallEvent,
    SupermarketOverflowFlag,
    SupermarketSnapshot,
)


@dataclass
class RunArtifacts:
    event_log: list[ScheduleEvent] = field(default_factory=list)
    snapshot_log: list[SupermarketSnapshot] = field(default_factory=list)
    shortfall_log: list[ShortfallEvent] = field(default_factory=list)
    daily_log: list[dict] = field(default_factory=list)
    gate_activity_log: list[GateActivityEntry] = field(default_factory=list)
    exotic_snapshot_log: list[ExoticSlotSnapshot] = field(default_factory=list)
    push_chute_log: list[PushChuteLogEntry] = field(default_factory=list)
    oee_draw_log: list[OEELossDrawEntry] = field(default_factory=list)
    supermarket_overflow_log: list[SupermarketOverflowFlag] = field(default_factory=list)
    push_delivery_log: list[PushDeliveryRecord] = field(default_factory=list)

    @classmethod
    def from_kenv(cls, kenv) -> "RunArtifacts":
        """Pull the same logs back off a kenv built the old
        attribute-bolting way — for call sites not yet migrated to
        constructing RunArtifacts up front and handing its lists in."""
        return cls(
            event_log=getattr(kenv, "event_log", None) or [],
            snapshot_log=getattr(kenv, "snapshot_log", None) or [],
            shortfall_log=getattr(kenv, "shortfall_log", None) or [],
            daily_log=getattr(kenv, "daily_log", None) or [],
            gate_activity_log=getattr(kenv, "gate_activity_log", None) or [],
            exotic_snapshot_log=getattr(kenv, "exotic_snapshot_log", None) or [],
            push_chute_log=getattr(kenv, "push_chute_log", None) or [],
            oee_draw_log=getattr(kenv, "oee_draw_log", None) or [],
            supermarket_overflow_log=getattr(kenv, "supermarket_overflow_log", None) or [],
            push_delivery_log=getattr(kenv, "push_delivery_log", None) or [],
        )
