"""
sim/drain/selection.py
========================
select_line_for_crew() — line-selection logic (Rule 1/2) used by
sim/drain/crew.py.
"""

from __future__ import annotations

from typing import Optional

from sim.context import RunContext


def select_line_for_crew(
    ctx: RunContext, held_line_id: Optional[int],
) -> Optional[int]:
    """
    Rule 1 (fresh pick — held_line_id=None) / Rule 2 (re-evaluation after
    a turn — held_line_id=the line this crew currently holds) line
    selection.

    Eligible lines: on-shift right now, with pending work
    (KanbanChuteResource.total_pending_cards > 0), and — unless it's the
    line we already hold — not currently locked by another crew
    (gate.resource.count == 0).

    Tie-breaking:
      - Among FRESH candidates (held_line_id=None), the max-load line
        wins; ties fall to iteration order (kenv.lines' own order, i.e.
        lowest line_id) — not specified by the spec beyond the
        crew-vs-crew timing case below, so this is a reasonable default.
      - When RE-EVALUATING a line already held, Rule 2's "equal amount
        stays on the same one" is implemented by requiring a candidate
        to be STRICTLY more loaded than the held line before switching.

    Crew-vs-crew arbitration: two crews becoming idle at the same
    simulated instant are NOT arbitrated by anything in this function —
    it's a pure (side-effect-free) read of current state. The
    arbitration happens naturally in crew_process(): requesting a line's
    gate.resource synchronously marks it busy before that crew's first
    yield, and SimPy runs same-instant processes in the order they were
    spawned (run_mixed() spawns crew_id=0 before crew_id=1, ...), so the
    lower-numbered crew's request is always visible to the next crew's
    call to this function — "crew 1 starts selecting, then crew 2",
    exactly as specified, with no extra bookkeeping needed here.
    """
    loads: dict[int, int] = {}
    for line in ctx.kenv.lines:
        lid = line.line_id
        if not ctx.is_line_on(line.line_name):
            continue
        if lid != held_line_id and ctx.gates[lid].resource.count > 0:
            continue
        load = ctx.chutes[lid].total_pending_cards
        if load > 0:
            loads[lid] = load

    if not loads:
        return None

    if held_line_id is not None and held_line_id in loads:
        held_load = loads[held_line_id]
        other_best_id, other_best_load = max(
            ((lid, ld) for lid, ld in loads.items() if lid != held_line_id),
            key=lambda kv: kv[1], default=(None, -1),
        )
        if other_best_load > held_load:
            return other_best_id
        return held_line_id

    return max(loads.items(), key=lambda kv: kv[1])[0]
