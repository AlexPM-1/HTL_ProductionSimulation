"""
sim/produce/part_lifecycle.py
==============================
part_lifecycle(), process_part_at_station(), inspection_outcome(),
run_lochfilter_drs_production(), _draw_from_chute(),
_sample_processing_time(), _safe_attr_name(). Called by
sim.produce.run_order.run_one_order() and
sim.produce.run_pull_batch.run_one_pull_batch().

Everything a single Part needs to travel through one line's stations,
including the FIFO-chute material draw and the parallel Lochfilter/DRS
sub-assembly process. See each function's own docstring below.
"""

from __future__ import annotations

from typing import Optional, TYPE_CHECKING

from sim.resources.environment import SimEnvironment
from sim.resources.part import Part
from sim.resources.stations import StationResource, BufferResource
from sim.resources.inventory import InventoryResource, MaterialChuteResource
from domain.products import ProductClass

if TYPE_CHECKING:
    # avoid circular import at runtime; OrderRecord is only needed for
    # type hints
    from domain.orders import OrderRecord


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _safe_attr_name(station_name: str) -> str:
    """
    Convert a canonical station name to a safe Python identifier used as the
    infix of Part timing attributes  (t_<attr>_start / t_<attr>_end).

    Examples
    --------
        "Beladen"          -> "beladen"
        "Stift_Einpressen" -> "stift_einpressen"
        "Sichtpruefung"    -> "sichtpruefung"
        "Lochfilter"       -> "lochfilter"
    """
    return (
        station_name
        .lower()
        .replace(" ", "_")
        .replace("ü", "u")
        .replace("ä", "a")
        .replace("ö", "o")
    )


def _sample_processing_time(
    rng,
    station_resource: StationResource,
    oee_loss: "Optional[float]" = None,
) -> float:
    """
    Gaussian processing-time sample (placeholder model).

    Parameters
    ----------
    rng              : random.Random (SimEnvironment.rng)
    station_resource : carries StationConfig with cycle_time_s
    oee_loss         : today's per-line CombinedProductionLoss (a fraction,
                        e.g. 0.15 for a 15 % loss), or None/0.0 if no OEE
                        sheet is configured for this line — see
                        sim.oee.OEELossTracker.get_current().
                        Applied as a REDUCTION IN PRODUCTION, i.e. it
                        stretches the sampled cycle time so the station
                        effectively produces fewer pieces per hour — it is
                        NOT modelled as a discrete breakdown/stoppage
                        event (no separate down-state, no extra blocking
                        beyond the normal BAS mechanics). See module docs.

    Returns
    -------
    float — seconds, always >= 0.1

    Replace ONLY this function body once empirical distributions
    (e.g. Weibull, Log-Normal) have been fitted. The signature is frozen
    apart from the additive oee_loss parameter above.

    Current parameterisation
    ------------------------
        mean  = cycle_time_s         (from Excel — Process sheet)
                / (1 − oee_loss)     (stretched by today's OEE loss, if any)
        sigma = 0.10 × mean          (10 % CoV — placeholder, applied to
                                       the OEE-adjusted mean, so variability
                                       scales with the slower effective rate)
    """
    mean = station_resource.station_cfg.cycle_time_s
    if oee_loss:
        # Clamp defensively: bin tables are expected to stay well under 1.0
        # (observed workbook values top out ~0.35), but a corrupt/edited
        # sheet could in principle produce a 1.0+ loss, which would blow up
        # (or invert) the division below.
        loss = min(max(oee_loss, 0.0), 0.95)
        if loss > 0.0:
            mean = mean / (1.0 - loss)
    sigma = 0.10 * mean
    return round(max(0.1, rng.gauss(mean, sigma)), 2)


