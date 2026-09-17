"""
sim/fill/push/
===============
The push (class-2) half of Movement 1:

    chunking.py       build_push_order_record(), split_into_chunks()
                       — used by dispatch.py
    dispatch.py        push_dispatch_process() — started by sim/runner.py
    exotic.py          ExoticSlotState, ExoticSupermarketTracker,
                        _deposit_push_chunk_to_supermarket(),
                        _withdraw_push_chunk_process() — used by
                        sim/drain/push_turn.py and sim/runner.py
    chute_tracker.py   PushChuteTracker — used by sim/runner.py
"""
