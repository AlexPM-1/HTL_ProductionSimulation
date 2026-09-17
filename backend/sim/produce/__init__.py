"""
sim/produce/
============
Process logic for running parts through a line.

    changeover.py         changeover(), _resolve_setup_time_s()
                           — used by run_kanban_batch.py, run_order.py
    part_lifecycle.py      part_lifecycle(), process_part_at_station(),
                           inspection_outcome(), run_lochfilter_drs_production(),
                           _draw_from_chute(), _sample_processing_time(),
                           _safe_attr_name()
                           — used by run_kanban_batch.py, run_order.py
    routing.py             material_stored_types()
                           — used by run_kanban_batch.py, run_order.py
    run_order.py           run_one_order() (push side)
                           — used by sim/drain/push_turn.py
    run_kanban_batch.py    run_one_kanban_batch(), _make_kanban_order_record()
                           (kanban/pull side)
                           — used by sim/drain/pull_turn.py

line_kpi_summary()/print_kpi() live in reports/kpi/by_line.py, not here.
"""

from __future__ import annotations