# ---------------------------------------------------------------------------
# Material flow — Inventory / FIFO Chute model ("Inventories" sheet)
# ---------------------------------------------------------------------------
#
# Every physical part draws its material from the line's FIFO chute(s)
# BEFORE occupying Beladen — modelling the push system where the Einsteller
# has already staged material at the chute. Lochfilter and DRS
# sub-assemblies are the interesting case: they are NOT produced in-line on
# the main line anymore (see domain.products._active_station_sequence);
# instead a separate, parallel process (run_lochfilter_drs_production, below)
# manufactures them and deposits the output into Inv_nach_Loch / Inv_nach_DRS,
# 1:1 with the main-line order quantity, on the Einsteller's command.
#
# Replenishment: a FIFO chute is NOT fed by a continuous arrival process.
# Every withdrawal from a MaterialChuteResource lane is followed by a level check:
# once the chute's own current_fill has dropped to (or below)
# trigger_amount_left, the SAME generator that triggered it immediately
# pulls a FIXED amount — replenish_qty_pcs (= ReplenishAmount packs x
# PackSize) — from the lane's upstream InventoryResource, up to the chute's
# capacity. If the upstream inventory is momentarily short (e.g.
# Inv_nach_Loch waiting on Lochfilter production), the `container.get()`
# call blocks naturally — this is the coupling mechanism between the main
# line and the parallel Lochfilter/DRS process, with no extra
# synchronization code required.

def _draw_from_chute(
    sim_env: SimEnvironment,
    ch: "MaterialChuteResource",
    qty: int = 1,
):
    """
    SimPy generator — withdraw `qty` units from FIFO chute lane `ch`,
    performing a level-triggered replenishment from its upstream
    inventory FIRST if needed, then withdrawing.

    IMPORTANT — refill happens BEFORE the withdrawal, not after. Chutes
    are built with initial_fill=0 (they start empty, per the "Inventories"
    sheet), so on a chute's very first draw the container genuinely has
    0 units in it. If the withdrawal (`container.get(qty)`) were attempted
    before checking/performing replenishment, it would block forever on
    that first call — nothing would ever exist to satisfy it, since the
    only code that puts material into the chute would be sequenced after
    the still-blocked get() and therefore unreachable. That is a hard
    deadlock (SimPy's env.run() eventually reports "no more schedulable
    events" while this generator — and everything waiting on it, e.g. the
    dispatcher process — never returns).

    Concurrency note: multiple parts can see the chute below its trigger
    (or short of `qty`) at the same time. ch.refill_lock (capacity=1)
    serializes the check-and-refill section; the re-check of the
    condition INSIDE the lock (in a `while`, not a single `if`) is what
    makes this correct (double-checked locking) — a part that queued on
    the lock while another was already refilling will find the chute
    already topped up and skip straight past without pulling again.

    Each refill pulls a FIXED quantity, ch.replenish_qty_pcs (packs x
    pack_size) — not "top up to capacity" as in the old kanban model —
    capped at the chute's remaining headroom so the container.put()
    never exceeds capacity. The loop keeps refilling (in
    replenish_qty_pcs increments) until either the chute holds enough to
    satisfy `qty` and is above its trigger level, or it's already full.
    """
    if (ch.current_fill < qty or ch.needs_refill) and ch.upstream_inventory is not None:
        with ch.refill_lock.request() as req:
            yield req
            while (
                (ch.current_fill < qty or ch.needs_refill)
                and ch.current_fill < ch.capacity
            ):
                refill_qty = min(ch.replenish_qty_pcs, ch.capacity - ch.current_fill)
                if refill_qty <= 0:
                    break
                yield ch.upstream_inventory.container.get(refill_qty)
                ch.upstream_inventory.record_withdrawal(refill_qty)
                yield ch.container.put(refill_qty)
                ch.record_refill(refill_qty)

    yield ch.container.get(qty)
    ch.record_consumption(qty)


# ---------------------------------------------------------------------------
# Inspection outcome sampler
# ---------------------------------------------------------------------------

def inspection_outcome(sim_env: SimEnvironment, part: Part, line_name: str) -> str:
    """
    Sample the inspection decision for *part* at the last station.

    Uses InspectionConfig for the specific line (cfg.inspection[line_name]):
        pass_rate   → "passed"
        rework_rate → "rework"
        scrap_rate  → "scrapped"

    Returns
    -------
    str — one of: "passed" | "rework" | "scrapped"
    """
    insp = sim_env.cfg.inspection[line_name]
    r    = sim_env.rng.random()
    if r < insp.pass_rate:
        return "passed"
    elif r < insp.pass_rate + insp.rework_rate:
        return "rework"
    else:
        return "scrapped"


# ---------------------------------------------------------------------------
# Parallel Lochfilter / DRS production ("Inventories" decoupled model)
# ---------------------------------------------------------------------------

