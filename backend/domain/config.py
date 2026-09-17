"""
domain/config.py
=================
Single module for all configuration loading. Reads:
  • ProductionPlanning_v6.xlsx  — push-model sheets (Process, Buffers,
    Inspection, SetupTimes_Matrix, Packaging, SetupState, Inventories)
    plus Kanban/pull sheets (KanbanConfig, CustomerDemandKanban,
    KanbanCardsSetup, Supermarkets) and OEE/Shifts sheets — all Kanban,
    OEE, and Shifts sheets are optional/additive: each parser returns an
    empty dict/list (never raises) if its sheet is absent, so an older
    workbook without them still loads fine.
  • HTL_setup_times.xlsx         — TTNr-based changeover times, 1-MA / 2-MA;
    one sheet per line.

This module also owns the StationLine dataclass and LINE_STATIONS
catalogue (station parameters per line), consumed by domain.products for
routing/cycle-time resolution. Nothing in this file depends on SimPy.

CustomerDemand / CustomerDemandKanban sheet layout
---------------------------------------------------
Both sheets are flat, long event tables — one row per demand/withdrawal
event, parsed by the shared `_parse_long_demand_rows`:

    Date | Time | Product | TotalQuantity | LineId

"LineId" is a DOWNSTREAM line (whoever is requesting the product, e.g.
"Line 12") — it is NOT one of our production lines (HTL3/HTL5/HTL6).
Deciding which of our production line(s) actually makes each order is the
job of a LinePriorityStrategy (see domain.line_priority) driven by
PRODUCT_MATRIX's feasible-lines data.

Kanban / pull config
---------------------
  KanbanConfig          → SimConfig.kanban_timing   : KanbanTimingConfig
                          (withdrawal cadence + collection-box emptying
                          interval, both in minutes)
  KanbanCardsSetup      → SimConfig.kanban_cards     : dict[sachnummer -> KanbanCardConfig]
                          (BatchSize pcs/card + CardsToTrigger per product)
  CustomerDemandKanban  → SimConfig.kanban_withdrawals: list[KanbanWithdrawalEvent]
                          (one KanbanWithdrawalEvent per row; 0-qty rows
                          are dropped)
  Supermarkets          → SimConfig.supermarkets     : dict[line -> list[SupermarketSlotConfig]]
                          (full per-slot layout: Line | RowNumber | Type |
                          Capacity | Sachnummer | InitialState |
                          InitialPcsPartial. Shared by both Kanban
                          ("Main runner" slots, pull) and push production
                          ("Exotic" slots, no fixed product) — see
                          SupermarketSlotConfig docstring. The parser also
                          accepts the older "SupermInState" /
                          "SupermarketInitialState" sheet names for
                          backward compatibility.)

OEE config
----------
  OEE                   → SimConfig.oee_distribution : dict[line -> list[OEEBinConfig]]
                          (per-line discrete probability distribution over
                          OEE values: Line | Bin_down | Bin_up |
                          Probability, one row per bin. This module only
                          reads the table; the sampling logic that
                          consumes it lives in sim.oee.)

Line-number ↔ line-name mapping
--------------------------------
    1 → "HTL3"   (Stift_Einpressen + optional Lochfilter / DRS / Pruefen)
    2 → "HTL5"   (Stift_Einpressen, no optionals)
    3 → "HTL6"   (no Stift_Einpressen)

Station name normalisation
--------------------------
    "Washen"          → "Waschen"          (typo in workbook)
    "Pruefen Filter"  → "Pruefen"          (routing identifier)
    "Stift einpressen"→ "Stift_Einpressen" (spacing / case)
    SetupState aliases: Load / Polish / Wash / Visual Inspect → canonical names

Usage
-----
    from domain.config import load_config
    cfg = load_config(
        excel_path="ProductionPlanning_v6.xlsx",
        setup_xlsx_path="HTL_setup_times.xlsx",
        product_master_path="product_master.xlsx",
    )
    print(cfg.summary())
"""

from __future__ import annotations

import re
import sys
import datetime as _dt
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Optional

import pandas as pd


# ===========================================================================
# Constants & name maps
# ===========================================================================

DEFAULT_OEE: float = 0.95

LINE_NUMBER_TO_NAME: dict[int, str] = {1: "HTL3", 2: "HTL5", 3: "HTL6"}
LINE_NAME_TO_NUMBER: dict[str, int] = {v: k for k, v in LINE_NUMBER_TO_NAME.items()}

# Excel cell text → canonical station name  (normalise workbook quirks)
_STATION_NAME_MAP: dict[str, str] = {
    "Washen":           "Waschen",
    "Pruefen Filter":   "Pruefen",
    "Stift einpressen": "Stift_Einpressen",
    "Sichtpruefen":      "Sichtpruefung",  # "Inventories" sheet spelling
}

# SetupState sheet uses human-friendly aliases → canonical names
_SETUP_STATE_ALIAS: dict[str, str] = {
    "Load":           "Beladen",
    "Polish":         "Supfina",
    "Wash":           "Waschen",
    "Visual Inspect": "Sichtpruefung",
}

_DEFAULT_EXCEL: Path = Path(__file__).with_name("ProductionPlanning_v6.xlsx")


# ===========================================================================
# StationLine  — physical station on one line
# ===========================================================================

@dataclass
class StationLine:
    """
    Physical station parameters for one station on one production line.
    Consumed by domain.products for routing and cycle-time resolution.

    Attributes
    ----------
    line          : "HTL3" | "HTL5" | "HTL6"
    station       : canonical station name
    cycle_time_s  : nominal cycle time in seconds (None = missing)
    oee           : technical availability 0–1
    mu_eff        : (1 / cycle_time_s) * oee  [parts/s]; None if ct missing
    variant_note  : free-text annotation (e.g. representative product label)
    """
    line: str
    station: str
    cycle_time_s: Optional[float]
    oee: float = field(default=DEFAULT_OEE)
    variant_note: Optional[str] = None
    mu_eff: Optional[float] = field(init=False)

    def __post_init__(self) -> None:
        self.mu_eff = (
            round((1.0 / self.cycle_time_s) * self.oee, 6)
            if self.cycle_time_s is not None else None
        )

    @property
    def is_complete(self) -> bool:
        return self.cycle_time_s is not None

    def __repr__(self) -> str:
        ct = f"{self.cycle_time_s} s" if self.cycle_time_s is not None else "⚠ TODO"
        mu = f"{self.mu_eff:.6f}/s" if self.mu_eff is not None else "⚠ TODO"
        note = f"  [{self.variant_note}]" if self.variant_note else ""
        return (
            f"StationLine(line={self.line}, station={self.station!r}, "
            f"ct={ct}, oee={self.oee}, mu_eff={mu}){note}"
        )


# ===========================================================================
# SimConfig data classes
# ===========================================================================

@dataclass
class StationConfig:
    """Cycle time + OEE for one station on one line, as loaded from Excel."""
    name: str
    line: str
    cycle_time_s: float
    oee: float
    mu_eff: float = field(init=False)
    available_time_min_day: float = 915.0

    def __post_init__(self):
        assert 0 < self.oee <= 1.0, f"OEE out of range for {self.name}: {self.oee}"
        assert self.cycle_time_s > 0, f"Cycle time must be > 0 for {self.name}"
        self.mu_eff = round((1.0 / self.cycle_time_s) * self.oee, 6)


@dataclass
class BufferConfig:
    """
    Inter-station buffer parameters.

    `downstream` is normally a single canonical station name. Some buffers
    (e.g. Buffer 1.1 on HTL3) feed into one of two possible next stations
    depending on which product is running — the workbook encodes this as a
    comma-separated cell (e.g. "Stift einpressen, Supfina"). For those rows,
    `downstream` is instead a dict mapping each candidate canonical station
    name to itself, e.g. {"Stift_Einpressen": "Stift_Einpressen",
    "Supfina": "Supfina"}. A future stations-sequence resolver (implemented
    elsewhere) picks the correct key based on the product's actual routing.
    """
    buffer_id: str
    upstream: str                    # canonical station name
    downstream: str | dict[str, str]  # canonical station name, or {candidate: candidate} options
    line: str
    capacity: int
    initial_fill: int
    blocking_protocol: str  # "BAS" or "BBS"


@dataclass
class InventoryConfig:
    """
    A (virtually) infinite or large-capacity store sitting between two
    production stages, as loaded from the "Inventories" sheet
    (Type == "Inventory").

    Examples: Inv_nach_ECM (raw material, upstream of everything — now
    THREE lanes: "All materials" feeding Beladen directly, "Lochfitler
    material" feeding the Lochfilter chute, "DRS material" feeding the
    DRS chute — all drawn from the same physical raw-material store),
    Inv_nach_Loch / Inv_nach_DRS (finished sub-assemblies awaiting pickup
    by a FIFO chute), Inv_nach_HTL (finished goods after Sichtpruefen).

    A single named inventory (e.g. "Inv_nach_ECM") may have several
    *lanes* — one row per `stored_type` — exactly like chutes/supermarkets
    already did; see SimConfig.inventories (dict[name -> list[InventoryConfig]]).

    `upstream_station` / `downstream_station` are informational — they
    record which station fills the inventory and which station (or
    chute) draws from it, mirroring the Excel columns. Resolution of the
    actual SimPy flow happens in sim.produce and sim.resources.build.
    """
    name: str                          # e.g. "Inv_nach_ECM"
    station: str                       # owning context, e.g. "All lines"
    upstream_station: Optional[str]    # e.g. "Lochfilter"; None ("-") if none
    downstream_station: Optional[str]  # e.g. "Beladen"; None ("-") if none
    stored_type: str                   # e.g. "All materials", "Lochfilter", "DRS"
    capacity: int
    initial_fill: int
    pack_size: int = 0                 # pcs per pack (informational for Inventory rows)
    replenish_amount: int = 0          # packs per replenishment (unused — inventories
                                        # are not themselves replenished in the sim)
    # True when the Capacity cell carried a "virtually infinite" (or similar)
    # annotation, e.g. "1000000 (virtually infinite)". Such inventories are
    # built as a truly unbounded simpy.Container (capacity=inf, init=inf) —
    # a literal N/N container is already FULL and would deadlock on the very
    # next put(); "virtually infinite" is a modelling shortcut meaning "never
    # track/limit this store", not "cap it at N".
    is_unbounded: bool = False


@dataclass
class ChuteConfig:
    """
    A capacity-limited FIFO chute immediately upstream of a station, as
    loaded from the "Inventories" sheet (Type == "FIFO_Chute").

    Unlike the old supermarket model, a chute is NOT fed by a continuous
    arrival process. Material is pulled from the upstream InventoryConfig
    in discrete packs, triggered purely by the chute's own remaining stock:
    once the chute's fill drops to `trigger_amount_left` (or below), a
    replenishment of `replenish_amount` packs (i.e. `replenish_amount *
    pack_size` pieces) is pulled from the upstream inventory and pushed
    into the chute — up to `capacity`. Withdrawal FROM the chute (to feed
    the station) is driven by the line's own throughput (the cycle time of
    the first station), not by any external arrival distribution.

    A single named chute (e.g. "Chu_vor_HTL3") may have several *lanes* —
    one row per `stored_type` (Standard / Lochfilter / DRS) — because HTL3
    draws three physically distinct materials through the same conceptual
    chute. Each lane is tracked independently (its own container, its own
    remaining-stock trigger).
    """
    name: str                          # e.g. "Chu_vor_HTL3"
    station: str                       # station this chute feeds, e.g. "HTL3"
    upstream_station: Optional[str]    # e.g. "Inventory"/"-"; source inventory ref
    downstream_station: Optional[str]  # e.g. "Beladen"
    stored_type: str                   # "Standard" | "Lochfilter" | "DRS" | ...
    capacity: int
    initial_fill: int
    trigger_amount_left: int           # chute fill level that triggers a pull
    pack_size: int                     # pcs per pack
    replenish_amount: int              # packs pulled per replenishment

    @property
    def replenish_qty_pcs(self) -> int:
        """Total pieces pulled per replenishment = packs x pieces/pack."""
        return self.replenish_amount * self.pack_size


