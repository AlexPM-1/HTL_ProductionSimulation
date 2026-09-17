"""
reports/movement/trace.py
============================
Per-piece (Part) and per-card (KanbanCard) movement traces for the
frontend's Movement Simulation page. Complements reports/all/gantt.py
(job/setup Gantt segments, at package level) and reports/pull/card_flow.py
(card-level census + transitions) with a part-level trace: for each Part,
when it entered/left each station it actually visited (Beladen, Stift
Einpressen, Lochfilter Pruefen, Supfina, Waschen, Inspect).

Every function below is used as a building block by
reports/movement/frames.py, reports/movement/supermarket.py,
reports/movement/collector.py, reports/movement/collection_box.py and
reports/movement/transit.py.

STATION COVERAGE: the 4 stations every part passes through (Beladen,
Supfina, Waschen, Inspect) plus 2 optional stations that only some
products/lines route through (Stift Einpressen, Lochfilter Pruefen).
A part that never visits an optional station simply has 0.0 for both
its start/end attrs, same as the "scrapped early" case below — so it's
naturally skipped rather than needing separate handling. FIFO chute
wait time and DRS are still NOT covered — Part has no fields for them
yet; extending this needs sim/produce/routing.py to see where those
operations run and whether it's safe to add e.g. Part.t_drs_start/end
there, mirroring the existing fields.

Works unmodified for BOTH push (SimEnvironment) and Kanban
(KanbanSimEnvironment, a subclass) runs, since Part / parts_out /
parts_in_wip / next_part_id are all defined once on SimEnvironment in
sim/resources/environment.py and simply inherited by
KanbanSimEnvironment.
"""

from __future__ import annotations

from typing import Iterable, Literal, Optional

TimeUnit = Literal["h", "min", "s"]
_DIVISORS = {"h": 3600.0, "min": 60.0, "s": 1.0}

# (station label, Part start-attr, Part end-attr) — in process order.
# "Stift Einpressen" and "Lochfilter Pruefen" are OPTIONAL: only some
# products/lines route through them, so most parts have 0.0 for both
# their start/end attrs and _raw_part_stations() below skips them —
# same "not visited" handling as a part scrapped before reaching a
# later mandatory station.
_STATION_FIELDS = [
    ("Beladen", "t_beladen_start", "t_beladen_end"),
    ("Stift Einpressen", "t_stift_einpressen_start", "t_stift_einpressen_end"),
    ("Lochfilter Pruefen", "t_pruefen_start", "t_pruefen_end"),
    ("Supfina", "t_supfina_start", "t_supfina_end"),
    ("Waschen", "t_waschen_start", "t_waschen_end"),
    ("Sichtpr\u00fcfung", "t_sichtpruefung_start", "t_sichtpruefung_end"),
]


def _raw_part_stations(part) -> list[dict]:
    """
    One entry per station this part actually visited, in RAW sim-clock
    seconds (no time_unit conversion, no offset yet — see
    build_part_trace_payload). A part scrapped early (e.g. at Beladen)
    never reaches later stations, so their t_*_start/end stay at the
    dataclass default 0.0 — skipped rather than emitted as a bogus
    zero-length visit at t=0.
    """
    out = []
    for label, start_attr, end_attr in _STATION_FIELDS:
        start_s = getattr(part, start_attr, 0.0) or 0.0
        end_s = getattr(part, end_attr, 0.0) or 0.0
        if start_s <= 0.0 and end_s <= 0.0:
            continue
        out.append({"name": label, "enter_t": start_s, "exit_t": end_s})
    return out


