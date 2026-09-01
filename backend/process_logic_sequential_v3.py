"""
process_logic_sequential_v2.py
===============================
Step iv – Process Logic  (v2 — single-line runner)
----------------------------------------------------
Drives Part entities through one HTL production line using TRUE PIPELINE
PARALLELISM.  A companion module (parallel_runner_v1.py, to be created)
will call run_line() for all three lines under a shared SimPy clock.

Key changes vs. process_logic_sequential_v1.py
-----------------------------------------------
1.  Config v2 compatibility
    - SimConfig now stores stations/buffers/inspection PER LINE:
          cfg.stations[line_name]   list[StationConfig]
          cfg.buffers[line_name]    list[BufferConfig]
          cfg.inspection[line_name] InspectionConfig

2.  Changeover rewrite — n_workers hardcoded
    - changeover() accepts an explicit `n_workers: int` argument (1 or 2).
    - Worker count is now hardcoded via N_WORKERS module constant (default 1).
      Override by changing N_WORKERS at the top of this file, or by passing
      n_workers explicitly to run_line() / changeover().
    - Setup time is fetched exclusively from the XLSX-based lookup:
          cfg.csv_setup_times[line_name][n_workers][(from_ttnr, to_ttnr)]  → seconds
      The Excel product-letter matrix fallback has been removed; setup times
      are now Excel-only (HTL_setup_times.xlsx via config_loader_v5).
    - If the TTNr pair is absent a zero-second changeover is used with a
      warning printed to stdout.
    - setup_time_s is logged clearly so the caller can see which path was used.

3.  run_line() signature extended
    - order parameter changed from dict[str, int] to list[OrderRecord] so
      that Sachnummer, TTNr, and product label are all available for
      changeover lookup.
    - New mandatory parameter: n_workers: int  (1 or 2)
      Defaults to N_WORKERS module constant (hardcoded); override per-call.
    - Optional parameter: verbose: bool = True  (suppress per-part noise)

4.  inspection_outcome uses per-line InspectionConfig
    - cfg.inspection[line_name] instead of cfg.inspection.

5.  _safe_attr_name — unchanged (same helper as v1)

6.  Self-test (_run_validation) updated for v2 API
    - Uses load_config / build_environment from v2 modules.
    - Builds an OrderRecord list from the DispatchPlan for line HTL3.

7.  Inventory / FIFO Chute material flow ("Inventories" sheet)
    - Lochfilter and DRS sub-assemblies are no longer in-line stations on
      the main routing (see product_line_matrix_v2._active_station_sequence).
      They are produced by run_lochfilter_drs_production(), a SEPARATE
      SimPy process launched 1:1 in parallel with the main-line order that
      needs them (Einsteller command), depositing finished pieces into
      Inv_nach_Loch / Inv_nach_DRS.
    - Every part draws its material from the line's FIFO chute(s)
      (Chu_vor_HTL3/5/6) before entering Beladen — see
      _material_stored_types() / _draw_from_chute() and the
      material_stored_types parameter of part_lifecycle(). Replenishment
      (a fixed-size pull of ReplenishAmount packs from the upstream
      InventoryResource once the chute's own stock drops to
      TriggerAmountLeft) happens inline, inside _draw_from_chute() — there
      is no external batch-arrival process; the chute is pulled purely on
      its own remaining stock, and withdrawn from purely by the
      consuming station's own throughput.
    - A part passing final inspection deposits 1 unit into the
      finished-goods inventory (Inv_nach_HTL) fed by its last station,
      keyed by the part's product_type (Sachnummer) via
      InventoryResource.record_deposit_product(). Inv_nach_HTL therefore
      tracks stock as a dict[product_type -> pieces], which
      InventoryResource.packs_by_product exposes as whole PackSize packs
      — e.g. 120 packs of 25 units of F00RC00419, 80 packs of 25 units
      of F00RC00638 — matching how finished goods are actually stored
      and picked up.

8.  run_line() MOVED to push_runner_v4.py
    - This module keeps only the generic, policy-agnostic building blocks
      that any scheduling strategy (push, pull, kanban, ...) can reuse:
      changeover(), run_one_order(), part_lifecycle(),
      process_part_at_station(), inspection_outcome(), the material/chute
      helpers, and the KPI helpers.
    - run_line() — "iterate a fixed, pre-assigned list of orders on one
      line, start to finish" — is a PUSH-specific scheduling policy, not a
      basic process primitive. It (together with N_WORKERS /
      DEFAULT_N_WORKERS, which only existed to give it a default) now
      lives in push_runner_v4.py, which imports run_one_order() from here
      to build it. A different runner (e.g. a future pull_runner) can
      import run_one_order() directly and drive orders with its own
      policy without depending on run_line() at all.

Pipelining model (unchanged from v1)
--------------------------------------
Every Part of a batch is launched as its own SimPy process (part_lifecycle)
at the start of the batch.  SimPy's FIFO Resource objects serialise access
to each physical station; parts at different stations run concurrently.
AllOf waits for the full batch before the next changeover starts.

BAS (Blocking After Service) — unchanged from v1
--------------------------------------------------
For station i (1-indexed):
    1. REQUEST station i's machine.
    2. Once granted: GET 1 token from the upstream buffer (skipped for i=1).
    3. TIMEOUT sampled processing time.
    4. PUT 1 token into the downstream buffer — STILL holding the machine.
       If the buffer is full, both the part and the machine are blocked
       until space opens (BAS protocol).  Skipped for the last station.
    5. Machine released when the `with` block exits (after step 4).

Processing-time distribution (Step iii placeholder)
-----------------------------------------------------
Gaussian, mean = cycle_time_s, sigma = 10 % CoV.
Replace _sample_processing_time() body in Step iii; signature is frozen.

Usage (single-line)
--------------------
    NOTE: run_line() now lives in push_runner_v4.py (see item 8 above).
    This module supplies run_one_order() as the reusable per-order building
    block; push_runner_v4.run_line() is one way to drive a sequence of
    orders with it (the "push" policy — a fixed, pre-assigned order list
    run start to finish).

    import simpy
    from config_loader_v5      import load_config
    from entities_resources_v4 import build_environment
    from order_dispatcher_simple_batch import build_dispatch_plan
    from push_runner_v4        import run_line

    cfg     = load_config("ProductionPlanning_v6.xlsx", "HTL_setup_times.xlsx")
    env     = simpy.Environment()
    sim_env = build_environment(env, cfg, seed=42)

    plan = build_dispatch_plan(cfg, period_index=0)

    env.process(run_line(sim_env, line_id=1, orders=plan.orders_for_line("HTL3"),
                         n_workers=1))
    env.run()

Usage (parallel — wired up in push_runner_v4.py)
--------------------------------------------------
    for line_id, line_name in enumerate(cfg.line_names, start=1):
        env.process(run_line(sim_env, line_id=line_id,
                             orders=plan.orders_for_line(line_name),
                             n_workers=DEFAULT_N_WORKERS))
    env.run()
"""

