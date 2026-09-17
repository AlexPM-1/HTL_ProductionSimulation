"""
domain/timeparse.py
====================
Timestamp parsing shared by push (CustomerDemand) and pull
(CustomerDemandKanban) event rows.

`row_due_datetime` and `row_datetime_string` both combine a row's separate
Date/Time columns, but stay separate because their callers want different
shapes:
  - `row_due_datetime`: used by sim.fill.push.dispatch to get a push
    order's due date directly as a datetime.
  - `row_datetime_string`: used by domain.epoch (epoch/horizon
    estimation), which calls `parse_datetime` on the result itself.
"""

from __future__ import annotations

import re
from datetime import date, datetime
from typing import Optional

# ---------------------------------------------------------------------------
# Bare date parsing
# ---------------------------------------------------------------------------
_DATE_RE = re.compile(r"^(\d{1,2})\.(\d{1,2})\.(\d{4})")


def parse_date_only(s: str) -> Optional[date]:
    """Parse a bare 'DD.MM.YYYY' (or 'DD.MM.YYYY ...') cell into a date."""
    if not isinstance(s, str):
        return None
    m = _DATE_RE.match(s.strip())
    if not m:
        return None
    d, mo, y = (int(x) for x in m.groups())
    try:
        return date(y, mo, d)
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Full timestamp parsing
# ---------------------------------------------------------------------------
_TIMESTAMP_FORMATS: tuple[str, ...] = (
    "%d.%m.%Y %H:%M:%S",
    "%d.%m.%Y %H:%M",
)


def parse_datetime(s: str) -> Optional[datetime]:
    """
    Parse a full 'DD.MM.YYYY HH:MM[:SS]' timestamp cell.

    Returns None for anything else (legacy bare "HH:MM", empty cell, or an
    unparseable value) — callers treat None as "fall back to legacy,
    single-day, fixed-cadence behaviour for this row". Never raises: this
    reads directly off a hand-maintained Excel sheet, so one malformed
    cell degrading gracefully is far preferable to crashing the whole run.
    """
    if s is None:
        return None
    s = str(s).strip()
    if not s:
        return None
    for fmt in _TIMESTAMP_FORMATS:
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    return None


# ---------------------------------------------------------------------------
# Row-level combinators — kept separate; two different sheets/shapes.
# ---------------------------------------------------------------------------

def row_due_datetime(row) -> Optional[datetime]:
    """
    Combine a CustomerDemand row's separate `period_label` (Date) and
    `time_slot` (Time) cells into one real datetime — the row's due date.
    Called by sim.fill.push.dispatch to get a push order's due date.
    """
    period = (row.period_label or "").strip()
    time_s = (getattr(row, "time_slot", "") or "").strip()
    return parse_datetime(f"{period} {time_s}".strip())


def row_datetime_string(evt) -> str:
    """
    Combine one KanbanWithdrawalEvent's separate `date` + `time` cells into
    the single "DD.MM.YYYY HH:MM[:SS]" string `parse_datetime` expects.
    Used by domain.epoch, which calls
    `parse_datetime(row_datetime_string(evt))` itself.
    """
    d = (evt.date or "").strip()
    t = (evt.time or "").strip()
    return f"{d} {t}".strip()
