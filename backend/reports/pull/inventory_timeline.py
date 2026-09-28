"""
reports/pull/inventory_timeline.py
=====================================
Supermarket inventory-timeline report — the Pull tab's per-supermarket,
per-product row-by-row ledger of every stock-moving event
(withdrawal / deposit_batch / initial), plus the KPI
strip that summarizes it.

Reads telemetry.records.SupermarketSnapshot straight off
menv.snapshot_log (t, line_id, line_name, product_type, event_type,
n_available, pcs_partial, card_size, delta_qty, kanban_card_id) — the
same log reports.all.supermarket_series.build_line_units_timeseries
reads, just walked event-by-event instead of resampled onto a bin grid.
Kept separate from that module (single-purpose, same convention as
reports.pull.card_flow / reports.pull.shortfall being kept apart) since
this report's shape (one row per raw event) is fundamentally different
from a time-bucketed series.

Two rows sharing one (line, product) pair (some supermarkets have 2
physical rows feeding the same product_number) already collapse into one
stream here for free: Recorder.record_supermarket() has always written
at (line_id, product_type) granularity, never per physical row, so
there is nothing to merge — see reports.all.supermarket_series's
main_runner_groups for the only place row-fan-out ever happens, and
that's a display-only expansion downstream of this same log, not a
second source of truth.

The ledger itself (build_inventory_timeline_payload) merges every
product a supermarket carries into one chronological row list per
line — see that function's docstring — while the KPI strip
(build_inventory_kpis) keeps summarizing per (line, product), since an
avg/min/max inventory or stockout count only means something computed
against one product's own quantity, never mixed across products.

Two things are filtered out of both the ledger and the KPI strip below
(see _grouped_snapshots): "deposit_partial" events (a per-piece
production tick that only bumps pcs_partial, never n_available — zero
information for this report, see _NOISE_EVENT_TYPES), and any (line,
product) pair that never actually held or moved kanban stock at all
(see _has_pull_activity) — a Supermarket row that's configured but
never saw a real withdrawal/deposit, which otherwise shows up as an
all-zero KPI block adding noise rather than information.
"""

from __future__ import annotations

import datetime as _dt
from typing import TYPE_CHECKING, Iterable, Optional

from domain.epoch import compute_epoch

if TYPE_CHECKING:
    from domain.config import SimConfig
    from sim.resources.environment import MixedSimEnvironment
    from telemetry.records import SupermarketSnapshot


# ---------------------------------------------------------------------------
# Time formatting
# ---------------------------------------------------------------------------

def _format_time(t: float, sim_epoch: "_dt.datetime") -> str:
    """
    'DD.MM.YYYY HH:MM:SS' — sim_epoch + timedelta(seconds=t), same
    (dt - ctx.sim_epoch)-style mapping sim.fill.pull.withdrawal uses, just
    inverted. sim_epoch is always a real datetime by the time this is
    called (see _resolve_sim_epoch — domain.epoch.compute_epoch() is a
    fixed constant, SIM_START, never None), so this always renders a
    real calendar date, never an elapsed "+HH:MM:SS" offset.
    """
    return (sim_epoch + _dt.timedelta(seconds=t)).strftime("%d.%m.%Y %H:%M:%S")


def _resolve_sim_epoch(menv, sim_epoch: "Optional[_dt.datetime]") -> "_dt.datetime":
    """
    Prefer an explicitly-passed sim_epoch (a caller that has the owning
    RunContext handy, e.g. api/legacy.py, can pass ctx.sim_epoch straight
    through); otherwise menv.sim_epoch if the environment happens to
    carry its own copy (reports/ modules never import
    sim.context.RunContext directly — see this package's "domain +
    telemetry only" import rule — so that's a best-effort duck-typed
    lookup, not a hard dependency); otherwise domain.epoch.compute_epoch()
    directly.

    That last fallback is what actually matters day to day: the epoch is
    a fixed constant (domain.constants.SIM_START) that RunContext.epoch
    is ALWAYS populated with at construction (see sim.context.RunContext
    .for_kanban — "always known and never None"), so there's no real
    "no dates in the sheet" case to fall back from. Any time this
    function used to return None, it was only because the epoch hadn't
    been threaded through this particular call, never because a
    genuine epoch-less run exists — so this now always returns a real
    datetime, never None.
    """
    if sim_epoch is not None:
        return sim_epoch
    menv_epoch = getattr(menv, "sim_epoch", None)
    if menv_epoch is not None:
        return menv_epoch
    return compute_epoch()


# ---------------------------------------------------------------------------
# Grouping helper — shared by the timeline and the KPIs so both walk the
# exact same per-(line, product) sorted event stream.
# ---------------------------------------------------------------------------

# Event types that never move n_available and therefore carry zero
# information for this ledger (a per-piece production tick that only
# bumps pcs_partial — see reports.pull.restmenge for that number's own
# dedicated report). Excluded here rather than upstream in
# telemetry.recorder.Recorder, since the raw event still exists on
# menv.snapshot_log for anything else that might want it — this report
# just doesn't display it.
_NOISE_EVENT_TYPES: frozenset[str] = frozenset({"deposit_partial"})