from __future__ import annotations

from typing import Optional, TYPE_CHECKING

import simpy

from entities_resources_v5 import (
    SimEnvironment, Part, StationResource, BufferResource,
    InventoryResource, ChuteResource,
)
from product_line_matrix_v3 import _active_buffer_sequence, ProductClass
from schedule_events import ScheduleEvent, PackageTracker

if TYPE_CHECKING:
    # avoid circular import at runtime; OrderRecord is only needed for type hints
    from dispatch_entities import OrderRecord


# ---------------------------------------------------------------------------
# NOTE: the N_WORKERS / DEFAULT_N_WORKERS module constants used to live here.
# They only existed to give run_line() a default n_workers value, and
# run_line() has moved to push_runner_v4.py — so they moved with it.
# changeover() and run_one_order() below both take n_workers as a required,
# explicit argument and have no dependency on that default.
# ---------------------------------------------------------------------------


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


def _sample_processing_time(rng, station_resource: StationResource) -> float:
    """
    STEP iii PLACEHOLDER — Gaussian processing-time sample.

    Parameters
    ----------
    rng              : random.Random (SimEnvironment.rng)
    station_resource : carries StationConfig with cycle_time_s

    Returns
    -------
    float — seconds, always >= 0.1

    Replace ONLY this function body in Step iii once empirical distributions
    (e.g. Weibull, Log-Normal) have been fitted.  The signature is frozen.

    Current parameterisation
    ------------------------
        mean  = cycle_time_s         (from Excel — Process sheet)
        sigma = 0.10 × cycle_time_s  (10 % CoV — placeholder)
    """
    mean  = station_resource.station_cfg.cycle_time_s
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
# the main line anymore (see product_line_matrix_v2._active_station_sequence);
# instead a separate, parallel process (run_lochfilter_drs_production, below)
# manufactures them and deposits the output into Inv_nach_Loch / Inv_nach_DRS,
# 1:1 with the main-line order quantity, on the Einsteller's command.
#
# Replenishment: a FIFO chute is NOT fed by a continuous arrival process.
# Every withdrawal from a ChuteResource lane is followed by a level check:
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
    ch: "ChuteResource",
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


