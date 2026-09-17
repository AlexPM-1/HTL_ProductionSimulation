"""
sim/resources/chute.py
=======================
KanbanChuteResource — the per-line, priority-ordered (H>M>L) production
ADMISSION queue shared by both pull (Kanban) and push work. NOT the same
thing as ChuteResource in sim/resources/inventory.py (the raw-material
FIFO chute immediately upstream of a station) despite the shared "Chute"
name — this one decides WHAT starts next; that one just tracks material
fill level.

Populated by sim.fill.pull.collection_box (push_batch) and
sim.fill.push.dispatch (push_chunk/push_rush_entry); drained by
sim.drain.crew.crew_process() via peek_next_of_class()/pop_next_of_class().
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import ClassVar, Optional

from sim.resources.cards import KanbanCard


@dataclass
class ChuteEntry:
    """
    One unit of work waiting for admission to a line's stations — either
    a released Kanban batch (class-1/pull, released by
    sim.fill.pull.collection_box.collection_box_emptying_process()) or a
    push chunk (class-2, from sim.fill.push.dispatch's push dispatcher). Both
    classes now share ONE queue per line (see KanbanChuteResource)
    because the frozen zone (domain.policy.PushPolicyConfig.
    frozen_zone_cards) freezes the front of that ONE combined queue — a
    rushed push chunk needs to see, and insert itself right before, the
    exact same boundary the pull side's releases are subject to.

    Attributes
    ----------
    sim_class    : "pull" | "push"
    product_type : sachnummer
    n_cards      : how many 200pcs/30min "card units" this entry is
                   worth — the shared sizing unit the frozen zone counts
                   in. For a pull entry this is len(cards) (a released
                   batch can bundle several cards at once, per
                   CardsToTrigger). For a push entry this is always 1 by
                   construction — push chunks are pre-sliced to
                   <= PUSH_CHUNK_SIZE, i.e. exactly one card.
    cards        : the KanbanCard objects (pull entries only; None for push)
    payload      : opaque handle for push entries — sim.fill.push's own
                   chunk/order object, round-tripped without this module
                   needing to import domain.orders.OrderRecord (keeps
                   sim/resources free of any push-specific type
                   dependency, same separation sim.produce already
                   keeps). None for pull entries.
    """
    sim_class: str
    product_type: str
    n_cards: int
    cards: Optional[list[KanbanCard]] = None
    payload: Optional[object] = None


@dataclass
class KanbanChuteResource:
    """
    Per-line priority-ordered admission queue — shared by BOTH class-1
    (Kanban, via push_batch()) and class-2 (push, via push_chunk() /
    push_rush_entry()) production. Populated on the pull side by
    sim.fill.pull.collection_box.collection_box_emptying_process() once a
    product's BatchCollectorResource bucket reaches its CardsToTrigger
    threshold, and on the push side by sim.fill.push.dispatch's rolling
    push dispatcher; drained by sim.drain.crew.crew_process() (via
    sim.drain.pull_turn/push_turn), which pops the highest-priority
    pending entry (respecting the frozen zone — see below) and drives it
    through sim.produce's part_lifecycle/run_kanban_batch/run_order chain.

    NOT the same thing as ChuteResource (the raw-material FIFO chute
    immediately upstream of Beladen, sim/resources/inventory.py) —
    unchanged, reused as-is once production of a popped entry actually
    starts. This class only decides WHAT starts next.

    --- Frozen zone + shared push/pull queue -------------------
    Ordering is priority H before M before L, FIFO within a tier, with
    the MOVABLE part of the queue re-sorted on every insertion. The front
    `frozen_zone_cards` worth of entries (summed n_cards, from the
    current front) are FROZEN:

      - pop_next() still drains them first, in their existing order —
        freezing doesn't delay them, it protects them.
      - No insertion (push_batch / push_chunk / push_rush_entry) may
        land at or before the frozen boundary, REGARDLESS of priority —
        a same-tick "H" pull release can no longer resort itself ahead
        of an already-frozen entry the way the old single-list-sort
        design would have allowed. This is enforced structurally: every
        insertion recomputes the frozen boundary, splits `_entries` into
        an untouched frozen prefix + a movable tail, inserts/sorts only
        within the tail, then recombines — the frozen prefix's slice is
        never part of any sort() call.
      - `frozen_zone_cards` is a plain mutable field (see
        set_frozen_zone_cards()) — deliberately NOT sourced from
        domain.policy.PushPolicyConfig directly (this module doesn't
        import the runner layer, to avoid a dependency cycle); instead
        sim.runner.apply_push_policy() (and run_mixed() at startup) calls
        set_frozen_zone_cards() on each line's chute. Defaults to 0 (no
        freezing) until set.

    push_rush_entry() implements the "12h-before-due-date: force-assign
    to least-busy line, stick right before the frozen zone" rule —
    inserting AT the frozen boundary (not through the normal rank/seq
    sort), so it becomes the very next thing this line runs once
    whatever's currently frozen finishes, without disturbing the frozen
    prefix or needing to out-rank it. See that method's docstring for
    the ordering behaviour when multiple rush entries land close together.

    pop_next()/pop_next_of_class() return a ChuteEntry, not a
    (product_type, cards) tuple — a push entry has no `cards` list, so a
    tuple shape can't represent it. Unpack as
    `entry.product_type, entry.cards` instead.

    Naming: kept as "KanbanChuteResource" even though push shares this
    queue too now, to avoid touching every existing construction site /
    class-1 reference for a rename.
    """
    line_id: int
    frozen_zone_cards: int = 0
    _entries: list[tuple[int, int, ChuteEntry]] = field(default_factory=list)
    _sequence: int = field(default=0, init=False, repr=False)

    _PRIORITY_RANK: ClassVar[dict[str, int]] = {"H": 0, "M": 1, "L": 2}

    # ------------------------------------------------------------------
    # Frozen-zone configuration & accounting
    # ------------------------------------------------------------------

    def set_frozen_zone_cards(self, n_cards: int) -> None:
        """
        Update the frozen-zone size in place. Safe to call at any time,
        including mid-simulation (e.g. a frontend edit) — the very next
        insertion/pop call picks up the new value; nothing is
        retroactively frozen or unfrozen for entries already popped.
        """
        if n_cards < 0:
            raise ValueError("frozen_zone_cards must be >= 0")
        self.frozen_zone_cards = n_cards

    def first_unfrozen_index(self) -> int:
        """
        Index of the first entry NOT in the frozen zone, walking from the
        front of the current pop order — i.e. the smallest k such that
        the first k entries' n_cards sum to >= frozen_zone_cards. Equals
        len(_entries) if the whole queue is smaller than the frozen zone
        (everything currently queued is frozen), or 0 if
        frozen_zone_cards is 0 (nothing is frozen).
        """
        remaining = self.frozen_zone_cards
        for i, (_, _, entry) in enumerate(self._entries):
            if remaining <= 0:
                return i
            remaining -= entry.n_cards
        return len(self._entries)

    @property
    def total_pending_cards(self) -> int:
        """Sum of n_cards across every pending entry — a cheap 'how busy
        is this line's queue right now' figure for the push dispatcher's
        least-busy-line comparison (see sim.fill.push)."""
        return sum(entry.n_cards for _, _, entry in self._entries)

    # ------------------------------------------------------------------
    # Enqueue
    # ------------------------------------------------------------------

    def push_batch(self, product_type: str, cards: list[KanbanCard]) -> None:
        """
        Enqueue one already-released Kanban batch (class-1/pull). Called
        by sim.fill.pull.collection_box.collection_box_emptying_process().
        `cards` is everything the Batch-Size Collector released together
        (its `CardsToTrigger` worth, e.g. 4 physical cards) — gathering
        that many cards' worth of stock into one release EVENT is
        correct Kanban behaviour; admitting them onto the shared line as
        one atomic, indivisible production job is not.

        Each card gets its OWN ChuteEntry (n_cards=1), so each waits its
        own turn on the chute: only the front one can ever be popped
        (pop_next / pop_next_of_class are front-only — see those
        methods), production runs it to completion, and only then does
        the next card's entry become poppable. This is also what makes
        the frozen zone's card-unit counting exact rather than only
        precise to the size of whatever bundle happened to release
        together.

        Priority is taken from the first card — the whole release
        happened together at the same priority context, so every card
        in it gets that priority — and each card keeps its own
        insertion-order tie-break via `_insert_ranked`'s `_sequence`
        counter, so splitting doesn't reorder them relative to each
        other or to anything already queued.
        """
        if not cards:
            return
        priority = cards[0].priority
        for card in cards:
            entry = ChuteEntry(
                sim_class="pull", product_type=product_type,
                n_cards=1, cards=[card],
            )
            self._insert_ranked(entry, priority=priority)

    def push_chunk(self, product_type: str, priority: str, payload: object) -> None:
        """
        Enqueue one push chunk (class-2), NOT within
        PushPolicyConfig.rush_threshold_h of its due date. Called by
        sim.fill.push.dispatch's push dispatcher, as
        `chute.push_chunk(sachnummer, priority="M", payload=chunk)`.
        `priority` is accepted for call-site/logging compatibility but
        does not drive placement.

        Placement goes through _insert_at_boundary() — the same helper
        push_rush_entry() uses — rather than the rank-based
        _insert_ranked(): a push chunk must be placed "before all cards
        on the chute", i.e. at the front of the movable (non-frozen)
        segment, ahead of every other pending entry, pull or push,
        regardless of rank. What still distinguishes a rush chunk from a
        normal one is upstream, in sim.fill.push.dispatch (which line
        gets picked, and whether the busy/retry search is skipped) — not
        chute placement.
        """
        entry = ChuteEntry(
            sim_class="push", product_type=product_type,
            n_cards=1, payload=payload,
        )
        self._insert_at_boundary(entry)

    def push_rush_entry(self, product_type: str, payload: object) -> None:
        """
        Force-insert a rushed push chunk (class-2, within
        PushPolicyConfig.rush_threshold_h of its due date) immediately
        after the frozen zone — ahead of every normally-ranked entry
        currently waiting, bypassing H/M/L ranking entirely. Called by
        sim.fill.push.dispatch. This implements the placement half of
        "12h before due date: assign to the least-busy compatible line,
        and stick them right before the frozen zone" — which LINE to
        target is sim.fill.push.dispatch's job (its own least-busy
        comparison, using total_pending_cards across candidate lines'
        chutes); this method only handles placement WITHIN the one
        already-chosen line's queue.

        Ordering among multiple rush entries: each call recomputes the
        frozen boundary fresh and inserts exactly there, so a second
        rush call arriving before the first has been absorbed into the
        (dynamically growing) frozen span lands AHEAD of it — i.e. LIFO
        among rush entries, but still strictly FIFO/immovable against
        the frozen prefix itself. This is a deliberate choice ("the
        newest emergency gets the front-most open slot") rather than an
        oversight — rush placement should be rare enough (only orders
        inside rush_threshold_h) that a stricter "oldest rush entry
        first" tie-break isn't worth extra bookkeeping unless it turns
        out to matter in practice.

        Thin wrapper over _insert_at_boundary() — the same helper
        push_chunk() uses (see that method's docstring for why the two
        share one placement path).
        """
        entry = ChuteEntry(
            sim_class="push", product_type=product_type,
            n_cards=1, payload=payload,
        )
        self._insert_at_boundary(entry)

    def _insert_at_boundary(self, entry: ChuteEntry) -> None:
        """
        Shared insertion path for push_chunk()/push_rush_entry(): inserts
        `entry` exactly at first_unfrozen_index() — the front of the
        movable segment — bypassing rank/seq sorting entirely. Every
        push chunk, rush or not, preempts to the front of whatever's
        still movable; if the whole queue is currently smaller than the
        frozen zone, first_unfrozen_index() returns len(_entries), so
        this becomes a same-position append (no jump possible) —
        matching "if there are less than 8 cards, priority is useless."

        Deliberately NOT going through _insert_ranked(): that method's
        rank-based sort is still correct for pull's push_batch()
        (Kanban H/M/L tiers should keep sorting against each other), but
        push entries always preempt regardless of rank, so this method
        never risks touching the frozen prefix's slice — the one
        invariant that must never break.
        """
        self._sequence += 1
        boundary = self.first_unfrozen_index()
        self._entries.insert(boundary, (self._PRIORITY_RANK["H"], self._sequence, entry))

    def _insert_ranked(self, entry: ChuteEntry, priority: str) -> None:
        """
        Shared insertion path for push_batch()/push_chunk(): re-sorts
        ONLY the movable tail of the queue (by rank, then insertion
        order) and leaves the frozen prefix's slice completely untouched
        — see the class docstring's "frozen zone" note for why this is
        the one thing that changed from the original single-list-sort
        behaviour.
        """
        rank = self._PRIORITY_RANK.get(priority, 1)
        self._sequence += 1
        boundary = self.first_unfrozen_index()
        frozen_part = self._entries[:boundary]
        movable_part = self._entries[boundary:]
        movable_part.append((rank, self._sequence, entry))
        movable_part.sort(key=lambda e: (e[0], e[1]))
        self._entries = frozen_part + movable_part

    # ------------------------------------------------------------------
    # Drain
    # ------------------------------------------------------------------

    @property
    def n_pending_batches(self) -> int:
        return len(self._entries)

    def pop_next(self) -> Optional[ChuteEntry]:
        """
        Pop the highest-priority (frozen-first, then ranked) pending
        entry regardless of class, or None if empty. Since frozen
        entries always occupy the front of `_entries` (see
        _insert_ranked / push_rush_entry, both of which refuse to insert
        before the frozen boundary), this naturally drains the frozen
        span first, in the order it froze — the "committed, about to
        run, guaranteed not preempted" property the frozen zone exists
        to provide.

        Not used by the crew-based drain path (sim.drain.crew, which
        needs class-aware access — see pop_next_of_class()/
        peek_next_of_class() below); kept for sim.resources.build's
        module self-test and any other class-agnostic caller.

        Returns a ChuteEntry, not a (product_type, cards) tuple — a push
        entry has no `cards` list, so a tuple shape can't represent it.
        Unpack as `entry.product_type, entry.cards`.
        """
        if not self._entries:
            return None
        _, _, entry = self._entries.pop(0)
        return entry

    def pop_next_of_class(self, sim_class: str) -> Optional[ChuteEntry]:
        """
        Pop the front entry ONLY if it belongs to `sim_class`; otherwise
        return None without touching the queue. Called by
        sim.drain.pull_turn/push_turn once a crew has secured the line's
        gate.

        Only self._entries[0] is ever eligible — never scans past it to
        find a same-class match further back — because this is ONE
        mechanical admission slot for a shared line: only one entry may
        ever be "in flight" (popped, and therefore in production) at a
        time, and it has to be whichever entry is currently at the
        front, not whichever entry happens to match the calling
        process's class. This is also what the frozen zone requires:
        nothing behind a frozen entry may leave the chute before that
        frozen entry does, regardless of class.

        If the front belongs to the other class, this is a genuine
        "nothing for me right now, wait your turn" — the caller
        re-blocks on ctx.activity_signal exactly as it already does for
        "queue empty" (see sim.drain.crew.crew_process), and is woken
        again once the front changes, i.e. whenever EITHER class
        successfully pops or a new entry is inserted (see
        sim.runner._install_crew_chute_hooks and
        sim.context.RunContext.notify()).
        """
        if not self._entries:
            return None
        _, _, front_entry = self._entries[0]
        if front_entry.sim_class != sim_class:
            return None
        self._entries.pop(0)
        return front_entry

    def peek_next_of_class(self, sim_class: str) -> Optional[ChuteEntry]:
        """
        Read-only counterpart to pop_next_of_class(): returns the front
        entry if (and only if) it belongs to `sim_class`, WITHOUT removing
        it from the queue. Returns None if the queue is empty or the front
        entry belongs to the other class. Called by
        sim.drain.crew.crew_process() to decide whether to run a push or
        pull turn before committing to one.

        The chute's front-of-queue / frozen-zone protection only protects
        an entry while it is still IN the queue — popping it before the
        crew has actually secured the LinePriorityGate would hand that
        protection away one step too early. peek_next_of_class() lets a
        caller check "is there an entry ready for me" without giving up
        the chute's ordering guarantee; only pop_next_of_class() is
        called once actually about to execute.
        """
        if not self._entries:
            return None
        _, _, front_entry = self._entries[0]
        if front_entry.sim_class != sim_class:
            return None
        return front_entry

    def __repr__(self) -> str:
        return (
            f"KanbanChuteResource(line={self.line_id}, "
            f"pending_batches={self.n_pending_batches}, "
            f"frozen_zone_cards={self.frozen_zone_cards})"
        )
