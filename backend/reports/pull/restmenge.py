"""
reports/pull/restmenge.py
===========================
Restmenge (leftover-pieces) report — build_restmenge_payload(). Returns
a plain dict rather than printing, since reports/ modules read
domain+telemetry only and produce JSON-ready data; a caller that wants
a console dump (e.g. sim/entrypoints/cli_mixed.py) should call this and
print the result itself.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from sim.resources.environment import MixedSimEnvironment


def build_restmenge_payload(menv: "MixedSimEnvironment") -> dict[tuple[str, str], int]:
    """
    Per (line, product), the count of finished pieces that PASSED
    inspection but hadn't yet accumulated to a full `card_size` chunk
    when the run ended — i.e. SupermarketResource.pcs_partial (a
    partial pack can never be withdrawn on its own; it carries over and
    is topped up by the next production run's deposit_finished_pcs()
    calls).

    Enumeration: menv.supermarkets is dict[line_id ->
    dict[product_type -> SupermarketResource]], so a plain nested
    iteration covers every (line, product) Supermarket that
    sim.resources.build.build_mixed_environment() built.

    Returns a plain dict, keyed (line_name, product_type) -> pcs_partial.
    Callers that want the API-response list-of-dicts shape (e.g.
    api/legacy.py's `restmenge` key) convert this dict's items
    themselves — see api/legacy.py.
    """
    restmenge: dict[tuple[str, str], int] = {}
    for line in menv.lines:
        line_id = line.line_id
        lane = menv.supermarkets.get(line_id, {})
        for product_type, sm in sorted(lane.items()):
            if sm.pcs_partial > 0:
                restmenge[(line.line_name, product_type)] = sm.pcs_partial
    return restmenge