def _material_stored_types(product_class: "ProductClass") -> list[str]:
    """
    Map a product's ProductClass to the chute lane(s) (Stored_type
    values from the "Inventories" sheet) it must draw from at Beladen.

    Every product draws "Standard material" (the base housing material). Products
    additionally requiring a Lochfilter or DRS sub-assembly also draw from
    their dedicated lane — those lanes only exist on lines that have them
    configured (HTL3, per the current "Inventories" sheet); on lines
    without a matching lane, chute_for() simply returns None and the
    draw is skipped (see part_lifecycle).

    ASSUMPTION (flag if wrong): a STAB_LOCHFILTER/DRS part still needs the
    Standard housing material in addition to its special sub-assembly —
    i.e. Beladen draws from BOTH lanes, not just the special one.
    """
    if product_class == ProductClass.STAB_LOCHFILTER:
        return ["Standard material", "Lochfilter"]
    elif product_class == ProductClass.DRS:
        return ["Standard material", "DRS"]
    return ["Standard material"]


# ---------------------------------------------------------------------------
# Setup-time lookup helper
# ---------------------------------------------------------------------------
#
# NOTE: There used to be a second, duplicate Excel reader here
# (_load_excel_setup_times / _get_excel_setup_table / _excel_setup_cache)
# that re-parsed HTL_setup_times.xlsx itself, comma-splitting a single
# column — wrong for the real multi-column workbook, and redundant since
# config_loader_v5.load_config() already parses the file correctly via
# _parse_xlsx_setup_times() and stores the result on cfg.csv_setup_times.
# That whole subsystem has been removed. This module now simply reads the
# table config_loader_v5 already built — the file is parsed exactly once,
# by config_loader_v5, regardless of how many changeovers run.


def _resolve_setup_time_s(
    cfg,
    line_name: str,
    from_product: "OrderRecord",
    to_product:   "OrderRecord",
    n_workers: int,
) -> tuple[float, str]:
    """
    Return (setup_seconds, source_description) for a changeover.

    Lookup — TTNr-based table built by config_loader_v5 from HTL_setup_times.xlsx
    -------------------------------------------------------------------------------
    The workbook has one sheet per line (HTL3 / HTL5 / HTL6) with proper
    columns: Line | StartTTNr | EndTTNr | Setup_1MA | Setup_2MA.
    config_loader_v5._parse_xlsx_setup_times() parses it once, at load_config()
    time, into:
        cfg.csv_setup_times[line_name][n_workers][(from_ttnr, to_ttnr)] → int seconds
        • line_name : "HTL3" | "HTL5" | "HTL6"  — maps to sheet name
        • n_workers : 1  → Setup_1MA column  (1 Mitarbeiter)
                      2  → Setup_2MA column  (2 Mitarbeiter)
        • key       : (int(StartTTNr), int(EndTTNr))
        • value     : int seconds

    If the TTNr pair is absent, or either TTNr attribute is missing on an
    OrderRecord, 0 s is used with a warning.

    Parameters
    ----------
    cfg         : SimConfig
    line_name   : "HTL3" | "HTL5" | "HTL6"
    from_product: OrderRecord of the product currently set up
    to_product  : OrderRecord of the incoming product
    n_workers   : 1 or 2
    """
    from_ttnr = getattr(from_product, "ttnr", None)
    to_ttnr   = getattr(to_product,   "ttnr", None)

    # If ttnr is not set directly on the OrderRecord, resolve it from the
    # sachnummer via the product_master lookup table (cfg.sachnummer_to_ttnr).
    # Example: sachnummer "F00RC00967" → TTNr 967.
    sachnummer_to_ttnr: dict = getattr(cfg, "sachnummer_to_ttnr", {})
    if from_ttnr is None:
        from_sachnr = getattr(from_product, "sachnummer", None)
        from_ttnr   = sachnummer_to_ttnr.get(from_sachnr) if from_sachnr else None
    if to_ttnr is None:
        to_sachnr = getattr(to_product, "sachnummer", None)
        to_ttnr   = sachnummer_to_ttnr.get(to_sachnr) if to_sachnr else None

    if from_ttnr is None or to_ttnr is None:
        from_id = getattr(from_product, "sachnummer", repr(from_product))
        to_id   = getattr(to_product,   "sachnummer", repr(to_product))
        return (
            0.0,
            f"⚠ TTNr missing for {from_id!r} → {to_id!r} on {line_name} "
            f"[{n_workers}MA] — treating as 0 s",
        )

    key = (int(from_ttnr), int(to_ttnr))

    table: dict = (
        getattr(cfg, "csv_setup_times", {})
        .get(line_name, {})
        .get(n_workers, {})
    )
    if key in table:
        secs = float (table[key])*60
        return (
            float(secs), # time in file is in minutes
            f"TTNr {from_ttnr}→{to_ttnr} [{n_workers}MA] = {secs} s",
        )

    return (
        0.0,
        f"⚠ TTNr pair ({from_ttnr}, {to_ttnr}) not found for "
        f"{line_name} [{n_workers}MA] — treating as 0 s",
    )