def run_lochfilter_drs_production(
    sim_env:    SimEnvironment,
    line_id:    int,
    order_rec:  "OrderRecord",
    verbose:    bool = True,
):
    """
    SimPy generator — Einsteller command to produce `order_rec.quantity`
    Lochfilter or DRS sub-assemblies, running IN PARALLEL with (decoupled
    from) the main-line order that will consume them.

    Launched (fire-and-forget, via env.process()) by run_one_order() /
    run_one_pull_batch() alongside the main-line parts, for orders whose
    product_class is STAB_LOCHFILTER or DRS. Does nothing for any other
    product_class.

    Flow (per piece, `order_rec.quantity` times)
    ----------------------------------------------
    1. Seize the line's physical Lochfilter/DRS StationResource (capacity=1
       — already built from the Process sheet, unchanged).
    2. Draw 1 unit of raw material from the Chu_vor_Lochfilter / Chu_vor_DRS
       FIFO chute (itself refilled from Inv_nach_ECM once its own stock
       drops to its trigger level).
    3. Sample processing time from the station's cycle_time_s and hold it.
    4. Deposit 1 finished unit into the matching output InventoryResource
       (Inv_nach_Loch / Inv_nach_DRS — resolved by matching
       InventoryConfig.upstream_station == this station's name, no
       hardcoded inventory names).

    This process does NOT participate in the main line's FIFO/BAS pipeline
    and is not waited on by run_one_order()'s simpy.AllOf — it produces at
    its own pace; the main line's Beladen naturally blocks on the output
    inventory if production falls behind (see _draw_from_chute).
    """
    env       = sim_env.env
    line      = sim_env.lines[line_id - 1]
    line_name = line.line_name

    if order_rec.product_class == ProductClass.STAB_LOCHFILTER:
        station_name, raw_stored_type = "Lochfilter", "Lochfitler material"
    elif order_rec.product_class == ProductClass.DRS:
        station_name, raw_stored_type = "DRS", "DRS material"
    else:
        return  # nothing to produce for this product class

    station_res = line.stations.get(station_name)
    if station_res is None:
        if verbose:
            print(
                f"  ⚠ {line_name}: no {station_name!r} station resource — "
                f"skipping parallel production for {order_rec.product_number}."
            )
        return

    raw_chute = sim_env.chute_for(station_name, raw_stored_type)
    out_inv = next(
        (inv for lanes in sim_env.inventories.values() for inv in lanes.values()
         if inv.inventory_cfg.upstream_station == station_name),
        None,
    )

    if verbose:
        print(
            f"  [t={env.now:10.1f}] {line_name}(L{line_id}): "
            f"Einsteller command — start parallel {station_name} production, "
            f"{order_rec.quantity} pcs for {order_rec.product_number!r}"
        )

    for _ in range(order_rec.quantity):
        with station_res.resource.request() as req:
            yield req

            if raw_chute is not None:
                yield from _draw_from_chute(sim_env, raw_chute, qty=1)

            t_start = round(env.now, 2)
            # NOTE: intentionally NOT OEE-adjusted (no oee_loss passed) —
            # this parallel Lochfilter/DRS sub-assembly process is outside
            # the scope of the OEE-loss wiring below (process_part_at_station
            # / part_lifecycle / run_one_order's main pipeline), which is
            # where sim.runner's daily CombinedProductionLoss draw is
            # consumed. Revisit if OEE loss should also apply here.
            proc_time = _sample_processing_time(sim_env.rng, station_res)
            yield env.timeout(proc_time)
            t_end = round(env.now, 2)

            station_res.n_processed     += 1
            station_res.total_busy_time += (t_end - t_start)

        if out_inv is not None:
            yield out_inv.container.put(1)
            out_inv.record_deposit(1)

    if verbose:
        print(
            f"  [t={env.now:10.1f}] {line_name}(L{line_id}): "
            f"{station_name} production COMPLETE — {order_rec.quantity} pcs "
            f"→ {out_inv.name if out_inv else '?'}"
        )


# ---------------------------------------------------------------------------
# Single-station process (core building block)  — BAS protocol
# ---------------------------------------------------------------------------

