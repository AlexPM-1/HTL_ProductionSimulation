"""
sim/drain/crew.py
===================
crew_process() — the per-crew SimPy generator. Started once per crew by
sim/runner.py's run_mixed(). Calls select_line_for_crew() (selection.py),
run_pull_turn() (pull_turn.py), and run_push_turn() (push_turn.py).
"""

from __future__ import annotations

from typing import Optional

from sim.context import RunContext
from sim.drain.selection import select_line_for_crew
from sim.drain.pull_turn import run_pull_turn
from sim.drain.push_turn import run_push_turn
from telemetry.records import ScheduleEvent

# How often crew_process re-polls is_line_on() for a line that has no
# further on-transition in the configured shift calendar horizon at all
# (see the off-line correction below) — only ever used in that edge
# case; a normal off period is woken exactly at its known on-transition
# instead, without polling.
_OFF_LINE_REPOLL_S: float = 3600.0


def crew_process(
    ctx: RunContext,
    crew_id: int,
    n_workers: int,
    event_log: Optional[list[ScheduleEvent]] = None,
):
    """
    SimPy generator — one instance per crew (sim.runner.run_mixed(n_crews=...)).
    The only consumer of every line's chute (see sim/runner.py's
    _install_crew_chute_hooks). Loop:

      1. Rule 1/2 (select_line_for_crew): pick the most-loaded eligible
         line — None if nothing anywhere has work right now.
      2. Hold that line's LinePriorityGate for as long as this crew keeps
         working it (possibly several turns in a row — see step 3).
      3. Run ONE turn: the whole push order if a push entry is at the
         chute's front (run_push_turn), or up to a 4-card
         same-product_number pull batch if a pull entry is at the front
         (run_pull_turn, with Rule 1.a carryover via
         ctx.pending_batches). If neither is available (chute emptied
         since selection), release the gate and re-select from scratch.
      4. Rule 2: re-run select_line_for_crew with this line as
         held_line_id. Strictly-more-loaded elsewhere -> release this
         gate and go back to step 1. Otherwise -> loop back to step 3 on
         the same line (still holding its gate).

    Idles (blocked on ctx.activity_signal) whenever step 1 finds nothing
    on-shift anywhere with pending work; woken by any chute insertion
    (_install_crew_chute_hooks) or any crew finishing a turn (ctx.notify,
    called from run_pull_turn/run_push_turn).

    Shift on/off: a line can go off-shift while this crew holds it
    (between turns — a turn already in progress is never preempted).
    Before starting a NEW turn, this loop waits out any off-shift period
    on the currently-held line rather than starting one — "off lines
    cannot be used" applies to STARTING work, not to finishing what's
    already running.
    """
    kenv = ctx.kenv
    env = kenv.env

    while True:
        line_id = select_line_for_crew(ctx, held_line_id=None)
        if line_id is None:
            yield ctx.activity_signal.get()
            continue

        gate = ctx.gates[line_id]
        with gate.resource.request() as req:
            yield req
            while True:
                line_name = next(l.line_name for l in kenv.lines if l.line_id == line_id)

                while not ctx.is_line_on(line_name):
                    wait_s = ctx.seconds_until_on(line_name)
                    yield env.timeout(wait_s if wait_s else _OFF_LINE_REPOLL_S)

                chute = ctx.chutes[line_id]
                if chute.peek_next_of_class("push") is not None:
                    yield from run_push_turn(ctx, line_id, line_name, n_workers, event_log, crew_id)
                elif chute.peek_next_of_class("pull") is not None:
                    yield from run_pull_turn(ctx, line_id, line_name, n_workers, event_log, crew_id)
                else:
                    break  # chute emptied since selection — release, re-select

                next_line_id = select_line_for_crew(ctx, held_line_id=line_id)
                if next_line_id != line_id:
                    break  # Rule 2: a strictly more-loaded line exists
                           # (or nothing is left anywhere) — release and
                           # go back to a fresh selection.
                # else: strictly equal, or nothing more loaded -> stay
                # and run another turn on this same line.
