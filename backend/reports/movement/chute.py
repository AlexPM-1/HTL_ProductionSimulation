"""
reports/movement/chute.py
============================
Kanban Chute box logic for the Movement Simulation page:
_pull_chute_events / _push_chute_events / _replay_chute_queue build the
merged pull+push queue for one line at one instant; build_chute_box()
assembles that into the "chute" key of a movement-state frame. Called by
reports/movement/frames.py's build_movement_state_payload().

push_chute_entries_at() at the bottom of this file is a push-only queue
replay kept alongside _replay_chute_queue's merged pull+push replay — see
its own docstring below for why the two aren't collapsed into one.

PushChuteLogEntry lives in telemetry/records.py;
_CHUTE_CAPACITY_SLACK's canonical home is reports/movement/layout.py.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Optional

from reports.movement.layout import _CHUTE_CAPACITY_SLACK

if TYPE_CHECKING:
    from telemetry.records import PushChuteLogEntry


# ---------------------------------------------------------------------------
# Event extraction — pull (from card transitions) and push (from
# PushChuteLogEntry), reshaped into one common dict shape so
# _replay_chute_queue() can treat both sides identically.
# ---------------------------------------------------------------------------

def _pull_chute_events(kenv, lid: int) -> list[dict]:
    """
    True deposit/drain event history for pull cards passing through a
    line's Chute, derived straight from each KanbanCard's own
    .transitions log (kenv.card_registry) — there's no PushChuteLogEntry
    -style structure on the pull side, but transitions already record
    every state change with its raw sim-clock timestamp, so a
    "released_to_chute" entry IS the deposit event and the very next
    transition after it (whatever state it moves to) IS the drain
    event. A card that re-enters the Chute more than once (e.g. a
    rework loop) yields one deposit/drain pair per visit. A card still
    sitting in "released_to_chute" as of its LAST recorded transition
    (i.e. nothing after it yet) yields a deposit with no matching drain
    — correctly left "open" so it stays in the replayed queue below
    until a drain event for it actually appears in the log.

    Shape matches PushChuteLogEntry closely enough that
    _replay_chute_queue() can merge both sides' events and replay them
    against ONE persistent list, the same way push_chute_entries_at()
    already does for push-only.
    """
    events: list[dict] = []
    for card in getattr(kenv, "card_registry", {}).values():
        if getattr(card, "line_id", None) != lid:
            continue
        transitions = list(getattr(card, "transitions", []))
        for idx, (state, t) in enumerate(transitions):
            if state != "released_to_chute":
                continue
            events.append({
                "t": t, "kind": "deposit", "chute_kind": "pull",
                "id": card.card_id, "product_type": getattr(card, "product_type", None),
                "rush": False,  # rush is a push-only concept today
            })
            if idx + 1 < len(transitions):
                events.append({
                    "t": transitions[idx + 1][1], "kind": "drain",
                    "chute_kind": "pull", "id": card.card_id,
                })
    return events


def _push_chute_events(push_chute_log, line_name: str) -> list[dict]:
    """Push-side deposit/drain events for one line, reshaped from
    PushChuteLogEntry into the same dict shape _pull_chute_events()
    produces, so _replay_chute_queue() can treat both sides identically."""
    events: list[dict] = []
    for ev in push_chute_log or []:
        if ev.line != line_name:
            continue
        if ev.kind == "deposit":
            events.append({
                "t": ev.t, "kind": "deposit", "chute_kind": "push",
                "id": ev.entry_id, "product_type": ev.sachnummer, "rush": ev.rush,
            })
        elif ev.kind == "drain":
            events.append({
                "t": ev.t, "kind": "drain", "chute_kind": "push", "id": ev.entry_id,
            })
    return events


def _replay_chute_queue(events: list[dict], edge_s: float, frozen_zone_cards: int) -> list[dict]:
    """
    THE single physical Chute queue for a line, as of edge_s, built by
    replaying every deposit/drain event (pull AND push, merged) in true
    chronological order against one persistent list — index 0 = closest
    to production. This replaces computing the frozen prefix and the
    movable tail as two independent things (previously: frozen = oldest
    entries by raw arrival-time rank; tail = a separately-built
    rush-priority merge). That split was the actual bug: an older
    non-rush entry that a rush entry had already jumped ahead of (in the
    tail's own ordering) could still get selected directly into the
    frozen prefix by the independent time-rank check — since that check
    never looked at the tail's ordering at all — which visually showed
    up as the rush entry pinned at the frozen-zone line while entries it
    had already passed slipped underneath it into the frozen zone. Here
    there is only ONE ordering; an entry can reach "frozen" (queue[:n])
    only by genuinely being at the front of the exact same list the tail
    is read from, so nothing can leapfrog past a rush entry's already-
    earned position.

    Deposit rule (mirrors PushChuteTracker.deposit()'s live insertion
    rule, extended with the frozen-zone lock the tracker itself doesn't
    know about): a non-rush deposit appends to the back; a rush deposit
    inserts right after any rush entries already in the queue, ahead of
    every non-rush entry — capped so it can never land at an index below
    frozen_zone_cards, i.e. it can never displace an entry already
    inside the locked frozen prefix. Replaying real drain events (not
    just re-deriving from "who's currently present, sorted by arrival
    time") is what lets a rush entry's earned position survive frame to
    frame instead of being recomputed differently as unrelated older
    entries happen to drain out.

    A drain event removes its entry by (chute_kind, id) wherever it
    currently sits — normally at or near the front, but not asserted,
    consistent with this module's "flag, don't block" stance elsewhere.
    """
    ordered = sorted(
        (e for e in events if e["t"] <= edge_s),
        key=lambda e: (e["t"], e["kind"] != "deposit"),  # deposits before drains on an exact tie
    )
    queue: list[dict] = []
    for e in ordered:
        if e["kind"] == "deposit":
            item = {
                "kind": e["chute_kind"], "id": e["id"],
                "product_type": e.get("product_type"), "rush": e.get("rush", False),
            }
            if item["rush"]:
                # Only count rush entries still sitting in the TAIL
                # (index >= frozen_zone_cards) — not every rush entry
                # ever deposited. Once an earlier rush entry advances
                # into the frozen zone it's locked there; it's no longer
                # part of the movable rush cluster a later rush deposit
                # should queue behind. Counting it anyway (the previous
                # bug) overstates how far into the queue the new rush
                # entry belongs — enough to push it past frozen_zone_cards
                # + len(tail), i.e. clamped by min() to the very back of
                # the queue, landing it BEHIND non-rush tail entries it
                # should have jumped ahead of.
                tail_rush_count = sum(1 for q in queue[frozen_zone_cards:] if q["rush"])
                insert_idx = min(frozen_zone_cards + tail_rush_count, len(queue))
                queue.insert(insert_idx, item)
            else:
                queue.append(item)
        else:  # drain
            key = (e["chute_kind"], e["id"])
            queue = [q for q in queue if (q["kind"], q["id"]) != key]
    return queue


# ---------------------------------------------------------------------------
# Frame assembly — the "chute" key of one line's movement-state entry.
# ---------------------------------------------------------------------------

def build_chute_box(kenv, lid: int, line_name: str, t_s: float, include_push: bool) -> dict:
    """
    Pull cards + push chunks, merged into one FIFO (see docstring: both
    classes share ONE physical KanbanChuteResource queue, so drawing
    them separately — pull cards as real tokens, push chunks as an
    anonymous "+N" badge — both under-counted the badge (a live
    snapshot, same value every animation frame) and hid which product
    each push entry actually was). `entries` merges both sides,
    oldest-first by the time each actually entered the chute, each
    carrying its own product_type so the frontend can color them the
    same way Supermarket/Batch Collector rows already are.

    Pull events always feed in (the Kanban-only tabs still need a plain
    FIFO chute); push events only when include_push is set.

    Returns {"capacity", "frozen_zone_cards", "entries", "overflow_entries",
    "card_ids", "overflow_card_ids", "frozen_card_ids"} — the last three
    are legacy pull-only fields, kept for back-compat, derived from the
    SAME unified queue "entries" above (just filtered to kind=="pull"),
    not recomputed separately, so they can't drift out of sync with it.
    """
    chute = kenv.kanban_chutes.get(lid) if getattr(kenv, "kanban_chutes", None) else None
    frozen_zone_cards = getattr(chute, "frozen_zone_cards", 0) if chute is not None else 0
    chute_capacity = frozen_zone_cards + _CHUTE_CAPACITY_SLACK  # matches plant_structure's chute.capacity

    chute_events = _pull_chute_events(kenv, lid)
    if include_push:
        chute_events += _push_chute_events(getattr(kenv, "push_chute_log", None), line_name)

    queue = _replay_chute_queue(chute_events, t_s, frozen_zone_cards)
    entries_raw = [
        {
            "kind": item["kind"], "id": item["id"], "product_type": item["product_type"],
            "rush": item["rush"], "frozen": i < frozen_zone_cards,
        }
        for i, item in enumerate(queue)
    ]

    chute_entries = entries_raw[:chute_capacity]
    overflow_entries = entries_raw[chute_capacity:] or None

    return {
        "capacity": chute_capacity,
        "frozen_zone_cards": frozen_zone_cards,
        # Merged pull+push queue, oldest-first, each entry carrying its
        # own product_type — this is what movement_layout.js should
        # render now.
        "entries": chute_entries,
        "overflow_entries": overflow_entries,
        # Legacy pull-only fields, kept for back-compat with any other
        # consumer still reading them directly.
        "card_ids": [e["id"] for e in entries_raw if e["kind"] == "pull"][:chute_capacity],
        "overflow_card_ids": (
            [e["id"] for e in entries_raw if e["kind"] == "pull"][chute_capacity:] or None
        ),
        "frozen_card_ids": [e["id"] for e in entries_raw if e["kind"] == "pull" and e["frozen"]],
    }


# ---------------------------------------------------------------------------
# push_chute_entries_at — push-only Chute queue replay.
# ---------------------------------------------------------------------------
#
# Kept separate from _replay_chute_queue() rather than collapsed into a
# filter over it: _replay_chute_queue()'s output items are {"kind", "id",
# "product_type", "rush"} — no "t_entered". This function's contract is
# {"entry_id", "sachnummer", "t_entered", "rush"}; deriving "t_entered"
# from _replay_chute_queue would need that function to expose more state
# than it currently does. Since no caller in this codebase currently
# uses push_chute_entries_at (the movement-state frame gets its chute
# data from build_chute_box() instead), it's kept as-is for now — check
# for callers before wiring it into a new endpoint, and consider removing
# it if none appear.

def push_chute_entries_at(
    log: list["PushChuteLogEntry"], line_name: str, edge_s: float
) -> list[dict]:
    """
    Replay `log` up to and including edge_s to reconstruct which push
    chunks were sitting in `line_name`'s Chute queue at that instant,
    oldest-first (index 0 = closest to production / next to drain).

    Returns [{"entry_id", "sachnummer", "t_entered", "rush"}, ...].
    """
    pending: list[dict] = []
    for ev in log:
        if ev.line != line_name or ev.t > edge_s:
            continue
        if ev.kind == "deposit":
            item = {"entry_id": ev.entry_id, "sachnummer": ev.sachnummer, "t_entered": ev.t, "rush": ev.rush}
            if ev.rush:
                idx = sum(1 for p in pending if p["rush"])
                pending.insert(idx, item)
            else:
                pending.append(item)
        elif ev.kind == "drain":
            pending = [p for p in pending if p["entry_id"] != ev.entry_id]
    return pending