def _has_pull_activity(snaps: "list[SupermarketSnapshot]") -> bool:
    """
    True iff this (line, product) stream ever actually held or moved
    stock — i.e. at least one snapshot with n_available > 0, or at
    least one nonzero delta_qty. False for a product that was
    "configured" (a Supermarket row exists for it) but never saw a real
    withdrawal or deposit — e.g. a product with no genuine kanban
    demand on this line, which otherwise shows up as an all-zero KPI
    block (avg/min/max inventory 0, qty_withdrawn/replenished 0) that
    adds noise without adding information.
    """
    for s in snaps:
        if s.n_available > 0:
            return True
        if s.delta_qty:
            return True
    return False


def _grouped_snapshots(
    snapshot_log: "Iterable[SupermarketSnapshot]",
    line_name: Optional[str],
    product_type: Optional[str],
) -> dict[tuple[str, str], list["SupermarketSnapshot"]]:
    grouped: dict[tuple[str, str], list["SupermarketSnapshot"]] = {}
    for snap in snapshot_log:
        if snap.event_type in _NOISE_EVENT_TYPES:
            continue
        if line_name is not None and snap.line_name != line_name:
            continue
        if product_type is not None and snap.product_type != product_type:
            continue
        grouped.setdefault((snap.line_name, snap.product_type), []).append(snap)
    for snaps in grouped.values():
        snaps.sort(key=lambda s: s.t)
    # Drop (line, product) pairs left with nothing meaningful: either
    # every snapshot they had was noise (now filtered above, so the key
    # never got created), or every real snapshot still sat at
    # n_available == 0 with no stock ever moving in or out.
    grouped = {k: v for k, v in grouped.items() if _has_pull_activity(v)}
    return grouped


# ---------------------------------------------------------------------------
# Inventory timeline — the row-by-row ledger.
# ---------------------------------------------------------------------------

def build_inventory_timeline_payload(
    menv: "MixedSimEnvironment",
    cfg: "SimConfig",
    line_name: Optional[str] = None,
    product_type: Optional[str] = None,
    sim_epoch: "Optional[_dt.datetime]" = None,
) -> dict:
    """
    Per supermarket (line), every SupermarketSnapshot in
    menv.snapshot_log across every product that line carries, merged
    into one chronological ledger — a (line, product) pair no longer
    gets its own separate row list; instead every product's events for
    a given line are interleaved by real timestamp `t` into a single
    stream, each row still carrying its own "product" so the table can
    show what moved. Two rows sharing one (line, product) pair (a
    supermarket with 2 physical rows feeding the same product_number, see
    module docstring) were already one stream before this merge; this
    step additionally folds every *other* product on that same line
    into that one stream too, matching how the dashboard prints one
    ledger per supermarket. Each row:

        {"time":            "DD.MM.YYYY HH:MM" (or "+HH:MM:SS", see
                             _format_time),
         "product":         product_type,
         "event":           event_type — "withdrawal" | "deposit_batch"
                             | "initial",
         "initial_qty":     final_qty - delta_qty,
         "delta_qty":       signed change (0 for the very first snapshot
                             of a (line, product) pair, which has no
                             prior baseline to diff against),
         "final_qty":       n_available (post-event, authoritative),
         "associated_card": kanban_card_id, or None}

    line_name / product_type optionally scope the ledger to a single
    supermarket / product (the dashboard's line/product filter
    dropdowns); both default to "everything". Ties on the same `t`
    (two products' events landing in the same sim second) break on
    product name, purely for a deterministic row order — real events
    never land on the exact same float second in practice.

    cfg is accepted (unused here) only to keep this function's call
    signature uniform with every other reports/ builder's (menv, cfg, ...)
    shape, the same reason
    reports.all.supermarket_series.build_supermarket_state_payload
    takes it too.
    """
    epoch = _resolve_sim_epoch(menv, sim_epoch)
    grouped = _grouped_snapshots(
        getattr(menv, "snapshot_log", None) or [], line_name, product_type
    )

    # Fold every (line, product) stream into one row list per line,
    # each row still tagged with its own product, then sort that
    # merged list by real timestamp so a supermarket's ledger reads
    # chronologically across all of its products at once.
    by_line: dict[str, list[tuple[float, str, dict]]] = {}
    for (l_name, p_type), snaps in grouped.items():
        for snap in snaps:
            delta = snap.delta_qty if snap.delta_qty is not None else 0
            row = {
                "time": _format_time(snap.t, epoch),
                "product": p_type,
                "event": snap.event_type,
                "initial_qty": snap.n_available - delta,
                "delta_qty": delta,
                "final_qty": snap.n_available,
                "associated_card": snap.kanban_card_id,
            }
            by_line.setdefault(l_name, []).append((snap.t, p_type, row))

    lines_out: list[dict] = []
    for l_name in sorted(by_line):
        ordered = sorted(by_line[l_name], key=lambda item: (item[0], item[1]))
        lines_out.append({
            "line_name": l_name,
            "rows": [row for (_t, _p, row) in ordered],
        })

    return {"supermarkets": lines_out}