@dataclass
class InspectionConfig:
    """Sichtpruefung outcome probabilities for one line."""
    line: str
    pass_rate: float
    rework_rate: float
    scrap_rate: float
    rework_multiplier: float = 1.0
    rework_loop_limit: Optional[int] = None

    def __post_init__(self):
        total = round(self.pass_rate + self.rework_rate + self.scrap_rate, 6)
        assert abs(total - 1.0) < 1e-4, (
            f"Inspection rates for {self.line} must sum to 1.0; got {total}"
        )

    @property
    def expected_passes(self) -> float:
        return 1.0 / max(1.0 - self.rework_rate, 1e-9)


@dataclass
class PackagingConfig:
    """Packaging and Restmenge rules."""
    package_size: int
    restmenge_as_wip: bool
    restmenge_priority: str         # "High" or "Normal"


@dataclass
class CustomerDemand:
    """
    One demand event row from the "CustomerDemand" sheet — a flat, long
    event table with one row per demand event:

        Date | Time | Product | TotalQuantity | LineId

    `LineId` is a DOWNSTREAM line — whoever downstream is requesting the
    product (e.g. "Line 12") — it is NOT one of our production lines
    (HTL3/HTL5/HTL6). Deciding which of our production line(s) actually
    makes each order is the job of a LinePriorityStrategy (see
    domain.line_priority) driven by PRODUCT_MATRIX's feasible-lines data.

    `line_id` is kept on the row purely for traceability / potential
    future use — e.g. matching this event back to a specific
    KanbanWithdrawalEvent — but nothing in this module currently uses it
    to pick a production line.

    Attributes
    ----------
    period_label : the "Date" cell, e.g. "01.09.2026" (kept as the
                   workbook's string; not parsed into a date object here)
    time_slot    : the "Time" cell, e.g. "06:00:00" (kept as the
                   workbook's string; converting to a sim offset is the
                   runner's job)
    product_id   : sachnummer, e.g. "F00RC00419"
    total_qty    : the "TotalQuantity" column — demand for this product
                   at this (Date, Time)
    line_id      : the downstream line requesting the product, e.g.
                   "Line 12" — NOT one of our production lines
    order_id     : 0-based index among duplicate (period_label, time_slot,
                   product_id) rows in the sheet, in case the same
                   product appears twice at the same timestamp
                   (0 = first/only occurrence)
    """
    period_label: str
    time_slot: str
    product_id: str
    total_qty: int
    line_id: str = ""
    order_id: int = 0

    @property
    def quantities(self) -> dict[str, int]:
        """
        Backward-compat shim for old callers that read
        `CustomerDemand.quantities` as a {product_id: qty} singleton dict.
        """
        return {self.product_id: self.total_qty}


# ===========================================================================
# Kanban / pull config dataclasses — loaded from ProductionPlanning_v6.xlsx:
# KanbanConfig, KanbanCardsSetup, CustomerDemandKanban, Supermarkets
# ===========================================================================

@dataclass
class KanbanCardConfig:
    """
    Per-product Kanban card parameters, from the "KanbanCardsSetup" sheet.

    One row per sachnummer (63 rows in the current workbook — every product
    in PRODUCT_MATRIX, even though only 8 of them currently appear in the
    CustomerDemandKanban withdrawal schedule).

    Attributes
    ----------
    sachnummer       : product identifier, e.g. "F00RC00419"
    batch_size       : pieces represented by one Kanban card (workbook
                       default 200; user has since hand-edited some rows —
                       do not assume a uniform value across products)
    cards_to_trigger : number of cards that must accumulate in the
                       Batch-Size Collector (per product) before that
                       batch is released to the Kanban Chute for
                       production (workbook default 5; user has since
                       hand-edited most rows, ranging 2–8 in the current
                       file — do not assume a uniform value)
    eligible_lines   : ordered list of line names (e.g. ["HTL3", "HTL5", "HTL6"])
                       this product's Kanban cards are drawn from /
                       produced on, parsed from the "HTL3"/"HTL5"/"HTL6"
                       columns of "KanbanCardsSetup" (an "X" marks a line
                       as eligible).
    """
    sachnummer: str
    batch_size: int
    cards_to_trigger: int
    eligible_lines: list[str] = field(default_factory=list)


@dataclass
class KanbanWithdrawalEvent:
    """
    One row from the "CustomerDemandKanban" sheet — a single customer
    withdrawal event: one product, withdrawn at one point in time, by one
    downstream line. The sheet is a flat, long event table, one row per
    withdrawal:

        Date | Time | Product | TotalQuantity | LineId

    `line_id` is a DOWNSTREAM line — whoever downstream is requesting the
    product (e.g. "Line 12") — it is NOT one of our production lines
    (HTL3/HTL5/HTL6), so it is taken as-is from the sheet and not
    validated against KanbanCardsSetup.eligible_lines. Assigning a
    withdrawal to one of our production lines happens downstream, in
    sim.fill.pull (assignment.select_supermarket_for_withdrawal).

    Attributes
    ----------
    date      : the "Date" cell, e.g. "01.09.2026" (kept as the
                workbook's string; not parsed into a date object here)
    time      : the "Time" cell, e.g. "06:00:00" (kept as the workbook's
                string; NOT converted to a simulation offset here — that
                conversion is the runner's job, since it also needs to
                know the sim start time and how times wrap past midnight)
    product   : sachnummer withdrawn, e.g. "F00RJ02491"
    quantity  : pieces withdrawn (only rows with quantity > 0 are kept —
                see _parse_customer_demand_kanban_sheet)
    line_id   : the downstream line requesting the product, e.g.
                "Line 12" — NOT one of our production lines
    """
    date: str
    time: str
    product: str
    quantity: int
    line_id: str = ""


@dataclass
class SupermarketSlotConfig:
    """
    One physical slot (lane) in a line's Supermarket, from the
    "Supermarkets" sheet (the parser also accepts the older "SupermInState"
    / "SupermarketInitialState" sheet names for backward compatibility):

        Line | RowNumber | Type | Capacity | Sachnummer | InitialState | InitialPcsPartial

    Supermarkets are shared by both Kanban (pull) and push production:
    each line's supermarket has some slots permanently dedicated to one
    product ("Main runner" rows — `sachnummer` is fixed, pull/Kanban
    replenished) and some slots for whichever other/exotic products need
    to be pushed through without a dedicated slot ("Exotic" rows —
    `sachnummer` is blank; not a specific product). A line can have
    several "Main runner" slots for the *same* sachnummer (e.g. HTL3 rows
    1 & 2 are both F00RJ02491) — these are separate physical lanes, not a
    duplicate/error, and each has its own capacity/seed stock.

    Attributes
    ----------
    line                 : line name, e.g. "HTL3"
    row_number           : 1-based row/slot number within this line's
                           supermarket
    slot_type            : "Main runner" (pull/Kanban, dedicated to
                           `sachnummer`) or "Exotic" (push, generic slot —
                           `sachnummer` is blank); see is_main_runner /
                           is_exotic
    capacity             : max whole cards/batches this slot can hold
    sachnummer           : product assigned to this slot; "" for Exotic
                           rows (no fixed product)
    initial_state        : t=0 seed stock (full cards/batches) sitting in
                           this slot
    initial_pcs_partial  : t=0 leftover pieces not yet forming a full
                           card/batch in this slot (0..batch_size-1); a
                           partial pack CANNOT be withdrawn on its own —
                           withdrawal must wait for production to
                           complete the pack (per open-question #3 in the
                           original handoff notes)
    """
    line: str
    row_number: int
    slot_type: str
    capacity: int
    sachnummer: str
    initial_state: int
    initial_pcs_partial: int

    @property
    def is_main_runner(self) -> bool:
        """True if this is a pull/Kanban slot dedicated to `sachnummer`."""
        return self.slot_type.strip().lower() == "main runner"

    @property
    def is_exotic(self) -> bool:
        """True if this is a generic push slot (no fixed product)."""
        return self.slot_type.strip().lower() == "exotic"


@dataclass
class KanbanTimingConfig:
    """
    Cadence parameters for the Kanban loop, from the "KanbanConfig" sheet
    (2 rows in the current workbook). Resolves open question #7 in the
    handoff notes: these were previously proposed as hardcoded constants
    in the process-logic module; they are now Excel-driven, consistent
    with everything else in SimConfig.

    Attributes
    ----------
    withdrawal_cadence_min       : minutes between successive
                                   CustomerDemandKanban rows (workbook: 15)
    collection_box_emptying_min  : minutes between Collection-Box →
                                   Batch-Size-Collector emptying events
                                   (workbook: 30 — i.e. every 2nd
                                   withdrawal tick at the current cadence)
    supermarket_capacity_cards   : max whole Kanban cards (batches) any one
                                   (line, product) Supermarket lane may hold
                                   at once — from the "KanbanConfig" sheet
                                   (Parameter cell containing "capacity" or
                                   "supermarket"), default 25 if absent.
                                   Enforced as the bound on the lane's
                                   simpy.Store in build_kanban_environment();
                                   a full lane makes _return_card_to_supermarket's
                                   `store.put()` block until space frees up.
    """
    withdrawal_cadence_min: float = 15.0
    collection_box_emptying_min: float = 30.0
    supermarket_capacity_cards: int = 25


@dataclass
class OEEBinConfig:
    """
    One OEE probability bin for one production line, from the "OEE" sheet.

        Line | Bin_down | Bin_up | Probability

    The sheet expresses, per line, a discrete probability distribution
    over OEE (technical availability) values: each row is one bin
    [bin_down, bin_up) together with the probability that the line's OEE
    (for whatever unit of time the sampling logic will use — day/shift/run)
    falls in that bin. Probabilities for a given line are expected to sum
    to ~1.0 across its rows, but that is not enforced by this parser —
    reading is purely mechanical here; sampling from the distribution
    (and any validation of it) is a separate, not-yet-implemented step.

    Attributes
    ----------
    line        : "HTL3" | "HTL5" | "HTL6"
    bin_down    : lower bound of the OEE bin (inclusive), typically 0..1
    bin_up      : upper bound of the OEE bin (exclusive), typically 0..1
    probability : probability mass assigned to this bin, 0..1
    """
    line: str
    bin_down: float
    bin_up: float
    probability: float


# Bare 'DD.MM.YYYY' (optionally followed by more text) — same convention as
# the CustomerDemand/CustomerDemandKanban "Date" cells throughout this
# module. Used only by the "Shifts" sheet's "Day" column parser below.
_SHIFT_DATE_RE = re.compile(r"^(\d{1,2})\.(\d{1,2})\.(\d{4})")


@dataclass
class ShiftDefinition:
    """
    One named shift's daily time-of-day window, from the "Shifts" sheet's
    top table (Shifts | Starts | Ends).

    Attributes
    ----------
    name  : "Morning" | "Afternoon" | "Night" (or whatever the workbook
            uses — not hardcoded, so a 4th/5th shift can be added purely
            in Excel with no code change)
    start : time-of-day the shift begins (inclusive)
    end   : time-of-day the shift ends (inclusive)

    Night shifts are expected to WRAP PAST MIDNIGHT (e.g. 22:00:00 ->
    05:59:59) — `end < start` is not an error, it's the normal case for
    an overnight shift; see `wraps_midnight` / `covers`.
    """
    name: str
    start: _dt.time
    end: _dt.time

    @property
    def wraps_midnight(self) -> bool:
        """True for a shift whose window crosses midnight (end <= start)."""
        return self.end <= self.start

    def covers(self, t: _dt.time) -> bool:
        """True if time-of-day `t` falls inside this shift's window."""
        if self.wraps_midnight:
            return t >= self.start or t <= self.end
        return self.start <= t <= self.end