def build_part_trace_payload(
    sim_env,
    time_unit: TimeUnit = "h",
    include_wip: bool = False,
    line_id: Optional[int] = None,
    day_window_s: Optional[tuple[float, float]] = None,
    time_offset_s: float = 0.0,
) -> dict:
    """
    sim_env: any object exposing .parts_out (list[Part]) and, if
    include_wip=True, .parts_in_wip — i.e. a SimEnvironment or
    KanbanSimEnvironment.

    line_id: filter to one line (1-indexed, matches Part.line_id), or
    None for all lines.

    day_window_s: optional (start_s, end_s) in RAW sim-clock seconds
    (BEFORE time_offset_s is applied) — parts whose t_created falls
    outside [start_s, end_s) are dropped. Use this to restrict a
    multi-day push run to one calendar day, e.g.
    (day_idx*DAY_SECONDS, (day_idx+1)*DAY_SECONDS). Leave None for
    a single-day/Kanban run — no filtering needed.

    time_offset_s: added to every raw timestamp before dividing by the
    time_unit's divisor. For stitching several single-day push runs
    onto one continuous axis (offset = day_idx * DAY_SECONDS). Leave at
    0.0 for a run that's already on its own single clock (Kanban, or a
    single day_window_s slice you don't want re-based to 0).
    """
    if time_unit not in _DIVISORS:
        raise ValueError(f"time_unit must be one of {list(_DIVISORS)}, got {time_unit!r}")
    divisor = _DIVISORS[time_unit]

    parts: list = list(sim_env.parts_out)
    if include_wip:
        parts = parts + list(sim_env.parts_in_wip)

    out_parts = []
    for part in parts:
        if line_id is not None and getattr(part, "line_id", None) != line_id:
            continue
        if day_window_s is not None:
            lo, hi = day_window_s
            t_created = getattr(part, "t_created", 0.0)
            if not (lo <= t_created < hi):
                continue

        stations = []
        for st in _raw_part_stations(part):
            stations.append({
                "name": st["name"],
                "enter_t": round((st["enter_t"] + time_offset_s) / divisor, 4),
                "exit_t": round((st["exit_t"] + time_offset_s) / divisor, 4),
            })

        t_exit = getattr(part, "t_exit", 0.0) or 0.0
        out_parts.append({
            "part_id": part.part_id,
            "product_type": part.product_type,
            "product_class": getattr(part, "product_class", None),
            "line_id": part.line_id,
            "status": part.status,
            "rework_pass": part.rework_pass,
            "t_created": round((part.t_created + time_offset_s) / divisor, 4),
            "t_exit": round((t_exit + time_offset_s) / divisor, 4) if t_exit > 0 else None,
            "stations": stations,
        })

    return {"time_unit": time_unit, "parts": out_parts}


def build_part_ids_payload(sim_env, line_id: Optional[int] = None) -> dict:
    """
    Cheap companion to build_part_trace_payload(): just IDs + product +
    status, to populate a "select a piece" dropdown without shipping
    every part's full station trace up front.
    """
    ids = []
    for part in sim_env.parts_out:
        if line_id is not None and getattr(part, "line_id", None) != line_id:
            continue
        ids.append({
            "part_id": part.part_id,
            "product_type": part.product_type,
            "product_class": getattr(part, "product_class", None),
            "line_id": part.line_id,
            "status": part.status,
        })
    return {"parts": ids}


def build_card_trace_payload(
    kenv,
    time_unit: TimeUnit = "h",
    card_id: Optional[int] = None,
    line_id: Optional[int] = None,
    day_window_s: Optional[tuple[float, float]] = None,
    time_offset_s: float = 0.0,
) -> dict:
    """
    Per-card transition history straight off kenv.card_registry — a
    lighter, filterable sibling to
    reports.pull.card_flow.build_card_flow_payload(), which also computes
    an expensive plant-wide census (n_bins samples across every card) on
    every call. Use THIS for the movement page (per-card animation); keep
    using build_card_flow_payload() for the Card Flow tab's stacked-area
    chart.

    kenv: a KanbanSimEnvironment — needs .card_registry
    (dict[card_id -> KanbanCard], each with .transitions: list[(state,
    t)] in chronological raw sim-clock seconds).

    day_window_s: optional (start_s, end_s) in RAW sim-clock seconds. A
    card is included only if it has a transition at or before day end
    (i.e. it existed by then). Its transitions list is trimmed to: the
    single latest transition strictly before start_s (relabeled to
    start_s, so the card's position at the top of the window is known —
    same "current state as of time t = last transition at or before t"
    rule build_card_flow_payload() uses) PLUS every transition that
    actually falls inside [start_s, end_s). Transitions at/after end_s
    are dropped.

    time_offset_s: added to every raw second before dividing by the
    time_unit divisor — mirrors build_part_trace_payload's parameter,
    for future multi-day stitching if ever needed. 0.0 is correct for a
    single Kanban run (which is already on one continuous clock).
    """
    if time_unit not in _DIVISORS:
        raise ValueError(f"time_unit must be one of {list(_DIVISORS)}, got {time_unit!r}")
    divisor = _DIVISORS[time_unit]

    out_cards = []
    for cid, card in kenv.card_registry.items():
        if card_id is not None and cid != card_id:
            continue
        c_line_id = getattr(card, "line_id", None)
        if line_id is not None and c_line_id != line_id:
            continue

        transitions = list(card.transitions)  # [(state, t)], chronological

        if day_window_s is not None:
            lo, hi = day_window_s
            carry_in_state = None
            in_window: list[tuple[str, float]] = []
            for state, t in transitions:
                if t < lo:
                    carry_in_state = state  # keep overwriting -> ends on the LAST one before lo
                elif t < hi:
                    in_window.append((state, t))
                else:
                    break  # transitions is chronological, nothing further matters
            if carry_in_state is None and not in_window:
                continue  # card has no presence at all in/by this window
            transitions = ([(carry_in_state, lo)] if carry_in_state is not None else []) + in_window

        if not transitions:
            continue

        out_cards.append({
            "card_id": cid,
            "product_type": getattr(card, "product_type", None),
            "line_id": c_line_id,
            "transitions": [
                {"state": s, "t": round((t + time_offset_s) / divisor, 4)}
                for s, t in transitions
            ],
        })

    return {"time_unit": time_unit, "cards": out_cards}


