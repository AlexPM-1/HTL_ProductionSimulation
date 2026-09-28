"""
telemetry/records.py
=====================
The timestamped-record dataclasses the simulation logs into as it runs.
Each is written by a specific recorder/tracker and read back by one or
more report builders:

  ScheduleEvent           written by telemetry.packaging.PackageTracker
                          and sim.produce.changeover; read by
                          reports.all.gantt and reports.serialization.
  SupermarketSnapshot     written by telemetry.recorder.Recorder
                          .record_supermarket(); read by
                          reports.all.supermarket_series.
  ShortfallEvent          written by telemetry.recorder.Recorder
                          .record_shortfall(); read by reports.pull.shortfall.
  GateActivityEntry       written by sim.drain.crew.crew_process; read by
                          reports.kpi.by_crew and api/legacy.py.
  ExoticSlotSnapshot      written by sim.fill.push.exotic
                          .ExoticSupermarketTracker; read by
                          reports.movement / push-side supermarket reports.
  PushChuteLogEntry       written by sim.fill.push.chute_tracker
                          .PushChuteTracker; read by reports.movement.chute.
  OEELossDrawEntry        written by sim.oee's daily OEE process; read by
                          OEE-related reports.
  SupermarketOverflowFlag written by sim.fill.push.exotic; read by
                          api/legacy.py's supermarket_overflow_log.
  PushDeliveryRecord      written wherever a push_card's delivered_date
                          is stamped (sim.fill.push); read by
                          reports.push.delivery.
"""

from __future__ import annotations

import datetime as _dt
from dataclasses import dataclass, field
from typing import Literal, Optional

EventType = Literal["setup", "job"]
ProductionType = Literal["pull", "push"]


# ---------------------------------------------------------------------------
# ScheduleEvent — one rectangle's worth of Gantt data.
# ---------------------------------------------------------------------------

@dataclass
class ScheduleEvent:
    """
    One timestamped segment on one production line.

    Attributes
    ----------
    line_name       : "HTL3" | "HTL5" | "HTL6"
    line_id         : 1-based line index (matches run_line's line_id)
    event_type      : "setup" (changeover) or "job" (package produced)
    start_s         : simulation start time, seconds
    end_s           : simulation end time, seconds
    product_number      : product identifier (job events; "from->to" not stored
                       here — see from_product_number/to_product_number for setup)
    kunde           : customer / market designation (job events only)
    product_class   : "Stift" | "DRS" | "Stab_Lochfilter" | etc. (job events)
    package_size    : nominal pieces per package (job events only)
    units_in_segment: actual pieces produced in this segment
                       (== package_size, except a possible partial final
                       package at the end of an order)
    n_workers       : changeover crew size (setup events only)
    from_product_number : outgoing product (setup events only)
    to_product_number   : incoming product (setup events only)
    note            : free-text (e.g. setup-time lookup source, or a
                       warning when TTNr data was missing)
    production_type : "pull" (class-1 Kanban) or "push" (class-2), or None
                       for callers that don't know/care about the
                       pull-vs-push distinction. Populated AFTER
                       construction by sim.runner.run_mixed, the only
                       caller that knows which class produced the event —
                       tagged post-hoc rather than threaded through the
                       constructor.
    crew_id         : 0-based id of the crew_process(crew_id=...) instance
                       that was holding this line's gate for this segment,
                       or None for callers that don't run the crew-based
                       depletion model at all, or for a "job" segment the
                       caller hasn't back-filled yet. Same post-hoc-
                       population idiom as production_type above.
    order_id        : domain.orders.OrderRecord.order_id of the order this
                       segment was worked on for.
                       For a "setup" segment this is the INCOMING order's id
                       is only ever handed incoming_rec's order — 
                       the outgoing/previous order isn't tracked here. 
                       For a "job" segment it's whichever
                       order the caller building that segment set it to.
                       None for any caller that doesn't have an OrderRecord
                       to hand (e.g. no order context at all).
    """
    line_name:        str
    line_id:           int
    event_type:        EventType
    start_s:            float
    end_s:              float
    product_number:         Optional[str] = None
    kunde:              Optional[str] = None
    product_class:      Optional[str] = None
    package_size:       Optional[int] = None
    units_in_segment:   Optional[int] = None
    n_workers:          Optional[int] = None
    from_product_number:    Optional[str] = None
    to_product_number:      Optional[str] = None
    note:               Optional[str] = None
    production_type:    Optional[ProductionType] = None
    crew_id:            Optional[int] = None
    order_id:           Optional[int] = None

    @property
    def duration_s(self) -> float:
        return round(self.end_s - self.start_s, 3)

    @property
    def duration_h(self) -> float:
        return self.duration_s / 3600.0

    @property
    def throughput_pcs_per_h(self) -> Optional[float]:
        """Pieces per hour for a 'job' segment (None for setup/zero-duration)."""
        if self.event_type != "job" or self.units_in_segment is None:
            return None
        if self.duration_h <= 0:
            return None
        return round(self.units_in_segment / self.duration_h, 1)

    def __repr__(self) -> str:
        tag = f" <{self.production_type}>" if self.production_type else ""
        crew_tag = f" crew{self.crew_id}" if self.crew_id is not None else ""
        if self.event_type == "job":
            return (
                f"ScheduleEvent(job{tag}{crew_tag} {self.product_number!r} on {self.line_name} "
                f"[{self.start_s:.1f}, {self.end_s:.1f}]s  "
                f"{self.units_in_segment}/{self.package_size} pcs  "
                f"rate={self.throughput_pcs_per_h} pcs/h)"
            )
        return (
            f"ScheduleEvent(setup{tag}{crew_tag} {self.from_product_number!r}->{self.to_product_number!r} "
            f"on {self.line_name} [{self.start_s:.1f}, {self.end_s:.1f}]s "
            f"{self.n_workers}MA)"
        )