@dataclass
class ShiftCalendar:
    """
    Per-line, per-day shift availability, from the "Shifts" sheet's
    bottom table (Line | Day | CalendarDay | <one column per shift name>).

    "Not every line will be available on every shift of every day" — this
    is the lookup table that answers, for a given production line and a
    given wall-clock instant, whether that line is "on" (may start new
    production) or "off" (must not).

    Attributes
    ----------
    shifts       : shift name -> ShiftDefinition, from the sheet's top
                   table. Iteration order matches the sheet's row order
                   (Morning, Afternoon, Night in the current workbook),
                   which only matters for tie-breaking if two shift
                   windows were ever mis-configured to overlap — not
                   expected, but `shift_at` returns the first match.
    availability : line_name -> {date -> {shift names marked "X"}}.
                   A shift name present in the set for (line, date) means
                   that line is ON during that shift on that calendar
                   date; a shift name absent (including the whole
                   (line, date) key being absent) means OFF. This module
                   does NOT default missing rows to "on" — an
                   unconfigured (line, date) is always OFF (see
                   `is_line_on`), matching the "if off cannot be used"
                   rule: an unplanned line is treated the same as an
                   explicitly-off one, not a permissive default.

    Date-key convention for overnight (Night) shifts
    -------------------------------------------------
    The sheet's "Day" column is the CALENDAR date the shift's row applies
    to — e.g. the "HTL3 | 01.09.2026 | ... | Night: X" row means HTL3's
    Night shift STARTING on the evening of 01.09.2026 (22:00) is on, even
    though that shift's window runs into the small hours of 02.09.2026
    (until 05:59:59). `is_line_on` accounts for this: a wall-clock instant
    at, say, 02:00 on 02.09.2026 is resolved against the "Day" row for
    01.09.2026, not 02.09.2026, because that's the row whose Night shift
    is still running at that instant.
    """
    shifts: dict[str, ShiftDefinition] = field(default_factory=dict)
    availability: dict[str, dict[_dt.date, set[str]]] = field(default_factory=dict)

    def shift_at(self, t: _dt.time) -> Optional[str]:
        """
        Name of the shift whose time-of-day window covers `t`, or None if
        `t` falls in a gap not covered by any configured shift (e.g. a
        misconfigured workbook with a hole between shifts — treated the
        same as "off", never as an error, since this table is data, not
        code).
        """
        for name, sd in self.shifts.items():
            if sd.covers(t):
                return name
        return None

    def is_line_on(self, line_name: str, at: _dt.datetime) -> bool:
        """
        True iff `line_name` is explicitly marked "on" for the shift
        covering wall-clock instant `at`.

        Both of these make the line count as OFF (never as an error):
          - `at`'s time-of-day isn't covered by any configured shift.
          - the (line_name, calendar-date) pair has no row in the sheet
            at all, or has a row that doesn't mark this shift.
        See the class docstring for how an overnight Night shift's
        small-hours tail is resolved against the PREVIOUS calendar date.
        """
        shift_name = self.shift_at(at.time())
        if shift_name is None:
            return False
        shift_def = self.shifts[shift_name]
        lookup_date = at.date()
        if shift_def.wraps_midnight and at.time() <= shift_def.end:
            # We're in the post-midnight tail of a shift that actually
            # started on the PREVIOUS calendar day's row.
            lookup_date = at.date() - _dt.timedelta(days=1)
        day_shifts = self.availability.get(line_name, {}).get(lookup_date)
        return bool(day_shifts) and shift_name in day_shifts

    def next_on_transition(
        self, line_name: str, after: _dt.datetime, horizon_days: int = 14,
    ) -> Optional[_dt.datetime]:
        """
        Earliest datetime strictly AFTER `after` at which `line_name`
        turns from off to on — i.e. the next shift-window START at which
        `is_line_on` would first become True, scanning forward at most
        `horizon_days` calendar days.

        Used by callers that need to sleep past an off period rather than
        poll (e.g. "wait until this line is on again, then start
        production" / "retry the line search every 1h, but never sooner
        than the line can actually turn on"). Only shift-boundary instants
        are ever candidates (there is no way for `is_line_on` to change
        value anywhere else), so this scans those instants directly
        rather than stepping second-by-second.

        Returns None if no ON instant is found within the horizon (e.g. a
        line with no availability configured at all, or configured
        permanently off) — callers must decide how to handle "never comes
        back on" rather than this method looping forever.
        """
        if not self.shifts:
            return None
        cursor_date = after.date()
        for _ in range(horizon_days + 1):
            for shift_name, shift_def in self.shifts.items():
                candidate = _dt.datetime.combine(cursor_date, shift_def.start)
                if candidate <= after:
                    continue
                if self.is_line_on(line_name, candidate):
                    return candidate
            cursor_date += _dt.timedelta(days=1)
        return None


@dataclass
class SimConfig:
    """
    Root configuration object — single source of truth for the simulation.
    Populated by load_config(); nothing else should construct this directly.

    Setup-time lookup conventions
    ------------------------------
    csv_setup_times   : full TTNr-based table from HTL_setup_times.xlsx
                        keyed as [line_name][n_workers][(from_ttnr, to_ttnr)] → seconds
    sachnummer_to_ttnr: loaded from product_master.xlsx
                        keyed as sachnummer (e.g. "F00RC00967") → TTNr (e.g. 967)
                        use this to resolve a product's sachnummer to its TTNr
                        before looking up setup times in csv_setup_times
    """
    n_lines: int
    line_names: list[str]

    # dict[line_name → ordered list]
    stations:   dict[str, list[StationConfig]]
    buffers:    dict[str, list[BufferConfig]]
    inspection: dict[str, InspectionConfig]

    packaging:  PackagingConfig
    demand:     list[CustomerDemand]

    # dict[line_name → dict[canonical_station → product_label]]
    initial_setup_state: dict[str, dict[str, str]]

    # dict[line_name → dict[n_workers → dict[(from_ttnr, to_ttnr) → minutes]]]
    csv_setup_times: dict[str, dict[int, dict[tuple[int, int], int]]]

    # sachnummer (e.g. "F00RC00967") → TTNr integer (e.g. 967)
    # loaded from product_master.xlsx; used to bridge product objects to
    # the TTNr-keyed csv_setup_times table before setup-time lookup
    sachnummer_to_ttnr: dict[str, int]

    # StationLine catalogue (consumed by domain.products)
    line_stations: dict[str, list[StationLine]]

    available_time_min_day: float

    # dict[inventory_name → list[InventoryConfig]]  — from "Inventories" sheet
    # (Type == "Inventory"). One entry per (name, stored_type) lane, e.g.
    # inventories["Inv_nach_ECM"] has 3 lanes: All materials / Lochfilter
    # material / DRS material — mirrors the chutes structure below.
    inventories: dict[str, list[InventoryConfig]] = field(default_factory=dict)

    # dict[chute_name → list[ChuteConfig]]  — from "Inventories" sheet
    # (Type == "FIFO_Chute"). One entry per (name, stored_type) lane, e.g.
    # chutes["Chu_vor_HTL3"] has 3 lanes: Standard / Lochfilter / DRS.
    chutes: dict[str, list[ChuteConfig]] = field(default_factory=dict)

    # --- Kanban / pull fields (all additive; empty/default when the
    # Kanban sheets are absent from the workbook — see module docstring) --

    # sachnummer → KanbanCardConfig, from "KanbanCardsSetup"
    kanban_cards: dict[str, KanbanCardConfig] = field(default_factory=dict)

    # one entry per CustomerDemandKanban row, in sheet order
    kanban_withdrawals: list[KanbanWithdrawalEvent] = field(default_factory=list)

    # line_name → list[SupermarketSlotConfig], from "Supermarkets"
    # (one entry per physical slot/row in that line's supermarket, in
    # sheet order — see SupermarketSlotConfig docstring)
    supermarkets: dict[str, list[SupermarketSlotConfig]] = field(default_factory=dict)

    # withdrawal cadence + collection-box emptying interval, from "KanbanConfig"
    kanban_timing: KanbanTimingConfig = field(default_factory=KanbanTimingConfig)

    # --- OEE field (additive; empty when the "OEE" sheet is absent) ------

    # line_name → list[OEEBinConfig], from "OEE" (one entry per bin row,
    # in sheet order within each line). This module only reads the
    # table; the sampling logic that consumes it lives in sim.oee.
    oee_distribution: dict[str, list[OEEBinConfig]] = field(default_factory=dict)

    # --- Shifts field (additive; empty when the "Shifts" sheet is absent
    # — see _parse_shifts_sheet docstring) ---------------------------------

    # shift-time definitions + per-line/per-day on/off grid, from "Shifts".
    # An empty ShiftCalendar() (bool(shift_calendar.shifts) is False) means
    # no shift data was configured at all — callers should treat that as
    # "shift on/off checking is not in effect" rather than "every line is
    # off". Once shifts ARE configured, a (line, date) pair absent from the
    # grid is genuinely OFF — see ShiftCalendar.is_line_on.
    shift_calendar: ShiftCalendar = field(default_factory=ShiftCalendar)

    queue_discipline: str = "FIFO"

    def summary(self) -> str:
        out = [
            "=== SimConfig Summary ===",
            f"Lines          : {self.n_lines}  {self.line_names}",
            f"Queue          : {self.queue_discipline}",
            f"Available time : {self.available_time_min_day} min/day",
        ]
        for ln in self.line_names:
            out.append(f"\n--- {ln} ---")
            for s in self.stations.get(ln, []):
                out.append(
                    f"  {s.name:<22} ct={s.cycle_time_s}s  "
                    f"oee={s.oee}  μ_eff={s.mu_eff}/s"
                )
            insp = self.inspection.get(ln)
            if insp:
                out.append(
                    f"  Inspection: pass={insp.pass_rate}  "
                    f"rework={insp.rework_rate}  scrap={insp.scrap_rate}"
                )
        if self.demand:
            periods = {d.period_label for d in self.demand}
            first_period = self.demand[0].period_label
            rows_first_period = [d for d in self.demand if d.period_label == first_period]
            out.append(
                f"\nDemand: {len(periods)} period(s), {len(self.demand)} rows total"
            )
            out.append(
                f"  First period ({first_period}): {len(rows_first_period)} demand event(s)"
            )
            for d in rows_first_period[:5]:
                out.append(
                    f"    {d.time_slot:<10} {d.product_id:<14} total={d.total_qty} "
                    f"line_id={d.line_id!r}"
                )
        n_csv = sum(
            len(m)
            for workers in self.csv_setup_times.values()
            for m in workers.values()
        )
        out.append(f"CSV setup-time entries : {n_csv}")

        if self.inventories:
            out.append("\n--- Inventories ---")
            for name, lanes in self.inventories.items():
                for inv in lanes:
                    out.append(
                        f"  {name:<16} stored={inv.stored_type:<20} "
                        f"cap={inv.capacity:<10} init={inv.initial_fill}"
                    )
        if self.chutes:
            out.append("\n--- FIFO Chutes ---")
            for name, lanes in self.chutes.items():
                for ch in lanes:
                    out.append(
                        f"  {name:<16} station={ch.station:<8} "
                        f"stored={ch.stored_type:<10} cap={ch.capacity:<6} "
                        f"init={ch.initial_fill:<6} trigger_left={ch.trigger_amount_left:<6} "
                        f"pack={ch.pack_size:<4} replenish={ch.replenish_amount}pk "
                        f"({ch.replenish_qty_pcs}pcs)"
                    )

        if self.kanban_cards:
            out.append("\n--- Kanban ---")
            out.append(
                f"  Timing: withdrawal every {self.kanban_timing.withdrawal_cadence_min} min, "
                f"collection-box emptied every {self.kanban_timing.collection_box_emptying_min} min"
            )
            out.append(f"  Cards configured : {len(self.kanban_cards)} products")
            out.append(f"  Withdrawal events: {len(self.kanban_withdrawals)}")
            n_slots = sum(len(v) for v in self.supermarkets.values())
            out.append(f"  Supermarket slots: {n_slots} across {len(self.supermarkets)} line(s)")

        if self.shift_calendar.shifts:
            out.append("\n--- Shifts ---")
            for name, sd in self.shift_calendar.shifts.items():
                wrap = "  (wraps midnight)" if sd.wraps_midnight else ""
                out.append(f"  {name:<10} {sd.start} - {sd.end}{wrap}")
            for ln in self.line_names:
                n_days = len(self.shift_calendar.availability.get(ln, {}))
                out.append(f"  {ln:<6}: {n_days} configured day(s)")
        return "\n".join(out)


