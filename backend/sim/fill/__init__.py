"""
sim/fill/
=========
Movement 1 — chute FILLING.

    sim/fill/pull/   withdrawal, supermarket assignment, collection-box
                      emptying, card recycling, day-boundary checkpoint,
                      the start_kanban_simulation() launcher
    sim/fill/push/   rolling per-order dispatcher, chunking, exotic
                      Supermarket deposit/withdraw tracking, chute
                      bookkeeping

Both sides take a single `sim.context.RunContext` (see that module's
docstring). Movement 2 (chute DEPLETION — crew_process and the
turn-runners) lives in sim/drain/.
"""
