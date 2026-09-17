"""
sim/resources/build.py
=======================
Factory functions that instantiate all SimPy resources for a run:
build_environment() (push model) and build_kanban_environment()
(sibling factory, layers the Kanban resources on top).

Called once per run by sim.runner.run_mixed().
"""

from __future__ import annotations

import random
from typing import Optional

import simpy

from domain.config import (
    SimConfig, StationConfig, BufferConfig, InventoryConfig, ChuteConfig,
    SupermarketSlotConfig,
)

from sim.resources.line import ProductionLine
from sim.resources.stations import StationResource, BufferResource
from sim.resources.inventory import InventoryResource, ChuteResource
from sim.resources.environment import SimEnvironment, KanbanSimEnvironment
from sim.resources.supermarket import SupermarketResource
from sim.resources.collector import CollectionBoxResource, BatchCollectorResource
from sim.resources.chute import KanbanChuteResource


# ===========================================================================
# Factory function – build_environment
# ===========================================================================

def build_environment(
    env: simpy.Environment,
    cfg: SimConfig,
    seed: int = 42,
) -> SimEnvironment:
    """
    Instantiate all SimPy resources (machines + buffers) for every line
    and return a fully initialised SimEnvironment.

    Parameters
    ----------
    env  : a fresh simpy.Environment()
    cfg  : validated SimConfig from domain.config.load_config()
    seed : random seed for the RNG (int)

    Returns
    -------
    SimEnvironment  — ready for process logic to be attached (sim.produce)
    """
    rng = random.Random(seed)
    lines: list[ProductionLine] = []

    for line_idx, line_name in enumerate(cfg.line_names):
        line_id = line_idx + 1  # 1-based

        # ---- Stations ----
        station_resources: dict[str, StationResource] = {}
        for station_cfg in cfg.stations.get(line_name, []):
            resource = simpy.Resource(env, capacity=1)
            sr = StationResource(
                station_cfg = station_cfg,
                line_id     = line_id,
                resource    = resource,
            )
            station_resources[station_cfg.name] = sr

        # ---- Buffers ----
        buffer_resources: list[BufferResource] = []
        for buf_cfg in cfg.buffers.get(line_name, []):
            # SimPy Container: capacity = K, initial level = initial_fill
            container = simpy.Container(
                env,
                capacity = buf_cfg.capacity,
                init     = buf_cfg.initial_fill,
            )
            br = BufferResource(
                buffer_cfg = buf_cfg,
                line_id    = line_id,
                container  = container,
            )
            buffer_resources.append(br)

        line = ProductionLine(
            line_id         = line_id,
            line_name       = line_name,
            stations        = station_resources,
            buffers         = buffer_resources,
            current_product = "",
        )
        lines.append(line)

    # ---- Inventories ("Inventories" sheet, Type == Inventory) ----
    # Each named inventory may have several lanes (one per Stored_type),
    # e.g. Inv_nach_ECM has "All materials" / "Lochfitler material" /
    # "DRS material" — all backed by the same virtually-infinite raw
    # source, tracked as independent containers so chutes can pull the
    # right stored_type.
    inventories: dict[str, dict[str, InventoryResource]] = {}
    all_inventory_lanes: list[InventoryResource] = []
    for inv_name, lanes in cfg.inventories.items():
        lane_dict: dict[str, InventoryResource] = {}
        for inv_cfg in lanes:
            if inv_cfg.is_unbounded:
                # capacity=inf so put() never blocks a sink-type inventory
                # (e.g. Inv_nach_HTL). init stays FINITE (the literal
                # initial_fill from the sheet, e.g. 1000000) — NOT inf:
                # SimPy's Container._do_put checks
                # `capacity - level >= amount`, and inf - inf is nan,
                # which is never >= anything, so an inf/inf container
                # deadlocks on the very first put(). A source-type
                # inventory (e.g. Inv_nach_ECM) just keeps its large
                # literal initial_fill as "virtually infinite" supply.
                container = simpy.Container(env, capacity=float("inf"), init=inv_cfg.initial_fill)
            else:
                container = simpy.Container(env, capacity=inv_cfg.capacity, init=inv_cfg.initial_fill)
            inv_res = InventoryResource(inventory_cfg=inv_cfg, container=container)
            lane_dict[inv_cfg.stored_type] = inv_res
            all_inventory_lanes.append(inv_res)
        inventories[inv_name] = lane_dict

    # ---- FIFO Chutes ("Inventories" sheet, Type == FIFO_Chute) ----
    # Each chute lane must resolve which InventoryResource lane it draws
    # from on a level-triggered pull. Matching is done in two steps:
    #
    #   1. Finished-goods match: an inventory lane whose `upstream_station`
    #      (the station that FILLS it, e.g. "Lochfilter"/"DRS") equals the
    #      chute lane's `stored_type` (e.g. "Lochfilter"/"DRS") — i.e. the
    #      HTL3 chute's Lochfilter lane draws from Inv_nach_Loch, the
    #      inventory that the Lochfilter station itself fills.
    #
    #   2. Raw-material match: a "source" inventory lane (upstream_station
    #      is None — e.g. any of Inv_nach_ECM's 3 lanes) whose own
    #      `stored_type` matches the chute lane's `stored_type` exactly
    #      (e.g. Chu_vor_Lochfilter's "Lochfitler material" lane <->
    #      Inv_nach_ECM's "Lochfitler material" lane). A chute lane with no
    #      exact stored_type match among source lanes (e.g. any "Standard"
    #      lane) falls back to the general "All materials" source lane —
    #      the raw-material store at the head of the whole flow.
    def _resolve_upstream_inventory(chute_cfg: "ChuteConfig") -> Optional[InventoryResource]:
        target = chute_cfg.stored_type.strip().lower()

        for inv in all_inventory_lanes:
            up = inv.upstream_station
            if up and up.strip().lower() == target:
                return inv

        source_lanes = [inv for inv in all_inventory_lanes if inv.upstream_station is None]
        for inv in source_lanes:
            if inv.stored_type.strip().lower() == target:
                return inv

        generic = [inv for inv in source_lanes if "all" in inv.stored_type.strip().lower()]
        if generic:
            return generic[0]
        return source_lanes[0] if source_lanes else None

    chutes: dict[str, dict[str, ChuteResource]] = {}
    chutes_by_station: dict[str, dict[str, ChuteResource]] = {}
    for chute_name, lanes in cfg.chutes.items():
        lane_dict: dict[str, ChuteResource] = {}
        for chute_cfg in lanes:
            container = simpy.Container(env, capacity=chute_cfg.capacity, init=chute_cfg.initial_fill)
            ch = ChuteResource(
                chute_cfg           = chute_cfg,
                container           = container,
                upstream_inventory  = _resolve_upstream_inventory(chute_cfg),
                refill_lock         = simpy.Resource(env, capacity=1),
            )
            lane_dict[chute_cfg.stored_type] = ch
            chutes_by_station.setdefault(chute_cfg.station, {})[chute_cfg.stored_type] = ch
        chutes[chute_name] = lane_dict

    return SimEnvironment(
        env   = env,
        cfg   = cfg,
        lines = lines,
        rng   = rng,
        inventories       = inventories,
        chutes            = chutes,
        chutes_by_station = chutes_by_station,
    )