# ===========================================================================
# StationLine helpers  (public API, used by domain.products and load_config)
# ===========================================================================

def load_line_stations(
    excel_path: str | Path = _DEFAULT_EXCEL,
    line_map: dict[int, str] | None = None,
) -> dict[str, list[StationLine]]:
    """
    Build the LINE_STATIONS catalogue from the Process sheet.

    One StationLine is created per unique (line, station) pair; when the
    sheet has multiple product rows for the same pair the first is used
    (cycle times are line-level, Product column is representative only).
    """
    excel_path = Path(excel_path)
    if not excel_path.exists():
        raise FileNotFoundError(f"Workbook not found: {excel_path}")

    lmap = line_map or LINE_NUMBER_TO_NAME
    df = pd.read_excel(excel_path, sheet_name="Process", header=2)
    df.columns = [str(c).strip() for c in df.columns]

    # "Line" holds line *names* (e.g. "HTL3"), not numbers — filter against
    # the configured line names rather than coercing to int.
    valid_line_names = set(lmap.values())
    df["Line"] = df["Line"].astype(str).str.strip()
    df = df[df["Line"].isin(valid_line_names)].copy()
    df["Station"] = df["Station"].astype(str).str.strip().map(
        lambda s: _STATION_NAME_MAP.get(s, s)
    )

    result: dict[str, list[StationLine]] = {n: [] for n in lmap.values()}
    seen: set[tuple[str, str]] = set()

    for _, row in df.iterrows():
        line_name = row["Line"]
        station = str(row["Station"]).strip()
        if (line_name, station) in seen:
            continue
        seen.add((line_name, station))

        ct_raw = row.get("Cycle Time (s)")
        oee_raw = row.get("OEE (0–1)")
        product_label = str(row.get("Product", "")).strip()

        result[line_name].append(StationLine(
            line=line_name,
            station=station,
            cycle_time_s=float(ct_raw) if pd.notna(ct_raw) else None,
            oee=float(oee_raw) if pd.notna(oee_raw) else DEFAULT_OEE,
            variant_note=f"representative product: {product_label}" if product_label else None,
        ))

    return result


# Module-level singleton — imported by domain.products
LINE_STATIONS: dict[str, list[StationLine]] = load_line_stations(_DEFAULT_EXCEL)


def get_station(line: str, station: str) -> StationLine:
    """Return the StationLine for (line, station); raises on unknown inputs."""
    if line not in LINE_STATIONS:
        raise KeyError(f"Unknown line: {line!r}. Valid: {list(LINE_STATIONS)}")
    matches = [s for s in LINE_STATIONS[line] if s.station == station]
    if not matches:
        raise ValueError(
            f"Station {station!r} not found on line {line!r}. "
            f"Available: {[s.station for s in LINE_STATIONS[line]]}"
        )
    return matches[0]


def station_names(line: str) -> list[str]:
    """Return ordered canonical station names for a given line."""
    if line not in LINE_STATIONS:
        raise KeyError(f"Unknown line: {line!r}")
    return [s.station for s in LINE_STATIONS[line]]


def incomplete_stations() -> list[StationLine]:
    """Return all StationLine entries that have no cycle_time_s."""
    return [s for stations in LINE_STATIONS.values() for s in stations if not s.is_complete]


# ===========================================================================
# Sheet parsers  (private)
# ===========================================================================

def _norm_station(raw: str) -> str:
    return _STATION_NAME_MAP.get(raw.strip(), raw.strip())


def _parse_process_sheet(
    xls: pd.ExcelFile,
    lmap: dict[int, str],
) -> tuple[dict[str, list[StationConfig]], float]:
    df = pd.read_excel(xls, sheet_name="Process", header=2)
    df.columns = [str(c).strip() for c in df.columns]

    # "Line" holds line *names* (e.g. "HTL3"), not numbers — filter against
    # the configured line names rather than coercing to int.
    valid_line_names = set(lmap.values())
    df["Line"] = df["Line"].astype(str).str.strip()
    df = df[df["Line"].isin(valid_line_names)].copy()
    df["Station"] = df["Station"].astype(str).str.strip().map(
        lambda s: _STATION_NAME_MAP.get(s, s)
    )

    result: dict[str, list[StationConfig]] = {n: [] for n in lmap.values()}
    seen: set[tuple[str, str]] = set()
    avail_time = 915.0

    for _, row in df.iterrows():
        line_name = row["Line"]
        station = str(row["Station"]).strip()
        if (line_name, station) in seen:
            continue
        seen.add((line_name, station))

        avail_raw = row.get("Available Time (min/day)")
        if pd.notna(avail_raw):
            avail_time = float(avail_raw)

        result[line_name].append(StationConfig(
            name=station,
            line=line_name,
            cycle_time_s=float(row["Cycle Time (s)"]),
            oee=float(row["OEE (0–1)"]),
            available_time_min_day=avail_time,
        ))

    return result, avail_time


def _parse_buffers_sheet(
    xls: pd.ExcelFile,
    lmap: dict[int, str],
) -> dict[str, list[BufferConfig]]:
    df = pd.read_excel(xls, sheet_name="Buffers", header=1)
    df.columns = [str(c).strip() for c in df.columns]

    valid_line_names = set(lmap.values())
    result: dict[str, list[BufferConfig]] = {n: [] for n in lmap.values()}

    for _, row in df.iterrows():
        # The "Line" column holds line *names* (e.g. "HTL3"), not numbers —
        # match directly against the configured line names rather than
        # coercing to int (which previously caused every row to be skipped).
        line_name = str(row.get("Line", "")).strip()
        if line_name not in valid_line_names:
            continue

        fill_raw = row["Initial Fill"]
        downstream_raw = str(row.get("Downstream Station", "")).strip()

        # Some buffers feed into one of two possible next stations depending
        # on the product being run (e.g. "Stift einpressen, Supfina").
        # Split these into a {candidate: candidate} dict of normalised
        # station names; a stations-sequence resolver (implemented
        # elsewhere) will later pick the correct key. Single-downstream
        # cells stay a plain canonical station-name string.
        downstream: str | dict[str, str]
        if not downstream_raw or downstream_raw.lower() == "nan":
            downstream = ""
        elif "," in downstream_raw:
            candidates = [_norm_station(part) for part in downstream_raw.split(",")]
            downstream = {c: c for c in candidates if c}
        else:
            downstream = _norm_station(downstream_raw)

        result[line_name].append(BufferConfig(
            buffer_id=str(row["Buffer ID"]).strip(),
            upstream=_norm_station(str(row["Upstream Station"])),
            downstream=downstream,
            line=line_name,
            capacity=int(row["Max Capacity (K)"]),
            initial_fill=int(fill_raw) if pd.notna(fill_raw) else 0,
            blocking_protocol=str(row["Blocking Protocol"]).strip(),
        ))

    return result


def _is_unbounded(raw) -> bool:
    """
    True if a Capacity cell carries a "virtually infinite" / "unendlich"
    style annotation, e.g. "1000000 (virtually infinite)". Shared by the
    Inventories parser to flag InventoryConfig rows that should be built
    as a truly unbounded simpy.Container rather than a literal N-cap
    container (see InventoryConfig.is_unbounded docstring).
    """
    if raw is None or (isinstance(raw, float) and pd.isna(raw)):
        return False
    s = str(raw).strip().lower()
    return "infinite" in s or "unendlich" in s or "unbegrenzt" in s


def _leading_int_or_none(raw) -> Optional[int]:
    """
    Extract the leading integer from a cell that may contain annotation
    text, e.g. "100000 (virtually infinite)" -> 100000, "-" -> None,
    NaN -> None.  Shared by the Inventories parser for the
    Capacity / InitialFill / TriggerAmountLeft / PackSize / ReplenishAmount
    columns.
    """
    import re

    if raw is None or (isinstance(raw, float) and pd.isna(raw)):
        return None
    s = str(raw).strip()
    if not s or s == "-":
        return None
    m = re.match(r"^\s*(\d+)", s)
    return int(m.group(1)) if m else None


def _norm_chute_type(raw) -> str:
    """
    Normalise a "Type" cell to one of "inventory" / "fifo_chute" / "" for
    matching. Accepts case/spacing/underscore variants of the FIFO chute
    label ("FIFO Chute", "FIFO_Chute", "fifo-chute", ...).
    """
    s = str(raw).strip().lower()
    s = s.replace("-", "_").replace(" ", "_")
    while "__" in s:
        s = s.replace("__", "_")
    return s


def _parse_inventories_sheet(
    xls: pd.ExcelFile,
) -> tuple[dict[str, list[InventoryConfig]], dict[str, list[ChuteConfig]]]:
    """
    Parse the "Inventories" sheet into InventoryConfig / ChuteConfig
    objects.

    Expected columns (row order per spec, header row auto-detected):
        Name | Type | Station | Upstream_Station | Downstream_Station |
        Stored_type | SupermarketCapacity_pcs | SupermarketInitialFill_pcs |
        TriggerAmountLeft | PackSize | ReplenishAmount

    `Type` distinguishes "Inventory" rows from "FIFO_Chute" rows
    (case/spacing-insensitive — see _norm_chute_type()).
    A "-" or blank in Upstream/Downstream Station means "no such link"
    (mapped to None). Capacity/InitialFill/TriggerAmountLeft/PackSize/
    ReplenishAmount cells may carry annotation text (e.g. "1000000
    (virtually infinite)") — the leading integer is extracted via
    _leading_int_or_none().

    Multiple rows may share the same Name with different Stored_type
    (e.g. "Inv_nach_ECM" has "All materials" / "Lochfitler material" /
    "DRS material" lanes, "Chu_vor_HTL3" has Standard / Lochfilter / DRS
    lanes) — these become separate entries (lanes) under the same dict key,
    for BOTH inventories and chutes.

    Returns
    -------
    (inventories, chutes)
        inventories : dict[name -> list[InventoryConfig]]  (lanes)
        chutes      : dict[name -> list[ChuteConfig]]       (lanes)

    If the sheet is absent (older workbook without this extension), both
    dicts are returned empty so the rest of load_config() still works —
    callers should treat empty dicts as "no inventory/chute model".
    """
    sheet_name = "Inventories"
    if sheet_name not in xls.sheet_names:
        if "Invent&Superm" in xls.sheet_names:
            # backward compat with the pre-correction workbook layout
            sheet_name = "Invent&Superm"
        else:
            import warnings
            warnings.warn(
                "'Inventories' sheet not found in workbook — inventories/"
                "chutes will be empty. Chute-gated material flow will be "
                "skipped by process logic.",
                stacklevel=2,
            )
            return {}, {}

    # Header row isn't fixed across the workbook's other sheets (some use
    # header=1, some header=2) — auto-detect by looking for "Name" in the
    # first few candidate header rows.
    df = None
    for header_row in (0, 1, 2):
        candidate = pd.read_excel(xls, sheet_name=sheet_name, header=header_row)
        candidate.columns = [str(c).strip() for c in candidate.columns]
        if "Name" in candidate.columns and "Type" in candidate.columns:
            df = candidate
            break
    if df is None:
        raise ValueError(
            f"Could not locate header row (columns 'Name'/'Type') in "
            f"{sheet_name!r} sheet within the first 3 rows."
        )

    def _norm_link(raw) -> Optional[str]:
        s = str(raw).strip()
        if not s or s == "-" or s.lower() == "nan":
            return None
        return _norm_station(s)

    def _get(row, *candidates):
        """Try several column-name spellings (space vs underscore variants)."""
        for c in candidates:
            if c in row.index:
                return row.get(c)
        return None

    inventories: dict[str, list[InventoryConfig]] = {}
    chutes: dict[str, list[ChuteConfig]] = {}

    for _, row in df.iterrows():
        name = str(row.get("Name", "")).strip()
        row_type = _norm_chute_type(row.get("Type", ""))
        if not name or name.lower() == "nan" or not row_type:
            continue

        station     = str(row.get("Station", "")).strip()
        upstream    = _norm_link(_get(row, "Upstream_Station", "Upstream Station"))
        downstream  = _norm_link(_get(row, "Downstream_Station", "Downstream Station"))
        stored_type = str(row.get("Stored_type", "")).strip()
        capacity    = _leading_int_or_none(row.get("SupermarketCapacity_pcs"))
        init_fill   = _leading_int_or_none(row.get("SupermarketInitialFill_pcs"))
        trigger_left = _leading_int_or_none(_get(row, "TriggerAmountLeft", "TriggerAmount"))
        pack_size   = _leading_int_or_none(row.get("PackSize"))
        replenish   = _leading_int_or_none(row.get("ReplenishAmount"))

        if row_type == "inventory":
            inventories.setdefault(name, []).append(InventoryConfig(
                name               = name,
                station            = station,
                upstream_station   = upstream,
                downstream_station = downstream,
                stored_type        = stored_type,
                capacity           = capacity if capacity is not None else 0,
                initial_fill       = init_fill if init_fill is not None else 0,
                pack_size          = pack_size if pack_size is not None else 0,
                replenish_amount   = replenish if replenish is not None else 0,
                is_unbounded       = _is_unbounded(row.get("SupermarketCapacity_pcs")),
            ))
        elif row_type == "fifo_chute":
            chutes.setdefault(name, []).append(ChuteConfig(
                name                = name,
                station             = station,
                upstream_station    = upstream,
                downstream_station  = downstream,
                stored_type         = stored_type,
                capacity            = capacity if capacity is not None else 0,
                initial_fill        = init_fill if init_fill is not None else 0,
                trigger_amount_left = trigger_left if trigger_left is not None else 0,
                pack_size           = pack_size if pack_size is not None else 0,
                replenish_amount    = replenish if replenish is not None else 0,
            ))
        # unknown Type values are silently skipped (forward-compat)

    return inventories, chutes


