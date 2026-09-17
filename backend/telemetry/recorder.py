"""
telemetry/recorder.py
======================
Recorder: the single write channel for a run's telemetry. Wraps a live
KanbanSimEnvironment and exposes log_event() / record_supermarket() /
record_shortfall() for callers such as sim.produce.changeover and
sim.produce.run_kanban_batch.run_one_kanban_batch to append
ScheduleEvent, SupermarketSnapshot, and ShortfallEvent records
(telemetry.records) without touching the environment's internals
directly.
"""

from __future__ import annotations

from telemetry.records import ScheduleEvent, ShortfallEvent, SupermarketSnapshot


class Recorder:
    """
    Wraps a live KanbanSimEnvironment (`kenv`) only far enough to read
    `env.now` and resolve line_id -> line_name — never mutates kenv,
    never owns simulation logic. Every log list is caller-owned/optional,
    same by-reference pattern the original module-level lists used.

    Pass a Recorder instance wherever sim.produce.changeover.changeover()
    (or anything else) expects a `sim_env` with a `.log_event(event)`
    method: `log_event()` appends to this Recorder's own `event_log`, and
    `__getattr__` forwards everything else (`.env`, `.lines`, `.cfg`, ...)
    straight through to the wrapped `kenv`.
    """

    def __init__(
        self,
        kenv,
        event_log: "list[ScheduleEvent] | None" = None,
        snapshot_log: "list[SupermarketSnapshot] | None" = None,
        shortfall_log: "list[ShortfallEvent] | None" = None,
    ):
        self.kenv = kenv
        self.event_log: list[ScheduleEvent] = (
            event_log if event_log is not None else []
        )
        self.snapshot_log: list[SupermarketSnapshot] = (
            snapshot_log if snapshot_log is not None else []
        )
        self.shortfall_log: list[ShortfallEvent] = (
            shortfall_log if shortfall_log is not None else []
        )

    def _line_name(self, line_id: int) -> str:
        return self.kenv.lines[line_id - 1].line_name

    def __getattr__(self, name):
        # Anything a caller reads that isn't defined on Recorder itself
        # (env, lines, cfg, ...) — delegate to the real kenv.
        return getattr(self.kenv, name)

    def log_event(self, event) -> None:
        """The one write channel for ScheduleEvents."""
        self.event_log.append(event)

    def record_supermarket(
        self, line_id: int, product_type: str, event_type: str, sm
    ) -> None:
        """Append one SupermarketSnapshot reflecting sm's state right now
        (post-event). Call only from the actual mutation site — never
        speculatively."""
        self.snapshot_log.append(
            SupermarketSnapshot(
                t=self.kenv.env.now,
                line_id=line_id,
                line_name=self._line_name(line_id),
                product_type=product_type,
                event_type=event_type,
                n_available=sm.n_available,
                pcs_partial=sm.pcs_partial,
                batch_size=sm.batch_size,
            )
        )

    def record_shortfall(
        self, line_id: int, product_type: str, start_s: float, end_s: float
    ) -> None:
        """Append one ShortfallEvent covering [start_s, end_s). Call only
        when a request found n_available == 0 at the instant it asked —
        see ShortfallEvent's docstring."""
        self.shortfall_log.append(
            ShortfallEvent(
                line_id=line_id,
                line_name=self._line_name(line_id),
                product_type=product_type,
                start_s=start_s,
                end_s=end_s,
            )
        )
