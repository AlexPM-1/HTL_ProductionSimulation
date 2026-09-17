"""
sim/entrypoints/cli_mixed.py
==============================
Command-line entrypoint for a mixed (class-1 Kanban + class-2 push) run.
Calls sim.runner.run_mixed().

    python -m sim.entrypoints.cli_mixed [excel_path] [setup_times_path] [n_workers] [n_crews]

All four positional arguments are optional:
excel_path -> "domain/ProductionPlanning_v6.xlsx",
setup_times_path -> "domain/HTL_setup_times.xlsx",
product_master_path -> "domain/product_master.xlsx",
n_workers -> domain.constants.N_WORKERS, n_crews -> 2.
"""

from __future__ import annotations

import sys

from domain.constants import N_WORKERS
from sim.runner import run_mixed


def main(argv: list[str] = None) -> None:
    argv = sys.argv if argv is None else argv
    excel = argv[1] if len(argv) > 1 else "domain/ProductionPlanning_v6.xlsx"
    setup = argv[2] if len(argv) > 2 else "domain/HTL_setup_times.xlsx"
    product_master = argv[3] if len(argv) > 2 else "domain/product_master.xlsx"
    workers = int(argv[4]) if len(argv) > 3 else N_WORKERS
    crews = int(argv[5]) if len(argv) > 4 else 2
    run_mixed(excel, setup, product_master, n_workers=workers, n_crews=crews)


if __name__ == "__main__":
    main()
