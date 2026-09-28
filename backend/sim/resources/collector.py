"""
sim/resources/collector.py
===========================
CollectionBoxResource and BatchCollectorResource — the two pull-side
holding stages between a withdrawal and a released batch. Both are
drained by sim.fill.pull.collection_box.collection_box_emptying_process().
"""

from __future__ import annotations

from dataclasses import dataclass, field

from sim.resources.cards import PullCard


@dataclass
class CollectionBoxResource:
    """
    Per-line box where withdrawn cards simply accumulate as they arrive,
    until the next periodic emptying (cadence =
    cfg.pull_timing_config.collection_box_emptying_min, default 30 min — read
    from config, not hardcoded here).

    Emptying is driven by
    sim.fill.pull.collection_box.collection_box_emptying_process(), which
    calls empty() and redistributes the returned cards into the line's
    BatchCollectorResource.
    """
    line_id: int
    cards: list[PullCard] = field(default_factory=list)
    last_emptied_at: float = 0.0

    def add(self, card: PullCard) -> None:
        self.cards.append(card)

    def empty(self, t: float) -> list[PullCard]:
        """Remove and return every card currently in the box, in arrival order."""
        emptied, self.cards = self.cards, []
        self.last_emptied_at = t
        return emptied

    @property
    def n_waiting(self) -> int:
        return len(self.cards)

    def __repr__(self) -> str:
        return f"CollectionBoxResource(line={self.line_id}, waiting={self.n_waiting})"


@dataclass
class BatchCollectorResource:
    """
    Per-line, per-product accumulation buckets that sit between the
    Collection Box and the Kanban Chute. A bucket's cards are released
    together, as one batch, once its count reaches that product's
    CardsToTrigger threshold (from cfg.pull_cards — a per-PRODUCT
    setting, not per-line).

    `cards_to_trigger` is shared (read-only) across all 3 lines' collector
    instances — it's the same {product_number: threshold} dict built once in
    build_mixed_environment() from cfg.pull_cards.
    """
    line_id: int
    cards_to_trigger: dict[str, int]
    buckets: dict[str, list[PullCard]] = field(default_factory=dict)

    def add(self, card: PullCard) -> None:
        self.buckets.setdefault(card.product_type, []).append(card)

    def is_ready(self, product_type: str) -> bool:
        """True once this product's bucket has reached its CardsToTrigger."""
        threshold = self.cards_to_trigger.get(product_type)
        if not threshold:
            return False
        return len(self.buckets.get(product_type, [])) >= threshold

    def pop_batch(self, product_type: str) -> list[PullCard]:
        """
        Pop exactly `cards_to_trigger[product_type]` cards off the front
        of that product's bucket (FIFO) and return them, leaving any
        surplus behind for the next trigger. Caller (kanban process
        logic) should check is_ready() first; popping from a bucket that
        hasn't reached threshold yet is allowed but returns fewer cards
        than the threshold, which the caller should not treat as a
        valid release.
        """
        threshold = self.cards_to_trigger.get(product_type, 0)
        bucket = self.buckets.get(product_type, [])
        released, remaining = bucket[:threshold], bucket[threshold:]
        self.buckets[product_type] = remaining
        return released

    def __repr__(self) -> str:
        counts = {p: len(c) for p, c in self.buckets.items() if c}
        return f"BatchCollectorResource(line={self.line_id}, buckets={counts})"
