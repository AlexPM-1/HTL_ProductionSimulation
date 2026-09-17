"""
sim/fill/push/exotic.py
=========================
Exotic-Supermarket deposit/withdraw tracking for the push side.

CAVEAT (please read before relying on this for cross-consumption): this
is a SELF-CONTAINED tracker, built from cfg.supermarkets'
SupermarketSlotConfig rows — it is NOT wired into sim.resources'
real Supermarket resources (the ones
sim.fill.pull.assignment.select_supermarket_for_withdrawal /
kenv.supermarket_for() actually read for PULL withdrawal). Pull-side
stock genuinely affects kanban's withdrawal logic; exotic stock tracked
here does NOT yet feed back into that — it only tracks exotic occupancy
for THIS module's own overflow-flagging and reporting purposes. If
sim.resources already exposes real exotic-slot resources with a similar
shape, ExoticSupermarketTracker.deposit_chunk()'s body is the only
thing that needs to change to call into those instead — the call site
(run_push_turn, sim/drain/push_turn.py, via crew_process) doesn't need
to change.

Deposit/withdraw symmetry: deposit_chunk() is called the moment a push
chunk finishes production (run_push_turn, step (e)); withdraw_chunk()
is called once that same chunk's due_date is actually reached
(_withdraw_push_chunk_process, spawned right after step (e)) — modeling
"produced early, sits in the exotic supermarket, customer takes it
at/near the due date" rather than growing occupancy without bound.
"""

from __future__ import annotations

import datetime as _dt
from dataclasses import dataclass, field
from typing import Optional

import simpy

from domain.config import SimConfig
from domain.orders import OrderRecord
from sim.context import RunContext
from telemetry.records import ExoticSlotSnapshot, SupermarketOverflowFlag


@dataclass
class ExoticSlotState:
    """
    One physical "Exotic" Supermarket row's current occupancy.

    A row that hasn't fully emptied before a different product starts
    filling it legitimately holds more than one product's cards at
    once — deposit_chunk()'s over-capacity fallback (pile onto the
    least-full slot) used to only ever grow a single `occupant`/`n_cards`
    pair, silently folding a second product's cards into whichever
    product got there first (and never updating `occupant` again once
    set, since it only overwrote a None occupant). `occupants` replaces
    that with a real per-product breakdown: sachnummer -> n_cards, for
    every product simultaneously sitting in the row.
    """
    line: str
    row_number: int
    capacity_cards: int
    occupants: dict[str, int] = field(default_factory=dict)

    @property
    def total_cards(self) -> int:
        """Cards of ALL products combined currently sitting in this row —
        what `capacity_cards` is actually checked against."""
        return sum(self.occupants.values())

    @property
    def occupant(self) -> Optional[str]:
        """Back-compat single-value view: the (deterministically) first
        product occupying the row, or None if empty. Prefer `occupants`
        for the full multi-product picture — this exists only for old
        call sites/consumers that haven't been updated yet."""
        if not self.occupants:
            return None
        return min(self.occupants)

    @property
    def n_cards(self) -> int:
        """Back-compat alias for total_cards."""
        return self.total_cards


