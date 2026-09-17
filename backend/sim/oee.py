"""
sim/oee.py
===========
OEE loss (CombinedProductionLoss), daily per-line draw.

domain.config's "OEE" sheet (domain.config.OEEBinConfig,
SimConfig.oee_distribution, _parse_oee_sheet — see that module)
supplies, per line, a table of [Bin_down, Bin_up) ranges each with a
selection Probability, in sheet order. Once a sim-day,
sample_combined_production_loss() below draws one CombinedProductionLoss
value per line from that table; OEELossTracker holds "today's value per
line" (plus which day it was drawn for, so a mid-day order read never
itself triggers a re-draw); oee_daily_process() is the SimPy process
that calls draw_for_day() once per sim-day and owns/appends to an
externally-visible OEELossDrawEntry log — same "state-holder only
mutates state, caller owns the log" split
sim.fill.push.exotic.ExoticSupermarketTracker uses for
ExoticSlotSnapshot.

Callers: sim.runner.run_mixed() constructs one OEELossTracker, attaches
it onto kenv (kenv.oee_tracker) rather than threading it through as a
sim.produce.run_order.run_one_order() parameter — so run_one_order can
read sim_env.oee_tracker.get_current(line_name) directly — and spawns
oee_daily_process() as one of the run's top-level SimPy processes. See
OEELossTracker's own docstring for why the tracker itself never touches
the externally-owned log.

Purely additive: an older workbook with no "OEE" sheet parses to an
empty oee_distribution, so oee_daily_process below simply has nothing
to draw for any line and oee_tracker.get_current() reads None
everywhere — same fallback spirit as the Kanban/Shifts sheets
elsewhere in this codebase.
"""

from __future__ import annotations

import random
from typing import Optional, TYPE_CHECKING

from domain.config import OEEBinConfig
from telemetry.records import OEELossDrawEntry

if TYPE_CHECKING:
    from sim.resources.environment import KanbanSimEnvironment


def sample_combined_production_loss(
    line_name: str,
    bins: list[OEEBinConfig],
    random_percentile: float,
    random_value: float,
) -> float:
    """
    Draw one CombinedProductionLoss value for `line_name` from its OEE bin
    table, via a two-stage random process.

    Stage 1 — bin selection: walk `bins` (domain.config.OEEBinConfig rows
    for this line, in sheet order) building a running cumulative
    Probability sum; the first bin whose cumulative sum, normalized
    against the line's ACTUAL total (not assumed to be exactly 1.0 — the
    sample workbook's own per-line totals land at 0.9999/1.0001, not
    exactly 1.0), exceeds `random_percentile` is the selected bin. A
    probability-0 bin contributes a zero-width slice of that cumulative
    range, so it can never be the one `random_percentile` lands in — no
    special-case skip needed to keep a 0-probability row unselectable.

    Stage 2 — value selection: once a bin [Bin_down, Bin_up) is selected,
    `random_value` places a uniformly distributed value inside it:
    `Bin_down + random_value * (Bin_up - Bin_down)`. Probabilities only
    ever decide WHICH bin; there is no additional distribution inside one.

    `random_percentile`/`random_value` are two INDEPENDENT draws in
    [0, 1), passed in rather than generated here — callers own the RNG
    (OEELossTracker.draw_for_day, below), which keeps this function
    itself trivially deterministic/testable given fixed inputs.
    """
    if not bins:
        raise ValueError(f"no OEE bins configured for line {line_name!r}")

    total = sum(b.probability for b in bins)
    if total <= 0:
        raise ValueError(
            f"line {line_name!r}'s OEE bin probabilities sum to {total}; "
            "cannot select a bin"
        )

    cumulative = 0.0
    for b in bins:
        cumulative += b.probability
        if random_percentile < cumulative / total:
            selected = b
            break
    else:
        # Float-rounding safety net only (e.g. random_percentile very near
        # 1.0 landing past the last edge by epsilon) — the loop above
        # always selects a bin in the normal case.
        selected = bins[-1]

    return selected.bin_down + random_value * (selected.bin_up - selected.bin_down)


