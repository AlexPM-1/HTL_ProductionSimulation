"""
sim/resources/stations.py
==========================
Wraps the SimPy Resource (machine) and SimPy Container (inter-station
buffer) primitives for one line.

Built by sim.resources.build.build_environment(); consumed by
sim.produce.part_lifecycle as parts move station-to-station.
"""

from __future__ import annotations

from dataclasses import dataclass

import simpy

from domain.config import StationConfig, BufferConfig


# ===========================================================================
# Resource wrappers
# ===========================================================================

@dataclass
class StationResource:
    """
    Wraps a SimPy Resource (the machine) for one station on one line.

    One StationResource instance = one physical station on one line.
    Capacity is always 1 (single-server) per the model specification.
    """
    station_cfg: StationConfig          # parameters from SimConfig
    line_id: int                        # which line
    resource: simpy.Resource            # the SimPy resource (capacity=1)

    # Statistics – filled in during simulation
    n_processed: int   = 0
    total_busy_time: float = 0.0

    @property
    def name(self) -> str:
        return self.station_cfg.name

    @property
    def utilisation(self) -> float:
        """ρ = total_busy_time / sim_time  (call after simulation ends)."""
        return self.total_busy_time   # numerator only; caller divides by horizon

    def __repr__(self) -> str:
        return f"StationResource(line={self.line_id}, station={self.name})"


@dataclass
class BufferResource:
    """
    Wraps a SimPy Container for a finite-capacity inter-station buffer.

    SimPy Container is ideal here: put() adds parts, get() removes them.
    Blocking on a full/empty buffer is handled by the caller
    (sim.produce.part_lifecycle) via the normal put()/get() semantics.

    One BufferResource instance = one physical buffer on one line.
    """
    buffer_cfg: BufferConfig        # parameters from SimConfig
    line_id: int                    # which line
    container: simpy.Container      # SimPy container (level = current fill)

    # Statistics
    max_observed_fill: int = 0

    @property
    def name(self) -> str:
        return self.buffer_cfg.buffer_id

    @property
    def capacity(self) -> int:
        return self.buffer_cfg.capacity

    @property
    def current_fill(self) -> int:
        return int(self.container.level)

    def __repr__(self) -> str:
        return (
            f"BufferResource(line={self.line_id}, id={self.name}, "
            f"fill={self.current_fill}/{self.capacity})"
        )