class ExoticSupermarketTracker:
    """
    Per-line pool of "Exotic" Supermarket slots (SupermarketSlotConfig
    rows where is_exotic is True), tracking push-chunk deposits/
    withdrawals for overflow-flagging and reporting purposes. See this
    module's caveat above re: not yet wired into the real per-product
    pull-side Supermarket resources.

    Two parallel bits of state, kept in sync by deposit_chunk()/
    withdraw_chunk():
      - `slots` (ExoticSlotState, per PHYSICAL row) — capacity/occupancy
        accounting, unchanged in shape from before. This is what
        snapshot()/ExoticSlotSnapshot reports on.
      - `_ready_stores` (one simpy.Store per (line, sachnummer), created
        lazily) — a FIFO queue of "which physical row number is this
        particular deposited-but-not-yet-withdrawn card sitting in".
        deposit_chunk() pushes the row_number it placed a card in;
        withdraw_chunk() pops the OLDEST one for that (line, sachnummer)
        and decrements that same row — so chunks of the same product are
        withdrawn in deposit order (FIFO), and a withdrawal for a product
        that hasn't been produced yet naturally BLOCKS (via `yield
        store.get()`) until a matching deposit arrives, instead of
        failing — mirroring the pull-side SupermarketResource.store's own
        "withdrawal blocks until production deposits" resolution of
        stockout behavior (see that class's docstring).

    Sizing simplification: every push chunk is treated as exactly ONE
    card occupying one unit of a slot's capacity_cards — matching how
    the deposit rule is phrased ("200pcs cards/chunks will be
    deposited"). A final chunk smaller than PUSH_CHUNK_SIZE (when an
    order's quantity isn't an exact multiple of it) is still counted as
    one full card for capacity-accounting purposes. This is a
    deliberate, documented simplification — piece-level partial-card
    tracking (mirroring SupermarketSlotConfig.initial_pcs_partial) can
    be added later if this proves inaccurate enough to matter; it isn't
    needed for the overflow-flag / reporting purpose this tracker serves
    today.

    snapshot() below is intentionally NOT called from inside
    deposit_chunk()/withdraw_chunk() themselves — this class stays a
    plain state holder, and the CALLER
    (_deposit_push_chunk_to_supermarket / _withdraw_push_chunk_process)
    is responsible for appending a timestamped ExoticSlotSnapshot into an
    externally-owned log, the same "caller owns the log, this class only
    mutates state" split sim.context.RunContext.record_supermarket()
    uses for SupermarketSnapshot.
    """

    def __init__(self, cfg: SimConfig):
        self.slots: dict[str, list[ExoticSlotState]] = {}
        for line_name, slot_cfgs in (getattr(cfg, "supermarkets", None) or {}).items():
            self.slots[line_name] = [
                ExoticSlotState(line=line_name, row_number=s.row_number, capacity_cards=s.capacity)
                for s in slot_cfgs if s.is_exotic
            ]
        # Lazily created on first deposit/withdraw of a given (line,
        # sachnummer) pair — see class docstring. Needs a live
        # simpy.Environment, which isn't available at __init__ time (this
        # tracker is built from cfg alone, before/independently of env in
        # some call sites), so deposit_chunk()/withdraw_chunk() take `env`
        # as an explicit argument instead of storing it here.
        self._ready_stores: dict[tuple[str, str], simpy.Store] = {}

    def _store_for(self, line_name: str, sachnummer: str, env: "simpy.Environment") -> simpy.Store:
        key = (line_name, sachnummer)
        store = self._ready_stores.get(key)
        if store is None:
            store = simpy.Store(env)
            self._ready_stores[key] = store
        return store

    def available_count(self, line_name: str, sachnummer: str) -> int:
        """How many cards of `sachnummer` are currently sitting ready for
        withdrawal in `line_name`'s exotic pool. Purely a diagnostics/
        logging helper (e.g. to tell "withdrawing immediately" apart from
        "waiting on production" in _withdraw_push_chunk_process) — not
        used for placement or withdrawal decisions themselves, those go
        through the FIFO store directly."""
        store = self._ready_stores.get((line_name, sachnummer))
        return len(store.items) if store is not None else 0

    def snapshot(self, line_name: str, t: float) -> list[ExoticSlotSnapshot]:
        """One ExoticSlotSnapshot per physical Exotic slot on `line_name`,
        as of `t` — used both for the initial t=0 snapshot (run_mixed())
        and after every deposit/withdrawal."""
        out = []
        for slot in self.slots.get(line_name, []):
            occupants_list = [
                {"sachnummer": sachnr, "n_cards": n}
                for sachnr, n in sorted(slot.occupants.items())
            ]
            out.append(ExoticSlotSnapshot(
                t=t, line=line_name, row_number=slot.row_number,
                capacity_cards=slot.capacity_cards,
                occupant=slot.occupant, n_cards=slot.n_cards,
                occupants=occupants_list,
            ))
        return out

    def deposit_chunk(
        self, line_name: str, sachnummer: str, env: "simpy.Environment",
    ) -> tuple[bool, Optional[str]]:
        """
        Deposit one push chunk (one card) of `sachnummer` into
        `line_name`'s exotic slot pool. ALWAYS succeeds — the deposit
        always happens, per "no problem, raise a flag and continue" — a
        full Supermarket is a planning signal here, not a hard stop.

        Placement preference (mirrors the pull-side "consolidate before
        spreading" instinct): (1) an existing slot already holding this
        sachnummer, with room; (2) an empty slot; (3) if every slot is
        either full or holds only different products with no room, pile
        onto an existing same-product slot anyway (soft over-capacity)
        or, if this product has no slot anywhere on this line, the
        least-full slot overall. Piling onto a slot that already holds a
        DIFFERENT product does NOT evict or relabel that product — the
        slot's `occupants` breakdown tracks both side by side, since a
        real physical row that hasn't fully emptied keeps whatever was
        already in it while a new product starts filling the rest.

        Whichever row_number the card physically lands in (or None, if
        this line has no exotic slots configured at all — see below) is
        pushed onto this (line, sachnummer)'s FIFO ready-store, making it
        available to the next withdraw_chunk() call for that pair — this
        is what a blocked/waiting withdrawal is actually woken up by.

        If this line has NO exotic slots configured at all, there is
        nowhere to physically place the card — but the chunk still very
        much exists and a customer will still come to withdraw it at its
        due_date, so it's still pushed onto the FIFO store (with
        row_number=None) purely so that withdrawal can succeed instead of
        blocking forever; there's simply no physical slot to decrement on
        the withdrawal side in that case.

        Returns (fit_within_capacity, reason). reason is None when it
        fit; a short string describing the overflow when it didn't (the
        caller is expected to log a SupermarketOverflowFlag with it).
        """
        slots = self.slots.get(line_name, [])
        if not slots:
            self._store_for(line_name, sachnummer, env).put(None)
            return False, "no exotic slots configured for this line"

        for slot in slots:
            if slot.occupants.get(sachnummer, 0) > 0 and slot.total_cards < slot.capacity_cards:
                slot.occupants[sachnummer] = slot.occupants.get(sachnummer, 0) + 1
                self._store_for(line_name, sachnummer, env).put(slot.row_number)
                return True, None

        for slot in slots:
            if not slot.occupants and slot.capacity_cards > 0:
                slot.occupants[sachnummer] = 1
                self._store_for(line_name, sachnummer, env).put(slot.row_number)
                return True, None

        same_product = [s for s in slots if sachnummer in s.occupants]
        target = same_product[0] if same_product else min(slots, key=lambda s: s.total_cards)
        target.occupants[sachnummer] = target.occupants.get(sachnummer, 0) + 1
        self._store_for(line_name, sachnummer, env).put(target.row_number)
        other_products = sorted(p for p in target.occupants if p != sachnummer)
        return False, (
            f"exotic supermarket full on {line_name} — piled onto slot "
            f"row {target.row_number} (now {target.total_cards}/{target.capacity_cards} cards"
            + (f", alongside {', '.join(other_products)}" if other_products else "")
            + ")"
        )

    def withdraw_chunk(self, line_name: str, sachnummer: str, env: "simpy.Environment"):
        """
        SimPy generator — the customer-side counterpart to deposit_chunk().
        This is what was missing before: deposit_chunk() alone only ever
        grows occupancy, so without a matching release the exotic pool
        fills up and never drains (see this class's original module-level
        caveat).

        `yield`s on the (line, sachnummer) FIFO ready-store's get(), which:
          - returns immediately, FIFO (oldest-deposited-first), if a card
            of this product is already sitting in the exotic pool on this
            line — matching chunks to customer pickups in deposit order,
            not "whichever slot happens to be fullest" as an earlier
            version of this method did;
          - otherwise BLOCKS until the next matching deposit_chunk() call
            for this exact (line, sachnummer) — i.e. if the customer's
            due_date arrives before production has actually finished that
            chunk, this simply waits for it to show up (most likely it's
            still in production and becomes available within the hour),
            exactly mirroring SupermarketResource.store's own pull-side
            stockout resolution rather than failing/warning and moving on.

        Once a card is claimed, decrements the SAME physical row it was
        deposited into (tracked via the row_number carried through the
        FIFO store) — not "whichever row has the most cards" — for the
        SPECIFIC product being withdrawn (`slot.occupants[sachnummer]`),
        leaving any other product already sitting in that row untouched.
        Clears that product's entry out of `occupants` entirely once it
        hits zero, so deposit_chunk()'s "prefer an empty slot" branch can
        tell a row is genuinely empty again (i.e. `occupants` is empty),
        not just that THIS product is gone from it.
        A row_number of None (only possible when this line has no exotic
        slots configured at all — see deposit_chunk()) has nothing to
        decrement.

        This is a generator: call it as `yield from
        tracker.withdraw_chunk(line_name, sachnummer, env)` from within a
        SimPy process.
        """
        store = self._store_for(line_name, sachnummer, env)
        row_number = yield store.get()

        if row_number is None:
            return
        for slot in self.slots.get(line_name, []):
            if slot.row_number == row_number:
                remaining = slot.occupants.get(sachnummer, 0) - 1
                if remaining > 0:
                    slot.occupants[sachnummer] = remaining
                else:
                    slot.occupants.pop(sachnummer, None)
                break


