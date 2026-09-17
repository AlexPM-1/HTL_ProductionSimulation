"""
sim/fill/push/chute_tracker.py
=================================
PushChuteTracker — fed by sim.fill.push.dispatch.push_dispatch_process()
and sim.drain.push_turn.run_push_turn() (via crew_process). Used by
sim/runner.py.
"""

from __future__ import annotations

from typing import Optional

from telemetry.records import PushChuteLogEntry


class PushChuteTracker:
    """
    Best-effort mirror of what's sitting in each line's shared Chute on
    the PUSH side only — the push-side analogue of
    ExoticSupermarketTracker, with the SAME caveat: this is a
    self-contained bookkeeping structure fed entirely from
    push_dispatch_process()'s chute.push_chunk()/chute.push_rush_entry()
    calls and run_push_turn()'s (sim/drain/push_turn.py, via crew_process)
    chute.pop_next_of_class("push") calls — the ONLY push-side
    admission/removal call sites — not wired into KanbanChuteResource's
    own internal queue. It approximates real ordering with the same rule
    push_dispatch_process itself follows: normal deposits append to the
    back, rush deposits jump ahead of every non-rush entry but queue up
    FIFO relative to any rush entries already pending (mirroring
    chute.push_rush_entry() semantics). It does NOT know about
    frozen-zone-driven reordering interactions with the PULL side —
    that logic lives entirely inside KanbanChuteResource — so this is
    an approximation of physical queue position, same spirit as
    ExoticSupermarketTracker's own "not the real resource" caveat.

    Deposits/drains are pushed onto `.log` (a PushChuteLogEntry list) as
    they happen, so reports.movement.chute.push_chute_entries_at() can
    replay "what was pending as of time t" for any past t — the
    historical counterpart to `.pending`/`.snapshot()`, which only ever
    reflect the current simulated instant.
    """

    def __init__(self):
        self._next_id = 1
        self.pending: dict[str, list[dict]] = {}   # line -> [{"entry_id","sachnummer","t_entered","rush"}], oldest-first
        self.log: list[PushChuteLogEntry] = []

    def deposit(self, line_name: str, sachnummer: str, t: float, rush: bool = False) -> int:
        """Record one push chunk entering `line_name`'s Chute queue.
        Returns the entry_id assigned (unique across the whole run, not
        just this line) — not currently needed by callers, but handy for
        tests/debugging."""
        entry_id = self._next_id
        self._next_id += 1
        item = {"entry_id": entry_id, "sachnummer": sachnummer, "t_entered": t, "rush": rush}
        lst = self.pending.setdefault(line_name, [])
        if rush:
            # Insert after any rush entries already pending, ahead of every
            # non-rush one — NOT always index 0. Index 0 would let each new
            # rush deposit cut in front of an earlier rush deposit that's
            # still waiting, undoing its jump instead of queuing behind it
            # (see push_chute_entries_at's docstring for the matching replay
            # rule — both must agree, or scrubbing back to an earlier t_s
            # would show a different order than what actually happened live).
            idx = sum(1 for p in lst if p["rush"])
            lst.insert(idx, item)
        else:
            lst.append(item)
        self.log.append(PushChuteLogEntry(
            t=t, line=line_name, kind="deposit", entry_id=entry_id,
            sachnummer=sachnummer, rush=rush,
        ))
        return entry_id

    def drain_one(self, line_name: str, t: float) -> Optional[dict]:
        """Pop and return whichever entry is at the front of this line's
        tracked pending list (the one run_push_turn (via crew_process)'s
        pop_next_of_class("push") call is assumed to have just drained —
        see class docstring), logging the removal. Returns None if this
        tracker's view is already empty for this line (shouldn't happen
        in normal operation — pop_next_of_class("push") only returns
        non-None when a push entry actually exists to drain — but
        guarded rather than raising, consistent with this module's
        "flag, don't block" stance elsewhere)."""
        lst = self.pending.get(line_name) or []
        if not lst:
            return None
        item = lst.pop(0)
        self.log.append(PushChuteLogEntry(
            t=t, line=line_name, kind="drain", entry_id=item["entry_id"],
        ))
        return item

    def snapshot(self, line_name: str) -> list[dict]:
        """Current (live, right-now) pending push entries for a line,
        oldest-first — i.e. index 0 is the next one run_push_turn (via
        crew_process) will drain. For a specific PAST instant, use
        reports.movement.chute.push_chute_entries_at(tracker.log, ...)
        instead; this only reflects the simulation's current moment."""
        return list(self.pending.get(line_name, []))


# NOTE: push_chute_entries_at() is a read-side replay, not sim engine —
# it lives in reports/movement/chute.py (one of two replays of the same
# queue, alongside _replay_chute_queue; that module keeps
# _replay_chute_queue as the canonical one and makes
# push_chute_entries_at a thin filter over it). Not imported here:
# nothing in this module's own sim engine calls it.
