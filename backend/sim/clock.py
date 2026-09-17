"""
sim/clock.py
=============
Sim-time (seconds since t=0, i.e. env.now) <-> wall-clock datetime
conversions.

These are plain functions taking `(kenv, epoch, ...)` explicitly, so they
can be called with just a KanbanSimEnvironment + epoch, with or without a
RunContext around. sim/context.py's RunContext.wall_clock() /
.is_line_on() / .seconds_until_on() are thin wrappers over these three
functions. Also used directly by sim/resources/environment.py and by
reports/ modules that only have a kenv + epoch on hand.

The "no Shifts sheet loaded -> always on" fallback comes from
KanbanSimEnvironment.is_line_on() (sim/resources/environment.py).
"""

from __future__ import annotations

import datetime as _dt
from typing import Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from sim.resources.environment import KanbanSimEnvironment


def wall_clock(kenv: "KanbanSimEnvironment", epoch: _dt.datetime,
                t: Optional[float] = None) -> _dt.datetime:
    """Sim-time seconds (default: right now, kenv.env.now) -> the shared
    wall-clock instant that maps to."""
    if t is None:
        t = kenv.env.now
    return epoch + _dt.timedelta(seconds=t)


def is_line_on(kenv: "KanbanSimEnvironment", epoch: _dt.datetime, line_name: str,
                t: Optional[float] = None) -> bool:
    """True iff `line_name` is on-shift at sim time `t` (default: now)."""
    return kenv.is_line_on(line_name, wall_clock(kenv, epoch, t))


def seconds_until_on(kenv: "KanbanSimEnvironment", epoch: _dt.datetime, line_name: str,
                      t: Optional[float] = None) -> Optional[float]:
    """
    Seconds from sim time `t` (default: now) until `line_name` next turns
    on. Returns 0.0 if it's ALREADY on at `t` — a caller asking "how long
    do I need to sleep before this line is on" wants 0, not a wait until
    the FOLLOWING shift, when the answer is already yes. Returns None if
    the line never turns on within the configured calendar horizon
    (including "no Shifts sheet loaded at all", in which case a line is
    always on and this branch is unreachable) — callers must decide how
    to handle "never comes back on" rather than this function looping or
    guessing a horizon of its own.
    """
    if t is None:
        t = kenv.env.now
    if is_line_on(kenv, epoch, line_name, t):
        return 0.0
    now_dt = wall_clock(kenv, epoch, t)
    nxt = kenv.next_line_on_transition(line_name, now_dt)
    if nxt is None:
        return None
    return (nxt - now_dt).total_seconds()