def _parse_inspection_sheet(
    xls: pd.ExcelFile,
    lmap: dict[int, str],
) -> dict[str, InspectionConfig]:
    df = pd.read_excel(xls, sheet_name="Inspection", header=1)
    df.columns = [str(c).strip() for c in df.columns]

    valid_line_names = set(lmap.values())
    result: dict[str, InspectionConfig] = {}

    for _, row in df.iterrows():
        # "Line" holds line *names* (e.g. "HTL3"), not numbers.
        line_name = str(row.get("Line", "")).strip()
        if line_name not in valid_line_names:
            continue

        loop_raw = row.get("Rework Loop Limit")
        mult_raw = row.get("Rework CT Multiplier")
        result[line_name] = InspectionConfig(
            line=line_name,
            pass_rate=float(row["Pass Rate"]),
            rework_rate=float(row["Rework Rate"]),
            scrap_rate=float(row["Scrap Rate"]),
            rework_multiplier=float(mult_raw) if pd.notna(mult_raw) else 1.0,
            rework_loop_limit=int(loop_raw) if pd.notna(loop_raw) else None,
        )

    return result


def _find_header_row(
    raw: pd.DataFrame,
    required: tuple[str, ...],
    max_scan: int = 5,
    default: int = 0,
) -> int:
    """
    Locate a sheet's real header row when it may be preceded by a title
    band (free-text note rows above the actual column headers).

    Scans the first `max_scan` rows and returns the index of the first one
    whose (lower-cased, stripped) cells contain every name in `required`.
    Falls back to `default` if no such row is found within the scan
    window.
    """
    for i in range(min(max_scan, len(raw))):
        cells = raw.iloc[i].fillna("").astype(str).str.strip().str.lower().tolist()
        if all(req in cells for req in required):
            return i
    return default


def _parse_long_demand_rows(xls: pd.ExcelFile, sheet_name: str) -> list[dict]:
    """
    Shared parser for the long-format demand tables — both
    "CustomerDemand" and "CustomerDemandKanban" share this layout,
    one row per demand/withdrawal event:

        Date | Time | Product | TotalQuantity | LineId

    "LineId" is a DOWNSTREAM line (whoever is requesting the product), NOT
    one of our production lines (HTL3/HTL5/HTL6) — see the module
    docstring and the CustomerDemand / KanbanWithdrawalEvent docstrings.

    Columns are matched by header name (case/whitespace/underscore
    insensitive), so column order in the workbook doesn't matter. Read
    with header=None first so the real header row can be located even if
    it's preceded by a title band, same defensive pattern used elsewhere
    in this module.

    Returns a list of dicts — {date, time, product, quantity, line_id} —
    in sheet row order. Rows with a blank Date or Product are skipped.
    Missing "Time" or "LineId" columns degrade gracefully (empty string
    for every row) rather than erroring, since older/partial workbooks
    may not have them yet.
    """
    if sheet_name not in xls.sheet_names:
        return []

    raw = pd.read_excel(xls, sheet_name=sheet_name, header=None)
    header_row_idx = _find_header_row(raw, required=("date", "product"))
    header_row = raw.iloc[header_row_idx].fillna("").astype(str).str.strip().tolist()

    col_idx: dict[str, int] = {}
    for i, h in enumerate(header_row):
        key = h.strip().lower().replace(" ", "").replace("_", "")
        if key == "date":
            col_idx.setdefault("date", i)
        elif key == "time":
            col_idx.setdefault("time", i)
        elif key == "product":
            col_idx.setdefault("product", i)
        elif key in ("totalquantity", "quantity", "qty"):
            col_idx.setdefault("quantity", i)
        elif key in ("lineid", "line"):
            col_idx.setdefault("line_id", i)

    missing = [c for c in ("date", "product", "quantity") if c not in col_idx]
    if missing:
        import warnings
        warnings.warn(
            f"'{sheet_name}' sheet is missing required column(s) {missing}; "
            "returning no rows.",
            stacklevel=2,
        )
        return []

    rows: list[dict] = []
    for _, row in raw.iloc[header_row_idx + 1:].iterrows():
        date_str = str(row.iloc[col_idx["date"]]).strip()
        product_str = str(row.iloc[col_idx["product"]]).strip()
        if not date_str or date_str.lower() == "nan" or not product_str or product_str.lower() == "nan":
            continue

        time_str = ""
        if "time" in col_idx:
            time_val = row.iloc[col_idx["time"]]
            if pd.notna(time_val):
                time_str = str(time_val).strip()
                if time_str.lower() == "nan":
                    time_str = ""

        qty_raw = row.iloc[col_idx["quantity"]]
        if pd.isna(qty_raw) or str(qty_raw).strip() in ("", "nan"):
            quantity = 0
        else:
            quantity = int(float(qty_raw))

        line_id = ""
        if "line_id" in col_idx:
            line_val = row.iloc[col_idx["line_id"]]
            if pd.notna(line_val):
                line_id = str(line_val).strip()
                if line_id.lower() == "nan":
                    line_id = ""

        rows.append({
            "date": date_str,
            "time": time_str,
            "product": product_str,
            "quantity": quantity,
            "line_id": line_id,
        })

    return rows


def _parse_demand_sheet(
    xls: pd.ExcelFile,
    line_names: Optional[list[str]] = None,
) -> list[CustomerDemand]:
    """
    Parse the "CustomerDemand" sheet — flat long layout:

        Date | Time | Product | TotalQuantity | LineId

    One row per (Date, Time, Product) demand event. "LineId" is a
    DOWNSTREAM line, not one of our production lines — see CustomerDemand
    docstring — there is no per-HTL-line split to parse; that's decided
    downstream by a LinePriorityStrategy (see domain.line_priority).

    Parameters
    ----------
    xls         : open ExcelFile handle
    line_names  : accepted for call-signature compatibility with
                  load_config(), which still passes it; unused, since the
                  sheet carries no per-HTL-line split.
    """
    rows = _parse_long_demand_rows(xls, "CustomerDemand")

    seen_counts: dict[tuple[str, str, str], int] = {}
    result: list[CustomerDemand] = []
    for r in rows:
        key = (r["date"], r["time"], r["product"])
        order_id = seen_counts.get(key, 0)
        seen_counts[key] = order_id + 1

        result.append(CustomerDemand(
            period_label=r["date"],
            time_slot=r["time"],
            product_id=r["product"],
            total_qty=r["quantity"],
            line_id=r["line_id"],
            order_id=order_id,
        ))

    return result


def _parse_packaging_sheet(xls: pd.ExcelFile) -> PackagingConfig:
    df = pd.read_excel(xls, sheet_name="Packaging", header=1)
    df.columns = [str(c).strip() for c in df.columns]
    id_col, val_col = df.columns[0], df.columns[2] if len(df.columns) > 2 else df.columns[1]
    kv = {
        str(row[id_col]).strip(): row[val_col]
        for _, row in df.iterrows()
        if str(row[id_col]).strip().lower() not in ("", "nan")
    }

    restmenge_raw = str(kv.get("PK2", "")).lower()
    priority_raw  = str(kv.get("PK3", "")).lower()
    return PackagingConfig(
        package_size=int(float(kv.get("PK1", 25))),
        restmenge_as_wip="next cycle" in restmenge_raw or "wip" in restmenge_raw,
        restmenge_priority="High" if "high" in priority_raw else "Normal",
    )


def _parse_setup_state_sheet(
    xls: pd.ExcelFile,
    lmap: dict[int, str],
) -> dict[str, dict[str, str]]:
    """
    Parse the SetupState sheet.

    3 data rows, one per line. Columns: Line (line name string e.g.
    "HTL3"), Currently Set Up For (Product), Notes. The initial product
    applies to the whole line (no per-station breakdown).
    Result: {line_name: {"line": product_id}}
    """
    df = pd.read_excel(xls, sheet_name="SetupState", header=1)
    df.columns = [str(c).strip() for c in df.columns]

    valid_line_names = set(lmap.values())
    result: dict[str, dict[str, str]] = {n: {} for n in lmap.values()}

    for _, row in df.iterrows():
        line_raw = str(row.get("Line", "")).strip()
        product = str(row.get("Currently Set Up For (Product)", "")).strip()

        if not line_raw or line_raw.lower() == "nan":
            continue
        if line_raw not in valid_line_names:
            continue
        if not product or product.lower() == "nan":
            continue

        result[line_raw]["line"] = product

    return result


# ===========================================================================
# Kanban / pull sheet parsers
#
# Every parser below returns an empty dict/list (never raises) if its sheet
# is absent, so load_config() stays usable against a workbook that
# predates the Kanban extension. Each parser also warns (not raises) so
# the gap is visible without breaking the push-model path.
# ===========================================================================

