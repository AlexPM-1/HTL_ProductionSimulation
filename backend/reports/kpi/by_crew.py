"""
reports/kpi/by_crew.py
========================
Per-crew production summary — build_kpi_by_crew(). Complements
reports/kpi/by_line.py and reports/kpi/by_class.py with a per-crew
breakdown, sourced from kenv.gate_activity_log.
"""

from __future__ import annotations


def build_kpi_by_crew(kenv) -> dict:
    """
    Per-crew production summary — the "production of crew 1 / production
    of crew 2" counterpart to kpi_by_line/kpi_by_class, built from
    kenv.gate_activity_log (every GateActivityEntry carries crew_id)
    rather than from kenv.parts_out, since a GateActivityEntry already IS
    one crew's one completed turn-unit (one pull card, or one push chunk)
    — no product-set membership lookup needed, unlike kpi_by_class.

    Reads kenv.n_crews (set by sim.runner.run_mixed(n_crews=...)) so a crew that
    happened to never get any work (e.g. more crews configured than lines
    exist) still gets a zeroed-out entry, instead of silently disappearing
    the way inferring crew count from "which crew_ids appear in the log"
    would.

    Shape, keyed "crew_1", "crew_2", ... (1-based label, matching how
    line_name keys already read to a frontend — crew_id itself, 0-based,
    is included inside each entry for anything that needs the raw index):
        {
          "crew_1": {
            "crew_id": 0,
            "pull": {"n_units": int, "total_quantity": int},
            "push": {"n_units": int, "total_quantity": int},
            "n_units_total": int,
            "total_quantity_total": int,
            "lines_worked": [line_name, ...],   # sorted, distinct
          },
          ...
        }
    """
    gate_log = getattr(kenv, "gate_activity_log", None) or []
    n_crews = getattr(kenv, "n_crews", None)
    if n_crews is None:
        # Defensive fallback for a kenv from a run predating kenv.n_crews
        # being set — infer from whatever crew_ids actually appear rather
        # than 500ing (same "degrade gracefully" spirit as the other
        # getattr-guarded push artifacts in this package).
        seen = {getattr(e, "crew_id", None) for e in gate_log}
        seen.discard(None)
        n_crews = (max(seen) + 1) if seen else 0

    id_to_name = {line.line_id: line.line_name for line in kenv.lines}

    result: dict = {}
    for crew_id in range(n_crews):
        entries = [e for e in gate_log if getattr(e, "crew_id", None) == crew_id]

        def _bucket(production_type: str) -> dict:
            subset = [e for e in entries if e.production_type == production_type]
            return {
                "n_units": len(subset),
                "total_quantity": sum(e.quantity or 0 for e in subset),
            }

        result[f"crew_{crew_id + 1}"] = {
            "crew_id": crew_id,
            "pull": _bucket("pull"),
            "push": _bucket("push"),
            "n_units_total": len(entries),
            "total_quantity_total": sum(e.quantity or 0 for e in entries),
            "lines_worked": sorted({
                id_to_name.get(e.line_id, str(e.line_id)) for e in entries
            }),
        }
    return result


# Private alias for call sites that import _build_kpi_by_crew directly.
_build_kpi_by_crew = build_kpi_by_crew
