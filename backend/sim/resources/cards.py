"""
sim/resources/cards.py
=======================
KanbanCard — one physical Kanban card. Created by
KanbanSimEnvironment.create_card() (sim.resources.environment) and
circulated by sim.produce/sim.fill.pull/sim.drain as it moves through
the pull loop.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class KanbanCard:
    """
    A physical Kanban card — one per circulating batch-slot for a given
    (line, product). card_id is PERMANENT: the same KanbanCard instance
    is reused every loop (in_supermarket -> withdrawn ->
    in_collection_box -> in_batch_collector -> released_to_chute ->
    in_production -> in_supermarket), never recreated. That makes `transitions` the card's
    complete lifetime history, so cycle-time-per-card is a simple diff
    between two of its own timestamps (e.g. successive 'withdrawn'
    entries, or 'withdrawn' -> the next 'in_supermarket').

    Attributes
    ----------
    card_id      : permanent unique identifier, assigned by
                   KanbanSimEnvironment.create_card()
    product_type : sachnummer this card is dedicated to for its whole
                   life (no product-switching on a card in this design)
    line_id      : 1-based line this card circulates on
    batch_size   : pieces represented by this card (from KanbanCardConfig
                   for this product — fixed, so fixed per card)
    priority     : "H" | "M" | "L" — (re)set at each withdrawal from the
                   CustomerDemandKanban row that triggered it; carried
                   through collection box / batch collector / chute so
                   KanbanChuteResource can order releases on it
    state        : current position in the card's state machine; one of
                   the six state-machine values listed above
    transitions  : list of (state, sim_time) tuples, one appended per
                   record_transition() call — the full audit trail
    """
    card_id: int
    product_type: str
    line_id: int
    batch_size: int
    priority: str = "M"
    state: str = "in_supermarket"
    transitions: list[tuple[str, float]] = field(default_factory=list)

    def record_transition(self, new_state: str, t: float) -> None:
        """Advance the card's state machine and timestamp the move."""
        self.state = new_state
        self.transitions.append((new_state, round(t, 3)))

    def __repr__(self) -> str:
        return (
            f"KanbanCard(id={self.card_id}, product={self.product_type}, "
            f"line={self.line_id}, state={self.state}, priority={self.priority})"
        )