# ---------------------------------------------------------------------------
# SupermarketSnapshot — one stock-level observation.
# ---------------------------------------------------------------------------

@dataclass
class SupermarketSnapshot:
    """
    One timestamped observation of a Supermarket's stock level, recorded
    every time that Supermarket's state actually changes.

    event_type is one of "withdrawal" | "deposit_batch" | "initial" —
    see telemetry.recorder.Recorder for the
    call sites that emit each. n_available / pcs_partial are read
    straight off SupermarketResource AFTER the triggering call, so
    they're always the authoritative post-event state, not a delta.

    delta_qty : signed change in n_available caused by this event, e.g.
                -8 for a withdrawal, +20 for a deposit. Computed by
                Recorder.record_supermarket() from the previous snapshot
                for this (line_id, product_type) — callers never need to
                pass it. None only for the very first "initial" snapshot
                of a run (nothing to diff against yet).
    kanban_card_id : identifier of the specific Kanban card this event is
                associated with (the card being withdrawn, the card a
                completed batch is being deposited against, or the card
                being released to the chute), if the call site has one
                to give. None for events with no single associated card
                (e.g. an "initial" snapshot).
    """
    t:              float
    line_id:        int
    line_name:      str
    product_type:   str
    event_type:     str   # "withdrawal" | "deposit_batch" | "initial"
    n_available:    int
    pcs_partial:    int
    card_size:     int
    delta_qty:      Optional[int] = None
    kanban_card_id: Optional[str] = None


# ---------------------------------------------------------------------------
# ShortfallEvent — one "customer asked, supermarket had none" interval.
# ---------------------------------------------------------------------------

@dataclass
class ShortfallEvent:
    """
    One interval during which a Kanban withdrawal request found its
    line's Supermarket for that product completely empty (n_available
    == 0 at the exact instant the request was made) and had to sit
    blocked until the next batch was produced/deposited before it could
    be granted.

    start_s : env.now the instant the request checked stock and found
              none — i.e. "the customer asked for a card".
    end_s   : env.now the instant the request actually resolved — a
              batch became available and was handed to it.

    Recorded ONLY for requests that genuinely had to wait; a request that
    finds stock already on the shelf never produces one of these.
    """
    line_id:        int
    line_name:      str
    product_type:   str
    start_s:        float
    end_s:          float


# ---------------------------------------------------------------------------
# GateActivityEntry — one completed line-gate hold.
# ---------------------------------------------------------------------------

@dataclass
class GateActivityEntry:
    """
    One completed hold of a line's LinePriorityGate — i.e. one contiguous
    interval during which *something* (a pull card or a push card,
    whichever a crew was running) actually occupied the line's stations,
    start to finish. Appended once per hold, AFTER it ends.

    possible_changeover: True if this entry's product_number differs from
    whatever the gate's current_rec was immediately before this hold
    started (i.e. a changeover() call was very likely made somewhere
    inside this interval). This is a coarse, whole-interval flag, NOT a
    resolved sub-boundary.

    production_type : "pull" | "push" — named to match
        domain.orders.OrderRecord.production_type and
        ScheduleEvent.production_type.
    """
    t_start: float
    t_end: float
    line_id: int
    product_number: str
    production_type: str        # "pull" | "push"
    crew_id: Optional[int] = None   # which crew_process(crew_id=...) ran
                                     # this unit; None only for entries
                                     # from before this field existed.
    possible_changeover: bool = False
    quantity: Optional[int] = None


