"""
sim/resources/supermarket.py
=============================
SupermarketResource — the per-(line, product) ready-batch store at the
head of the Kanban loop. Built/seeded by
sim.resources.build.build_mixed_environment(); withdrawn from by
sim.fill.pull.withdrawal, deposited into by sim.produce.run_pull_batch.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import simpy


@dataclass
class SupermarketResource:
    """
    Ready-batch store for ONE (line, product) pair — the "Supermarket"
    box at the head of the Kanban loop.

    `store` holds one PullCard per whole, ready batch of `card_size`
    finished parts, card still attached. It is a plain simpy.Store
    (unbounded capacity) rather than a Container, precisely so a
    withdrawal that finds it empty can `yield store.get()` and block
    until production deposits the next batch — no separate "stockout"
    branch is needed, the withdrawing process simply waits.

    `pcs_partial` tracks leftover finished pieces that haven't yet
    accumulated to a full `card_size` chunk — a partial pack can NEVER
    be withdrawn on its own; only deposit_finished_pcs() converting it
    into one or more whole cards via `store` makes it available. This
    mirrors InventoryResource.partial_pack_by_product's bookkeeping-only
    style (sim/resources/inventory.py): the caller still performs the
    actual `yield store.get()` / card creation; this class only does the
    piece-counting arithmetic and simple stats.
    """
    line_id: int
    product_type: str
    card_size: int
    store: simpy.Store
    capacity: int = field(default=25)
    pcs_partial: int = 0
    is_exotic_routed: bool = False
    """
    True when this product has NO dedicated "Main runner" row on this line
    in the "Supermarkets" sheet, but the line DOES have "Exotic" rows —
    i.e. this is a deliberately-exotic product per the current
    Supermarkets layout (not a missing/incomplete-workbook case). The
    real physical storage for this product's cards
    is one of the line's shared "Exotic" slots, tracked separately by
    sim.fill.push.exotic.ExoticSupermarketTracker. Frontend/reporting
    code should use this flag to render such lanes under the line's
    Exotic pool rather than as their own dedicated Main-runner row.
    """
    n_withdrawals: int = 0
    n_deposits: int = 0

    @property
    def n_available(self) -> int:
        """Whole batches (cards) currently sitting ready in the supermarket."""
        return len(self.store.items)

    @property
    def is_stocked(self) -> bool:
        return self.n_available > 0

    @property
    def is_full(self) -> bool:
        """True once the lane holds `capacity` whole batches. `store` is a
        bounded simpy.Store (capacity=self.capacity, set in
        build_mixed_environment) so store.put() already blocks on its own
        once this is True — this property is for reporting/pre-checks
        only, not itself an enforcement mechanism."""
        return self.n_available >= self.capacity

    def record_withdrawal(self) -> None:
        """Bookkeeping only — caller performs the actual `yield store.get()`."""
        self.n_withdrawals += 1

    def deposit_finished_pcs(self, qty: int) -> int:
        """
        Accumulate `qty` freshly finished pieces of this product into
        pcs_partial and return how many WHOLE card_size chunks that
        completes. The caller (kanban process logic) is responsible for
        creating/reusing exactly that many PullCard objects — flipping
        each to 'in_supermarket' via record_transition() — and pushing
        them into `store` (e.g. `store.items.append(card)` — a direct
        append rather than `yield store.put(card)`, mirroring how
        build_mixed_environment seeds initial cards; see
        sim/resources/build.py). Any leftover < card_size stays in
        pcs_partial, unwithdrawable.
        """
        self.pcs_partial += qty
        n_whole_batches, self.pcs_partial = divmod(self.pcs_partial, self.card_size)
        if n_whole_batches:
            self.n_deposits += n_whole_batches
        return n_whole_batches

    def __repr__(self) -> str:
        return (
            f"SupermarketResource(line={self.line_id}, product={self.product_type}, "
            f"batches_ready={self.n_available}/{self.capacity}, "
            f"partial_pcs={self.pcs_partial}/{self.card_size})"
        )
