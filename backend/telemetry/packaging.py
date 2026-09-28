"""
telemetry/packaging.py
=======================
PackageTracker: turns per-part completions from sim.produce.part_lifecycle
into package-level ScheduleEvent records (telemetry.records).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from telemetry.records import ScheduleEvent


@dataclass
class PackageTracker:
    """
    Stateful helper used by run_line() to turn a stream of per-part
    completions into package-level ScheduleEvents.

    One instance covers exactly one OrderRecord's run on one line.
    Call `on_finish(env_now, status)` after every part in the order
    finishes (passed or scrapped); call `flush(env_now)` once after the
    whole order's batch has cleared to emit any partial trailing package.
    """
    line_name:      str
    line_id:        int
    product_number:     str
    kunde:          str
    product_class:  str
    package_size:   int
    event_log:      list[ScheduleEvent]
    count_in_pkg:   int = field(default=0, init=False)
    pkg_start_s:    float = field(default=0.0, init=False)
    _started:       bool = field(default=False, init=False)

    def on_finish(self, env_now: float, status: str) -> None:
        """Register one finished part (any status) and emit a job event
        every time a full package's worth has completed."""
        if not self._started:
            self.pkg_start_s = env_now
            self._started = True

        # Only "passed" output counts toward a shippable package; scrapped
        # parts still consumed line time but are not packaged. Adjust here
        # if the packaging rule should include scrap as gross throughput.
        if status != "passed":
            return

        self.count_in_pkg += 1
        if self.count_in_pkg >= self.package_size:
            self._emit(env_now)
            self.count_in_pkg = 0
            self.pkg_start_s = env_now

    def flush(self, env_now: float) -> None:
        """Emit a partial package for whatever didn't reach package_size."""
        if self.count_in_pkg > 0:
            self._emit(env_now, units=self.count_in_pkg)
            self.count_in_pkg = 0

    def _emit(self, env_now: float, units: Optional[int] = None) -> None:
        units = units if units is not None else self.package_size
        start = self.pkg_start_s
        end = max(env_now, start)  # guard against zero-length rounding
        self.event_log.append(
            ScheduleEvent(
                line_name=self.line_name,
                line_id=self.line_id,
                event_type="job",
                start_s=round(start, 3),
                end_s=round(end, 3),
                product_number=self.product_number,
                kunde=self.kunde,
                product_class=self.product_class,
                package_size=self.package_size,
                units_in_segment=units,
            )
        )
