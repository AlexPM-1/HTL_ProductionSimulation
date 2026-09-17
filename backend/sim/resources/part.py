"""
sim/resources/part.py
======================
Defines the Part entity — the object that moves through a production
line's stations. Pure data, no simpy.

Created by sim.resources.environment.SimEnvironment.create_part() and
advanced by sim.produce.part_lifecycle.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from domain.products import ProductClass


# ===========================================================================
# Entity – Part
# ===========================================================================

@dataclass
class Part:
    """
    A single coupling blank flowing through the production line.

    Attributes are set at creation and updated as the part progresses
    through stations, inspection, and packaging.
    """

    # ---- identity ----
    part_id: int            # unique sequential number (global counter)
    product_type: str       # e.g. "A", "B", … matches Kundenbedarf column
    product_class: ProductClass

    # ---- line assignment ----
    line_id: int            # which of the n lines this part is assigned to

    # ---- timing ----
    t_created: float        # simulation time when part entered the system
    t_cycle_start: float    = 0.0
    t_beladen_start: float  = 0.0   # time seizing Beladen
    t_beladen_end: float    = 0.0
    t_supfina_start: float  = 0.0
    t_supfina_end: float    = 0.0
    t_waschen_start: float  = 0.0
    t_waschen_end: float    = 0.0
    t_sichtpruefung_start: float  = 0.0
    t_sichtpruefung_end: float    = 0.0
    t_exit: float           = 0.0   # time leaving system (pass / scrap)

    # ---- quality / rework ----
    rework_pass: int        = 0     # number of times sent back to Beladen
    status: str             = "in_progress"
    # status values: "in_progress" | "passed" | "scrapped"

    # ---- packaging ----
    package_id: Optional[int] = None  # assigned when part joins a complete package

    # ---- flags ----
    is_restmenge: bool      = False   # True if this part is a carry-over from previous cycle

    @property
    def total_passes(self) -> int:
        """Number of complete passes through the line (1 = no rework)."""
        return self.rework_pass + 1

    @property
    def cycle_time(self) -> float:
        """Wall-clock residence time from creation to exit."""
        return self.t_exit - self.t_cycle_start if self.t_cycle_start > 0 else 0.0

    def __repr__(self) -> str:
        return (
            f"Part(id={self.part_id}, type={self.product_type}, "
            f"line={self.line_id}, rework={self.rework_pass}, "
            f"status={self.status})"
        )
