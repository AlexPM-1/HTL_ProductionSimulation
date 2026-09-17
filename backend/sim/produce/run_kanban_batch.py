"""
sim/produce/run_kanban_batch.py
=================================
run_one_kanban_batch() and _make_kanban_order_record() — the Kanban
(pull) side counterpart of sim.produce.run_order.run_one_order(). Both
are called by sim.drain.pull_turn._run_one_pull_card(), which imports
them directly from this module.
"""

from __future__ import annotations

from typing import Optional, TYPE_CHECKING

import simpy

from sim.resources.environment import KanbanSimEnvironment
from sim.resources.stations import StationResource, BufferResource
from domain.orders import OrderRecord
from domain.products import _active_buffer_sequence, ProductClass
from telemetry.packaging import PackageTracker
from telemetry.recorder import Recorder

from sim.produce.changeover import changeover
from sim.produce.part_lifecycle import part_lifecycle, run_lochfilter_drs_production
from sim.produce.routing import material_stored_types as _material_stored_types

if TYPE_CHECKING:
    # avoid a real circular import at runtime (sim.drain.pull_turn imports
    # THIS module, and imports RunContext too) — RunContext is only
    # needed here for type hints.
    from sim.context import RunContext


def run_one_kanban_batch(
    kenv: KanbanSimEnvironment,
    line_id: int,
    current_rec: "OrderRecord | None",
    order_rec: OrderRecord,
    n_workers: int,
    on_finish,
    verbose: bool = True,
    event_log: Optional[list] = None,
):
    """
    SimPy generator - run ONE Kanban-released batch to completion on
    *line_id*, and return the OrderRecord to use as `current_rec` for the
    NEXT batch's changeover lookup.

    This mirrors sim.produce.run_order.run_one_order()'s thin
    orchestration (changeover -> resolve station/buffer lists -> launch
    parts -> AllOf) EXACTLY, calling the same underlying primitives —
    the only difference is a caller-supplied `on_finish` instead of a
    hardwired PackageTracker, so a part passing final inspection can
    deposit into the Kanban Supermarket / recycle its card instead of
    (or in addition to, if you also want package-level tracking) ticking
    a package counter.

    The normal "product not feasible on this line" case is already
    filtered out one level up, in sim.drain.pull_turn._run_one_pull_card
    (via _make_kanban_order_record below), which pushes the batch back
    onto the Kanban Chute for a retry before this function is ever
    called. What remains here is a stricter, should-never-happen guard:
    the product IS feasible per the catalogue, but this SimEnvironment's
    build didn't actually construct one of its named StationResource
    objects (e.g. an inconsistency between the product matrix and the
    loaded workbook). In that case the batch is logged and dropped
    (current_rec returned unchanged) rather than silently lost with no
    trace — this function has no access to the raw KanbanCard list to
    requeue them, only the caller (_run_one_pull_card) does.

    event_log: caller-owned list[telemetry.records.ScheduleEvent], same
    by-reference pattern as snapshot_log. part_lifecycle() itself has no
    event_log parameter — in the push model, run_one_order() gets its
    Gantt segments by wiring a telemetry.packaging.PackageTracker's
    .on_finish as part_lifecycle's `on_finish` callback (see that
    function). This function does the same thing here, composed with
    the caller's own on_finish (Kanban Supermarket deposit + card
    recycle) so both fire on every completed part: the PackageTracker's
    handler groups finished parts into package_size chunks and appends a
    ScheduleEvent to `event_log` each time one closes, exactly like a
    push-model job segment. Optional — omit it and the batch still runs,
    it just won't be represented on a Gantt chart.
    """
    env = kenv.env
    cfg = kenv.cfg
    line = kenv.lines[line_id - 1]
    line_name = line.line_name
    rework_limit = cfg.inspection[line_name].rework_loop_limit

    # ── 1. Changeover (reused, unchanged) ─────────────────────────────────
    # Pass a Recorder, not kenv itself, so changeover()'s internal
    # `sim_env.log_event(...)` call for the "setup" ScheduleEvent lands in
    # *this* run's caller-owned `event_log` list (same one PackageTracker
    # writes "job" segments into below) instead of KanbanSimEnvironment's
    # own, disconnected internal event store — see telemetry.recorder.Recorder.
    yield env.process(
        changeover(
            Recorder(kenv, event_log=event_log),
            line_id, current_rec, order_rec, n_workers, verbose,
        )
    )

    # ── 2. Resolve station / buffer lists for this product's routing ─────
    station_list: list[StationResource] = []
    missing: list[str] = []
    for sname in order_rec.station_sequence:
        sr = line.stations.get(sname)
        if sr is None:
            missing.append(sname)
        else:
            station_list.append(sr)

    if missing or not station_list:
        print(f"  ⚠ {line_name}: cannot resolve routing for Kanban batch "
              f"{order_rec.sachnummer!r} (missing stations: {missing}) — "
              f"re-queuing batch on the Kanban Chute for retry.")
        return current_rec

    active_buf_cfgs = _active_buffer_sequence(
        line_name, [sr.name for sr in station_list], cfg.buffers,
    )
    buf_by_name: dict[str, BufferResource] = {br.name: br for br in line.buffers}
    buffer_list: list[BufferResource] = [
        buf_by_name[bc.buffer_id] for bc in active_buf_cfgs if bc.buffer_id in buf_by_name
    ]

    if verbose:
        print(f"  [t={env.now:10.1f}] {line_name}(L{line_id}): "
              f"Kanban PRODUCTION START — {order_rec.quantity} × "
              f"{order_rec.sachnummer!r}  |  "
              f"route: {' → '.join(order_rec.station_sequence)}")

    # Einsteller command for Lochfilter/DRS sub-assembly — reused,
    # unchanged, fire-and-forget exactly as in run_one_order().
    if order_rec.product_class in (ProductClass.STAB_LOCHFILTER, ProductClass.DRS):
        env.process(
            run_lochfilter_drs_production(kenv, line_id, order_rec, verbose=verbose)
        )

    material_stored_types = _material_stored_types(order_rec.product_class)

    # ── 3. Launch all parts concurrently (reused, unchanged) ──────────────
    # Gantt segments: same mechanism run_one_order() uses (a PackageTracker
    # wired as part_lifecycle's on_finish), composed here with the caller's
    # own on_finish (Kanban Supermarket deposit + card recycle) so both
    # fire on every completed part. part_lifecycle() has no event_log
    # parameter of its own — see this function's docstring.
    pkg_tracker: Optional[PackageTracker] = None
    if event_log is not None:
        pkg_tracker = PackageTracker(
            line_name=line_name,
            line_id=line_id,
            sachnummer=order_rec.sachnummer,
            kunde=order_rec.kunde,
            product_class=order_rec.product_class,
            package_size=cfg.packaging.package_size,
            event_log=event_log,
        )

    def _on_finish(t: float, status: str):
        if pkg_tracker is not None:
            pkg_tracker.on_finish(t, status)
        on_finish(t, status)

    part_processes: list[simpy.Process] = []
    for _ in range(order_rec.quantity):
        part = kenv.create_part(
            product_type  = order_rec.sachnummer,
            product_class = order_rec.product_class,
            line_id       = line_id,
        )
        proc = env.process(
            part_lifecycle(
                kenv, part, station_list, buffer_list, rework_limit, line_name,
                material_stored_types=material_stored_types,
                verbose=False,
                on_finish=_on_finish,
            )
        )
        part_processes.append(proc)

    # ── 4. Wait for the full batch to clear the line ──────────────────────
    if part_processes:
        yield simpy.AllOf(env, part_processes)

    if pkg_tracker is not None:
        pkg_tracker.flush(env.now)

    if verbose:
        passed = sum(1 for p in kenv.parts_out
                     if p.product_type == order_rec.sachnummer and p.status == "passed"
                     and p.line_id == line_id)
        print(f"  [t={env.now:10.1f}] {line_name}(L{line_id}): "
              f"Kanban batch {order_rec.sachnummer!r} COMPLETE "
              f"(cumulative line passed so far: {passed})")

    return order_rec


