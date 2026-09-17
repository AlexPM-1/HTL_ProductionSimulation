"""
sim/drain/
===========
Movement 2 — chute DEPLETION by crews.

    sim/drain/crew.py        crew_process(), the per-crew generator that
                              sim/runner.py starts one of per crew
    sim/drain/selection.py   select_line_for_crew(), called by crew.py
    sim/drain/pull_turn.py   run_pull_turn() and PendingPullBatch,
                              called by crew.py
    sim/drain/push_turn.py   run_push_turn(), called by crew.py

`ctx: RunContext` everywhere below is the shared per-run state object
defined in sim/context.py.
"""