def process_part_at_station(
    sim_env:           SimEnvironment,
    part:              Part,
    station_res:       StationResource,
    upstream_buffer:   "BufferResource | None",
    downstream_buffer: "BufferResource | None",
    line_name:         "str | None" = None,
):
    """
    SimPy generator — one Part's pass through ONE station.

    This generator describes a single (station, part) pair.  It is called
    repeatedly from part_lifecycle (one call per station per pass) via
    `yield from`, so SimPy events are yielded directly by the parent process.
    This lets different parts occupy different stations simultaneously
    (pipeline parallelism).

    BAS sequencing
    --------------
    1. REQUEST the station's SimPy Resource (FIFO queue when busy).
    2. Once granted: GET 1 token from upstream_buffer (part leaves buffer).
       Skipped for the first station (infinite input queue before Beladen).
    3. TIMEOUT the sampled processing time — stretched by today's OEE loss
       for `line_name`, if any (see "OEE loss" below).
    4. PUT 1 token into downstream_buffer WHILE still holding the machine.
       If the buffer is full, both part and machine are blocked until space
       opens (BAS: Blocking After Service).
       Skipped for the last station (unlimited final storage).
    5. Machine released when the `with` block exits.

    OEE loss (CombinedProductionLoss)
    ----------------------------------
    If `sim_env` carries an `oee_tracker` (sim.oee.OEELossTracker —
    attached by sim.runner.run_mixed() as `menv.oee_tracker`;
    absent/None for callers that don't use it), this looks up
    `line_name`'s already-drawn value for today via
    `oee_tracker.get_current(line_name)` — a read-only lookup, it never
    triggers a draw itself — and passes it into _sample_processing_time()
    as a REDUCTION IN PRODUCTION: the sampled cycle time is stretched, so
    the station effectively produces fewer pieces per hour. This is
    deliberately NOT modelled as a breakdown/stoppage — there is no
    separate down-state and no extra blocking beyond the ordinary BAS
    mechanics above; the part still occupies the station for one
    (longer) timeout, same as any other cycle. `line_name=None` (or no
    oee_tracker, or no OEE bins configured for this line) means no
    adjustment — the nominal cycle_time_s is used, unchanged from before.

    Timing attributes set on Part
    ------------------------------
    part.t_<attr>_start  and  part.t_<attr>_end
    where <attr> = _safe_attr_name(station_res.name).

    cycle_start is set on the very first station the part enters.

    Statistics updated
    ------------------
    station_res.n_processed      += 1
    station_res.total_busy_time  += pure (OEE-adjusted) processing time
                                     (step 3 only)
    downstream_buffer.max_observed_fill updated after each PUT.
    """
    env  = sim_env.env
    attr = _safe_attr_name(station_res.name)

    with station_res.resource.request() as req:
        yield req   # wait in FIFO queue for this station's machine

        # Step 2 — formally leave the upstream buffer
        if upstream_buffer is not None:
            yield upstream_buffer.container.get(1)

        # Step 3 — process
        if part.t_cycle_start == 0.0:
            part.t_cycle_start = env.now

        t_start = round(env.now, 2)
        setattr(part, f"t_{attr}_start", t_start)

        oee_tracker = getattr(sim_env, "oee_tracker", None)
        oee_loss = (
            oee_tracker.get_current(line_name)
            if oee_tracker is not None and line_name is not None
            else None
        )
        proc_time = _sample_processing_time(sim_env.rng, station_res, oee_loss)
        yield env.timeout(proc_time)

        t_end = round(env.now, 2)
        setattr(part, f"t_{attr}_end", t_end)

        station_res.n_processed    += 1
        station_res.total_busy_time += (t_end - t_start)

        # Step 4 — BAS: hand the part to the downstream buffer
        # (machine stays seized until this succeeds)
        if downstream_buffer is not None:
            yield downstream_buffer.container.put(1)
            fill = downstream_buffer.current_fill
            if fill > downstream_buffer.max_observed_fill:
                downstream_buffer.max_observed_fill = fill
    # Step 5 — machine released here (with-block exit)


# ---------------------------------------------------------------------------
# Per-part lifecycle — independent SimPy process (enables pipelining)
# ---------------------------------------------------------------------------

