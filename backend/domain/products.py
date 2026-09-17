"""
domain/products.py
===================
Product-line compatibility matrix for the HTL Homburg plant.

This module encodes which products can run on which lines, what type of
product they are, which stations they require, and what the
approval status is.

Design principles
-----------------
* The product catalogue itself (Product ID, Kunde, Type, Freigabe, Prio
  per line) is read at import time from ProductMatrix.xlsx
* Station parameters (LINE_STATIONS) live in domain.config.
* `lookup()` and `PRODUCT_MATRIX` are the consumers' entry points — used
  by domain.line_priority (MatrixPriorityStrategy), sim.produce
  (routing/changeover), and sim.fill.push.dispatch to resolve a part's
  routing, cycle times, and feasible lines.

Class hierarchy
---------------
ProductClass  (Enum)   — product family / colour group
Freigabe      (Enum)   — approval / feasibility status per line
LineClass              — line-specific view: approval, active stations,
                         total cycle time, priority
ProductLineInfo        — full product record: IDs + dict[line → LineClass]

ProductMatrix.xlsx columns
---------------------------
ProductNumber | Kunde | Type | HTL3_Freigabe | HTL3_Prio |
HTL5_Freigabe | HTL5_Prio | HTL6_Freigabe | HTL6_Prio

Usage
-----
    from domain.products import lookup, PRODUCT_MATRIX
    info = lookup("0001234567")          # by product_id
    info = lookup("BOSCH-CLIENT")        # by Kunde name

    # Check which lines can produce this part
    for line_name, lc in info.lines.items():
        print(line_name, lc.freigabe, lc.total_cycle_time_s)
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from enum import Enum
from pathlib import Path
from typing import Optional

import openpyxl

from domain.config import LINE_STATIONS, StationLine, get_station, BufferConfig

# Location of the compatibility matrix workbook. Defaults to a file living
# next to this module; override by setting products.EXCEL_PATH before
# import, or by calling load_matrix_from_excel() with an explicit path.
EXCEL_PATH = Path(__file__).resolve().parent / "ProductMatrix.xlsx"


# ===========================================================================
# Enumerations
# ===========================================================================

class ProductClass(str, Enum):
    """
    Product family derived from text colour in the source Excel.

    STIFT           – red text   – requires Stift_Einpressen press station
    DRS             – blue text  – DRS sub-assembly is supplied via the
                       Chu_vor_HTL3 "DRS" FIFO chute lane / Inv_nach_DRS,
                       NOT an in-line station on the main routing anymore
                       (see "Inventories" sheet — DRS is produced by a
                       parallel process, decoupled from the main line).
    STAB_LOCHFILTER – purple text – Lochfilter sub-assembly is likewise
                       supplied via the Chu_vor_HTL3 "Lochfilter" lane /
                       Inv_nach_Loch (parallel process); Pruefen remains
                       an in-line FIFO station after Beladen.
    STAHLRAHMEN     – green text  – steel-frame product, standard route
    STANDARD         – fallback when colour data is unavailable
    """
    STIFT           = "Stift"
    DRS             = "DRS"
    STAB_LOCHFILTER = "Stab_Lochfilter"
    STAHLRAHMEN     = "Stahlrahmen"
    STANDARD         = "Standard"


class Freigabe(str, Enum):
    """
    Approval status for a product on a specific line (cell background colour).

    VORHANDEN      – green  – approved, production is authorised
    MOEGLICH       – yellow – approval is possible / under review
    NICHT_MOEGLICH – red    – not feasible on this line
    """
    VORHANDEN      = "Freigabe vorhanden"       # green
    MOEGLICH       = "Freigabe moeglich"        # yellow
    NICHT_MOEGLICH = "Freigabe nicht moeglich"  # red


# ===========================================================================
# Helper: determine active station sequence for a product on a line
# ===========================================================================

def _active_station_sequence(
    line: str,
    product_class: ProductClass,
) -> list[StationLine]:
    """
    Return the ordered list of StationLine objects that a given product
    activates on a given line.
    """
    all_stations: dict[str, StationLine] = {
        s.station: s for s in LINE_STATIONS[line]
    }

    base = []

    # NOTE (Inventories/chute model): Lochfilter and DRS are not part of
    # the main line's in-line FIFO/BAS sequence. Their finished
    # sub-assemblies are supplied through the Chu_vor_HTL3 "Lochfilter" /
    # "DRS" FIFO chute lanes (backed by Inv_nach_Loch / Inv_nach_DRS),
    # filled by the separate, parallel
    # sim.produce.part_lifecycle.run_lochfilter_drs_production. The main
    # line's Beladen simply draws 1 unit from that chute lane before
    # starting — it never physically visits the Lochfilter/DRS machine
    # itself. Pruefen (STAB_LOCHFILTER's inspection step) DOES remain
    # in-line, after Beladen.
    if product_class == ProductClass.STAB_LOCHFILTER:
        base += ["Beladen", "Pruefen"]
    elif product_class == ProductClass.DRS:
        base += ["Beladen"]
    elif product_class == ProductClass.STIFT:
        base += ["Beladen","Stift_Einpressen"]
    else:
        base += ["Beladen"]

    base += ["Supfina", "Waschen", "Sichtpruefung"]

    # Resolve to StationLine objects (skip if not present on this line)
    sequence: list[StationLine] = []
    for name in base:
        if name in all_stations:
            sequence.append(all_stations[name])

    return sequence


def _total_cycle_time(stations: list[StationLine]) -> Optional[float]:
    """
    Sum of cycle times for the active station sequence — this is the
    single-part LATENCY (how long one part takes to travel the whole
    route), used only as the one-time pipeline fill/drain cost for a
    batch, never as a per-part pacing rate.
    Returns None if ANY station in the sequence is missing its cycle time (⚠ TODO).
    See _bottleneck_time() for the per-part pacing rate (takt time).
    """
    total = 0.0
    for s in stations:
        if s.cycle_time_s is None:
            return None          # incomplete data — caller should flag this
        total += s.cycle_time_s
    return round(total, 4)


def _bottleneck_time(stations: list[StationLine]) -> Optional[float]:
    """
    Max single-station cycle time along the active station sequence — the
    takt-limiting station. In a pipelined/BAS line (see sim.produce, which
    runs the part-by-part production loop) this, not the summed latency,
    is what governs steady-state throughput: once the pipeline is full, a
    new part clears the line every `bottleneck_time_s` seconds, not every
    `total_cycle_time_s` seconds.
    Returns None if ANY station in the sequence is missing its cycle time.
    """
    times = [s.cycle_time_s for s in stations]
    if not times or any(t is None for t in times):
        return None
    return round(max(times), 4)


def _active_buffer_sequence(
    line: str,
    stations_sequence: list[str],
    buffers: dict[str, list[BufferConfig]],
) -> list[BufferConfig]:
    """
    Return the ordered list of BufferConfig objects that are active for a
    given product's station sequence on a given line.

    A buffer is active when its `upstream` station is present in
    `stations_sequence`. Most buffers have a single, fixed `downstream`
    station name and are included as-is once their upstream qualifies.

    Some buffers (e.g. Buffer 1.1 on HTL3 / Buffer 2.1 on HTL5) have a
    `downstream` that is a dict of candidate stations rather than a single
    string, because the next station depends on which optional station the
    product actually visits (e.g. Beladen feeds either Stift_Einpressen or
    straight into Supfina). For those, the candidate key that appears in
    `stations_sequence` is selected and a resolved copy of the BufferConfig
    (with `downstream` set to that single station name) is returned in its
    place — the original BufferConfig objects in `buffers` are left
    untouched. If none of the candidates are in the sequence, the buffer is
    skipped, on the assumption that it doesn't apply to this routing.

    Note: this only resolves buffers using the upstream/downstream
    relationships already declared in `buffers`; it does not invent new
    buffer relationships for stations that have no buffer row at all in the
    source data (e.g. there is no declared Beladen → Lochfilter or
    Beladen → DRS buffer on HTL3 — only Beladen → {Stift_Einpressen, Supfina}
    is declared).

    Parameters
    ----------
    line               : "HTL3" | "HTL5" | "HTL6"
    stations_sequence  : ordered canonical station names active for the
                         product on this line (e.g. as produced by
                         _active_station_sequence(...).station_names)
    buffers            : dict[line_name → list[BufferConfig]], as loaded by
                         domain.config.load_config().buffers

    Returns
    -------
    list[BufferConfig], in the same order as `buffers[line]`, with any
    dual-downstream buffers resolved to a single concrete downstream
    station for this product's routing.
    """
    sequence_set = set(stations_sequence)
    active: list[BufferConfig] = []

    for buf in buffers.get(line, []):
        if buf.upstream not in sequence_set:
            continue

        if isinstance(buf.downstream, dict):
            resolved = next(
                (candidate for candidate in buf.downstream if candidate in sequence_set),
                None,
            )
            if resolved is None:
                continue  # neither candidate is active for this routing
            active.append(replace(buf, downstream=resolved))
        else:
            if buf.downstream not in sequence_set:
                continue
            active.append(buf)

    return active


# ===========================================================================
# LineClass
# ===========================================================================

@dataclass
class LineClass:
    """
    Line-specific view of a product's routing and timing.

    Attributes
    ----------
    line            : line identifier ("HTL3" | "HTL5" | "HTL6")
    freigabe        : approval status (Freigabe enum)
    stations        : ordered list of StationLine objects active for this product
    total_cycle_time_s : sum of cycle times for active stations (None if any are missing)
                         — single-part LATENCY, i.e. pipeline fill/drain cost,
                         NOT the per-part pacing rate. Do not multiply this by
                         qty to estimate a batch's duration; use bottleneck_time_s.
    bottleneck_time_s  : max single-station cycle time (the takt-limiting
                         station) — this IS the steady-state per-part pacing
                         rate on a pipelined/BAS line (None if any station in
                         the route is missing its cycle time).
    priority        : priority string ("1" / "2" / "3" / None) from ProductMatrix.xlsx
    """
    line: str
    freigabe: Freigabe
    stations: list[StationLine]
    total_cycle_time_s: Optional[float]     # None = ⚠ incomplete station data
    bottleneck_time_s: Optional[float] = None  # None = ⚠ incomplete station data
    priority: Optional[str] = None

    @property
    def station_names(self) -> list[str]:
        return [s.station for s in self.stations]

    @property
    def is_feasible(self) -> bool:
        """True when Freigabe is VORHANDEN or MOEGLICH."""
        return self.freigabe in (Freigabe.VORHANDEN, Freigabe.MOEGLICH)

    def __repr__(self) -> str:
        ct = (
            f"{self.total_cycle_time_s} s"
            if self.total_cycle_time_s is not None
            else "⚠ TODO"
        )
        bt = f"{self.bottleneck_time_s} s" if self.bottleneck_time_s is not None else "⚠ TODO"
        return (
            f"LineClass(line={self.line}, freigabe={self.freigabe.value}, "
            f"stations={self.station_names}, total_ct={ct}, bottleneck={bt})"
        )


# ===========================================================================
# ProductLineInfo
# ===========================================================================

@dataclass
class ProductLineInfo:
    """
    Full product record: all identifiers + per-line routing information.

    Attributes
    ----------
    product_id      : Bosch part number (primary key for lookup)
    kunde           : customer name / market designation
    hd              : HD (Hochdruck) designation or variant label
    product_class   : product family (determines optional stations)
    lines           : dict mapping line name → LineClass
                      Only lines with Freigabe != NICHT_MOEGLICH by default,
                      but NICHT_MOEGLICH lines are also stored for completeness.
    """
    product_id: str
    kunde: str
    product_class: ProductClass
    lines: dict[str, LineClass]             # "HTL3" | "HTL5" | "HTL6" → LineClass

    def feasible_lines(self) -> list[str]:
        """Return list of line names where production is approved or possible."""
        return [ln for ln, lc in self.lines.items() if lc.is_feasible]

    def describe(self) -> None:
        print(
            f"\nProduct: {self.product_id}  |  Kunde: {self.kunde}  "
            f"|  HD: {self.hd}  "
            f"|  Class: {self.product_class.value}"
        )
        print(f"  Feasible lines: {self.feasible_lines()}")
        for ln, lc in self.lines.items():
            ct = (
                f"{lc.total_cycle_time_s} s"
                if lc.total_cycle_time_s is not None
                else "⚠ TODO (missing cycle time)"
            )
            bt = (
                f"{lc.bottleneck_time_s} s"
                if lc.bottleneck_time_s is not None
                else "⚠ TODO"
            )
            prio = f"  prio={lc.priority}" if lc.priority else ""
            print(
                f"    {ln}: {lc.freigabe.value:<30} "
                f"stations={lc.station_names}  total={ct}  bottleneck={bt}{prio}"
            )


# ===========================================================================
# Internal factory helper
# ===========================================================================

def _make_line_class(
    line: str,
    freigabe: Freigabe,
    product_class: ProductClass,
    priority: Optional[str] = None,
) -> LineClass:
    """
    Build a LineClass by resolving the active station sequence and cycle times
    from line_stations.py. Keeps the matrix-loading code below compact.
    """
    stations = _active_station_sequence(line, product_class)
    total_ct = _total_cycle_time(stations)
    bottleneck_ct = _bottleneck_time(stations)

    return LineClass(
        line=line,
        freigabe=freigabe,
        stations=stations,
        total_cycle_time_s=total_ct,
        bottleneck_time_s=bottleneck_ct,
        priority=priority,
    )


# ===========================================================================
# PRODUCT MATRIX  — loaded from ProductMatrix.xlsx (Appendix C)
# ===========================================================================
#
# Each row in the sheet becomes one ProductLineInfo, with one LineClass per
# HTL3/HTL5/HTL6 column pair (Freigabe + Prio). See load_matrix_from_excel()
# below for the loading logic.
# ---------------------------------------------------------------------------

# Map the single-letter Freigabe codes stored in Excel back to the enum.
_FREIGABE_FROM_CODE: dict[str, Freigabe] = {
    "V": Freigabe.VORHANDEN,
    "M": Freigabe.MOEGLICH,
    "N": Freigabe.NICHT_MOEGLICH,
}

_LINE_COLUMNS = ("HTL3", "HTL5", "HTL6")


def _prio_from_cell(value: Optional[str]) -> Optional[str]:
    """Excel stores '-' for "no priority"; convert that back to None."""
    if value is None:
        return None
    value = str(value).strip()
    return None if value in ("", "-") else value


def load_matrix_from_excel(
    path: Path = EXCEL_PATH, sheet_name: str = "ProductMatrix"
) -> dict[str, ProductLineInfo]:
    """
    Read ProductMatrix.xlsx and build the same dict[str, ProductLineInfo]
    shape that used to be hardcoded here.

    Expected columns (sheet "ProductMatrix"):
    ProductNumber | Kunde | Type | HTL3_Freigabe | HTL3_Prio |
    HTL5_Freigabe | HTL5_Prio | HTL6_Freigabe | HTL6_Prio
    """
    wb = openpyxl.load_workbook(path, data_only=True)
    ws = wb[sheet_name]

    # Header -> column-index map so column order in the file doesn't matter.
    header_row = next(ws.iter_rows(min_row=1, max_row=1, values_only=True))
    col_index = {name: idx for idx, name in enumerate(header_row)}

    matrix: dict[str, ProductLineInfo] = {}

    for row in ws.iter_rows(min_row=2, values_only=True):
        if row[col_index["ProductNumber"]] is None:
            continue  # skip blank trailing rows

        product_id = str(row[col_index["ProductNumber"]]).strip()
        kunde = row[col_index["Kunde"]]
        pc = ProductClass[str(row[col_index["Type"]]).strip()]

        lines: dict[str, LineClass] = {}
        for line_name in _LINE_COLUMNS:
            code = row[col_index[f"{line_name}_Freigabe"]]
            freigabe = _FREIGABE_FROM_CODE[str(code).strip()]
            prio = _prio_from_cell(row[col_index[f"{line_name}_Prio"]])

            lines[line_name] = _make_line_class(line_name, freigabe, pc, prio)

        matrix[product_id] = ProductLineInfo(
            product_id=product_id,
            kunde=kunde,
            product_class=pc,
            lines=lines,
        )

    return matrix


# ===========================================================================
# Lookup API — used by sim.produce, sim.fill.push.dispatch, and
# domain.line_priority to resolve a part's routing/feasible lines
# ===========================================================================

def lookup(identifier: str) -> ProductLineInfo:
    """
    Find a product by Product_id, Kunde name.

    Search order: product_id → kunde
    Raises ValueError if not found.

    Parameters
    ----------
    identifier : any of the three ID fields

    Returns
    -------
    ProductLineInfo
    """
    identifier = identifier.strip()

    # 1. Direct product_id match
    if identifier in PRODUCT_MATRIX:
        return PRODUCT_MATRIX[identifier]

    # 2. Kunde name match (case-insensitive)
    for info in PRODUCT_MATRIX.values():
        if info.kunde.lower() == identifier.lower():
            return info

    raise ValueError(
        f"Product not found for identifier: {identifier!r}. "
        f"Known product_idn: {list(PRODUCT_MATRIX.keys())}"
    )


def all_feasible_for_line(line: str) -> list[ProductLineInfo]:
    """Return all products that can run on the given line (Freigabe != NICHT_MOEGLICH)."""
    return [
        info for info in PRODUCT_MATRIX.values()
        if line in info.lines and info.lines[line].is_feasible
    ]

# Module-level singleton — imported by other modules
PRODUCT_MATRIX: dict[str, ProductLineInfo] = load_matrix_from_excel()


# ===========================================================================
# Quick self-test
# ===========================================================================

if __name__ == "__main__":
    print("=== Product Line Matrix ===")
    print(f"Total products in matrix: {len(PRODUCT_MATRIX)}\n")

    # Show every product
    for info in PRODUCT_MATRIX.values():
        info.describe()

    print("\n--- Lookup by Kunde: 'Iveco F4A' ---")
    lookup("Iveco F4A").describe()


    print("\n--- All feasible products for HTL3 ---")
    htl3_products = all_feasible_for_line("HTL3")

    print(f"  Count: {len(htl3_products)}")
    for p in htl3_products:
        lc = p.lines["HTL3"]
        print(f"    {p.product_id} ({p.product_class.value}) → {lc.freigabe.value}")


    print("\n--- All feasible products for HTL5 ---")
    htl5_products = all_feasible_for_line("HTL5")

    print(f"  Count: {len(htl5_products)}")
    for p in htl5_products:
        lc = p.lines["HTL5"]
        print(f"    {p.product_id} ({p.product_class.value}) → {lc.freigabe.value}")


    print("\n--- All feasible products for HTL6 ---")
    htl6_products = all_feasible_for_line("HTL6")

    print(f"  Count: {len(htl6_products)}")
    for p in htl6_products:
        lc = p.lines["HTL6"]
        print(f"    {p.product_id} ({p.product_class.value}) → {lc.freigabe.value}")