# ===========================================================================
# Factory function – build_kanban_environment
# ===========================================================================

def build_kanban_environment(
    env: simpy.Environment,
    cfg: SimConfig,
    seed: int = 42,
) -> KanbanSimEnvironment:
    """
    Sibling factory to build_environment() — builds the Kanban-loop
    resources on top of the exact same station/buffer/inventory/chute
    wiring, by calling build_environment() itself (so that wiring stays
    completely unchanged/unduplicated) and copying its fields into a new
    KanbanSimEnvironment, then layering the 4 new resource dicts and
    seeding them from cfg.kanban_cards / cfg.supermarkets.

    A SupermarketResource is created for each (line, product) pair drawn
    from cfg.kanban_cards, but restricted to that product's
    KanbanCardConfig.eligible_lines (the HTL3/HTL5/HTL6 columns on the
    KanbanCardsSetup sheet — see domain.config's sheet parser). This
    module still has no import-time dependency on domain.products'
    routing internals beyond ProductClass; eligible_lines is read purely
    off cfg.kanban_cards, which domain.config already resolved from the
    sheet.

    Backward-compat fallback: if a product's eligible_lines comes back empty
    (e.g. an older workbook without the HTL3/HTL5/HTL6 columns — see the
    domain.config docstring note that eligible_lines is simply empty for
    every row in that case), it is treated as "eligible on every line" rather
    than "eligible on none", so older workbooks keep building exactly the
    unfiltered (line, product) matrix this function used to build
    unconditionally.

    --- Capacity/seed stock come from the "Supermarkets" sheet, not a
    flat global constant ---------------------------------------------
    A per-slot layout — Line | RowNumber | Type | Capacity | Sachnummer |
    InitialState | InitialPcsPartial — and, importantly, a line can have
    SEVERAL physical "Main runner" rows for the SAME product (e.g. HTL3
    rows 1 & 2 both F00RJ02491 — separate physical lanes, not a
    duplicate/error; see that dataclass's docstring). Since ONE
    SupermarketResource models a (line, product) pair as a single
    logical store, every "Main runner" row matching a given (line,
    sachnummer) is summed together here: capacity = sum(row.capacity),
    seed cards = sum(row.initial_state), seed partial pieces =
    sum(row.initial_pcs_partial) — with the summed partial pieces folded
    into extra whole seed cards if they cross a batch_size boundary
    (mirrors SupermarketResource.deposit_finished_pcs()'s own divmod
    logic; two lanes each sitting on a partial pack can genuinely add up
    to a full card once combined into one logical store).

    "Exotic" rows (push/generic slots, no fixed Sachnummer — see
    SupermarketSlotConfig.is_exotic) are NOT touched by this function at
    all: no SupermarketResource is built for them here. They currently
    exist only as raw SupermarketSlotConfig rows on cfg.supermarkets;
    sim.fill.push.exotic.ExoticSupermarketTracker reads those directly
    and tracks push-chunk deposits independently (see that class's
    module-level caveat about not yet being wired into a resource object
    from this module).

    If a product is eligible (per KanbanCardsSetup) on a line but the
    "Supermarkets" sheet has NO "Main runner" row at all for that exact
    (line, sachnummer), there are two possible reasons, distinguished by
    whether that line has any "Exotic" rows at all:
      - Line HAS Exotic rows: expected/by-design — this product is
        intentionally routed through the line's shared Exotic pool
        instead of getting a private Main-runner lane. A synthetic
        fallback SupermarketResource is still built (capacity =
        kanban_timing.supermarket_capacity_cards, zero seed stock, so
        kanban process logic always has somewhere to withdraw from) but
        it's flagged via `SupermarketResource.is_exotic_routed = True`,
        and only a quiet informational (ℹ) note is printed — this is NOT
        a workbook problem.
      - Line has NO Exotic rows either: genuinely under-specified — same
        synthetic fallback, but this prints a ⚠ warning, since there's
        nowhere (dedicated or shared) for this product's cards to
        physically live on that line. Likely an incomplete/not-yet-
        migrated workbook.

    Parameters
    ----------
    env  : a fresh simpy.Environment()
    cfg  : validated SimConfig from domain.config.load_config()
           (must have been loaded with a workbook containing the 4 Kanban
           sheets for kanban_cards/kanban_withdrawals/supermarkets/
           kanban_timing to be non-empty/non-default; if absent, this
           still returns a valid but empty KanbanSimEnvironment)
    seed : random seed for the RNG (int) — passed straight through to
           build_environment(), so a push and a kanban run built from the
           same seed share identical station/buffer RNG behavior

    Returns
    -------
    KanbanSimEnvironment — ready for sim.fill.pull.bootstrap /
    sim.drain.crew to attach withdrawal_process, day_boundary_process,
    collection_box_emptying_process, and crew_process().
    """
    base = build_environment(env, cfg, seed=seed)

    kenv = KanbanSimEnvironment(
        env=base.env,
        cfg=base.cfg,
        lines=base.lines,
        rng=base.rng,
        parts_out=base.parts_out,
        parts_in_wip=base.parts_in_wip,
        part_counter=base.part_counter,
        event_log=base.event_log,
        inventories=base.inventories,
        chutes=base.chutes,
        chutes_by_station=base.chutes_by_station,
    )

    # Shared, read-only across all 3 lines' BatchCollectorResource instances.
    cards_to_trigger: dict[str, int] = {
        sachnr: card_cfg.cards_to_trigger for sachnr, card_cfg in cfg.kanban_cards.items()
    }

    # {(line_name, sachnummer): [SupermarketSlotConfig, ...]} — every
    # "Main runner" (pull) row from the "Supermarkets" sheet, grouped for
    # the aggregation described above. "Exotic" rows are deliberately
    # excluded here (see the docstring note on why this function doesn't
    # build resources for them).
    main_runner_rows: dict[tuple[str, str], list[SupermarketSlotConfig]] = {}
    # {line_name: bool} — whether this line has ANY "Exotic" row at all in
    # the "Supermarkets" sheet. Used below to tell "this product is
    # intentionally routed through the shared Exotic pool" (line has
    # Exotic rows) apart from "this workbook is genuinely
    # incomplete/not-yet-migrated" (line has neither a Main-runner row for
    # this product NOR any Exotic rows to fall back on).
    line_has_exotic_rows: dict[str, bool] = {}
    for line_name, slot_cfgs in (cfg.supermarkets or {}).items():
        line_has_exotic_rows[line_name] = any(s.is_exotic for s in slot_cfgs)
        for slot in slot_cfgs:
            if slot.is_exotic or not slot.sachnummer:
                continue
            main_runner_rows.setdefault((line_name, slot.sachnummer), []).append(slot)

    for line in kenv.lines:
        line_id = line.line_id
        line_name = line.line_name

        # ---- Supermarket (one SupermarketResource per eligible product) ----
        supermarket_lane: dict[str, SupermarketResource] = {}
        for sachnr, card_cfg in cfg.kanban_cards.items():
            # Empty eligible_lines means the sheet had no HTL3/HTL5/HTL6
            # columns at all (older workbook) — fall back to "eligible on
            # every line" so those workbooks still build the full matrix
            # this function used to build unconditionally. A non-empty
            # eligible_lines list is honored literally: skip this line if
            # it isn't listed.
            if card_cfg.eligible_lines and line_name not in card_cfg.eligible_lines:
                continue

            rows = main_runner_rows.get((line_name, sachnr), [])
            exotic_routed = False
            if rows:
                capacity = sum(r.capacity for r in rows)
                n_seed_cards = sum(r.initial_state for r in rows)
                pcs_partial = sum(r.initial_pcs_partial for r in rows)
                if pcs_partial >= card_cfg.batch_size:
                    # Summing leftover pieces from several physical lanes
                    # can itself complete one or more whole cards (e.g.
                    # two lanes each sitting on 150/200pcs sum to 300 =
                    # 1 full card + 100 leftover) — fold that in exactly
                    # like SupermarketResource.deposit_finished_pcs() does.
                    extra_cards, pcs_partial = divmod(pcs_partial, card_cfg.batch_size)
                    n_seed_cards += extra_cards
            elif line_has_exotic_rows.get(line_name, False):
                # No dedicated Main-runner row for this (line, sachnr), but
                # the line DOES have Exotic rows — this is the expected,
                # by-design case for a product that's routed through the
                # shared Exotic pool rather than a private lane, not a
                # workbook problem. Quiet, informational only.
                print(f"  ℹ {sachnr!r} on {line_name} has no dedicated 'Main "
                      f"runner' Supermarkets row — routed via the shared "
                      f"'Exotic' pool instead (per KanbanCardsSetup eligibility).")
                capacity = cfg.kanban_timing.supermarket_capacity_cards
                n_seed_cards = 0
                pcs_partial = 0
                exotic_routed = True
            else:
                print(f"  ⚠ No 'Main runner' Supermarkets row found for {sachnr!r} "
                      f"on {line_name} (eligible per KanbanCardsSetup), and this "
                      f"line has no 'Exotic' rows either to fall back on — falling "
                      f"back to kanban_timing.supermarket_capacity_cards="
                      f"{cfg.kanban_timing.supermarket_capacity_cards}, zero seed "
                      f"stock. Likely an incomplete/not-yet-migrated workbook.")
                capacity = cfg.kanban_timing.supermarket_capacity_cards
                n_seed_cards = 0
                pcs_partial = 0

            sm = SupermarketResource(
                line_id         = line_id,
                product_type    = sachnr,
                batch_size      = card_cfg.batch_size,
                store           = simpy.Store(env, capacity=capacity),
                capacity        = capacity,
                is_exotic_routed= exotic_routed,
            )
            sm.pcs_partial = pcs_partial
            if n_seed_cards > capacity:
                print(f"  ⚠ Supermarket seed for {sachnr!r} on {line_name}: "
                      f"initial_state totals {n_seed_cards} card(s) across "
                      f"{len(rows)} row(s), exceeding capacity={capacity} "
                      f"— clipping to {capacity}.")
                n_seed_cards = capacity
            for _ in range(n_seed_cards):
                card = kenv.create_card(
                    product_type = sachnr,
                    line_id      = line_id,
                    batch_size   = card_cfg.batch_size,
                    priority     = "M",
                )
                # Direct list append (not `yield store.put(...)`): this
                # runs at build time, before env.run() starts, exactly
                # mirroring how build_environment() seeds Container
                # levels via the `init=` constructor argument above.
                # Safe to bypass capacity enforcement here since we've
                # already clipped n_seed_cards to `capacity` above.
                sm.store.items.append(card)
            supermarket_lane[sachnr] = sm
        kenv.supermarkets[line_id] = supermarket_lane

        # ---- Collection Box / Batch Collector / Kanban Chute (one each per line) ----
        kenv.collection_boxes[line_id] = CollectionBoxResource(line_id=line_id)
        kenv.batch_collectors[line_id] = BatchCollectorResource(
            line_id=line_id, cards_to_trigger=cards_to_trigger,
        )
        kenv.kanban_chutes[line_id] = KanbanChuteResource(line_id=line_id)

    return kenv


