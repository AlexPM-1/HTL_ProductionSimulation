"""
domain/line_priority.py
========================
Pluggable line-selection rule: given a product and the currently active
lines, decide which lines to try and in what order.

`MatrixPriorityStrategy` is the only strategy in use — it reads priority
straight off `domain.products.PRODUCT_MATRIX`. Used by
sim.fill.push.dispatch (push_dispatch_process) to pick candidate lines
for a push order.
"""

from __future__ import annotations

from typing import Optional

from domain.products import Freigabe, ProductLineInfo


class LinePriorityStrategy:
    """
    Base interface for "which lines, in what order, should this product
    be tried on" decisions.

    Subclass this (or duck-type it) to implement more complicated rules
    later without touching the calling dispatcher's own assignment
    machinery — only `order()` needs to change.
    """

    def order(self, info: ProductLineInfo, active_lines: list[str]) -> list[str]:
        """
        Return the lines this product may run on, restricted to
        `active_lines`, ordered from most- to least-preferred.
        An empty list means "no feasible line" (order goes unassigned).
        """
        raise NotImplementedError


class MatrixPriorityStrategy(LinePriorityStrategy):
    """
    Default rule: priority comes from PRODUCT_MATRIX itself, per product.

    - Only lines with Freigabe VORHANDEN carry a real priority
      (LineClass.priority, e.g. "1" / "2" / "3" — 1 = most preferred).
      They are tried first, sorted by that priority ascending.
    - Lines with Freigabe MOEGLICH have no priority by definition — they
      are always tried after every VORHANDEN line, in `active_lines`
      order (stable, deterministic, but not "prioritised" among
      themselves).
    - Lines with Freigabe NICHT_MOEGLICH (or not feasible at all) never
      appear.
    - A VORHANDEN line whose priority field is missing/unparseable is
      still tried before MOEGLICH lines, just after the ones that do
      have a numeric priority (keeps `order()` total even with
      incomplete data instead of silently dropping the line).
    """

    @staticmethod
    def _priority_rank(raw: Optional[str]) -> int:
        """Parse LineClass.priority into a sortable int; unparseable/None
        sorts after every real number but still before MOEGLICH lines."""
        if raw is None:
            return 10_000
        try:
            return int(str(raw).strip())
        except ValueError:
            return 10_000

    def order(self, info: ProductLineInfo, active_lines: list[str]) -> list[str]:
        vorhanden: list[str] = []
        moeglich: list[str] = []

        for ln in active_lines:
            lc = info.lines.get(ln)
            if lc is None or not lc.is_feasible:
                continue  # not present, or NICHT_MOEGLICH
            if lc.freigabe == Freigabe.VORHANDEN:
                vorhanden.append(ln)
            elif lc.freigabe == Freigabe.MOEGLICH:
                moeglich.append(ln)

        vorhanden.sort(key=lambda ln: self._priority_rank(info.lines[ln].priority))

        # moeglich keeps active_lines order (no priority concept applies)
        return vorhanden + moeglich
