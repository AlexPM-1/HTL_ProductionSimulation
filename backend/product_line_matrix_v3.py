"""
product_line_matrix.py
======================
Hardcoded product-line compatibility matrix for the HTL Homburg plant.

This module encodes which products can run on which lines, what type of
product they are, which optional stations they require, and what the
approval status is — all derived from the colour-coded Excel appendix C
and the project specification document.

Design principles
-----------------
* No runtime Excel reading — all data lives here as Python objects.
* Single source of truth for the *product catalogue*; station parameters
  live in line_stations.py.
* process_logic_sequence.py is the consumer: it calls lookup() and
  build_line_info() to resolve routing + cycle times for a given part.

Class hierarchy
---------------
ProductClass  (Enum)   — product family / colour group
Freigabe      (Enum)   — approval / feasibility status per line
LineClass              — line-specific view: approval, active stations,
                         total cycle time, notes, priority (placeholder)
ProductLineInfo        — full product record: IDs + dict[line → LineClass]

Colour legend from the source Excel
------------------------------------
Cell background (line feasibility):
    Grün  (green)  → Freigabe.VORHANDEN    (approved, can produce)
    Gelb  (yellow) → Freigabe.MOEGLICH     (approval possible / conditional)
    Rot   (red)    → Freigabe.NICHT_MOEGLICH (not feasible)

Text colour (product type / extra stations needed):
    Rot   (red)    → ProductClass.STIFT     (requires Stift_Einpressen)
                     Note: on HTL6 Stift_Einpressen is absent, so the
                     station is simply omitted from the active sequence.
    Blau  (blue)   → ProductClass.DRS       (requires DRS optional station)
    Lila  (purple) → ProductClass.STAB_LOCHFILTER  (requires Lochfilter + Pruefen)
    Grün  (green)  → ProductClass.STAHLRAHMEN (steel-frame products)

Cell text inside HTL3/HTL5/HTL6 columns encodes priority / variant info;
it is preserved in LineClass.cell_text and LineClass.note for future use.

Usage
-----
    from product_line_matrix import lookup, PRODUCT_MATRIX
    info = lookup("0001234567")          # by Sachnummer
    info = lookup("BOSCH-CLIENT")        # by Kunde name
    info = lookup("SIS-0099")            # by SiS-Sachnummer

    # Check which lines can produce this part
    for line_name, lc in info.lines.items():
        print(line_name, lc.freigabe, lc.total_cycle_time_s)
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from enum import Enum
from typing import Optional

from config_loader_v6 import LINE_STATIONS, StationLine, get_station, BufferConfig


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

    # NOTE (Inventories/chute model): Lochfilter and DRS are no longer part
    # of the main line's in-line FIFO/BAS sequence. Their finished
    # sub-assemblies are supplied through the Chu_vor_HTL3 "Lochfilter" /
    # "DRS" FIFO chute lanes (backed by Inv_nach_Loch / Inv_nach_DRS),
    # which are filled by a SEPARATE, PARALLEL Lochfilter/DRS production
    # process (see process_logic_sequential_v3.run_lochfilter_drs_production).
    # The main line's Beladen simply draws 1 unit from that chute
    # lane before starting — it never physically visits the Lochfilter/DRS
    # machine itself. Pruefen (STAB_LOCHFILTER's inspection step) DOES
    # remain in-line, after Beladen.
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
    takt-limiting station. In a pipelined/BAS line (see
    process_logic_sequential_v3.py) this, not the summed latency, is what
    governs steady-state throughput: once the pipeline is full, a new part
    clears the line every `bottleneck_time_s` seconds, not every
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
                         config_loader_v5.load_config().buffers

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
    cell_text       : raw text from the Excel cell (priority / variant info)
    note            : free-text annotation
    priority        : placeholder — priority logic to be added in a separate function
    """
    line: str
    freigabe: Freigabe
    stations: list[StationLine]
    total_cycle_time_s: Optional[float]     # None = ⚠ incomplete station data
    bottleneck_time_s: Optional[float] = None  # None = ⚠ incomplete station data
    cell_text: Optional[str] = None         # raw Excel cell content (preserved as-is)
    priority: Optional[str] = None          # empty — to be implemented later
    note: Optional[str] = None

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
    sachnummer      : Bosch part number (primary key for lookup)
    kunde           : customer name / market designation
    sis_sachnummer  : SiS internal part number (may be None)
    hd              : HD (Hochdruck) designation or variant label
    product_class   : product family (determines optional stations)
    lines           : dict mapping line name → LineClass
                      Only lines with Freigabe != NICHT_MOEGLICH by default,
                      but NICHT_MOEGLICH lines are also stored for completeness.
    """
    sachnummer: str
    kunde: str
    sis_sachnummer: Optional[str]
    hd: Optional[str]
    product_class: ProductClass
    lines: dict[str, LineClass]             # "HTL3" | "HTL5" | "HTL6" → LineClass

    def feasible_lines(self) -> list[str]:
        """Return list of line names where production is approved or possible."""
        return [ln for ln, lc in self.lines.items() if lc.is_feasible]

    def describe(self) -> None:
        print(
            f"\nProduct: {self.sachnummer}  |  Kunde: {self.kunde}  "
            f"|  SiS: {self.sis_sachnummer}  |  HD: {self.hd}  "
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
            cell = f"  cell='{lc.cell_text}'" if lc.cell_text else ""
            print(
                f"    {ln}: {lc.freigabe.value:<30} "
                f"stations={lc.station_names}  total={ct}  bottleneck={bt}  {cell}"
            )


# ===========================================================================
# Internal factory helper
# ===========================================================================

def _make_line_class(
    line: str,
    freigabe: Freigabe,
    product_class: ProductClass,
    cell_text: Optional[str] = None,
    priority: Optional[str] = None,
    note: Optional[str] = None,
    
) -> LineClass:
    """
    Build a LineClass by resolving the active station sequence and cycle times
    from line_stations.py.  Keeps the matrix definition below compact.
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
        cell_text=cell_text,
        priority=priority,
        note=note,
    )