def _deposit_push_chunk_to_supermarket(
    ctx: RunContext, line_name: str, chunk: OrderRecord,
) -> None:
    """
    Deposit this finished push chunk (one card, PUSH_CHUNK_SIZE pieces of
    chunk.sachnummer by construction — see ExoticSupermarketTracker's
    sizing-simplification note for the one edge case) into `line_name`'s
    exotic Supermarket slot pool.

    Always succeeds (see ExoticSupermarketTracker.deposit_chunk) — if
    every exotic slot is already full, this raises a flag onto
    ctx.overflow_log and keeps going rather than blocking or discarding
    the chunk, per the "no problem, raise a flag and continue" rule (the
    flag is meant to feed a future supermarket-sizing optimization pass,
    not to stop this simulation). A no-op if ctx.exotic_tracker wasn't
    supplied (keeps this callable from ad-hoc tests without requiring a
    full cfg.supermarkets).
    """
    if ctx.exotic_tracker is None:
        return

    fit, reason = ctx.exotic_tracker.deposit_chunk(line_name, chunk.sachnummer, ctx.kenv.env)
    if ctx.exotic_snapshot_log is not None:
        # One reading per physical slot on this line, right after the
        # deposit — gives the row-level time series its next data point.
        # See ExoticSlotSnapshot's docstring for why this lives here
        # rather than inside deposit_chunk() itself.
        ctx.exotic_snapshot_log.extend(
            ctx.exotic_tracker.snapshot(line_name, ctx.kenv.env.now)
        )
    if not fit:
        flag = SupermarketOverflowFlag(
            t=ctx.kenv.env.now, line=line_name, sachnummer=chunk.sachnummer,
            reason=reason or "exotic supermarket full",
        )
        if ctx.overflow_log is not None:
            ctx.overflow_log.append(flag)
        if ctx.verbose:
            print(f"  ⚑ [t={ctx.kenv.env.now:10.1f}] exotic supermarket overflow: "
                  f"{line_name} {chunk.sachnummer!r} — {flag.reason}")