def _parse_kanban_config_sheet(xls: pd.ExcelFile) -> KanbanTimingConfig:
    """
    Parse the "KanbanConfig" sheet: a simple Parameter/Value/Unit table,
    2 rows in the current workbook:
        kanban withdrawal cadence | 15 | min
        collection-box emptying   | 30 | min

    Matching is substring-based on the Parameter cell (lower-cased) so
    minor wording tweaks in the sheet don't break parsing. Falls back to
    the KanbanTimingConfig defaults (15 / 30 min) for any parameter not
    found, with a warning.
    """
    sheet_name = "KanbanConfig"
    if sheet_name not in xls.sheet_names:
        return KanbanTimingConfig()

    df = pd.read_excel(xls, sheet_name=sheet_name, header=0)
    df.columns = [str(c).strip() for c in df.columns]

    withdrawal_cadence: Optional[float] = None
    collection_box: Optional[float] = None
    supermarket_capacity: Optional[float] = None

    for _, row in df.iterrows():
        param = str(row.get("Parameter", "")).strip().lower()
        val_raw = row.get("Value")
        if not param or pd.isna(val_raw):
            continue
        val = float(val_raw)
        if "withdrawal" in param:
            withdrawal_cadence = val
        elif "collection" in param or "collection-box" in param or "emptying" in param:
            collection_box = val
        elif "capacity" in param or "supermarket" in param:
            supermarket_capacity = val

    defaults = KanbanTimingConfig()
    if withdrawal_cadence is None or collection_box is None:
        import warnings
        warnings.warn(
            "'KanbanConfig' sheet is missing the withdrawal-cadence or "
            "collection-box-emptying parameter; falling back to defaults "
            f"({defaults.withdrawal_cadence_min} / "
            f"{defaults.collection_box_emptying_min} min) for the missing one(s).",
            stacklevel=2,
        )
    if supermarket_capacity is None:
        import warnings
        warnings.warn(
            "'KanbanConfig' sheet is missing the supermarket-capacity "
            f"parameter; falling back to default ({defaults.supermarket_capacity_cards} cards).",
            stacklevel=2,
        )

    return KanbanTimingConfig(
        withdrawal_cadence_min=withdrawal_cadence if withdrawal_cadence is not None else defaults.withdrawal_cadence_min,
        collection_box_emptying_min=collection_box if collection_box is not None else defaults.collection_box_emptying_min,
        supermarket_capacity_cards=int(supermarket_capacity) if supermarket_capacity is not None else defaults.supermarket_capacity_cards,
    )


def _parse_kanban_cards_sheet(xls: pd.ExcelFile) -> dict[str, KanbanCardConfig]:
    """
    Parse the "KanbanCardsSetup" sheet into {sachnummer: KanbanCardConfig}.

    Expected columns (header row 0): Sachnummer | BatchSize | CardsToTrigger
    | HTL3 | HTL5 | HTL6 (a trailing free-text notes column may also be
    present — ignored).

    The HTL3/HTL5/HTL6 columns mark, per product, which line(s) that
    product's Kanban cards are eligible to be drawn from / produced on —
    an "X" (case-insensitive, any surrounding whitespace) marks a line as
    eligible; blank/NaN means not eligible for that line. Eligible lines
    are collected into eligible_lines in a fixed canonical order (HTL3,
    HTL5, HTL6), regardless of the column order actually present in the
    sheet. Only line columns that actually appear in the sheet are
    checked, so older workbooks without these columns still parse fine
    (eligible_lines simply comes back empty for every row).

    Rows with a blank Sachnummer, or a missing BatchSize/CardsToTrigger, are
    skipped silently (mirrors the "-"/blank-tolerance pattern used elsewhere
    in this module).
    """
    sheet_name = "KanbanCardsSetup"
    if sheet_name not in xls.sheet_names:
        return {}

    df = pd.read_excel(xls, sheet_name=sheet_name, header=0)
    df.columns = [str(c).strip() for c in df.columns]

    # Fixed canonical order, independent of the sheet's actual column order;
    # only columns actually present in this sheet are consulted.
    line_cols = [ln for ln in ("HTL3", "HTL5", "HTL6") if ln in df.columns]

    def _is_marked(raw) -> bool:
        if raw is None or (isinstance(raw, float) and pd.isna(raw)):
            return False
        return str(raw).strip().lower() == "x"

    result: dict[str, KanbanCardConfig] = {}
    for _, row in df.iterrows():
        sachnr = str(row.get("Sachnummer", "")).strip()
        if not sachnr or sachnr.lower() == "nan":
            continue
        batch_raw = row.get("BatchSize")
        trigger_raw = row.get("CardsToTrigger")
        if pd.isna(batch_raw) or pd.isna(trigger_raw):
            continue

        eligible_lines = [ln for ln in line_cols if _is_marked(row.get(ln))]

        result[sachnr] = KanbanCardConfig(
            sachnummer=sachnr,
            batch_size=int(batch_raw),
            cards_to_trigger=int(trigger_raw),
            eligible_lines=eligible_lines,
        )

    return result


def _parse_customer_demand_kanban_sheet(
    xls: pd.ExcelFile,
    kanban_cards: Optional[dict[str, "KanbanCardConfig"]] = None,
) -> list[KanbanWithdrawalEvent]:
    """
    Parse the "CustomerDemandKanban" sheet — flat long layout:

        Date | Time | Product | TotalQuantity | LineId

    Returns one KanbanWithdrawalEvent per row (one row = one withdrawal
    event), in sheet order.

    "LineId" is a DOWNSTREAM line (whoever downstream is requesting the
    product), NOT one of our production lines (HTL3/HTL5/HTL6) — `line_id`
    is taken as-is from the sheet and not validated against `kanban_cards`
    at all.

    `kanban_cards` is accepted for call-signature compatibility with
    load_config(), which passes the already-parsed KanbanCardsSetup dict
    in, but is currently unused by this parser.

    Rows with quantity <= 0 (or blank) are dropped — nothing was actually
    withdrawn, so there's nothing to act on downstream.
    """
    rows = _parse_long_demand_rows(xls, "CustomerDemandKanban")

    result: list[KanbanWithdrawalEvent] = []
    for r in rows:
        if r["quantity"] <= 0:
            continue
        result.append(KanbanWithdrawalEvent(
            date=r["date"],
            time=r["time"],
            product=r["product"],
            quantity=r["quantity"],
            line_id=r["line_id"],
        ))

    return result


def _parse_supermarkets_sheet(
    xls: pd.ExcelFile,
) -> dict[str, list[SupermarketSlotConfig]]:
    """
    Parse the "Supermarkets" sheet into {line: [SupermarketSlotConfig, ...]},
    one entry per physical slot/row, in sheet order within each line.

        Line | RowNumber | Type | Capacity | Sachnummer | InitialState | InitialPcsPartial

    Also accepts the older "SupermInState" or "SupermarketInitialState"
    sheet names for backward compatibility with not-yet-migrated
    workbooks; tries "Supermarkets" first. Those older sheets used a
    narrower per-(line,sachnummer) layout (Line | Sachnummer |
    InitialCards | InitialPcsPartial, no RowNumber/Type/Capacity) — if one
    of those is found, the missing columns simply default (RowNumber=0,
    Type="", Capacity=0) rather than erroring, with a warning.

    This is a full per-slot layout of every line's supermarket, including
    "Exotic" (push, no fixed product) slots — see SupermarketSlotConfig
    docstring.
    """
    sheet_name = None
    for candidate in ("Supermarkets", "SupermInState", "SupermarketInitialState"):
        if candidate in xls.sheet_names:
            sheet_name = candidate
            break
    if sheet_name is None:
        return {}

    df = pd.read_excel(xls, sheet_name=sheet_name, header=0)
    df.columns = [str(c).strip() for c in df.columns]

    required = ("Line", "RowNumber", "Type", "Capacity", "Sachnummer", "InitialState", "InitialPcsPartial")
    missing_cols = [c for c in required if c not in df.columns]
    if missing_cols:
        import warnings
        warnings.warn(
            f"'{sheet_name}' sheet is missing column(s) {missing_cols} "
            "(pre-v5 layout?); those fields will default to 0/blank for every row.",
            stacklevel=2,
        )

    result: dict[str, list[SupermarketSlotConfig]] = {}
    for _, row in df.iterrows():
        line = str(row.get("Line", "")).strip()
        if not line or line.lower() == "nan":
            continue

        row_number_raw = row.get("RowNumber")
        slot_type = str(row.get("Type", "")).strip()
        capacity_raw = row.get("Capacity")

        sachnr_raw = row.get("Sachnummer", "")
        sachnr = "" if pd.isna(sachnr_raw) else str(sachnr_raw).strip()
        if sachnr.lower() == "nan":
            sachnr = ""

        init_state_raw = row.get("InitialState")
        partial_raw = row.get("InitialPcsPartial")

        slot = SupermarketSlotConfig(
            line=line,
            row_number=int(row_number_raw) if pd.notna(row_number_raw) else 0,
            slot_type=slot_type,
            capacity=int(capacity_raw) if pd.notna(capacity_raw) else 0,
            sachnummer=sachnr,
            initial_state=int(init_state_raw) if pd.notna(init_state_raw) else 0,
            initial_pcs_partial=int(partial_raw) if pd.notna(partial_raw) else 0,
        )
        result.setdefault(line, []).append(slot)

    return result


def _parse_oee_sheet(xls: pd.ExcelFile) -> dict[str, list[OEEBinConfig]]:
    """
    Parse the "OEE" sheet into {line: [OEEBinConfig, ...]}, one entry per
    bin row, in sheet order within each line.

        Line | Bin_down | Bin_up | Probability

    Gracefully no-ops (returns {}) if the sheet is absent, so workbooks
    without it still load fine — this is purely additive, mirroring the
    Kanban/Supermarkets/Shifts sheets elsewhere in this module. The
    sampling logic that will actually consume this distribution (e.g.
    drawing a per-line OEE value from its bins) is implemented separately
    as a later step; this parser only reads the raw bin table.

    Bin_down/Bin_up/Probability are read as floats as-is — pandas already
    resolves the workbook's own (locale-specific) decimal formatting to a
    real float value, so no string/comma parsing is needed here regardless
    of whether the workbook displays e.g. "0,0500" (German) or "0.0500".

    Rows with a blank Line, or a blank Bin_down/Bin_up/Probability, are
    skipped silently (mirrors the blank-tolerance pattern used elsewhere
    in this module).
    """
    sheet_name = "OEE"
    if sheet_name not in xls.sheet_names:
        return {}

    df = pd.read_excel(xls, sheet_name=sheet_name, header=0)
    df.columns = [str(c).strip() for c in df.columns]

    required = ("Line", "Bin_down", "Bin_up", "Probability")
    missing_cols = [c for c in required if c not in df.columns]
    if missing_cols:
        import warnings
        warnings.warn(
            f"'{sheet_name}' sheet is missing column(s) {missing_cols}; "
            "oee_distribution will be empty.",
            stacklevel=2,
        )
        return {}

    result: dict[str, list[OEEBinConfig]] = {}
    for _, row in df.iterrows():
        line = str(row.get("Line", "")).strip()
        if not line or line.lower() == "nan":
            continue

        bin_down_raw = row.get("Bin_down")
        bin_up_raw = row.get("Bin_up")
        prob_raw = row.get("Probability")
        if pd.isna(bin_down_raw) or pd.isna(bin_up_raw) or pd.isna(prob_raw):
            continue

        result.setdefault(line, []).append(OEEBinConfig(
            line=line,
            bin_down=float(bin_down_raw),
            bin_up=float(bin_up_raw),
            probability=float(prob_raw),
        ))

    return result


def _parse_shift_time_cell(raw) -> Optional[_dt.time]:
    """
    Parse one "Starts"/"Ends" cell from the Shifts sheet's top table.
    Excel hands these back to pandas in whichever of these shapes the
    workbook's own cell formatting produces — all three are handled:
      - a bare python/pandas time-of-day value (datetime.time)
      - a full timestamp with a throwaway date part (pandas.Timestamp /
        datetime.datetime) — Excel's own behaviour for a cell formatted
        as "time" but read generically
      - a plain "HH:MM:SS" (or "HH:MM") string
    Returns None (never raises) for anything else, so a malformed cell
    degrades to "skip this shift definition with a warning" at the call
    site rather than crashing the whole load.
    """
    if raw is None or (isinstance(raw, float) and pd.isna(raw)):
        return None
    if isinstance(raw, _dt.time):
        return raw
    if isinstance(raw, (pd.Timestamp, _dt.datetime)):
        return raw.time()
    if isinstance(raw, str):
        s = raw.strip()
        for fmt in ("%H:%M:%S", "%H:%M"):
            try:
                return _dt.datetime.strptime(s, fmt).time()
            except ValueError:
                continue
    return None