# ===========================================================================
# PRODUCT MATRIX  — hardcoded from Appendix C / colour-coded Excel
# ===========================================================================
#
# Each entry is a ProductLineInfo built with:
#   _make_line_class(line, Freigabe, ProductClass, cell_text, note)
#
# cell_text = raw text found inside the HTL3/HTL5/HTL6 cell in the Excel.
#             It likely encodes priority, variant, or programme codes.
#             The exact logic to parse it will be implemented as a separate
#             function when the mapping is confirmed by the plant.
#
# ⚠ NOTE: The Appendix C table from the actual Excel workbook was NOT
#   available in the provided project files.  The entries below are
#   representative placeholders built from the information given in:
#     - Project Summary document (Phase 1, Operational parameters)
#     - User instructions (colour legend, station sequences, product types)
#
#   FILL IN the real Sachnummern, Kunde values, SiS numbers, and cell_text
#   values from the actual Appendix C / Excel when available.
#   The structure and all logic are correct and ready to receive the data.
#
# ---------------------------------------------------------------------------


# ===========================================================================
# Lookup API  — used by process_logic_sequence.py
# ===========================================================================

def lookup(identifier: str) -> ProductLineInfo:
    """
    Find a product by Sachnummer, Kunde name, or SiS-Sachnummer.

    Search order: sachnummer → kunde → sis_sachnummer
    Raises ValueError if not found.

    Parameters
    ----------
    identifier : any of the three ID fields

    Returns
    -------
    ProductLineInfo
    """
    identifier = identifier.strip()

    # 1. Direct sachnummer match
    if identifier in PRODUCT_MATRIX:
        return PRODUCT_MATRIX[identifier]

    # 2. Kunde name match (case-insensitive)
    for info in PRODUCT_MATRIX.values():
        if info.kunde.lower() == identifier.lower():
            return info

    # 3. SiS-Sachnummer match
    for info in PRODUCT_MATRIX.values():
        if info.sis_sachnummer and info.sis_sachnummer.lower() == identifier.lower():
            return info

    raise ValueError(
        f"Product not found for identifier: {identifier!r}. "
        f"Known sachnummern: {list(PRODUCT_MATRIX.keys())}"
    )


def all_feasible_for_line(line: str) -> list[ProductLineInfo]:
    """Return all products that can run on the given line (Freigabe != NICHT_MOEGLICH)."""
    return [
        info for info in PRODUCT_MATRIX.values()
        if line in info.lines and info.lines[line].is_feasible
    ]