# ---------------------------------------------------------------------------
# Changeover — whole-line barrier
# ---------------------------------------------------------------------------

def changeover(
    sim_env:      SimEnvironment,
    line_id:      int,
    current_rec:  "OrderRecord | None",
    incoming_rec: "OrderRecord",
    n_workers:    int,
    verbose:      bool = True,
):
    """
    SimPy generator — insert a setup delay when the product on the line changes.

    Called by run_one_order() (in turn called per-order by whatever runner
    is driving the line — e.g. push_runner_v4.run_line()) AFTER all parts
    of the previous product have exited, i.e. this is a whole-line barrier
    (not a per-station setup).

    Parameters
    ----------
    sim_env      : SimEnvironment
    line_id      : 1-based line index
    current_rec  : OrderRecord currently set up on the line (None = first run)
    incoming_rec : OrderRecord about to start
    n_workers    : 1 or 2 Mitarbeiter performing the changeover — required,
                   no default here; the caller decides (e.g. push_runner_v4
                   defaults it via its own DEFAULT_N_WORKERS constant)
                   1 → Setup_1MA column from HTL_setup_times.xlsx
                   2 → Setup_2MA column from HTL_setup_times.xlsx

    Behaviour
    ---------
    No delay when:
      • current_rec is None  (first product ever — no previous setup)
      • current_rec.sachnummer == incoming_rec.sachnummer  (same product)

    Otherwise: yield env.timeout(setup_time_s) and log the event.

    After the timeout, line.current_product is set to incoming_rec.sachnummer.
    """
    env  = sim_env.env
    line = sim_env.lines[line_id - 1]

    # No changeover needed
    if current_rec is None or (
        current_rec.sachnummer == incoming_rec.sachnummer
    ):
        line.current_product = incoming_rec.sachnummer
        return   # generator exits without yielding — zero-cost

    setup_s, source = _resolve_setup_time_s(
        sim_env.cfg,
        line.line_name,
        current_rec,
        incoming_rec,
        n_workers,
    )

    if verbose:
        print(
            f"  [t={env.now:10.1f}] {line.line_name}(L{line_id}): "
            f"CHANGEOVER  {current_rec.sachnummer!r} → {incoming_rec.sachnummer!r}  "
            f"| workers={n_workers}MA  | source: {source}"
        )

    t0 = env.now
    if setup_s > 0.0:
        yield env.timeout(setup_s)

    line.current_product = incoming_rec.sachnummer

    if setup_s > 0.0:
        sim_env.log_event(ScheduleEvent(
            line_name       = line.line_name,
            line_id         = line_id,
            event_type      = "setup",
            start_s         = t0,
            end_s           = env.now,
            n_workers       = n_workers,
            from_sachnummer = current_rec.sachnummer,
            to_sachnummer   = incoming_rec.sachnummer,
            note            = source,
        ))

    if verbose and setup_s > 0.0:
        print(
            f"  [t={env.now:10.1f}] {line.line_name}(L{line_id}): "
            f"Changeover complete — line ready for {incoming_rec.sachnummer!r}"
        )


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

    Launched (fire-and-forget, via env.process()) by run_one_order()
    alongside the main-line parts, for orders whose product_class is
    STAB_LOCHFILTER or DRS. Does nothing for any other product_class.

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
                f"skipping parallel production for {order_rec.sachnummer}."
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
            f"{order_rec.quantity} pcs for {order_rec.sachnummer!r}"
        )

    for _ in range(order_rec.quantity):
        with station_res.resource.request() as req:
            yield req

            if raw_chute is not None:
                yield from _draw_from_chute(sim_env, raw_chute, qty=1)

            t_start = round(env.now, 2)
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
    3. TIMEOUT the sampled processing time.
    4. PUT 1 token into downstream_buffer WHILE still holding the machine.
       If the buffer is full, both part and machine are blocked until space
       opens (BAS: Blocking After Service).
       Skipped for the last station (unlimited final storage).
    5. Machine released when the `with` block exits.

    Timing attributes set on Part
    ------------------------------
    part.t_<attr>_start  and  part.t_<attr>_end
    where <attr> = _safe_attr_name(station_res.name).

    cycle_start is set on the very first station the part enters.

    Statistics updated
    ------------------
    station_res.n_processed      += 1
    station_res.total_busy_time  += pure processing time (step 3 only)
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

        proc_time = _sample_processing_time(sim_env.rng, station_res)
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
       chute_for(line_name, ...)
       before entering the first station. Lanes that don't exist on this
       line (chute_for returns None) are silently skipped. Rework
       passes reuse the same physical part, so no re-draw on rework.
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
                   (see _material_stored_types()); None/[] = skip (e.g. for
                   callers not using the Inventories/chute model).
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
                sim_env, part, station_res, upstream, downstream
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


