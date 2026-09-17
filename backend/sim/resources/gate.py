"""
sim/resources/gate.py
======================
LinePriorityGate — the per-line mutex crews compete for. Created by
sim.runner.run_mixed(), one per line; held by sim.drain.crew.crew_process()
for the duration of a turn.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import simpy

from domain.orders import KanbanBatchSpec, OrderRecord


@dataclass
class LinePriorityGate:
    """
    Per-line mutex: whoever holds it currently owns the right to run one
    "unit" of production through this line's stations. Backed by
    simpy.Resource(capacity=1).

    Kept as a plain simpy.Resource(capacity=1) rather than a priority
    resource: the old priority=0/1 split existed only to arbitrate two
    independent per-class drain loops fighting over one line — with
    crews as the sole consumer, there is only ever one kind of requester
    per line at a time, so nothing is left to prioritize.

    last_activity_t : env.now() of the last time a crew finished a unit
        here. Not read by any control-flow logic — kept purely as a
        bookkeeping timestamp for anything that wants "when did this
        line last do something".
    current_rec : whatever was last executed on this line, across BOTH
        classes — the changeover() argument. Read/written by whichever
        crew is currently holding this line, regardless of whether it's
        running a push or pull turn — see sim.drain.push_turn.run_push_turn
        / sim.drain.pull_turn._run_one_pull_card. On the push side this is
        a real, customer-tracked OrderRecordPush; on the pull side it's
        the internal KanbanBatchSpec (see that class's docstring for why
        it isn't an OrderRecord). Every reader of this field (changeover,
        api/legacy.py's gate_status, pull_turn's changeover-detection)
        only ever duck-types off `.sachnummer` / `getattr(...)`, so both
        types are interchangeable in practice.
    """
    resource: simpy.Resource
    last_activity_t: float = 0.0
    current_rec: Optional["OrderRecord | KanbanBatchSpec"] = None

    def touch(self, t: float) -> None:
        self.last_activity_t = t