def part_lifecycle(
    sim_env:      SimEnvironment,
    part:         Part,
    station_list: list[StationResource],
    buffer_list:  list[BufferResource],
    rework_limit: "int | None",
    line_name:    str,
    material_stored_types: "list[str] | None" = None,
    verbose:      bool = True,
    on_finish:    "Optional[callable]" = None,
):
    """
    SimPy generator — one Part's complete journey through the line.

    Launched via env.process(part_lifecycle(...)) so it runs as an
    independent process: while THIS part is at station k, other
    part_lifecycle processes can be simultaneously progressing through
    stations 1 … k-1 and k+1 … N (pipeline parallelism).

    Loop
    ----
    0. On the FIRST pass only (part.rework_pass == 0): draw 1 unit from
       each FIFO chute lane in `material_stored_types` (e.g.
       ["Standard material"] or ["Standard material", "Lochfilter"]) via
       chute_for(line_name, ...) — see sim.produce.routing.material_stored_types
       for how this list is computed — before entering the first station.
       Lanes that don't exist on this line (chute_for returns None) are
       silently skipped. Rework passes reuse the same physical part, so
       no re-draw on rework.
    1. Pass through every station via process_part_at_station (`yield from`).
    2. At the last station, sample inspection_outcome:
         "passed"   → finish_part(status="passed"), return.
         "scrapped" → finish_part(status="scrapped"), return.
         "rework"   → increment part.rework_pass, loop from station 0.
                      If rework_limit is set and exceeded, force-scrap.

    Parameters
    ----------
    station_list : ordered list of StationResource for this line/product
    buffer_list  : ordered list of BufferResource (len = len(station_list) − 1)
    rework_limit : max rework passes before forced scrap (None = unlimited)
    line_name    : "HTL3" | "HTL5" | "HTL6" — for InspectionConfig lookup
    material_stored_types : FIFO chute lane(s) to draw from before Beladen
                   (see sim.produce.routing.material_stored_types()); None/[]
                   = skip (e.g. for callers not using the Inventories/chute
                   model).
    verbose      : print rework events when True
    """
    env = sim_env.env

    # Resolve once: the inventory fed by this route's LAST station (e.g.
    # Inv_nach_HTL, whose upstream_station == "Sichtpruefung") — deposited
    # into, per product_type, whenever a part passes final inspection
    # (see record_deposit_product() / packs_by_product below). None if no
    # such inventory is configured (older workbook without "Inventories").
    finished_goods_inv = next(
        (inv for lanes in sim_env.inventories.values() for inv in lanes.values()
         if station_list and inv.inventory_cfg.upstream_station == station_list[-1].name),
        None,
    )

    while True:
        if part.rework_pass == 0 and material_stored_types:
            for stored_type in material_stored_types:
                ch = sim_env.chute_for(line_name, stored_type)
                if ch is not None:
                    yield from _draw_from_chute(sim_env, ch, qty=1)

        for idx, station_res in enumerate(station_list):
            # upstream buffer exists for every station except the first
            upstream   = buffer_list[idx - 1] if idx > 0                else None
            # downstream buffer exists for every station except the last
            downstream = buffer_list[idx]     if idx < len(buffer_list) else None

            yield from process_part_at_station(
                sim_env, part, station_res, upstream, downstream,
                line_name=line_name,
            )

        # ── Inspection at the last station ────────────────────────────────
        outcome = inspection_outcome(sim_env, part, line_name)

        if outcome == "passed":
            if finished_goods_inv is not None:
                yield finished_goods_inv.container.put(1)
                finished_goods_inv.record_deposit_product(part.product_type, 1)
            sim_env.finish_part(part, status="passed")
            if on_finish is not None:
                on_finish(env.now, "passed")
            return

        elif outcome == "scrapped":
            sim_env.finish_part(part, status="scrapped")
            if on_finish is not None:
                on_finish(env.now, "scrapped")
            return

        else:   # "rework"
            part.rework_pass += 1
            if rework_limit is not None and part.rework_pass >= rework_limit:
                if verbose:
                    print(
                        f"  [t={env.now:10.1f}] Part #{part.part_id}: "
                        f"rework limit {rework_limit} reached → scrapped"
                    )
                sim_env.finish_part(part, status="scrapped")
                if on_finish is not None:
                    on_finish(env.now, "scrapped")
                return
            if verbose:
                print(
                    f"  [t={env.now:10.1f}] Part #{part.part_id}: "
                    f"rework pass #{part.rework_pass} — returning to {station_list[0].name}"
                )
            # while-loop continues from station_list[0]
