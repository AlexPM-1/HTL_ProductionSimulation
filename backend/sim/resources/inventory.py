"""
sim/resources/inventory.py
===========================
InventoryResource (slow, virtually-unbounded stores) and ChuteResource
(the MATERIAL FIFO chute immediately upstream of a station — unrelated
to KanbanChuteResource in sim/resources/chute.py despite the shared
"Chute" name; see that module's naming note).

Built by sim.resources.build.build_environment(); drawn from by
sim.produce.part_lifecycle._draw_from_chute().
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

import simpy

from domain.config import InventoryConfig, ChuteConfig


# ===========================================================================
# Inventory / FIFO Chute resources  (Lochfilter / DRS decoupling model)
# ===========================================================================

@dataclass
class InventoryResource:
    """
    Wraps a SimPy Container for ONE lane of ONE inventory (one row of the
    "Inventories" sheet with Type == "Inventory") — e.g. Inv_nach_ECM has
    three lanes: "All materials" (feeds Beladen directly, Standard chutes),
    "Lochfitler material" (feeds the Chu_vor_Lochfilter chute), "DRS
    material" (feeds the Chu_vor_DRS chute) — all backed by the same
    virtually-infinite raw-material source, but tracked as separate lanes
    so each chute pulls the right stored_type.

    Inventories are the *slow* stores in the model: either the (virtually
    infinite) raw-material buffer feeding every chute (Inv_nach_ECM), the
    finished-goods stores fed by the Lochfilter / DRS side-stations
    (Inv_nach_Loch, Inv_nach_DRS), or the terminal finished-parts store
    fed by Sichtpruefen (Inv_nach_HTL).

    FIFO chutes pull (get) from these when their trigger fires; the
    Lochfilter/DRS production process pushes (put) into Inv_nach_Loch /
    Inv_nach_DRS as it produces; Sichtpruefen pushes into Inv_nach_HTL.
    """
    inventory_cfg: InventoryConfig      # parameters from SimConfig (one lane)
    container: simpy.Container          # SimPy container (level = current fill)

    # Statistics
    n_withdrawals:   int = 0
    total_withdrawn: int = 0
    n_deposits:      int = 0
    total_deposited: int = 0

    # Per-product piece tally — used by finished-goods inventories
    # (Inv_nach_HTL, fed by Sichtpruefen) to report how many PACKS of
    # each product are physically stored, e.g. 120 packs of 25 units of
    # F00RC00419, 80 packs of 25 units of F00RC00638, etc. Populated via
    # record_deposit_product(); lanes that never call it (raw-material
    # Inv_nach_ECM lanes, generic Inv_nach_Loch / Inv_nach_DRS) simply
    # keep this empty and behave exactly as before.
    product_pieces: dict[str, int] = field(default_factory=dict)

    @property
    def name(self) -> str:
        return self.inventory_cfg.name

    @property
    def stored_type(self) -> str:
        return self.inventory_cfg.stored_type

    @property
    def upstream_station(self) -> Optional[str]:
        return self.inventory_cfg.upstream_station

    @property
    def capacity(self) -> "int | float":
        return float("inf") if self.inventory_cfg.is_unbounded else self.inventory_cfg.capacity

    @property
    def current_fill(self) -> "int | float":
        # int(float('inf')) raises OverflowError — keep it as float for
        # unbounded (is_unbounded) inventories instead of truncating.
        level = self.container.level
        return level if level == float("inf") else int(level)

    @property
    def pack_size(self) -> int:
        """Pieces per pack (from the sheet's PackSize column, e.g. 25)."""
        return self.inventory_cfg.pack_size

    def record_withdrawal(self, qty: int) -> None:
        """Bookkeeping only — caller performs the actual `yield container.get(qty)`."""
        self.n_withdrawals   += 1
        self.total_withdrawn += qty

    def record_deposit(self, qty: int) -> None:
        """Bookkeeping only — caller performs the actual `yield container.put(qty)`."""
        self.n_deposits      += 1
        self.total_deposited += qty

    def record_deposit_product(self, product_type: str, qty: int = 1) -> None:
        """
        Bookkeeping for a finished-goods deposit tied to a specific
        product (e.g. Inv_nach_HTL, fed by Sichtpruefen once a part
        passes final inspection). Updates the aggregate counters exactly
        like record_deposit(), plus the per-product piece tally that
        packs_by_product() turns into whole PackSize packs.
        Caller still performs the actual `yield container.put(qty)`.
        """
        self.record_deposit(qty)
        self.product_pieces[product_type] = self.product_pieces.get(product_type, 0) + qty

    @property
    def packs_by_product(self) -> dict[str, int]:
        """
        dict[product_type -> whole packs currently stored], e.g.
        {"F00RC00419": 120, "F00RC00638": 80, ...} for a 25-pcs PackSize.
        A product with fewer than pack_size pieces accumulated so far
        shows 0 whole packs (its pieces are a partial pack — see
        partial_pack_by_product). Falls back to raw piece counts if
        pack_size is 0/unset on this inventory row.
        """
        if not self.pack_size:
            return dict(self.product_pieces)
        return {p: pieces // self.pack_size for p, pieces in self.product_pieces.items()}

    @property
    def partial_pack_by_product(self) -> dict[str, int]:
        """dict[product_type -> leftover pieces not yet forming a whole pack]."""
        if not self.pack_size:
            return {p: 0 for p in self.product_pieces}
        return {p: pieces % self.pack_size for p, pieces in self.product_pieces.items()}

    def __repr__(self) -> str:
        return (
            f"InventoryResource(name={self.name}, lane={self.stored_type}, "
            f"fill={self.current_fill}/{self.capacity})"
        )


@dataclass
class ChuteResource:
    """
    Wraps a SimPy Container for ONE lane of ONE FIFO chute (one row of
    the "Inventories" sheet with Type == "FIFO_Chute") — e.g.
    Chu_vor_HTL3 has three lanes: Standard, Lochfilter, DRS, each its
    own ChuteResource with its own container.

    A chute is a small, pull-triggered buffer directly upstream of a
    station — NOT a kanban supermarket with a continuous arrival process.
    Replenishment (executed by sim.produce process logic, not here —
    this class only tracks state) is purely LEVEL-triggered:
        Once the chute's own current_fill drops to (or below)
        trigger_amount_left, the caller pulls replenish_qty_pcs
        (= replenish_amount packs x pack_size) units from
        `upstream_inventory` and calls record_refill(qty).
    Withdrawal FROM the chute is driven by the consuming station's own
    throughput (its cycle time), not by any external arrival distribution.

    NOT the same thing as KanbanChuteResource (sim/resources/chute.py's
    priority-ordered card admission queue) despite the shared "Chute"
    name — this one is the material FIFO chute; see that module's
    naming note.
    """
    chute_cfg:            ChuteConfig
    container:             simpy.Container
    upstream_inventory:    Optional[InventoryResource]   # resolved at build time
    refill_lock:           simpy.Resource                # capacity=1 — serializes
    # the "check trigger, then refill" critical section (see
    # sim.produce's _draw_from_chute). Without this, multiple parts that
    # all see the chute at/below trigger_amount_left before the first
    # refill completes (get()/put() both yield) would each independently
    # see needs_refill == True and each perform a redundant refill,
    # over-pulling from upstream_inventory.

    # Statistics
    n_refills:         int = 0
    total_refilled:    int = 0
    total_consumed:    int = 0
    max_observed_fill: int = 0

    @property
    def name(self) -> str:
        return self.chute_cfg.name

    @property
    def station(self) -> str:
        return self.chute_cfg.station

    @property
    def stored_type(self) -> str:
        return self.chute_cfg.stored_type

    @property
    def capacity(self) -> int:
        return self.chute_cfg.capacity

    @property
    def trigger_amount_left(self) -> int:
        return self.chute_cfg.trigger_amount_left

    @property
    def pack_size(self) -> int:
        return self.chute_cfg.pack_size

    @property
    def replenish_amount(self) -> int:
        return self.chute_cfg.replenish_amount

    @property
    def replenish_qty_pcs(self) -> int:
        """Total pieces pulled per replenishment = packs x pieces/pack."""
        return self.chute_cfg.replenish_qty_pcs

    @property
    def current_fill(self) -> int:
        return int(self.container.level)

    @property
    def needs_refill(self) -> bool:
        """
        True once the chute's own remaining stock has dropped to (or below)
        trigger_amount_left. Unlike the old kanban model, this is a pure
        fill-level check — there is no "consumed since last refill" counter.
        A freshly-built chute starts at initial_fill (per the sheet, 0 by
        default), which is already <= trigger_amount_left, so the very
        first check will correctly request an initial replenishment.
        """
        return self.trigger_amount_left > 0 and self.current_fill <= self.trigger_amount_left

    def record_consumption(self, qty: int = 1) -> None:
        """Bookkeeping only — caller performs the actual `yield container.get(qty)`."""
        self.total_consumed += qty

    def record_refill(self, qty: int) -> None:
        """Bookkeeping only — caller performs the actual container get/put pair."""
        self.n_refills      += 1
        self.total_refilled += qty
        if self.current_fill > self.max_observed_fill:
            self.max_observed_fill = self.current_fill

    def __repr__(self) -> str:
        return (
            f"ChuteResource(name={self.name}, lane={self.stored_type}, "
            f"fill={self.current_fill}/{self.capacity}, "
            f"trigger_left={self.trigger_amount_left}, "
            f"replenish={self.replenish_amount}pk({self.replenish_qty_pcs}pcs))"
        )