# ---------------------------------------------------------------------------
# Single-order body — shared by run_line() (static batch) and by
# order_dispatcher_per_order_v1.py (dynamic, per-order dispatch).
# ---------------------------------------------------------------------------

def run_one_order(
    sim_env:     SimEnvironment,
    line_id:     int,
    current_rec: "OrderRecord | None",
    order_rec:   "OrderRecord",
    n_workers:   int,
    verbose:     bool = True,
) -> bool:
    """
    SimPy generator — run ONE OrderRecord to completion on *line_id*.

    This is exactly the per-order body that used to live inline inside
    run_line()'s `for order_rec in orders:` loop (changeover → resolve
    station/buffer lists → launch all parts → wait for the batch to fully
    clear the line). It was extracted, unchanged, so that:

      • run_line() (now in push_runner_v4.py — a push-specific policy
        that drives a static, pre-assigned list of orders) can keep using
        it unchanged, and
      • a dynamic dispatcher (order_dispatcher_per_order_v1.py) can drive
        ONE order at a time, deciding at runtime which line gets the next
        order — while still getting correct changeover costs, because the
        caller is responsible for persisting `current_rec` per line across
        calls (see that module's `line_current_rec` dict) and passing it
        back in on the next call for the same line.

    Parameters
    ----------
    current_rec : the OrderRecord last run on this line (None = first ever
                  run on this line, or line just reset) — same role as the
                  loop-local `current_rec` inside run_line().
    order_rec   : the OrderRecord to run now.

    Returns
    -------
    bool — True if the order actually ran (so the caller should advance
           its `current_rec` bookkeeping to `order_rec`), False if it was
           skipped (missing station/buffer resources — same "skip" cases
           run_line() already handled via `continue`; caller must NOT
           advance current_rec in that case, matching run_line()'s
           original behaviour).
    """
    env       = sim_env.env
    cfg       = sim_env.cfg
    line      = sim_env.lines[line_id - 1]
    line_name = line.line_name
    package_size = cfg.packaging.package_size
    rework_limit = cfg.inspection[line_name].rework_loop_limit   # None = unlimited

    # ── 1. Changeover ────────────────────────────────────────────────────
    yield env.process(
        changeover(sim_env, line_id, current_rec, order_rec, n_workers, verbose)
    )

    # ── 2. Resolve station / buffer lists for THIS product's routing ─────
    station_list: list[StationResource] = []
    missing: list[str] = []
    for sname in order_rec.station_sequence:
        sr = line.stations.get(sname)
        if sr is None:
            missing.append(sname)
        else:
            station_list.append(sr)

    if missing:
        print(
            f"  ⚠ {line_name}: stations {missing} not found in line resources "
            f"for order {order_rec.sachnummer} — skipping order."
        )
        return False

    if not station_list:
        print(
            f"  ⚠ {line_name}: empty station list for {order_rec.sachnummer} "
            f"— skipping order."
        )
        return False

    active_buf_cfgs = _active_buffer_sequence(
        line_name,
        [sr.name for sr in station_list],
        cfg.buffers,
    )
    buf_by_name: dict[str, BufferResource] = {br.name: br for br in line.buffers}
    buffer_list: list[BufferResource] = []
    missing_bufs: list[str] = []
    for bc in active_buf_cfgs:
        br = buf_by_name.get(bc.buffer_id)
        if br is None:
            missing_bufs.append(bc.buffer_id)
        else:
            buffer_list.append(br)
    if missing_bufs:
        print(
            f"  ⚠ {line_name}: buffer resource(s) {missing_bufs} not found "
            f"in line resources for order {order_rec.sachnummer} — BAS may "
            f"not function correctly."
        )

    if len(buffer_list) != len(station_list) - 1:
        print(
            f"  ⚠ {line_name}: resolved buffer count ({len(buffer_list)}) != "
            f"station count - 1 ({len(station_list) - 1}) for "
            f"{order_rec.sachnummer}.  BAS may not function correctly."
        )

    if verbose:
        print(
            f"  [t={env.now:10.1f}] {line_name}(L{line_id}): "
            f"Releasing {order_rec.quantity} × {order_rec.sachnummer!r} "
            f"({order_rec.kunde})  |  "
            f"route: {' → '.join(order_rec.station_sequence)}"
        )

    # ── 3. Launch all parts concurrently ─────────────────────────────────
    pkg_tracker = PackageTracker(
        line_name      = line_name,
        line_id        = line_id,
        sachnummer     = order_rec.sachnummer,
        kunde          = order_rec.kunde,
        product_class  = order_rec.product_class,
        package_size   = package_size,
        event_log      = sim_env.event_log,
    )

    # Einsteller command: if this order needs a Lochfilter or DRS
    # sub-assembly, kick off its production 1:1 in parallel, decoupled
    # from the main line (see run_lochfilter_drs_production docstring).
    # Fire-and-forget — NOT included in the AllOf wait below.
    if order_rec.product_class in (ProductClass.STAB_LOCHFILTER, ProductClass.DRS):
        env.process(
            run_lochfilter_drs_production(sim_env, line_id, order_rec, verbose=verbose)
        )

    material_stored_types = _material_stored_types(order_rec.product_class)

    part_processes: list[simpy.Process] = []
    for _ in range(order_rec.quantity):
        part = sim_env.create_part(
            product_type  = order_rec.sachnummer,
            product_class = order_rec.product_class,
            line_id       = line_id,
        )
        proc = env.process(
            part_lifecycle(
                sim_env,
                part,
                station_list,
                buffer_list,
                rework_limit,
                line_name,
                material_stored_types=material_stored_types,
                verbose=False,
                on_finish=pkg_tracker.on_finish,
            )
        )
        part_processes.append(proc)

    # ── 4. Wait for the full batch to clear the line ──────────────────────
    if part_processes:
        yield simpy.AllOf(env, part_processes)

    pkg_tracker.flush(env.now)

    if verbose:
        passed  = sum(
            1 for p in sim_env.parts_out
            if p.product_type == order_rec.sachnummer and p.status == "passed"
        )
        scrapped = sum(
            1 for p in sim_env.parts_out
            if p.product_type == order_rec.sachnummer and p.status == "scrapped"
        )
        print(
            f"  [t={env.now:10.1f}] {line_name}(L{line_id}): "
            f"Order {order_rec.sachnummer!r} COMPLETE — "
            f"passed={passed}  scrapped={scrapped}  "
            f"(line cleared, ready for changeover)"
        )

    return True