# ---------------------------------------------------------------------------
# ExoticSlotSnapshot — one timestamped exotic-slot occupancy reading.
# ---------------------------------------------------------------------------

@dataclass
class ExoticSlotSnapshot:
    """
    One timestamped reading of a single physical Exotic slot's occupancy
    — the Exotic-side counterpart to SupermarketSnapshot (which only ever
    covers Main-runner (pull) product groups, never Exotic rows).

    `occupants` is the full multi-product breakdown at t — one
    {"product_number", "n_cards"} entry per product simultaneously occupying
    the row (a row that hasn't fully emptied before a different product
    starts filling it legitimately holds more than one at once).
    `occupant`/`n_cards` are kept alongside it purely for back-compat with
    readers that haven't been updated to consume `occupants` yet —
    `occupant` is the (deterministically) first product, or None if
    empty, and `n_cards` is the SUM across every product. New code should
    read `occupants` instead.
    """
    t: float
    line: str
    row_number: int
    capacity_cards: int
    occupant: Optional[str]
    n_cards: int
    occupants: list[dict] = field(default_factory=list)


# ---------------------------------------------------------------------------
# PushChuteLogEntry — one push-side chute admission/removal event.
# ---------------------------------------------------------------------------

@dataclass
class PushChuteLogEntry:
    """
    One PUSH-side Chute admission ("deposit") or removal ("drain") event
    — the push-side analogue of ExoticSlotSnapshot, timestamped so a
    consumer can reconstruct which push cards were actually sitting in a
    line's shared Chute at any past instant, rather than only ever
    reporting the run's current live total.

    A "drain" event's entry_id names exactly which earlier "deposit"
    left the queue — always whichever entry was at the front at that
    moment.
    """
    t: float
    line: str
    kind: str                          # "deposit" | "drain"
    entry_id: int
    product_number: Optional[str] = None   # populated on deposit only
    rush: bool = False                 # populated on deposit only


# ---------------------------------------------------------------------------
# OEELossDrawEntry — one day's CombinedProductionLoss draw for one line.
# ---------------------------------------------------------------------------

@dataclass
class OEELossDrawEntry:
    """
    One day's CombinedProductionLoss draw for one line — a full history
    of every draw, not just "latest value", so a run can be inspected/
    reported on after the fact. Appended by sim.oee's daily OEE process
    (the caller of OEELossTracker.draw_for_day()) — never by
    OEELossTracker itself.
    """
    t: float
    day_index: int
    line: str
    combined_production_loss: float


# ---------------------------------------------------------------------------
# SupermarketOverflowFlag — one "exotic slot pool was already full" event.
# ---------------------------------------------------------------------------

@dataclass
class SupermarketOverflowFlag:
    """
    One "this exotic slot pool was already full" event — logged, not
    raised as an exception (no problem, raise a flag and continue).
    Intended for later supermarket-sizing analysis, not for stopping a
    simulation.
    """
    t: float
    line: str
    product_number: str
    reason: str


# ---------------------------------------------------------------------------
# PushDeliveryRecord — one completed push_card's delivery outcome.
# ---------------------------------------------------------------------------

@dataclass
class PushDeliveryRecord:
    """
    One completed push_card's delivery outcome — the flat, log-friendly
    artifact "due date vs. delivered date" KPI graphs and summaries are
    meant to read, rather than reaching into OrderRecord/push_card internals
    directly. Appended the same instant push_card.delivered_date is stamped
    on the underlying OrderRecord (both places always agree — this is a
    mirror, not a second source of truth).

    Attributes
    ----------
    product_number      : product
    assigned_line   : which line actually produced this push_card
    quantity        : pieces in this push_card (<= PUSH_CARD_SIZE)
    due_date        : from the originating PushCustomerDemand row
    delivered_date  : when this push_card's production finished
    delivery_delta_h : hours late (positive) or early (negative) —
                       mirrors OrderRecord.delivery_delta_h
    rush            : True if this push_card was force-placed under the
                      rush_threshold_h override rather than normally
                      ranked
    """
    product_number: str
    assigned_line: str
    quantity: int
    due_date: Optional[_dt.datetime]
    delivered_date: _dt.datetime
    delivery_delta_h: Optional[float]
    rush: bool
