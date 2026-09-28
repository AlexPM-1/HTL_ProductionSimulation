"""
sim.resources
=============
Entities, resources, and their factory functions — no simpy-dependent
process logic lives here. gate.py (LinePriorityGate) is the one module
whose class is constructed directly by sim/runner.py rather than by
build.py.

    part.py         Part
    stations.py     StationResource, BufferResource
    inventory.py    InventoryResource, MaterialChuteResource (material FIFO chute)
    line.py         ProductionLine
    cards.py        PullCard
    supermarket.py  SupermarketResource
    collector.py    CollectionBoxResource, BatchCollectorResource
    chute.py        ChuteEntry, ChuteResource (admission queue)
    gate.py         LinePriorityGate
    environment.py  SimEnvironment, MixedSimEnvironment
    build.py        build_environment, build_mixed_environment

Import from the specific submodule you need (see each module's header)
rather than relying on re-exports here.
"""
