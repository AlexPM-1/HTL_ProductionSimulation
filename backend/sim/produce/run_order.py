"""
sim/produce/run_order.py
==========================
run_one_order() — the single-order body for the push side. Called by
sim.drain.push_turn.run_push_turn().
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import simpy

from sim.resources.environment import SimEnvironment
from sim.resources.stations import StationResource, BufferResource
from domain.products import _active_buffer_sequence, ProductClass
from telemetry.packaging import PackageTracker

from sim.produce.changeover import changeover
from sim.produce.part_lifecycle import part_lifecycle, run_lochfilter_drs_production
from sim.produce.routing import material_stored_types as _material_stored_types

if TYPE_CHECKING:
    # avoid circular import at runtime; OrderRecord is only needed for
    # type hints
    from domain.orders import OrderRecord


# ---------------------------------------------------------------------------
# Single-order body, called per turn by sim.drain.push_turn.run_push_turn().
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
    SimPy generator — run ONE OrderRecord to completion on *line_id*
    (changeover → resolve station/buffer lists → launch all parts →
    wait for the batch to fully clear the line). Called once per chunk
    by run_push_turn() (sim/drain/push_turn.py), which persists
    `current_rec` per line across calls and passes it back in on the
    next call for the same line so changeover costs stay correct.

    Parameters
    ----------
    current_rec : the OrderRecord last run on this line (None = first ever
                  run on this line, or line just reset).
    order_rec   : the OrderRecord to run now.

    Returns
    -------
    bool — True if the order actually ran (so the caller should advance
           its `current_rec` bookkeeping to `order_rec`), False if it was
           skipped (missing station/buffer resources) — caller must NOT
           advance current_rec in that case.
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
            f"Note: {order_rec.note}"
        )

    return True