def _parse_shift_day_cell(raw) -> Optional[_dt.date]:
    """
    Parse one "Day" cell from the Shifts sheet's bottom (availability)
    table into a real date — needed (unlike CustomerDemand's "Date"
    column, which this module deliberately keeps as a raw string; see
    that sheet's parser) because ShiftCalendar does real date arithmetic
    (the Night-shift previous-day lookup, next_on_transition's forward
    scan). Handles the same shapes _parse_shift_time_cell does for time
    cells: a bare date, a full timestamp, or a "DD.MM.YYYY" string —
    Excel may hand back either depending on how the "Day" column happens
    to be formatted in the workbook.
    """
    if raw is None or (isinstance(raw, float) and pd.isna(raw)):
        return None
    if isinstance(raw, _dt.date) and not isinstance(raw, _dt.datetime):
        return raw
    if isinstance(raw, (pd.Timestamp, _dt.datetime)):
        return raw.date()
    if isinstance(raw, str):
        m = _SHIFT_DATE_RE.match(raw.strip())
        if m:
            d, mo, y = (int(x) for x in m.groups())
            try:
                return _dt.date(y, mo, d)
            except ValueError:
                return None
    return None


def _parse_shifts_sheet(xls: pd.ExcelFile) -> ShiftCalendar:
    """
    Parse the "Shifts" sheet — two stacked tables in one sheet:

      1) Shift-time definitions (top):
             Shifts    | Starts   | Ends
             Morning   | 06:00:00 | 13:59:59
             Afternoon | 14:00:00 | 21:59:59
             Night     | 22:00:00 | 05:59:59
         Night's Ends < Starts is EXPECTED — it wraps past midnight; see
         ShiftDefinition.wraps_midnight. Parsing of this table stops at
         the first row whose first cell isn't a name introduced by the
         header (blank separator row, or the second table's own header).

      2) Per-line, per-day availability grid (further down the same
         sheet, located by scanning for a row containing both "Line" and
         "Day"):
             Line | Day        | CalendarDay | Morning | Afternoon | Night
             HTL3 | 01.09.2026 | Tue         | X       | X         | X
             HTL6 | 01.09.2026 | Tue         | X       | X         |
         "CalendarDay" (weekday name) is read from the header but never
         used — it's redundant with "Day" (the actual date) and present
         in the workbook purely for human readability, per the sheet's
         own layout. Only columns whose header matches an already-parsed
         shift name (table 1) are read as shift-availability columns, so
         a workbook with shift columns in a different order, or with
         extra/renamed shifts, still parses correctly. An "X"
         (case-insensitive, whitespace-tolerant) marks that (line, date,
         shift) as ON; blank/NaN/anything else means OFF.

    A `(line, date)` pair with NO row at all in the grid is also OFF —
    this parser does not synthesize rows for uncovered lines/dates; see
    ShiftCalendar.is_line_on's docstring for why that's the intended
    "off-by-default" behaviour, not a gap to fill in here.

    Returns an empty ShiftCalendar (no shifts, no availability at all) if
    the "Shifts" sheet is absent from the workbook, so older workbooks
    without shift support keep loading exactly as before — mirrors the
    additive/optional convention used for the Kanban sheets.
    Callers that need to distinguish "no shift sheet at all" (shift
    on/off checking is simply not in effect) from "shift sheet present
    but this particular line/day is off" should check `bool(cfg.
    shift_calendar.shifts)` first.
    """
    sheet_name = "Shifts"
    if sheet_name not in xls.sheet_names:
        return ShiftCalendar()

    raw = pd.read_excel(xls, sheet_name=sheet_name, header=None)

    # --- table 1: shift-time definitions ------------------------------------
    def_header_row = _find_header_row(raw, required=("shifts", "starts", "ends"), max_scan=5)
    shifts: dict[str, ShiftDefinition] = {}
    r = def_header_row + 1
    while r < len(raw):
        name_cell = raw.iat[r, 0]
        name = "" if pd.isna(name_cell) else str(name_cell).strip()
        if not name or name.lower() == "nan":
            break
        # Stop at the second table's own header row ("Line | Day | ...").
        # Workbooks don't always leave a blank separator row between the
        # two stacked tables (the grid header can sit directly under the
        # last shift-time-definition row), so a blank/NaN first cell isn't
        # a reliable end-of-table-1 signal on its own — checking for the
        # grid header's telltale "line"+"day" cells is.
        row_cells_lower = (
            raw.iloc[r].fillna("").astype(str).str.strip().str.lower().tolist()
        )
        if "line" in row_cells_lower and "day" in row_cells_lower:
            break
        start = _parse_shift_time_cell(raw.iat[r, 1])
        end = _parse_shift_time_cell(raw.iat[r, 2])
        if start is None or end is None:
            import warnings
            warnings.warn(
                f"'{sheet_name}' sheet: shift {name!r} (row {r}) has an "
                "unparseable Starts/Ends cell — skipping this shift "
                "definition entirely.",
                stacklevel=2,
            )
            r += 1
            continue
        shifts[name] = ShiftDefinition(name=name, start=start, end=end)
        r += 1

    if not shifts:
        import warnings
        warnings.warn(
            f"'{sheet_name}' sheet is present but no shift-time "
            "definitions could be parsed from its top table — the "
            "availability grid (if any) will be ignored and every line "
            "will report as OFF for every shift.",
            stacklevel=2,
        )
        return ShiftCalendar()

    # --- table 2: per-line, per-day availability grid -----------------------
    grid_header_row: Optional[int] = None
    for i in range(r, len(raw)):
        cells = raw.iloc[i].fillna("").astype(str).str.strip().str.lower().tolist()
        if "line" in cells and "day" in cells:
            grid_header_row = i
            break

    if grid_header_row is None:
        import warnings
        warnings.warn(
            f"'{sheet_name}' sheet has shift-time definitions but no "
            "Line/Day availability grid beneath them — every line will "
            "report as OFF for every shift (see ShiftCalendar.is_line_on's "
            "off-by-default rule).",
            stacklevel=2,
        )
        return ShiftCalendar(shifts=shifts)

    header_cells = raw.iloc[grid_header_row].fillna("").astype(str).str.strip().tolist()
    col_idx: dict[str, int] = {}
    for i, h in enumerate(header_cells):
        key = h.strip().lower()
        if key in ("line", "day", "calendarday"):
            col_idx.setdefault(key, i)
        elif h in shifts:
            # Exact (case-sensitive) match against an already-parsed
            # shift name — "Morning"/"Afternoon"/"Night" etc.
            col_idx.setdefault(h, i)

    missing = [c for c in ("line", "day") if c not in col_idx]
    if missing:
        import warnings
        warnings.warn(
            f"'{sheet_name}' sheet's availability grid is missing "
            f"required column(s) {missing}; no availability will be "
            "loaded (every line reports OFF for every shift).",
            stacklevel=2,
        )
        return ShiftCalendar(shifts=shifts)

    shift_cols = [name for name in shifts if name in col_idx]

    availability: dict[str, dict[_dt.date, set[str]]] = {}
    for i in range(grid_header_row + 1, len(raw)):
        row = raw.iloc[i]

        line_raw = row.iat[col_idx["line"]]
        line = "" if pd.isna(line_raw) else str(line_raw).strip()
        if not line or line.lower() == "nan":
            continue

        the_date = _parse_shift_day_cell(row.iat[col_idx["day"]])
        if the_date is None:
            continue

        on_shifts: set[str] = set()
        for shift_name in shift_cols:
            mark = row.iat[col_idx[shift_name]]
            if isinstance(mark, str) and mark.strip().lower() == "x":
                on_shifts.add(shift_name)

        availability.setdefault(line, {})[the_date] = on_shifts

    return ShiftCalendar(shifts=shifts, availability=availability)


def _parse_xlsx_product_master(
    xlsx_path: str | Path,
) -> dict[str, int]:
    """
    Parse product_master.xlsx and return a sachnummer → TTNr mapping.

    Expected columns (first row is header):
        TTNr | FullName | Prefix | Kunde

    The TTNr column may contain dirty values such as
    "410 (672 SIS= 4 SKA Teile abgeben)" — the leading integer is extracted
    via regex rather than a bare int() cast to avoid crashes on such rows.

    The FullName column holds the sachnummer (e.g. "F00RC00419").

    Returns
    -------
    dict[sachnummer_str, ttnr_int]
        e.g. {"F00RC00419": 419, "F00RC00515": 515, ...}
    """
    import re

    xlsx_path = Path(xlsx_path)
    if not xlsx_path.exists():
        raise FileNotFoundError(f"product_master.xlsx not found: {xlsx_path}")

    df = pd.read_excel(xlsx_path, header=0)
    df.columns = [str(c).strip() for c in df.columns]

    result: dict[str, int] = {}
    _leading_int = re.compile(r"^\s*(\d+)")

    for _, row in df.iterrows():
        raw_ttnr    = row.get("TTNr", "")
        raw_sachnr  = row.get("FullName", "")

        if pd.isna(raw_ttnr) or pd.isna(raw_sachnr):
            continue

        sachnummer = str(raw_sachnr).strip()
        if not sachnummer or sachnummer.lower() == "nan":
            continue

        # Extract leading integer from TTNr cell (handles dirty values)
        m = _leading_int.match(str(raw_ttnr))
        if not m:
            continue
        ttnr = int(m.group(1))

        result[sachnummer] = ttnr

    return result


def _parse_xlsx_setup_times(
    xlsx_path: str | Path,
) -> dict[str, dict[int, dict[tuple[int, int], int]]]:
    """
    Parse HTL_setup_times.xlsx.

    The workbook has one sheet per line (HTL3, HTL5, HTL6).
    Each sheet has a header row followed by data rows with five columns:
        Line | StartTTNr | EndTTNr | Setup_1MA | Setup_2MA

    The "Line" column is redundant (the sheet name is the line); it is ignored.

    Returns [line_name][n_workers][(from_ttnr, to_ttnr)] → seconds.
        n_workers 1 → Setup_1MA column  (1 Mitarbeiter)
        n_workers 2 → Setup_2MA column  (2 Mitarbeiter)
    """
    import warnings
    import openpyxl

    xlsx_path = Path(xlsx_path)
    if not xlsx_path.exists():
        raise FileNotFoundError(f"Setup-times Excel not found: {xlsx_path}")

    # NOTE on read_only=False (deliberate, do not "optimise" back to True):
    # openpyxl's read_only mode trusts the <dimension> tag cached inside each
    # sheet's XML to decide where the data ends, instead of scanning the
    # actual rows. If that cached tag is stale — smaller than the real
    # extent of the data, which happens when rows are appended later by a
    # tool/process that doesn't refresh the cached dimension — iter_rows()
    # in read_only mode SILENTLY stops at that cached (wrong) row count.
    # No exception, no warning: rows past the stale boundary are simply
    # never yielded, which is exactly what caused specific TTNr pairs
    # (e.g. rows >500 in a 1261-row sheet) to resolve as "0 s / not found"
    # even though the data was present in the workbook. Loading normally
    # (read_only=False) fully parses the sheet XML regardless of the
    # cached dimension tag, so this class of bug can't happen. For
    # setup-time workbooks (hundreds to a few thousand rows) the memory/
    # speed cost of non-read-only mode is negligible.
    result: dict[str, dict[int, dict[tuple[int, int], int]]] = {}
    wb = openpyxl.load_workbook(xlsx_path, read_only=False, data_only=True)

    for sheet_name in wb.sheetnames:
        ws = wb[sheet_name]
        line_table: dict[int, dict[tuple[int, int], int]] = {1: {}, 2: {}}
        first_row = True

        n_data_rows   = 0
        n_parsed      = 0
        skipped_none: list[int] = []
        skipped_bad:  list[int] = []

        for row_idx, row in enumerate(
            ws.iter_rows(min_col=1, max_col=5, values_only=True), start=1
        ):
            if first_row:          # skip header row
                first_row = False
                continue
            if all(v is None for v in row[0:5]):
                continue           # fully blank row (e.g. trailing rows) — not an error
            n_data_rows += 1
            if any(v is None for v in row[1:5]):
                skipped_none.append(row_idx)
                continue
            try:
                key      = (int(row[1]), int(row[2]))
                secs_1ma = int(row[3])
                secs_2ma = int(row[4])
            except (ValueError, TypeError):
                skipped_bad.append(row_idx)
                continue

            line_table[1][key] = secs_1ma
            line_table[2][key] = secs_2ma
            n_parsed += 1

        result[sheet_name] = line_table

        # Loud diagnostics: never again let rows silently disappear.
        if skipped_none or skipped_bad:
            warnings.warn(
                f"HTL_setup_times.xlsx sheet {sheet_name!r}: parsed {n_parsed}/"
                f"{n_data_rows} data rows. Skipped rows (1-based, incl. header) "
                f"with missing values: {skipped_none[:20]}"
                f"{' …' if len(skipped_none) > 20 else ''}; "
                f"non-numeric/malformed: {skipped_bad[:20]}"
                f"{' …' if len(skipped_bad) > 20 else ''}",
                stacklevel=2,
            )

    wb.close()
    return result


