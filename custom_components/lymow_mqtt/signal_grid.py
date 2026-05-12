"""Signal-quality heat-map accumulator.

Bins (pose.x, pose.y) samples into coarse spatial cells and tracks an EWMA
of horizontal_accuracy / position_quality / wifi_signal / lte_signal per
cell. The point is to build up a spatial picture of where RTK / cellular
signal is good vs. bad across the property — feeds the signal-map camera
entity.

Design choices:

- **EWMA per cell, not a sample list.** Storing aggregates instead of raw
  samples keeps the grid bounded by yard footprint (a 100m×100m yard at
  0.5m cells is at most 40,000 cells). No retention scheduling needed —
  the math IS the retention. With ``alpha = 0.1`` the EWMA effectively
  weights the last ~10 samples; older signal-quality readings fade as
  new ones land.
- **Pure data layer, no HA imports.** Importable into unit tests without
  spinning up the integration. Persistence (Store API) and sampling
  (pboutput hook) live in the coordinator.
- **All four metrics accumulated, even if v1 only renders one.** Cheap to
  store, expensive to retrofit if we change our minds — and the user
  always has the option to surface new heat layers without re-mowing the
  whole property.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

# Smoothing factor for the per-cell EWMA.
#
#   new_value = old_value * (1 - alpha) + sample * alpha
#
# With alpha=0.1, the most recent ~10 samples dominate the value (half-life
# ~6.6 samples). Tune here if the heat map looks too noisy or too lagged.
EWMA_ALPHA = 0.1

# Side length of one cell in mower-local meters. 0.5 m is fine enough to
# resolve walking-pattern detail without exploding the cell count.
CELL_M = 0.5

# Persisted-blob format version. Bump if `to_dict()` / `from_dict()`
# semantics change incompatibly.
GRID_SCHEMA_VERSION = 1


def _ewma(prev: float | None, sample: float) -> float:
    """Update an EWMA — first sample becomes the value verbatim."""
    if prev is None:
        return float(sample)
    return prev * (1.0 - EWMA_ALPHA) + float(sample) * EWMA_ALPHA


def cell_key(x: float, y: float) -> tuple[int, int]:
    """Map a world-frame (x, y) to its cell key.

    Cell (cx, cy) covers world coordinates
    [cx*CELL_M, (cx+1)*CELL_M) x [cy*CELL_M, (cy+1)*CELL_M).
    """
    return (int(x // CELL_M), int(y // CELL_M))


@dataclass
class GridCell:
    """One cell of the signal grid.

    All metric values are EWMA-smoothed and may be None if that particular
    metric has never been observed in this cell (e.g. cellular dropped out
    for a stretch of mowing).
    """

    n: int = 0
    horizontal_accuracy: float | None = None   # meters; lower = better
    position_quality:    float | None = None   # 0..3 (LocalQuality enum); higher = better
    wifi_signal:         float | None = None   # dBm; less-negative = better
    lte_signal:          float | None = None   # dBm; less-negative = better

    def update(
        self,
        *,
        horizontal_accuracy: float | None = None,
        position_quality: float | int | None = None,
        wifi_signal: float | int | None = None,
        lte_signal: float | int | None = None,
    ) -> None:
        self.n += 1
        if horizontal_accuracy is not None:
            self.horizontal_accuracy = _ewma(self.horizontal_accuracy, horizontal_accuracy)
        if position_quality is not None:
            self.position_quality = _ewma(self.position_quality, position_quality)
        if wifi_signal is not None:
            self.wifi_signal = _ewma(self.wifi_signal, wifi_signal)
        if lte_signal is not None:
            self.lte_signal = _ewma(self.lte_signal, lte_signal)


class SignalGrid:
    """Cell-keyed accumulator over all four signal-quality metrics.

    Designed to be persisted as a single JSON blob via HA's Store API.
    `to_dict` / `from_dict` round-trip cleanly; tuple keys are
    string-encoded ("cx,cy") so the blob is valid JSON.
    """

    def __init__(self) -> None:
        self._cells: dict[tuple[int, int], GridCell] = {}

    # ── recording ───────────────────────────────────────────────────────
    def record(
        self,
        x: float,
        y: float,
        *,
        horizontal_accuracy: float | None = None,
        position_quality: float | int | None = None,
        wifi_signal: float | int | None = None,
        lte_signal: float | int | None = None,
    ) -> None:
        """Apply one (pose, signal) sample to the appropriate cell.

        If all four metric values are None the call is a no-op — there's
        nothing to fold in. (The caller doesn't always have all four; e.g.
        cellular may be off, RTK may not have fixed yet.)
        """
        if (
            horizontal_accuracy is None
            and position_quality is None
            and wifi_signal is None
            and lte_signal is None
        ):
            return
        key = cell_key(x, y)
        cell = self._cells.get(key)
        if cell is None:
            cell = GridCell()
            self._cells[key] = cell
        cell.update(
            horizontal_accuracy=horizontal_accuracy,
            position_quality=position_quality,
            wifi_signal=wifi_signal,
            lte_signal=lte_signal,
        )

    # ── inspection ──────────────────────────────────────────────────────
    def cells(self) -> dict[tuple[int, int], GridCell]:
        """Read-only(ish) view of the underlying cell dict."""
        return self._cells

    def __len__(self) -> int:
        return len(self._cells)

    # ── persistence ─────────────────────────────────────────────────────
    def to_dict(self) -> dict[str, Any]:
        """Serialize to a JSON-safe dict (string keys, no tuples)."""
        return {
            "version": GRID_SCHEMA_VERSION,
            "cells": {
                f"{cx},{cy}": {
                    "n": c.n,
                    "ha": c.horizontal_accuracy,
                    "pq": c.position_quality,
                    "wifi": c.wifi_signal,
                    "lte": c.lte_signal,
                }
                for (cx, cy), c in self._cells.items()
            },
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "SignalGrid":
        """Inverse of `to_dict` — tolerant of partial / corrupt cells."""
        grid = cls()
        if not isinstance(data, dict):
            return grid
        for key_str, raw in (data.get("cells") or {}).items():
            try:
                cx_s, cy_s = key_str.split(",")
                key = (int(cx_s), int(cy_s))
            except (ValueError, AttributeError):
                continue
            if not isinstance(raw, dict):
                continue
            grid._cells[key] = GridCell(
                n=int(raw.get("n") or 0),
                horizontal_accuracy=_as_float_or_none(raw.get("ha")),
                position_quality=_as_float_or_none(raw.get("pq")),
                wifi_signal=_as_float_or_none(raw.get("wifi")),
                lte_signal=_as_float_or_none(raw.get("lte")),
            )
        return grid


def _as_float_or_none(v: Any) -> float | None:
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None