# ---------------------------------------------------------------------------
# NOTE: run_line() — the top-level single-line runner that iterates a fixed
# list of orders via run_one_order() — used to live here. It has MOVED to
# push_runner_v4.py, since "run this pre-assigned order list start to
# finish" is a push-specific scheduling policy built on top of
# run_one_order(), not a basic process primitive. Import it from there:
#     from push_runner_v4 import run_line
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# KPI helper (can be called after env.run())
# ---------------------------------------------------------------------------

def line_kpi_summary(sim_env: SimEnvironment, line_id: int, sim_time: float) -> dict:
    """
    Compute and return per-line KPIs after env.run() has finished.

    Returns a dict with:
        passed, scrapped, reworked, total_created,
        station_utilisation  {station_name: rho},
        buffer_max_fill      {buffer_id: (max_fill, capacity)},
        mean_cycle_time_s    (over passed parts),
    """
    line = sim_env.lines[line_id - 1]

    parts_out = [
        p for p in sim_env.parts_out if p.line_id == line_id
    ]
    passed   = [p for p in parts_out if p.status == "passed"]
    scrapped = [p for p in parts_out if p.status == "scrapped"]
    reworked = [p for p in parts_out if p.rework_pass > 0]

    station_util = {
        name: (sr.total_busy_time / sim_time if sim_time > 0 else 0.0)
        for name, sr in line.stations.items()
    }
    buffer_fill = {
        br.name: (br.max_observed_fill, br.capacity)
        for br in line.buffers
    }
    mean_ct = (
        sum(p.cycle_time for p in passed) / len(passed)
        if passed else None
    )

    return {
        "line_id":             line_id,
        "line_name":           line.line_name,
        "passed":              len(passed),
        "scrapped":            len(scrapped),
        "reworked":            len(reworked),
        "total_created":       sum(1 for p in sim_env.parts_in_wip + parts_out
                                   if p.line_id == line_id),
        "station_utilisation": station_util,
        "buffer_max_fill":     buffer_fill,
        "mean_cycle_time_s":   mean_ct,
    }


