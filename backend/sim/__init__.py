"""
sim
===
The simulation engine package.

    sim/resources/   entities & SimPy resources (Part, stations, buffers, chutes, ...)
    sim/produce/     process logic: changeover, part lifecycle, routing
    sim/context.py   RunContext, the shared per-run state object
    sim/clock.py     wall_clock / is_line_on / seconds_until_on helpers
    sim/fill/        chute filling — pull side and push side
    sim/drain/       crew-based chute depletion (pull turns + push turns)
    sim/oee.py       OEE loss tracking
    sim/runner.py    top-level simulation runner (run_mixed)
    sim/entrypoints/ CLI entrypoints
"""
