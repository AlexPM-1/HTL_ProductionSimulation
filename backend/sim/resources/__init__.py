"""
sim.resources
=============
Entities, resources, and their factory functions — no simpy-dependent
process logic lives here. gate.py (LinePriorityGate) is the one module
whose class is constructed directly by sim/runner.py rather than by
build.py.

    part.py         Part
    stations.py     StationResource, BufferResource
    inventory.py    InventoryResource, ChuteResource (material FIFO chute)
    line.py         ProductionLine
    cards.py        KanbanCard
    supermarket.py  SupermarketResource
    collector.py    CollectionBoxResource, BatchCollectorResource
    chute.py        ChuteEntry, KanbanChuteResource (admission queue)
    gate.py         LinePriorityGate
    environment.py  SimEnvironment, KanbanSimEnvironment
    build.py        build_environment, build_kanban_environment

Import from the specific submodule you need (see each module's header)
rather than relying on re-exports here.
"""
