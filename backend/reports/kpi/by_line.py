"""
reports/kpi/by_line.py
=========================
Per-line KPI summary: line_kpi_summary(), print_kpi(), and
build_kpi_by_line() (the {line_name: line_kpi_summary(...)} map used by
api/routes_simulate.py).

build_kpi_by_line combines passed, scrapped, reworked, total_created,
station_utilisation, buffer_max_fill, mean_cycle_time_s across BOTH
class-1 (pull) and class-2 (push) parts together, because Part carries
no class flag of its own — station/buffer stats are genuinely shared
physical resources anyway, so a per-class split of those two fields
wouldn't mean anything different from the combined figure. See
reports/kpi/by_class.py for the pull/push-split counterpart.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from sim.resources.environment import SimEnvironment


def line_kpi_summary(sim_env: "SimEnvironment", line_id: int, sim_time: float) -> dict:
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


def print_kpi(sim_env: "SimEnvironment", line_id: int, sim_time: float) -> None:
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


def build_kpi_by_line(kenv) -> dict:
    """{line_name: line_kpi_summary(kenv, line_id, kenv.env.now)} for
    every line in kenv.lines. Called by api/routes_simulate.py's
    simulate_mixed()."""
    return {
        line.line_name: line_kpi_summary(kenv, line.line_id, kenv.env.now)
        for line in kenv.lines
    }