# ---------------------------------------------------------------------------
# KPIs — one summary block per (line, product).
# ---------------------------------------------------------------------------

def build_inventory_kpis(
    menv: "MixedSimEnvironment",
    cfg: "SimConfig",
    line_name: Optional[str] = None,
    product_type: Optional[str] = None,
) -> dict:
    """
    Per (line, product), summarizing the same menv.snapshot_log stream
    build_inventory_timeline_payload() walks:

      avg_inventory / min_inventory / max_inventory : n_available,
          time-weighted for the average (sum of n_available * the
          duration it held that value, divided by total duration —
          reports/binning.py has no ready-made helper for this integral,
          only a last-value-at-a-bin-edge lookup (state_at), so it's
          done directly here), min/max are plain extrema over every
          observed n_available.
      time_without_stock_s : total seconds spent at n_available == 0.
      stockouts             : count of 0 -> positive n_available
          transitions (i.e. how many times the shelf recovered from
          empty — equivalently, how many separate stockout intervals
          occurred, barring one still open when the run ends).
      qty_withdrawn / qty_replenished : running sums of negative /
          positive delta_qty across the stream (absolute pieces moved
          each direction, not a net).

    The window for both "last value holds until" and the average's
    denominator is [first snapshot's t, menv.env.now] — before the first
    snapshot there is no recorded state to weight, and every stream ends
    open-ended at "now".
    """
    sim_time_s = menv.env.now
    grouped = _grouped_snapshots(
        getattr(menv, "snapshot_log", None) or [], line_name, product_type
    )

    kpis_out: list[dict] = []
    for (l_name, p_type) in sorted(grouped):
        snaps = grouped[(l_name, p_type)]

        weighted_sum = 0.0
        total_duration = 0.0
        min_inventory = None
        max_inventory = None
        time_without_stock_s = 0.0
        stockouts = 0
        qty_withdrawn = 0
        qty_replenished = 0

        prev_n_available: Optional[int] = None
        for i, snap in enumerate(snaps):
            n = snap.n_available
            min_inventory = n if min_inventory is None else min(min_inventory, n)
            max_inventory = n if max_inventory is None else max(max_inventory, n)

            segment_end = snaps[i + 1].t if i + 1 < len(snaps) else sim_time_s
            duration = max(0.0, segment_end - snap.t)
            weighted_sum += n * duration
            total_duration += duration
            if n == 0:
                time_without_stock_s += duration

            if prev_n_available == 0 and n > 0:
                stockouts += 1
            prev_n_available = n

            if snap.delta_qty:
                if snap.delta_qty < 0:
                    qty_withdrawn += -snap.delta_qty
                else:
                    qty_replenished += snap.delta_qty

        avg_inventory = (weighted_sum / total_duration) if total_duration > 0 else (
            snaps[0].n_available if snaps else 0
        )

        kpis_out.append({
            "line_name": l_name,
            "product": p_type,
            "avg_inventory": round(avg_inventory, 2),
            "min_inventory": min_inventory if min_inventory is not None else 0,
            "max_inventory": max_inventory if max_inventory is not None else 0,
            "time_without_stock_s": round(time_without_stock_s, 3),
            "stockouts": stockouts,
            "qty_withdrawn": qty_withdrawn,
            "qty_replenished": qty_replenished,
        })

    return {"supermarkets": kpis_out}


# ---------------------------------------------------------------------------
# Combined builder — called directly by api/legacy.py's simulate_mixed(),
# same convention as build_card_flow_payload / build_shortfall_payload /
# build_restmenge_payload (not routed through reports.base's registry).
# ---------------------------------------------------------------------------

def build_pull_inventory_timeline_report(
    menv: "MixedSimEnvironment",
    cfg: "SimConfig",
    line_name: Optional[str] = None,
    product_type: Optional[str] = None,
    **_ignored,
) -> dict:
    """
    Combines build_inventory_timeline_payload() + build_inventory_kpis()
    into the one dict shape api/legacy.py's simulate_mixed() folds into
    its composite response under "inventory_timeline" — mirrors how
    that same function already merges reports.pull.card_flow +
    reports.pull.shortfall by hand; done once here, in this combined
    builder, since both halves share the exact same
    (menv, line_name, product_type) scope and there is no other caller
    needing them kept separate.
    """
    timeline = build_inventory_timeline_payload(
        menv, cfg, line_name=line_name, product_type=product_type
    )
    kpis = build_inventory_kpis(
        menv, cfg, line_name=line_name, product_type=product_type
    )
    return {"timeline": timeline["supermarkets"], "kpis": kpis["supermarkets"]}