def _withdraw_push_chunk_process(ctx: RunContext, line_name: str, chunk: OrderRecord):
    """
    SimPy generator — one spawned per delivered push chunk (see
    run_push_turn, sim/drain/push_turn.py, via crew_process, step (e)).
    Models "the customer withdraws this
    exotic-supermarket card when they actually need it", i.e. at
    chunk.due_date — the push-side counterpart to the pull-side
    withdrawal_process, and the missing half of ExoticSupermarketTracker
    (deposit_chunk() alone only ever grows occupancy; this is what drains
    it back down).

    Two separate waits happen here, in order:
      1. Sleeps until chunk.due_date (relative to ctx.epoch + env.now) —
         "the customer doesn't want it before they need it". If due_date
         is already in the past by the time this process starts (e.g. a
         chunk that finished production late), this step is skipped
         (delay clamped to 0) rather than sleeping a negative amount.
      2. `yield from ExoticSupermarketTracker.withdraw_chunk(...)` — FIFO
         (oldest-deposited-first) withdrawal of a card matching
         chunk.sachnummer from line_name's exotic pool. Because that
         method blocks on the underlying store's get() rather than
         failing when nothing is available yet, if THIS chunk itself is
         somehow still mid-production when its own due_date arrives (late
         push production), this simply waits until
         _deposit_push_chunk_to_supermarket() actually deposits it —
         "most likely it's still in production and becomes available
         within the hour" — instead of warning and moving on. (In normal
         operation this never actually waits here, since this process is
         only spawned once its OWN chunk has already been deposited by
         run_push_turn, sim/drain/push_turn.py, via crew_process; the
         block only matters for the general
         mechanism / any future caller of withdraw_chunk that isn't as
         tightly sequenced as this one.)

    Runs as a detached background process (fire-and-forget via
    env.process(), not `yield from`'d by run_push_turn, sim/drain/push_turn.py,
    via crew_process) so it
    doesn't block that loop from moving on to the next chunk.

    If chunk.due_date is None (shouldn't happen for a
    CustomerDemand-sourced push row, but guards against ad-hoc/test
    chunks) or ctx.exotic_tracker wasn't supplied, this is a no-op —
    mirrors _deposit_push_chunk_to_supermarket's own no-op guard.
    """
    if ctx.exotic_tracker is None or chunk.due_date is None:
        return
    kenv = ctx.kenv
    env = kenv.env
    tracker = ctx.exotic_tracker

    now_dt = ctx.epoch + _dt.timedelta(seconds=env.now)
    delay_s = max(0.0, (chunk.due_date - now_dt).total_seconds())
    if delay_s:
        yield env.timeout(delay_s)

    waiting_on_production = tracker.available_count(line_name, chunk.sachnummer) == 0
    if waiting_on_production and ctx.verbose:
        print(f"  [t={env.now:10.1f}] {line_name}: due_date reached for exotic "
              f"push card {chunk.sachnummer!r} but none in stock yet — waiting "
              f"for production to deposit it.")

    yield from tracker.withdraw_chunk(line_name, chunk.sachnummer, env)

    if ctx.exotic_snapshot_log is not None:
        # Same "one reading per physical slot on this line, right after
        # the mutation" convention _deposit_push_chunk_to_supermarket uses
        # for deposits — keeps the row-level time series accurate through
        # withdrawals too, not just deposits.
        ctx.exotic_snapshot_log.extend(
            tracker.snapshot(line_name, env.now)
        )

    if ctx.verbose:
        waited_str = " (after waiting on production)" if waiting_on_production else ""
        print(f"  [t={env.now:10.1f}] {line_name}: customer withdrew exotic "
              f"push card {chunk.sachnummer!r}{waited_str}")