class OEELossTracker:
    """
    Per-line "today's CombinedProductionLoss" state-holder.

    Plain state-holder, matching ExoticSupermarketTracker's own scope: it
    only ever mutates its own state (draw_for_day()) or reports it
    (get_current()/is_stale()) — it never appends to a log itself. The
    caller (oee_daily_process, below) owns and appends to an
    externally-visible OEELossDrawEntry log every time it actually draws
    a new value, mirroring the "caller owns the log" split
    ExoticSupermarketTracker uses for ExoticSlotSnapshot.

    Two bits of state per line, kept in sync by draw_for_day():
      - `current_value[line]`  — today's CombinedProductionLoss.
      - `current_day[line]`    — which sim-day index that value was drawn
        for, so a mid-day order (via get_current()) never itself
        triggers a re-draw, and so the daily process (via is_stale())
        knows whether today's line still needs rolling over.

    A line absent from `oee_distribution` (no rows on the "OEE" sheet for
    it) simply never gets an entry in either dict — get_current() returns
    None for it, exactly like "no OEE sheet at all" — purely additive,
    same fallback spirit as the Kanban/Shifts sheets elsewhere in this
    codebase.
    """

    def __init__(self, oee_distribution: dict[str, list[OEEBinConfig]]):
        self.oee_distribution: dict[str, list[OEEBinConfig]] = oee_distribution
        self.current_value: dict[str, float] = {}
        self.current_day: dict[str, int] = {}

    def is_stale(self, line_name: str, day_index: int) -> bool:
        """
        True if `line_name` has never been drawn for, or its stored value
        was drawn for a different day than `day_index` — the daily
        process's own rollover check.
        """
        return self.current_day.get(line_name) != day_index

    def draw_for_day(
        self, line_name: str, day_index: int, rng: random.Random,
    ) -> Optional[float]:
        """
        Draw and store `line_name`'s CombinedProductionLoss for
        `day_index`, via two independent draws off `rng`. Returns the
        drawn value, or None (no-op — nothing stored) if this line has no
        configured OEE bins, so callers can skip logging cleanly instead
        of special-casing an unconfigured line themselves.
        """
        bins = self.oee_distribution.get(line_name)
        if not bins:
            return None
        value = sample_combined_production_loss(
            line_name, bins, rng.random(), rng.random(),
        )
        self.current_value[line_name] = value
        self.current_day[line_name] = day_index
        return value

    def get_current(self, line_name: str) -> Optional[float]:
        """
        Read-only: `line_name`'s already-drawn CombinedProductionLoss for
        whatever day it was last drawn for, or None if it's never been
        drawn (no OEE sheet, or this line has no rows on it). Never
        triggers a draw itself — see class docstring.
        """
        return self.current_value.get(line_name)


def oee_daily_process(
    kenv: "KanbanSimEnvironment",
    tracker: OEELossTracker,
    log: list[OEELossDrawEntry],
    day_length_s: float,
    rng: random.Random,
):
    """
    SimPy process — rolls every configured line's OEELossTracker entry
    over once per sim-day (every `day_length_s`, starting at t=0 so a
    value is already in place before any order can run), appending one
    OEELossDrawEntry per line actually drawn into the externally-owned
    `log` this call (see OEELossTracker's own docstring for why the
    tracker itself never touches this log).

    Lines with no OEE bins configured are silently skipped each day
    (draw_for_day() returns None for them) rather than logged as
    no-op entries.
    """
    env = kenv.env
    day_index = 0
    while True:
        for line in kenv.lines:
            value = tracker.draw_for_day(line.line_name, day_index, rng)
            if value is not None:
                log.append(OEELossDrawEntry(
                    t=env.now, day_index=day_index, line=line.line_name,
                    combined_production_loss=value,
                ))
        day_index += 1
        yield env.timeout(day_length_s)