def export_setup_times_summary(
    csv_setup_times: dict[str, dict[int, dict[tuple[int, int], int]]],
    out_path: str | Path = "csv_setup_times_full.json",
) -> Path:
    """
    Dump the FULL parsed setup-times structure to a JSON file, bypassing any
    IDE debugger display cap (e.g. PyCharm/PyDev's default 500-item limit
    on container inspection, controlled by PYDEVD_CONTAINER_RANDOM_ACCESS_MAX_ITEMS).

    Use this whenever you need to eyeball *all* rows instead of the first
    500 the debugger shows you:

        cfg = load_config(...)
        export_setup_times_summary(cfg.csv_setup_times)

    Also prints a per-line/per-worker-count row count to stdout so you can
    sanity-check totals against the source workbook without opening it.
    """
    import json

    out_path = Path(out_path)

    serializable: dict[str, dict[str, dict[str, int]]] = {}
    for line_name, per_workers in csv_setup_times.items():
        serializable[line_name] = {}
        for n_workers, pairs in per_workers.items():
            print(f"{line_name}  {n_workers}-MA:  {len(pairs)} rows")
            serializable[line_name][str(n_workers)] = {
                f"{frm}->{to}": secs for (frm, to), secs in pairs.items()
            }

    out_path.write_text(json.dumps(serializable, indent=2, ensure_ascii=False))
    print(f"\nFull setup-times dump written to: {out_path.resolve()}")
    return out_path


# ===========================================================================
# Public API
# ===========================================================================

def load_config(
    excel_path: str | Path = "ProductionPlanning_v6.xlsx",
    setup_xlsx_path: str | Path = "HTL_setup_times.xlsx",
    product_master_path: str | Path = "product_master.xlsx",
    line_map: dict[int, str] | None = None,
) -> SimConfig:
    """
    Parse the production planning + setup-times Excel workbooks; return a
    validated SimConfig. Called by api.routes_simulate (simulate_mixed)
    and sim.entrypoints.cli_mixed.

    Parameters
    ----------
    excel_path           : path to ProductionPlanning_v6.xlsx
    setup_xlsx_path      : path to HTL_setup_times.xlsx (one sheet per line)
    product_master_path  : path to product_master.xlsx
                           (columns: TTNr | FullName | Prefix | Kunde)
                           used to build the sachnummer → TTNr lookup so
                           that sim.produce can resolve a product's
                           sachnummer before indexing into csv_setup_times
    line_map             : override {int → line_name} mapping
                           (default: {1: "HTL3", 2: "HTL5", 3: "HTL6"})
    """
    excel_path = Path(excel_path)
    if not excel_path.exists():
        raise FileNotFoundError(f"Excel workbook not found: {excel_path}")

    lmap = line_map or LINE_NUMBER_TO_NAME
    xls  = pd.ExcelFile(excel_path)

    stations, avail_time = _parse_process_sheet(xls, lmap)

    # Load sachnummer → TTNr map; return empty dict if file is absent so
    # the rest of the simulation can still run (with degraded setup-time lookup).
    product_master_path = Path(product_master_path)
    if product_master_path.exists():
        sachnummer_to_ttnr = _parse_xlsx_product_master(product_master_path)
    else:
        import warnings
        warnings.warn(
            f"product_master.xlsx not found at {product_master_path!r}; "
            "sachnummer_to_ttnr will be empty — setup-time lookup by sachnummer will fail.",
            stacklevel=2,
        )
        sachnummer_to_ttnr = {}

    inventories, chutes = _parse_inventories_sheet(xls)

    cfg = SimConfig(
        n_lines               = len(lmap),
        line_names            = list(lmap.values()),
        stations              = stations,
        buffers               = _parse_buffers_sheet(xls, lmap),
        inspection            = _parse_inspection_sheet(xls, lmap),
        packaging             = _parse_packaging_sheet(xls),
        demand                = _parse_demand_sheet(xls, line_names=list(lmap.values())),
        initial_setup_state   = _parse_setup_state_sheet(xls, lmap),
        csv_setup_times       = _parse_xlsx_setup_times(setup_xlsx_path),
        sachnummer_to_ttnr    = sachnummer_to_ttnr,
        line_stations         = load_line_stations(excel_path, lmap),
        available_time_min_day= avail_time,
        inventories           = inventories,
        chutes                = chutes,
        kanban_cards          = (_kanban_cards_parsed := _parse_kanban_cards_sheet(xls)),
        kanban_withdrawals    = _parse_customer_demand_kanban_sheet(xls, kanban_cards=_kanban_cards_parsed),
        supermarkets          = _parse_supermarkets_sheet(xls),
        kanban_timing         = _parse_kanban_config_sheet(xls),
        shift_calendar        = _parse_shifts_sheet(xls),
        oee_distribution      = _parse_oee_sheet(xls),
    )

    _validate(cfg)
    return cfg


def _validate(cfg: SimConfig) -> None:
    errors: list[str] = []
    if cfg.n_lines < 1:
        errors.append("n_lines must be >= 1")
    for ln in cfg.line_names:
        if not cfg.stations.get(ln):
            errors.append(f"No stations loaded for line {ln}")
        if ln not in cfg.buffers:
            errors.append(f"No buffers loaded for line {ln}")
        if ln not in cfg.inspection:
            errors.append(f"No inspection config for line {ln}")
        else:
            insp = cfg.inspection[ln]
            total = round(insp.pass_rate + insp.rework_rate + insp.scrap_rate, 6)
            if abs(total - 1.0) > 1e-4:
                errors.append(f"Inspection rates for {ln} sum to {total}, must be 1.0")
    if errors:
        raise ValueError("SimConfig validation failed:\n  " + "\n  ".join(errors))

    # --- Shifts: soft (warning-only) checks -------------------------------
    # A line with zero configured availability isn't a hard error — an
    # off-by-default line is valid per ShiftCalendar's own contract — but
    # it's a common workbook mistake (e.g. a line's rows accidentally
    # deleted/omitted for the whole run), so it's worth surfacing.
    if cfg.shift_calendar.shifts:
        for ln in cfg.line_names:
            if not cfg.shift_calendar.availability.get(ln):
                import warnings
                warnings.warn(
                    f"Line {ln!r} has no rows at all in the 'Shifts' "
                    "availability grid — it will be treated as OFF for "
                    "every shift, every day (off-by-default; see "
                    "ShiftCalendar.is_line_on). If this line should be "
                    "schedulable, add its rows to the sheet.",
                    stacklevel=2,
                )


# ===========================================================================
# Quick self-test
# ===========================================================================
if __name__=="__main__":
    cfg = load_config("domain/ProductionPlanning_v6.xlsx", "domain/HTL_setup_times.xlsx", "domain/product_master.xlsx")
    print(len(cfg.kanban_withdrawals))
    print(cfg.kanban_withdrawals[0] if cfg.kanban_withdrawals else "EMPTY LIST")

if __name__ == "__main__":
    excel          = sys.argv[1] if len(sys.argv) > 1 else "domain/ProductionPlanning_v6.xlsx"
    setup_xlsx     = sys.argv[2] if len(sys.argv) > 2 else "domain/HTL_setup_times.xlsx"
    product_master = sys.argv[3] if len(sys.argv) > 3 else "domain/product_master.xlsx"
    cfg            = load_config(excel, setup_xlsx, product_master)
    print(cfg.summary())

    print("\n--- Initial setup state ---")
    for ln, state in cfg.initial_setup_state.items():
        print(f"  {ln}: {state}")

    print("\n--- Excel setup times (HTL3, 1MA, first 5) ---")
    for (f, t), secs in list(cfg.csv_setup_times.get("HTL3", {}).get(1, {}).items())[:5]:
        print(f"  TTNr {f} → {t}: {secs} s")

    # Full row counts + JSON dump, so you can see everything (debugger
    # variable viewers cap display at 500 items per container by default).
    export_setup_times_summary(cfg.csv_setup_times)

    print("\n--- sachnummer → TTNr (first 5) ---")
    for sachnr, ttnr in list(cfg.sachnummer_to_ttnr.items())[:5]:
        print(f"  {sachnr} → {ttnr}")

    print("\n--- StationLine catalogue ---")
    for ln, stations in cfg.line_stations.items():
        print(f"  {ln}: {[s.station for s in stations]}")

    print("\n--- Inventories ---")
    for name, lanes in cfg.inventories.items():
        for inv in lanes:
            print(f"  {inv}")

    print("\n--- FIFO Chutes ---")
    for name, lanes in cfg.chutes.items():
        for ch in lanes:
            print(f"  {ch}")

    print("\n--- Kanban timing ---")
    print(f"  {cfg.kanban_timing}")

    print(f"\n--- Kanban cards ({len(cfg.kanban_cards)} products, first 5) ---")
    for sachnr, card in list(cfg.kanban_cards.items())[:5]:
        print(f"  {card}")

    print(f"\n--- Kanban withdrawal events ({len(cfg.kanban_withdrawals)}, first 3) ---")
    for ev in cfg.kanban_withdrawals[:3]:
        print(f"  {ev}")

    n_slots = sum(len(v) for v in cfg.supermarkets.values())
    print(f"\n--- Supermarkets ({n_slots} slots across {len(cfg.supermarkets)} line(s)) ---")
    for line, slots in cfg.supermarkets.items():
        for slot in slots:
            print(f"  {slot}")

    print(f"\n--- Shifts ({len(cfg.shift_calendar.shifts)} shift(s) defined) ---")
    for name, sd in cfg.shift_calendar.shifts.items():
        print(f"  {name}: {sd.start} - {sd.end}"
              f"{'  (wraps midnight)' if sd.wraps_midnight else ''}")
    for ln in cfg.line_names:
        days = cfg.shift_calendar.availability.get(ln, {})
        print(f"  {ln}: {len(days)} configured day(s)")

    n_oee_bins = sum(len(v) for v in cfg.oee_distribution.values())
    print(f"\n--- OEE distribution ({n_oee_bins} bin(s) across {len(cfg.oee_distribution)} line(s)) ---")
    for line, bins in cfg.oee_distribution.items():
        total_p = round(sum(b.probability for b in bins), 6)
        print(f"  {line}  (Σprobability = {total_p}):")
        for b in bins:
            print(f"    [{b.bin_down:.4f}, {b.bin_up:.4f}) -> p={b.probability}")