def print_kpi(sim_env: SimEnvironment, line_id: int, sim_time: float) -> None:
    """Pretty-print KPIs for one line after simulation."""
    kpi  = line_kpi_summary(sim_env, line_id, sim_time)
    SEP  = "=" * 65
    SEP2 = "-" * 65
    print(SEP)
    print(f"  KPI SUMMARY — {kpi['line_name']} (Line {kpi['line_id']})")
    print(SEP)
    print(f"  Passed               : {kpi['passed']}")
    print(f"  Scrapped             : {kpi['scrapped']}")
    print(f"  Had ≥1 rework        : {kpi['reworked']}")
    if kpi["mean_cycle_time_s"] is not None:
        print(f"  Mean cycle time      : {kpi['mean_cycle_time_s']:.2f} s")
    print(f"  Simulation end time  : {sim_time:.1f} s  ({sim_time/3600:.4f} h)")
    print(SEP2)
    print("  Station utilisation (ρ = busy_time / sim_time):")
    for name, rho in kpi["station_utilisation"].items():
        print(f"    {name:<22} ρ = {rho:.4f}")
    print(SEP2)
    print("  Buffer max observed fill:")
    for bid, (mx, cap) in kpi["buffer_max_fill"].items():
        print(f"    {bid:<36} {mx}/{cap}")
    print(SEP)


# ---------------------------------------------------------------------------
# Self-test  (python process_logic_sequential_v2.py [excel] [csv] [n_workers])
# ---------------------------------------------------------------------------

