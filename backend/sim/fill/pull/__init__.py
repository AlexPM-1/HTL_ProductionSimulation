"""
sim/fill/pull/
==============
The pull (Kanban) half of Movement 1:

    withdrawal.py       withdrawal_process(), _withdrawal_stream(),
                         _n_cards_for_row(), _withdraw_one_card()
                         — used by bootstrap.py
    assignment.py       build_supermarket_state_lookup(),
                         select_supermarket_for_withdrawal()
                         — used by withdrawal.py
    collection_box.py   collection_box_emptying_process()
                         — used by bootstrap.py
    cards.py            _return_card_to_supermarket()
                         — used by sim/drain/pull_turn.py
    day_boundary.py      day_boundary_process(), print_restmenge_report(),
                         _pipeline_backlog_cards(), _PIPELINE_STATES
                         — used by bootstrap.py
    bootstrap.py        start_mixed_simulation(), the pull-side launcher
                         — used by sim/runner.py
"""