def _build_matrix() -> dict[str, ProductLineInfo]:
    V = Freigabe.VORHANDEN
    M = Freigabe.MOEGLICH
    N = Freigabe.NICHT_MOEGLICH

    PC = ProductClass

    matrix: dict[str, ProductLineInfo] = {}

    def add(
        sachnummer: str,
        kunde: str,
        sis: Optional[str],
        hd: Optional[str],
        pc: ProductClass,
        htl3: tuple[Freigabe, Optional[str], Optional[str]],
        htl5: tuple[Freigabe, Optional[str],Optional[str]],
        htl6: tuple[Freigabe, Optional[str],Optional[str]],
        note: Optional[str] = None,
        priority: Optional[str] = None,
    ) -> None:
        matrix[sachnummer] = ProductLineInfo(
            sachnummer=sachnummer,
            kunde=kunde,
            sis_sachnummer=sis,
            hd=hd,
            product_class=pc,
            lines={
                "HTL3": _make_line_class("HTL3", htl3[0], pc, htl3[1], htl3[2], note),
                "HTL5": _make_line_class("HTL5", htl5[0], pc, htl5[1], htl5[2], note),
                "HTL6": _make_line_class("HTL6", htl6[0], pc, htl6[1], htl6[2], note),
            },
        )

    raw = [
        ("F00RJ02034","FAW",None,"Rotation",PC.STANDARD,(M,"(x)",None),(V,"X1","1"),(V,"x V2","2")),
        ("F00RJ02072","MAN/LMB Liebherr",None,"Rotation",PC.STANDARD,(V,"x V3","2"),(V,"X4","3"),(V,"X2","1")),
        ("F00RJ02129","MMZ",None,"Extern",PC.STANDARD,(N,None,None),(V,"X2","2"),(V,"x V1","1")),
        ("F00RJ02198","Kamaz",None,"Extern",PC.STANDARD,(V,"X3","3"),(V,"x V2","2"),(V,"X1","1")),
        ("F00RJ02203","Volvo Penta",None,"Extern",PC.STANDARD,(V,"x V1","1"),(V,"X2","2"),(N,None,None)),
        ("F00RJ02298","FAW/Weichai",None,"Extern",PC.STANDARD,(V,"X2","2"),(M,"(x)",None),(V,"x V1","1")),
        ("F00RJ02325","MAN D08","F00RJ04556-R00","Rotation",PC.STANDARD,(V,"X3","3"),(V,"x V1","1"),(V,"X2","2")),
        ("F00RJ02336","MAN .030","F00RJ02890-R00","Rotation",PC.STANDARD,(V,"X1","1"),(V,"x V2","2"),(V,"X3","3")),
        ("F00RJ02338","MAN .044","F00RJ02997-R00","Rotation",PC.STANDARD,(V,"X1","1"),(V,"x V2","2"),(V,"X3","3")),
        ("F00RJ02340","MAN .045","F00RJ04409-R00","Rotation",PC.STANDARD,(V,"X1","1"),(V,"x V2","2"),(V,"X3","3")),
        ("F00RJ02344","Iveco F4A",None,"Rotation",PC.STIFT,(V,"X1","1"),(V,"XV2","2"),(V,"X3","3")),
        ("F00RJ02346","Sisu 645-4V",None,"Rotation",PC.STANDARD,(V,"X2","2"),(V,"x V3","3"),(V,"X1","1")),
        ("F00RJ02352","Deutz 2012 4V",None,"Rotation",PC.STANDARD,(N,None,None),(V,"x V3","3"),(V,"X1","1")),
        ("F00RJ02364","Sisu 645-2V",None,"Extern",PC.STAHLRAHMEN,(N,None,None),(V,"x (H)3","2"),(V,"X1","1")),
        ("F00RJ02367","Deutz 2012 2V",None,"Extern",PC.STAHLRAHMEN,(N,None,None),(V,"x (H)3","2"),(V,"x V1","1")),
        ("F00RJ02370","Ashok Leyland",None,"Extern",PC.STANDARD,(V,"x V3","2"),(N,None,None),(V,"X1","1")),
        ("F00RJ02433","Jamz/Ashok Leyland",None,"Rotation",PC.STANDARD,(N,None,None),(V,"X V2","1"),(V,"X3","3")),
        ("F00RJ02438","MAN Itec","F00RJ04581-R00","Rotation",PC.STANDARD,(V,"X2","2"),(V,"x V3","3"),(V,"X1","1")),
        ("F00RJ02446","Iveco Cursor",None,"Extern",PC.STANDARD,(V,"X2","2"),(N,None,None),(V,"x V1","1")),
        ("F00RJ02465","Ford Otosan",None,"Rotation",PC.STANDARD,(V,"x V2","2"),(N,None,None),(V,"X1","1")),
        ("F00RJ02471","D-Max","F00RJ04057-R00","Extern",PC.STANDARD,(V,"X2","2"),(V,"x V1","1"),(V,"X3","3")),
        ("F00RJ02491","CMEP",None,"Rotation",PC.STIFT,(V,"X1","1"),(V,"X V2","2"),(V,"X3","3")),
        ("F00RJ02516","D-Max","F00RJ02896-R00","Extern",PC.STANDARD,(V,"X3","3"),(V,"X1","1"),(V,"x V2","2")),
        ("F00RJ02611","Iveco Sofim",None,"Extern",PC.STANDARD,(V,"x V1","1"),(N,None,None),(N,None,None)),
        ("F00RJ02711","Sisu",None,"Rotation",PC.STANDARD,(V,"X1","1"),(V,"x V2","2"),(V,"X3","3")),
        ("F00RJ02724","RVI/DFM Shiyan",None,"Rotation",PC.STANDARD,(V,"x V1","1"),(N,None,None),(N,None,None)),
        ("F00RJ02733","Deutz 2013 4V",None,"Rotation",PC.STANDARD,(N,None,None),(V,"x V2","2"),(V,"X1","1")),
        ("F00RJ03113","Cummins",None,"Rotation",PC.STIFT,(N,None,None),(V,"x V1","1"),(V,"X2","2")),
        ("F00RJ04447","MAN",None,"Rotation",PC.STANDARD,(V,"X1","1"),(V,"x V2","2"),(V,"X3","3")),
        ("F00RJ04832","CAT",None,"Rotation",PC.STANDARD,(V,"X2","2"),(M,"(x)3","3"),(V,"X V1","1")),
        ("F00RJ04862","Kamaz",None,"Rotation",PC.STANDARD,(M,"(x)2","2"),(V,"x V3","3"),(V,"X1","1")),
        ("F00RJ05042","Tata Cummins",None,"Rotation",PC.STIFT,(V,"X1","1"),(V,"X V2","2"),(N,None,None)),
        ("F00RJ05175","FAW DE",None,"Rotation",PC.STANDARD,(N,None,None),(V,"X2","2"),(V,"X V1","1")),
        ("F00RJ05840","Tata Cummins","Neue Nr. zu 042","Rotation",PC.STIFT,(V,"X1","1"),(V,"X V2","2"),(N,None,None)),

        ("F00RC00172","Deutz",None,"Rotation",PC.STANDARD,(V,"x V1","1"),(V,"X2","2"),(V,"X3","3")),
        ("F00RC00410","Navistar","F00RC00672-R00","Rotation",PC.STANDARD,(V,"x V1","1"),(V,"X2","2"),(V,"X3","3")),
        ("F00RC00419","Hino",None,"Extern",PC.STAB_LOCHFILTER,(V,"x V1","1"),(N,None,None),(N,None,None)),
        ("F00RC00515","Navistar",None,"Rotation",PC.STANDARD,(V,"x V1","1"),(V,"X2","2"),(V,"X3","3")),
        ("F00RC00525","FPT",None,"Extern",PC.DRS,(V,"x V1","1"),(N,None,None),(N,None,None)),
        ("F00RC00540","Cummins",None,"Rotation",PC.STIFT,(V,"x V1","1"),(V,"X2","2"),(N,None,None)),
        ("F00RC00555","MAN RX",None,"Rotation",PC.STANDARD,(V,"x V1","1"),(N,None,None),(N,None,None)),
        ("F00RC00578","SiSU",None,"Rotation",PC.STANDARD,(V,"x V1","1"),(V,"X2","2"),(N,None,None)),
        ("F00RC00595","MTU (ESU)",None,"Extern",PC.DRS,(V,"x V1","1"),(N,None,None),(N,None,None)),
        ("F00RC00607","FPT (ESU)",None,"Extern",PC.DRS,(V,"x V1","1"),(N,None,None),(N,None,None)),
        ("F00RC00613","Leonardo (ex FPT)",None,"Extern",PC.DRS,(V,"x V1","1"),(N,None,None),(N,None,None)),
        ("F00RC00622","FPT",None,"Extern",PC.DRS,(V,"x V1","1"),(N,None,None),(N,None,None)),
        ("F00RC00627","FPT",None,"Extern",PC.DRS,(V,"x V1","1"),(N,None,None),(N,None,None)),
        ("F00RC00634","Cummins",None,"Rotation",PC.STIFT,(V,"x V1","1"),(V,"X2","2"),(V,"X3","3")),
        ("F00RC00638","Scania",None,"Rotation",PC.STANDARD,(N,None,None),(N,None,None),(V,"x V2","2")),
        ("F00RC00667","FPT",None,"Extern",PC.DRS,(V,"x V1","1"),(N,None,None),(N,None,None)),
        ("F00RC00718","John Deere",None,"Rotation",PC.STANDARD,(V,"X V1","1"),(N,None,None),(N,None,None)),
        ("F00RC00745","MAN",None,"Rotation",PC.STANDARD,(V,"X V1","1"),(V,"x2","2"),(V,"X3","3")),
        ("F00RC00850","John Deere",None,"Rotation",PC.STANDARD,(V,"X V1","1"),(N,None,None),(N,None,None)),
        ("F00RC00868","AGCO",None,"Rotation",PC.STANDARD,(V,"X V1","1"),(N,None,None),(N,None,None)),
        ("F00RC00886","MAN V12",None,"Rotation",PC.STANDARD,(N,None,None),(N,None,None),(V,"X V1","1")),
        ("F00RC00917","Scania",None,"Rotation",PC.STANDARD,(N,None,None),(N,None,None),(V,"X V1","1")),
        ("F00RC00935","FPT",None,"Extern",PC.DRS,(V,"X V1","1"),(N,None,None),(N,None,None)),
        ("F00RC00967","FAW",None,"Rotation",PC.STANDARD,(N,None,None),(N,None,None),(V,"X V3","1")),
        ("F00RC00973","FPT",None,"Extern",PC.DRS,(V,"X V1","1"),(N,None,None),(N,None,None)),
        ("F00RC01007","HDHI",None,"Rotation",PC.STANDARD,(V,"X2","2"),(N,None,None),(V,"X V1","1")),
        ("F00RC01015","DAI",None,"Extern",PC.STAB_LOCHFILTER,(V,"X1","1"),(N,None,None),(N,None,None)),
        ("F00RC01041","Cummins Barracuda",None,"Rotation",PC.STIFT,(N,None,None),(V,"X V1","1"),(N,None,None)),
        ("F00RC01053","Weichai",None,"Extern",PC.DRS,(V,"X V1","1"),(N,None,None),(N,None,None)),
    ]

    for row in raw:
        add(*row)

    return matrix