def _make_kanban_order_record(rt: "RunContext", line_name: str, product_type: str,
                               quantity: int) -> Optional[OrderRecord]:
    """
    Build the (duck-typed but real) OrderRecord run_one_kanban_batch()
    needs, from the domain.products catalogue. Returns None if the
    product turns out not to be feasible on this line (shouldn't happen —
    build_kanban_environment only creates a (line, product) Supermarket
    for eligible products — but checked defensively).
    """
    info = rt.product_info(product_type)
    line_class = info.lines.get(line_name)
    if line_class is None or not line_class.station_names:
        return None
    return OrderRecord(
        # Throwaway internal "what to produce" record for this batch —
        # NOT the customer-facing OrderRecordPull tracked in
        # kenv.order_registry (see SimEnvironment.create_order()), so it
        # has no real order_id to assign and nothing ever reads this
        # field off it. -1 is a sentinel marking "not a registered
        # order" rather than a valid sequential id.
        order_id=-1,
        period_label="Kanban",
        sachnummer=product_type,
        kunde=info.kunde,
        product_class=info.product_class,
        quantity=quantity,
        assigned_line=line_name,
        freigabe=line_class.freigabe.value,
        station_sequence=line_class.station_names,
        feasible_lines=info.feasible_lines(),
        note="kanban batch",
    )