def _run_validation():
    """
    Smoke-test: run a small order set on line HTL3 and print KPIs.

    Demonstrates:
      - v2 config (per-line stations / inspection)
      - changeover with selectable n_workers (1MA vs 2MA)
      - pipeline parallelism (verified by overlap check on first two parts)

    Run:
        python process_logic_sequential_v2.py [excel] [setup_xlsx] [n_workers]
        python process_logic_sequential_v2.py ProductionPlanning_v6.xlsx HTL_setup_times.xlsx 1
    """
    import sys
    from config_loader_v6      import load_config
    from entities_resources_v5 import build_environment
    from order_dispatcher_simple_adjuster import build_dispatch_plan
    from dispatch_entities import OrderRecord
    # run_line() now lives in push_runner_v4.py (see item 8 in the module
    # docstring above) — imported locally here, at self-test time, to avoid
    # a module-level circular import (push_runner_v4 imports FROM this
    # module at its own module scope).
    from push_runner_v4 import run_line

    excel      = sys.argv[1] if len(sys.argv) > 1 else "ProductionPlanning_v6.xlsx"
    setup_xlsx = sys.argv[2] if len(sys.argv) > 2 else "HTL_setup_times.xlsx"
    n_workers  = int(sys.argv[3]) if len(sys.argv) > 3 else 1   # default: 1 Mitarbeiter

    assert n_workers in (1, 2), "n_workers must be 1 or 2"

    SEP  = "=" * 65
    SEP2 = "-" * 65

    print(SEP)
    print("process_logic_sequential_v2.py — self-test")
    print(f"  Excel         : {excel}")
    print(f"  Setup times   : {setup_xlsx}")
    print(f"  Workers       : {n_workers}MA")
    print(SEP)

    cfg = load_config(excel, setup_xlsx)
    # cfg.csv_setup_times is already fully populated by load_config() above
    # (parsed once, from the proper multi-column workbook) — nothing further
    # to wire up here.
    env     = simpy.Environment()
    sim_env = build_environment(env, cfg, seed=42)

    # Build dispatch plan and take the first 3 orders for HTL3 (keeps test short)
    plan = build_dispatch_plan(cfg, period_index=0)
    htl3_orders: list[OrderRecord] = plan.orders_for_line("HTL3")[:3]

    if not htl3_orders:
        print("  ⚠ No orders dispatched to HTL3 — check DispatchPlan.")
        return

    print(f"  Orders for HTL3 ({len(htl3_orders)} orders, first 3 used):")
    for rec in htl3_orders:
        print(f"    {rec}")
    print(SEP2)

    # Launch the single-line runner
    env.process(
        run_line(sim_env, line_id=1, orders=htl3_orders, n_workers=n_workers, verbose=True)
    )
    env.run()

    sim_time = env.now
    print_kpi(sim_env, line_id=1, sim_time=sim_time)

    # ── Pipelining check ───────────────────────────────────────────────────
    parts_line1 = sorted(
        [p for p in sim_env.parts_out if p.line_id == 1],
        key=lambda p: p.part_id,
    )
    first_product_sachnr = htl3_orders[0].sachnummer
    first_batch = [p for p in parts_line1 if p.product_type == first_product_sachnr]

    station_names = htl3_orders[0].station_sequence
    attrs = [_safe_attr_name(s) for s in station_names]

    print()
    print(SEP2)
    print("Pipelining check — per-station times for first two parts of batch 1:")
    print(SEP2)
    for p in first_batch[:2]:
        print(f"\n  Part #{p.part_id}  (rework={p.rework_pass}, status={p.status})")
        for attr, sname in zip(attrs, station_names):
            t0 = getattr(p, f"t_{attr}_start", None)
            t1 = getattr(p, f"t_{attr}_end",   None)
            if t0 is not None:
                print(f"    {sname:<22} start={t0:10.2f}  end={t1:10.2f}")

    if len(first_batch) >= 2 and len(attrs) >= 2:
        p1, p2 = first_batch[0], first_batch[1]
        a0, a1 = attrs[0], attrs[1]
        p1_s2_start = getattr(p1, f"t_{a1}_start", None)
        p1_s2_end   = getattr(p1, f"t_{a1}_end",   None)
        p2_s1_start = getattr(p2, f"t_{a0}_start", None)
        p2_s1_end   = getattr(p2, f"t_{a0}_end",   None)

        if all(v is not None for v in [p1_s2_start, p1_s2_end, p2_s1_start, p2_s1_end]):
            overlap_start = max(p1_s2_start, p2_s1_start)
            overlap_end   = min(p1_s2_end,   p2_s1_end)
            overlaps = overlap_start < overlap_end
            print()
            print(f"  Part #1 at station 2 ({station_names[1]}): "
                  f"[{p1_s2_start:.2f}, {p1_s2_end:.2f}]")
            print(f"  Part #2 at station 1 ({station_names[0]}): "
                  f"[{p2_s1_start:.2f}, {p2_s1_end:.2f}]")
            if overlaps:
                print(f"  ✓ PIPELINING CONFIRMED — overlap [{overlap_start:.2f}, {overlap_end:.2f}]")
            else:
                print("  (No overlap — check buffer sizes / cycle times.)")

    # ── Sanity checks ─────────────────────────────────────────────────────
    print()
    print(SEP2)
    errors: list[str] = []
    line = sim_env.lines[0]
    if sim_env.parts_in_wip:
        errors.append(f"FAIL: {len(sim_env.parts_in_wip)} parts still in WIP")
    for br in line.buffers:
        if br.max_observed_fill > br.capacity:
            errors.append(f"FAIL: buffer {br.name} exceeded capacity")
    if errors:
        for e in errors:
            print(f"  *** {e}")
    else:
        print("  Sanity checks PASSED.")
    print(SEP)
    print("process_logic_sequential_v2.py — validation complete.")
    print(SEP)


if __name__ == "__main__":
    _run_validation()