# Module-level singleton — imported by other modules
PRODUCT_MATRIX: dict[str, ProductLineInfo] = _build_matrix()


# ===========================================================================
# Quick self-test
# ===========================================================================

if __name__ == "__main__":
    print("=== Product Line Matrix ===")
    print(f"Total products in matrix: {len(PRODUCT_MATRIX)}\n")

    # Show every product
    for info in PRODUCT_MATRIX.values():
        info.describe()

    # Lookup examples
    print("\n--- Lookup by Sachnummer: 'F00RJ02129' ---")
    lookup("F00RJ02129").describe()

    print("\n--- Lookup by Kunde: 'Iveco F4A' ---")
    lookup("Iveco F4A").describe()

    print("\n--- Lookup by SiS: 'F00RJ04556-R00' ---")
    lookup("F00RJ04556-R00").describe()

    print("\n--- All feasible products for HTL3 ---")
    htl3_products = all_feasible_for_line("HTL3")

    print(f"  Count: {len(htl3_products)}")
    for p in htl3_products:
        lc = p.lines["HTL3"]
        print(f"    {p.sachnummer} ({p.product_class.value}) → {lc.freigabe.value}")


    print("\n--- All feasible products for HTL5 ---")
    htl5_products = all_feasible_for_line("HTL5")

    print(f"  Count: {len(htl5_products)}")
    for p in htl5_products:
        lc = p.lines["HTL5"]
        print(f"    {p.sachnummer} ({p.product_class.value}) → {lc.freigabe.value}")


    print("\n--- All feasible products for HTL6 ---")
    htl6_products = all_feasible_for_line("HTL6")

    print(f"  Count: {len(htl6_products)}")
    for p in htl6_products:
        lc = p.lines["HTL6"]
        print(f"    {p.sachnummer} ({p.product_class.value}) → {lc.freigabe.value}")
