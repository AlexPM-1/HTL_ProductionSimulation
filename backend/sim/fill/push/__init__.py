"""
sim/fill/push/
===============
The push (class-2) half of Movement 1:

    splitting.py       build_push_order_record(), split_into_push_cards()
                       — used by dispatch.py
    dispatch.py        push_dispatch_process() — started by sim/runner.py
    exotic.py          ExoticSlotState, ExoticSupermarketTracker,
                        _deposit_push_card_to_supermarket(),
                        _withdraw_push_card_process() — used by
                        sim/drain/push_turn.py and sim/runner.py
    chute_tracker.py   PushChuteTracker — used by sim/runner.py
"""
