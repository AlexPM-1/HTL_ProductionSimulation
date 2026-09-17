"""
sim/resources/line.py
======================
ProductionLine — the static per-line bundle of stations and buffers.
Built by sim.resources.build; the on/off (shift) state itself is
queried separately, via SimEnvironment.is_line_on()/
next_line_on_transition() (sim.resources.environment), since that
depends on wall-clock time which this class doesn't track.
"""

from __future__ import annotations

import datetime as _dt
from dataclasses import dataclass
from typing import Optional

from domain.config import ShiftCalendar
from sim.resources.stations import StationResource, BufferResource


@dataclass
class ProductionLine:
    """
    All SimPy resources for a single production line.

    Attributes
    ----------
    line_id  : 1-based line number
    stations : dict mapping station name → StationResource
               ordered as Beladen → Supfina → Waschen → Sichtpruefung
    buffers  : list of BufferResource in station order
               [Puffer1(Beladen→Supfina), Puffer2(Supfina→Waschen),
                Puffer3(Waschen→Sichtpruefung)]
    current_product : product type currently set up on this line ("" = none)

    Note on shift on/off: this class deliberately holds no live on/off
    flag or reference to the shift calendar — "is this line on right
    now" depends on wall-clock time, which this class has no notion of
    (that mapping from env.now to a real datetime lives in
    sim.context.RunContext). describe() below accepts an optional
    (at, shift_calendar) pair purely for on-demand reporting; the actual
    gating logic (waiting for a line to turn on before starting
    production) lives in sim.drain.crew.crew_process(), via
    SimEnvironment.is_line_on()/next_line_on_transition().
    """

    line_id: int
    line_name: str                     # canonical name e.g. "HTL3"
    stations: dict[str, StationResource]
    buffers: list[BufferResource]
    current_product: str = ""          # last product type set up on this line

    def describe(
        self,
        at: Optional[_dt.datetime] = None,
        shift_calendar: Optional[ShiftCalendar] = None,
    ) -> None:
        """
        Print this line's static resource layout. If both `at` (a
        wall-clock instant) and `shift_calendar` are supplied, also print
        that line's on/off status at that instant — purely informational,
        for a debug dump / status snapshot.
        """
        status = ""
        if at is not None and shift_calendar is not None:
            on = shift_calendar.is_line_on(self.line_name, at)
            status = f"  [{'ON' if on else 'OFF'} @ {at.strftime('%d.%m.%Y %H:%M:%S')}]"
        print(f"  Line {self.line_id} ({self.line_name}){status}")
        for sr in self.stations.values():
            print(f"    {sr.name:<18} SimPy resource cap={sr.resource.capacity}")
        for br in self.buffers:
            print(f"    {br.name:<36} "
                  f"fill={br.current_fill}/{br.capacity}  "
                  f"protocol={br.buffer_cfg.blocking_protocol}")
