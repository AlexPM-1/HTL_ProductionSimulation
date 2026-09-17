"""
domain/constants.py
====================
Module-level knobs shared across the sim.
"""

from __future__ import annotations

import datetime as _dt

# ---------------------------------------------------------------------------
# Simulation start time (t=0). Manually edited for now — will move to the
# frontend later.
# ---------------------------------------------------------------------------
SIM_START: _dt.datetime = _dt.datetime(2026, 9, 1, 6, 0, 0)

# ---------------------------------------------------------------------------
# Calendar / horizon knobs
# ---------------------------------------------------------------------------
DAY_START_HOUR: int = 6
DAY_LENGTH_S: float = 24 * 3600.0
DRAIN_DAYS: int = 1
SIM_HORIZON_S: float = 24 * 3600.0

# ---------------------------------------------------------------------------
# Push chunking
# ---------------------------------------------------------------------------
# Push "unit" size: an OrderRecord larger than this is split into several
# chunks (each run via one run_one_order() call), so class-1 never waits
# longer than one chunk's worth of production. Tune per real card size —
# there's no single right answer without knowing typical push order size
# vs. typical kanban batch_size; starting conservative.
PUSH_CHUNK_SIZE: int = 200

# ---------------------------------------------------------------------------
# Workers / determinism
# ---------------------------------------------------------------------------
N_WORKERS: int = 1
DEFAULT_N_WORKERS: int = N_WORKERS  # alias kept for backward compatibility
SEED: int = 42