# ===========================================================================
# Quick self-test
# ===========================================================================
if __name__ == "__main__":
    import sys
    from domain.config import load_config

    excel = sys.argv[1] if len(sys.argv) > 1 else "ProductionPlanning_v6.xlsx"
    csv   = sys.argv[2] if len(sys.argv) > 2 else "HTL_setup_times.xlsx"
    cfg  = load_config(excel, csv)
    env  = simpy.Environment()
    sim  = build_environment(env, cfg, seed=42)
    sim.describe()

    # Create a few test parts and verify the entity structure
    print("\n--- Test Part Creation ---")
    for product, qty in list(cfg.demand[0].quantities.items())[:3]:
        p = sim.create_part(product_type=product, line_id=1)
        print(f"  Created: {p}")

    # Simulate a part completing the line
    p_test = sim.parts_in_wip[0]
    sim.finish_part(p_test, status="passed")
    print(f"\n  Finished (passed): {p_test}")
    print(f"  WIP count : {len(sim.parts_in_wip)}")
    print(f"  Out count : {len(sim.parts_out)}")

    # ---- Kanban environment self-test ----
    print("\n\n=== build_kanban_environment() self-test ===")
    kenv_raw = simpy.Environment()
    ksim = build_kanban_environment(kenv_raw, cfg, seed=42)
    ksim.describe_kanban()

    if cfg.kanban_cards:
        sachnr = next(iter(cfg.kanban_cards))
        sm = ksim.supermarket_for(line_id=1, product_type=sachnr)
        print(f"\n  Supermarket lane for {sachnr!r} on line 1: {sm}")

        # Simulate production finishing a batch's worth of pieces
        if sm is not None:
            n_new = sm.deposit_finished_pcs(sm.batch_size)
            print(f"  deposit_finished_pcs({sm.batch_size}) -> {n_new} whole batch(es)")
            for _ in range(n_new):
                card = ksim.create_card(sachnr, line_id=1, batch_size=sm.batch_size)
                sm.store.items.append(card)
            print(f"  Supermarket lane after deposit: {sm}")

        # Exercise the priority chute ordering (H before M before L, FIFO within tie)
        chute = ksim.kanban_chute_for(line_id=1)
        c1 = ksim.create_card(sachnr, line_id=1, batch_size=50, priority="L")
        c2 = ksim.create_card(sachnr, line_id=1, batch_size=50, priority="H")
        c3 = ksim.create_card(sachnr, line_id=1, batch_size=50, priority="M")
        chute.push_batch(sachnr, [c1])
        chute.push_batch(sachnr, [c2])
        chute.push_batch(sachnr, [c3])
        print(f"\n  Chute pending before pops: {chute.n_pending_batches}")
        order = []
        while chute.n_pending_batches:
            entry = chute.pop_next()
            product_type, cards = entry.product_type, entry.cards
            order.append(cards[0].priority)
        print(f"  Pop order (expect H, M, L): {order}")
