"""
reports/pull/card_flow.py
==========================
Card Flow tab payload — build_card_flow_payload(). Shortfall data is
computed separately by reports/pull/shortfall.build_shortfall_payload();
callers that need both (e.g. api/legacy.py, reports/serialization.py's
dump_pull_events_json) call both functions and merge the results, so
each report module stays single-purpose.

Dependency-free at runtime: MixedSimEnvironment is only referenced
under TYPE_CHECKING; this module only ever duck-types on
menv.card_registry / menv.lines / PullCard.transitions.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal

from reports.binning import TIME_UNIT_DIVISORS

if TYPE_CHECKING:
    from domain.config import SimConfig  # noqa: F401  (not used directly, kept for parity)
    from sim.resources.environment import MixedSimEnvironment

TimeUnit = Literal["h", "min", "s"]

_DIVISORS = TIME_UNIT_DIVISORS

# Canonical card-state ordering (matches the sequence documented on
# SupermarketSnapshot / the record_transition() call sites in
# sim/produce/run_pull_batch.py and sim/fill/pull/*). Any state
# encountered that isn't in this list is appended afterwards, sorted, so
# the payload never silently drops a state the sim logic gains later.
_CANONICAL_CARD_STATES: list[str] = [
    "withdrawn",
    "in_collection_box",
    "in_batch_collector",
    "released_to_chute",
    "in_production",
    "in_supermarket",
]


def build_card_flow_payload(
    menv: "MixedSimEnvironment",
    n_bins: int | None = None,
    time_unit: TimeUnit = "h",
) -> dict:
    """
    Flatten menv.card_registry into a JSON-ready payload for the
    dashboard's Card Flow tab.

    Three things come out of this (shortfall data is a separate payload,
    see reports/pull/shortfall.build_shortfall_payload):

    1.  "cards" — one entry per card with its full transition history
        (state, t), for drill-down / debugging.
    2.  "series" — a time-bucketed census, PLANT-WIDE: n_bins+1 evenly
        -spaced samples across [0, sim_time_s] (the extra sample is the
        t=0 point itself), each giving the count of cards in every state
        as of that point in the run.
    3.  "series_by_line" — the same census, split out per line. Keyed by
        line_id (as a string, for JSON-object-key safety); "lines" carries
        the (line_id, line_name) pairs actually present, sorted by
        line_id.

    A card's "current state as of time t" is the state of its last
    transition at or before t. n_bins: if None (default), derived from
    sim_time_s so the bin width is always ~15 real-world minutes.

    sim_time_s is read off menv.env.now, so call this AFTER env.run(...)
    returns.
    """
    divisor = _DIVISORS[time_unit]
    sim_time_s = menv.env.now
    if n_bins is None:
        n_bins = max(1, round(sim_time_s / 600.0))

    line_names: dict[int, str] = {
        line.line_id: line.line_name for line in menv.lines
    }

    cards: list[dict] = []
    seen_states: set[str] = set()
    for card_id, card in menv.card_registry.items():
        transitions = [
            {"state": state, "t": round(t / divisor, 4)}
            for state, t in card.transitions
        ]
        seen_states.update(state for state, _ in card.transitions)
        cards.append({
            "card_id": card_id,
            "product_type": getattr(card, "product_type", None),
            "line_id": getattr(card, "line_id", None),
            "transitions": transitions,
        })

    states = [s for s in _CANONICAL_CARD_STATES if s in seen_states]
    states += sorted(seen_states - set(_CANONICAL_CARD_STATES))

    line_ids = sorted({
        getattr(card, "line_id", None) for card in menv.card_registry.values()
        if getattr(card, "line_id", None) is not None
    })

    series: list[dict] = []
    series_by_line: dict[str, list[dict]] = {str(lid): [] for lid in line_ids}
    if n_bins > 0:
        for i in range(0, n_bins + 1):
            edge_s = sim_time_s * i / n_bins
            t_out = round(edge_s / divisor, 4)
            counts = {s: 0 for s in states}
            counts_by_line = {lid: {s: 0 for s in states} for lid in line_ids}
            for card in menv.card_registry.values():
                current_state = None
                for state, t in card.transitions:
                    if t > edge_s:
                        break
                    current_state = state
                if current_state is not None:
                    counts[current_state] = counts.get(current_state, 0) + 1
                    lid = getattr(card, "line_id", None)
                    if lid in counts_by_line:
                        counts_by_line[lid][current_state] += 1
            series.append({"t": t_out, **counts})
            for lid in line_ids:
                series_by_line[str(lid)].append({"t": t_out, **counts_by_line[lid]})

    return {
        "time_unit": time_unit,
        "horizon": round(sim_time_s / divisor, 4),
        "states": states,
        "cards": cards,
        "series": series,
        "series_by_line": series_by_line,
        "lines": [
            {"line_id": lid, "line_name": line_names.get(lid, str(lid))}
            for lid in line_ids
        ],
    }
