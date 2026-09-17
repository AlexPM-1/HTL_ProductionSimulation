"""
sim/fill/pull/day_boundary.py
===============================
The once-per-production-day KPI/Restmenge/backlog checkpoint
(day_boundary_process), print_restmenge_report(), and the
pipeline-backlog helper it uses. Started by
sim.fill.pull.bootstrap.start_kanban_simulation().
"""

from __future__ import annotations

from datetime import timedelta

from sim.context import RunContext
from sim.resources.environment import KanbanSimEnvironment
from reports.kpi.by_line import print_kpi
from reports.pull.restmenge import build_restmenge_payload

# ===========================================================================
# End-of-run "Restmenge" report
# ===========================================================================

def print_restmenge_report(kenv: KanbanSimEnvironment, verbose: bool = True) -> dict:
    """
    Print, per (line, product), the count of finished pieces that PASSED
    inspection but hadn't yet accumulated to a full `batch_size` chunk
    when the run ended — i.e. SupermarketResource.pcs_partial (a partial
    pack can never be withdrawn on its own; it carries over and is
    topped up by the next production run's deposit_finished_pcs()
    calls).

    For a single call at the end of a run, this reports whatever
    kenv's Supermarkets look like at that moment. For an ONCE-PER-DAY
    report on a multi-day sheet, see day_boundary_process() below,
    which calls this same function every rt.day_length_s seconds.

    Returns the same data as a plain dict, keyed
    (line_name, product_type) -> pcs_partial, for a caller that wants it
    for a KPI/export instead of (or in addition to) the printed report.

    The payload itself is built by
    reports.pull.restmenge.build_restmenge_payload(kenv); this function
    is a thin wrapper adding the console-printing side effect, called by
    day_boundary_process() below.
    """
    restmenge = build_restmenge_payload(kenv)
    if not verbose:
        return restmenge

    print(f"=== Restmenge (partial packages) @ t={kenv.env.now:10.1f} ===")
    for (line_name, product_type), pcs_partial in restmenge.items():
        line_id = next((l.line_id for l in kenv.lines if l.line_name == line_name), None)
        batch_size = kenv.supermarkets.get(line_id, {}).get(product_type)
        batch_size = batch_size.batch_size if batch_size is not None else "?"
        print(f"  {line_name}(L{line_id}) {product_type!r}: "
              f"{pcs_partial}/{batch_size} pcs carrying into next run")

    if not restmenge:
        print("  (none — every product finished on a whole-batch boundary)")
    print("=" * 55)

    return restmenge


# ===========================================================================
# Per-day checkpoint (multi-day support)
# ===========================================================================

# States a KanbanCard passes through between leaving a Supermarket
# ("withdrawn") and landing back on one ("in_supermarket" — deliberately
# excluded here, that's the "at rest" state, not backlog). Mirrors the
# record_transition() call sites throughout sim/fill/pull/.
_PIPELINE_STATES: frozenset[str] = frozenset({
    "withdrawn", "in_collection_box", "in_batch_collector",
    "released_to_chute", "in_production",
})


def _pipeline_backlog_cards(kenv: KanbanSimEnvironment) -> int:
    """
    Count Kanban cards currently "in flight": already withdrawn from a
    Supermarket, not yet deposited back into one (sitting in the
    collection box, batch collector, Kanban chute, or mid-production).
    This is the "units remaining to be produced" figure for the
    end-of-day carry-over warning — reads straight off
    KanbanCard.transitions (the permanent per-card audit trail).

    A card's LATEST recorded transition tells you where it currently
    sits. A card with NO transitions yet is one of the initial seed
    cards build_kanban_environment() placed straight onto a Supermarket
    (see sim.resources.build) — resting there, not backlog, hence
    the "in_supermarket" default below.

    Deliberately does NOT count scrapped parts: a scrapped part's card
    is still physically in flight (it re-enters the recycle queue via
    the normal on_finish path) rather than vanishing, so it's correctly
    included if and only if its card hasn't made it back to
    "in_supermarket" yet — same rule as everything else here.
    """
    n = 0
    for card in kenv.card_registry.values():
        state = card.transitions[-1][0] if card.transitions else "in_supermarket"
        if state in _PIPELINE_STATES:
            n += 1
    return n


def day_boundary_process(rt: RunContext, verbose: bool = True):
    """
    SimPy generator — ONE background process, started once by
    start_kanban_simulation() alongside withdrawal_process() and every
    line's collection_box_emptying_process().
    Ticks every rt.day_length_s seconds (the production-day length — see
    RunContext) and, on every tick, takes a full "end of day N" checkpoint:

      1. Per-line KPI dump — reports.kpi.by_line.print_kpi(). A
         CUMULATIVE snapshot (everything since t=0), taken once per day
         so the trend across days is visible, not a per-day delta.
      2. Restmenge (partial-pack) report — print_restmenge_report()
         above, likewise an instantaneous snapshot of current
         Supermarket state, not a per-day delta.
      3. Pipeline-backlog count — _pipeline_backlog_cards() above.
         >0 means real work (already-withdrawn cards) is still
         mid-pipeline as this day ends. Nothing here pauses, resets, or
         otherwise interrupts the simulation for that — a part
         mid-station keeps running straight through the boundary
         exactly like a real line would, and no NEW demand appears
         until the next day's own withdrawal rows fire.

    One dict is appended to rt.daily_log (caller-owned, same
    by-reference pattern as snapshot_log/event_log) per day, for a
    caller that wants the numbers programmatically instead of / in
    addition to the printed report:

        {"day_index": int,       # 1, 2, 3, ... (1 = the first FULL day
                                  # that just ended)
         "day_label": str,       # "Day 2 (2026-09-02)" if rt.sim_epoch is
                                  # known, else "Day 2"
         "t": float,             # env.now at this checkpoint
         "backlog_cards": int,   # _pipeline_backlog_cards() result
         "restmenge": dict}      # print_restmenge_report()'s return value

    Runs for as long as the environment does — like withdrawal_process
    (and, in a mixed run, crew_process), this generator never naturally
    terminates; env.run(until=horizon_s) is what stops it.
    """
    kenv = rt.kenv
    env = kenv.env
    day_index = 0

    while True:
        yield env.timeout(rt.day_length_s)
        day_index += 1

        if rt.sim_epoch is not None:
            day_date = (rt.sim_epoch + timedelta(seconds=(day_index - 1) * rt.day_length_s)).date()
            day_label = f"Day {day_index} ({day_date.isoformat()})"
        else:
            day_label = f"Day {day_index}"

        if verbose:
            print(f"\n{'='*70}\n  END OF {day_label} — t={env.now:.1f}s "
                  f"({env.now/3600:.2f}h)\n{'='*70}")

        for line in kenv.lines:
            print_kpi(kenv, line.line_id, env.now)

        restmenge = print_restmenge_report(kenv, verbose=verbose)

        backlog = _pipeline_backlog_cards(kenv)
        if backlog > 0:
            print(f"  ⚠ {backlog} Kanban card(s) still in the pull pipeline "
                  f"(withdrawn, not yet back in a Supermarket) as {day_label} "
                  f"ends — carrying over into next day. (Scrapped pieces are "
                  f"not redone — this count is only cards still legitimately "
                  f"in flight, not lost scrap.)")
        elif verbose:
            print(f"  (no backlog — every withdrawn card is back in a "
                  f"Supermarket as {day_label} ends)")

        rt.daily_log.append({
            "day_index": day_index,
            "day_label": day_label,
            "t": env.now,
            "backlog_cards": backlog,
            "restmenge": restmenge,
        })