# ===========================================================================
# Per-card "where is it right now" resolution
# ===========================================================================
#
# build_card_trace_payload() above gives a card's full transition list;
# build_part_trace_payload() gives a part's station enter/exit times. Both
# answer "what happened over the whole run". The Movement Simulation's
# spatial Plant Layout view (movement_layout.js) instead needs "at THIS
# instant, which physical box/slot is each card sitting in" — the two
# functions below are the card-side building block for that.
#
# Deliberately NOT resolved all the way down to a physical row/slot here:
# that needs the Supermarket/BatchCollector row-capacity layout from
# SimConfig (cfg.supermarkets, cfg.kanban_cards), and this module has no
# import-time dependency on domain.config by design (see the module
# docstring's "Works unmodified for BOTH push ... and Kanban ..." framing
# — it only ever touches attributes on whatever sim_env/kenv object it's
# handed). reports.movement.frames.build_movement_state_payload() (via
# reports.movement.supermarket / collector / collection_box / chute) is
# the config-aware caller that turns this module's per-card groups into
# actual row/slot placements.

def card_state_at(card, t_s: float) -> tuple[str, float]:
    """
    (state, since_t) — the state `card` was in, and the raw sim-clock time
    it entered that state, as of time t_s. This is the last entry in
    card.transitions with t <= t_s (the same "last reading at or before
    this instant" convention reports.binning.state_at uses for
    Supermarket snapshots).

    A KanbanCard's `transitions` list always has at least one entry —
    KanbanSimEnvironment.create_card() records "in_supermarket" at
    creation time before the card is ever handed back to a caller — so
    this never needs a sentinel/None return, even for t_s before the
    card existed (it just reports the creation state/time, which is
    harmless: a caller windowing to a period before creation should
    filter the card out itself, e.g. via day_window_s elsewhere in this
    module, not rely on this function to hide it).
    """
    state, since = card.transitions[0][0], card.transitions[0][1]
    for s, t in card.transitions:
        if t > t_s:
            break
        state, since = s, t
    return state, since


def build_movement_frame_cards(
    kenv,
    t_s: float,
    line_id: Optional[int] = None,
) -> dict[tuple[int, str, str], list[dict]]:
    """
    One "frame" of card positions at raw sim-clock time t_s: every card in
    kenv.card_registry, grouped by (line_id, state, product_type), each
    group's cards ordered OLDEST-arrival-into-that-state first (i.e. by
    the timestamp returned alongside its state from card_state_at(), then
    card_id as a tiebreak for a same-instant tie).

    That ordering matters: it's the FIFO order a physical
    Supermarket/BatchCollector/CollectionBox slot-filling approximation
    should assign into row/slot position 0, 1, 2, ... — the same
    "first-fill by index" convention
    reports.all.supermarket_series.build_supermarket_state_payload
    already uses for its per-row COUNTS; here it's applied per-card, so
    an actual card_id lands at each derived slot instead of just
    incrementing a number. See reports.movement.frames.
    build_movement_state_payload for that row/slot resolution step,
    which is the intended (and, short of duplicating SimConfig's row
    layout in this module, the only sensible) consumer of this
    function's output.

    kenv: a KanbanSimEnvironment — needs .card_registry (see
    build_card_trace_payload's docstring for its shape).

    Returns
    -------
    {(line_id, state, product_type): [{"card_id": int, "since_t": float}, ...]}
    Every card currently in kenv.card_registry is present in exactly one
    group (a card always has a state), INCLUDING states with no Step-1
    Plant Layout box of their own ("withdrawn": between Supermarket and
    Collection Box; "in_production": between Chute release and the next
    "in_supermarket" transition) — callers should decide how to render
    those (e.g. an "in transit" marker) rather than assume every state
    key maps onto a drawn box.
    """
    groups: dict[tuple[int, str, str], list[tuple[int, float]]] = {}
    for card in kenv.card_registry.values():
        if line_id is not None and card.line_id != line_id:
            continue
        state, since = card_state_at(card, t_s)
        groups.setdefault((card.line_id, state, card.product_type), []).append(
            (card.card_id, since)
        )
    for items in groups.values():
        items.sort(key=lambda x: (x[1], x[0]))
    return {
        key: [{"card_id": cid, "since_t": since} for cid, since in items]
        for key, items in groups.items()
    }
