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
from typing import Optional


@dataclass
class CardHistoryEntry:
    """
    One order-cycle's worth of timing for a KanbanCard — i.e. everything
    that happened to this physical card between being withdrawn for order
    `order_id` and being repositioned back onto its Supermarket shelf.

    Since card_id is PERMANENT (see KanbanCard's docstring), a card
    accumulates one of these per trip round the loop, appended to
    KanbanCard.history — so per-order production reporting (which line,
    which card, exactly when) can be reconstructed straight off the card
    without needing a separate per-card telemetry stream.

    All timestamps are simpy sim-time floats (env.now), same convention
    as ScheduleEvent/GateActivityEntry — None until that stage has
    actually happened for this entry. Fields are set/updated in place by
    whichever module owns that stage of the loop:

    order_id                  : the OrderRecord.order_id this cycle
                                 fulfils — set the instant the card is
                                 handed to a withdrawal request (see
                                 sim.fill.pull.withdrawal._withdraw_one_card)
    t_withdrawn_time           : moment the card left the Supermarket
                                 (sim.fill.pull.withdrawal)
    t_collection_box_start     : moment the card entered its line's
                                 collection box (sim.fill.pull.withdrawal /
                                 sim.fill.pull.collection_box)
    t_collection_box_end       : moment the card left the collection box
                                 (sim.fill.pull.collection_box)
    t_batch_collector_start    : moment the card entered the batch
                                 collector (sim.fill.pull.collection_box)
    t_batch_collector_end      : moment the card left the batch collector,
                                 released onto the Kanban Chute
                                 (sim.fill.pull.collection_box)
    t_chute_start               : moment the card was placed on the Kanban
                                 Chute (sim.resources.chute /
                                 sim.fill.pull.collection_box)
    t_chute_end                 : moment the card was popped off the front
                                 of the Kanban Chute for production
                                 (sim.drain.pull_turn)
    t_production_start          : moment production actually started on
                                 this card (sim.drain.pull_turn /
                                 sim.produce.run_kanban_batch)
    t_production_end            : moment this card's package finished
                                 production (sim.fill.pull.cards
                                 ._return_card_to_supermarket's `t`)
    t_reposition_supermarket    : moment the card was put back onto the
                                 Supermarket shelf, closing this cycle
                                 (sim.fill.pull.cards
                                 ._return_card_to_supermarket)
    """
    order_id: int
    t_withdrawn_time: Optional[float] = None
    t_collection_box_start: Optional[float] = None
    t_collection_box_end: Optional[float] = None
    t_batch_collector_start: Optional[float] = None
    t_batch_collector_end: Optional[float] = None
    t_chute_start: Optional[float] = None
    t_chute_end: Optional[float] = None
    t_production_start: Optional[float] = None
    t_production_end: Optional[float] = None
    t_reposition_supermarket: Optional[float] = None


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
    history      : list of CardHistoryEntry, one per order-cycle this
                   card has fulfilled — the per-order counterpart to
                   `transitions` above. `transitions` answers "what state
                   was this card in and when, across its whole life";
                   `history` answers "which customer order was this card
                   working on, and how long did each stage of THAT cycle
                   take". Opened via start_order_history() at withdrawal
                   time, then updated stage-by-stage in place by whichever
                   module owns that stage (see CardHistoryEntry's
                   docstring for exactly which module writes which field).
    """
    card_id: int
    product_type: str
    line_id: int
    batch_size: int
    priority: str = "M"
    state: str = "in_supermarket"
    transitions: list[tuple[str, float]] = field(default_factory=list)
    history: list[CardHistoryEntry] = field(default_factory=list)

    def record_transition(self, new_state: str, t: float) -> None:
        """Advance the card's state machine and timestamp the move."""
        self.state = new_state
        self.transitions.append((new_state, round(t, 3)))

    def start_order_history(self, order_id: int, t: float) -> CardHistoryEntry:
        """
        Open a new CardHistoryEntry for `order_id`, stamping
        t_withdrawn_time = t, append it to `history`, and return it.
        Called once per withdrawal (sim.fill.pull.withdrawal
        ._withdraw_one_card) — one call per order-cycle, never re-opened
        mid-cycle.
        """
        entry = CardHistoryEntry(order_id=order_id, t_withdrawn_time=round(t, 3))
        self.history.append(entry)
        return entry

    @property
    def current_history(self) -> Optional[CardHistoryEntry]:
        """
        The most recently opened CardHistoryEntry (in-progress, or just
        completed and not yet followed by a new withdrawal) — None if
        this card has never been withdrawn for an order yet. Callers at
        each stage of the loop (collection box, batch collector, chute,
        production, reposition) fetch this and set their own field on it
        directly, e.g. `card.current_history.t_chute_start = t`.
        """
        return self.history[-1] if self.history else None

    def __repr__(self) -> str:
        return (
            f"KanbanCard(id={self.card_id}, product={self.product_type}, "
            f"line={self.line_id}, state={self.state}, priority={self.priority})"
        )
