"""
sim/produce/changeover.py
==========================
changeover() — whole-line setup barrier — plus its private helper
_resolve_setup_time_s(). Called by sim.produce.run_order.run_one_order()
and sim.produce.run_pull_batch.run_one_pull_batch().

Callers pass anything duck-typed as a sim_env (SimEnvironment,
MixedSimEnvironment, or telemetry.recorder.Recorder wrapping one) —
this module only ever touches `.env`, `.lines`, `.cfg`, and
`.log_event()`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from telemetry.records import ScheduleEvent

if TYPE_CHECKING:
    # avoid circular import at runtime; OrderRecord/SimEnvironment are only
    # needed for type hints
    from domain.orders import OrderRecord
    from sim.resources.environment import SimEnvironment


# ---------------------------------------------------------------------------
# Setup-time lookup helper
# ---------------------------------------------------------------------------
#
# Reads the table domain.config.load_config() already built at cfg
# .csv_setup_times — the setup-times workbook is parsed exactly once,
# regardless of how many changeovers run.

def _resolve_setup_time_s(
    cfg,
    line_name: str,
    from_product: "OrderRecord",
    to_product:   "OrderRecord",
    n_workers: int,
) -> tuple[float, str]:
    """
    Return (setup_seconds, source_description) for a changeover.

    Lookup — TTNr-based table built by domain.config from HTLSetupTimes.xlsx
    -------------------------------------------------------------------------------
    The workbook has one sheet per line (HTL3 / HTL5 / HTL6) with proper
    columns: Line | StartTTNr | EndTTNr | Setup_1MA | Setup_2MA.
    domain.config's xlsx setup-time parser parses it once, at load_config()
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
    # product_number via the product_master lookup table (cfg.product_number_to_ttnr).
    # Example: product_number "F00RC00967" → TTNr 967.
    product_number_to_ttnr: dict = getattr(cfg, "product_number_to_ttnr", {})
    if from_ttnr is None:
        from_product_number = getattr(from_product, "product_number", None)
        from_ttnr   = product_number_to_ttnr.get(from_product_number) if from_product_number else None
    if to_ttnr is None:
        to_product_number = getattr(to_product, "product_number", None)
        to_ttnr   = product_number_to_ttnr.get(to_product_number) if to_product_number else None

    if from_ttnr is None or to_ttnr is None:
        from_id = getattr(from_product, "product_number", repr(from_product))
        to_id   = getattr(to_product,   "product_number", repr(to_product))
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
        secs = float(table[key]) * 60
        return (
            float(secs),  # time in file is in minutes
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
    sim_env:      "SimEnvironment",
    line_id:      int,
    current_rec:  "OrderRecord | None",
    incoming_rec: "OrderRecord",
    n_workers:    int,
    verbose:      bool = True,
):
    """
    SimPy generator — insert a setup delay when the product on the line changes.

    Called by run_one_order() / run_one_pull_batch() (in turn called
    per-order by whatever runner is driving the line) AFTER all parts of
    the previous product have exited, i.e. this is a whole-line barrier
    (not a per-station setup).

    Parameters
    ----------
    sim_env      : SimEnvironment (or a duck-typed wrapper, e.g.
                   telemetry.recorder.Recorder)
    line_id      : 1-based line index
    current_rec  : OrderRecord currently set up on the line (None = first run)
    incoming_rec : OrderRecord about to start
    n_workers    : 1 or 2 Mitarbeiter performing the changeover — required,
                   no default here; the caller decides.
                   1 → Setup_1MA column from HTLSetupTimes.xlsx
                   2 → Setup_2MA column from HTLSetupTimes.xlsx

    Behaviour
    ---------
    No delay when:
      • current_rec is None  (first product ever — no previous setup)
      • current_rec.product_number == incoming_rec.product_number  (same product)

    Otherwise: yield env.timeout(setup_time_s) and log the event.

    After the timeout, line.current_product is set to incoming_rec.product_number.

    The logged ScheduleEvent's order_id/production_type are stamped from
    incoming_rec this is a "what started next" record, not a "what finished before" 
    one, so current_rec's order is never stored here.
    """
    env  = sim_env.env
    line = sim_env.lines[line_id - 1]

    # No changeover needed
    if current_rec is None or (
        current_rec.product_number == incoming_rec.product_number
    ):
        line.current_product = incoming_rec.product_number
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
            f"CHANGEOVER  {current_rec.product_number!r} → {incoming_rec.product_number!r}  "
            f"| workers={n_workers}MA  | source: {source}"
        )

    t0 = env.now
    if setup_s > 0.0:
        yield env.timeout(setup_s)

    line.current_product = incoming_rec.product_number

    if setup_s > 0.0:
        sim_env.log_event(ScheduleEvent(
            line_name       = line.line_name,
            line_id         = line_id,
            event_type      = "setup",
            start_s         = t0,
            end_s           = env.now,
            n_workers       = n_workers,
            from_product_number = current_rec.product_number,
            to_product_number   = incoming_rec.product_number,
            note            = source,
            production_type = getattr(incoming_rec, "production_type", None),
            order_id        = getattr(incoming_rec, "order_id", None),
        ))

    if verbose and setup_s > 0.0:
        print(
            f"  [t={env.now:10.1f}] {line.line_name}(L{line_id}): "
            f"Changeover complete — line ready for {incoming_rec.product_number!r}"
        )
