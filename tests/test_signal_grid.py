"""Tests for the signal-quality heat-map accumulator."""
from __future__ import annotations

import pytest

from lymow_mqtt import signal_grid as sg


class TestCellKey:
    """Cell-key tests written against ``sg.CELL_M`` rather than a hard-coded
    value so tweaks to the grid resolution don't drag the tests along.
    """

    def test_origin_maps_to_zero_zero(self):
        assert sg.cell_key(0.0, 0.0) == (0, 0)

    def test_positive_values_floor_into_cell(self):
        # x in [0, CELL_M) → cx=0
        assert sg.cell_key(0.5 * sg.CELL_M, 0.5 * sg.CELL_M) == (0, 0)
        # x in [CELL_M, 2*CELL_M) → cx=1
        assert sg.cell_key(sg.CELL_M, sg.CELL_M) == (1, 1)
        assert sg.cell_key(1.5 * sg.CELL_M, 1.5 * sg.CELL_M) == (1, 1)
        # x in [2*CELL_M, 3*CELL_M) → cx=2
        assert sg.cell_key(2.5 * sg.CELL_M, 2.0 * sg.CELL_M) == (2, 2)

    def test_negative_values_floor_correctly(self):
        # Python's // floors toward -inf, which is what we want — the cell
        # spanning [-CELL_M, 0) should be key -1, not 0.
        assert sg.cell_key(-0.5 * sg.CELL_M, -0.5 * sg.CELL_M) == (-1, -1)
        # Boundary point at exactly -CELL_M still floors to -1.
        assert sg.cell_key(-sg.CELL_M, -sg.CELL_M) == (-1, -1)
        # Anything strictly below -CELL_M lands in cell -2.
        assert sg.cell_key(-1.01 * sg.CELL_M, -1.01 * sg.CELL_M) == (-2, -2)


class TestEwma:
    def test_first_sample_is_the_value(self):
        cell = sg.GridCell()
        cell.update(horizontal_accuracy=0.42)
        assert cell.horizontal_accuracy == 0.42
        assert cell.n == 1

    def test_repeated_constant_samples_converge(self):
        cell = sg.GridCell()
        for _ in range(50):
            cell.update(horizontal_accuracy=0.1)
        # After many samples of the same value the EWMA equals it.
        assert cell.horizontal_accuracy == pytest.approx(0.1, abs=1e-6)
        assert cell.n == 50

    def test_step_change_decays_over_window(self):
        # Saturate the cell at one value, then push the opposite value
        # and verify the EWMA has moved most of the way after ~10 samples
        # (the alpha=0.1 window).
        cell = sg.GridCell()
        for _ in range(50):
            cell.update(horizontal_accuracy=0.05)
        for _ in range(10):
            cell.update(horizontal_accuracy=1.05)
        # 10 samples at alpha=0.1 → ~1 - 0.9**10 ≈ 0.65 of the way to the new value.
        # Old value 0.05, new value 1.05, expected ≈ 0.05 + 0.65 = 0.70.
        assert cell.horizontal_accuracy == pytest.approx(0.70, abs=0.05)

    def test_each_metric_is_independent(self):
        cell = sg.GridCell()
        cell.update(horizontal_accuracy=0.1, position_quality=3, wifi_signal=-65)
        # lte_signal was never sent, should remain None
        assert cell.horizontal_accuracy == pytest.approx(0.1)
        assert cell.position_quality == pytest.approx(3.0)
        assert cell.wifi_signal == pytest.approx(-65.0)
        assert cell.lte_signal is None

    def test_missing_metric_does_not_advance_value(self):
        cell = sg.GridCell()
        cell.update(horizontal_accuracy=0.1)
        cell.update(position_quality=3)  # no HA in this update
        # HA stays at 0.1 because the second update didn't touch it.
        assert cell.horizontal_accuracy == pytest.approx(0.1)
        assert cell.position_quality == pytest.approx(3.0)
        # But n increments for every update regardless of which metrics.
        assert cell.n == 2


class TestSignalGrid:
    def test_record_no_metrics_is_noop(self):
        g = sg.SignalGrid()
        g.record(1.0, 2.0)  # nothing to record
        assert len(g) == 0

    def test_samples_in_same_cell_merge(self):
        g = sg.SignalGrid()
        # Both points fall in cell (1, 2) — slightly off-center within the
        # same bin regardless of CELL_M choice.
        g.record(1.10 * sg.CELL_M, 2.20 * sg.CELL_M, horizontal_accuracy=0.05)
        g.record(1.90 * sg.CELL_M, 2.90 * sg.CELL_M, horizontal_accuracy=0.05)
        assert len(g) == 1
        cell = g.cells()[(1, 2)]
        assert cell.n == 2
        assert cell.horizontal_accuracy == pytest.approx(0.05, abs=1e-6)

    def test_samples_in_different_cells_stay_separate(self):
        g = sg.SignalGrid()
        g.record(0.2 * sg.CELL_M, 0.2 * sg.CELL_M, horizontal_accuracy=0.05)
        g.record(10.2 * sg.CELL_M, 10.2 * sg.CELL_M, horizontal_accuracy=0.50)
        assert len(g) == 2
        assert g.cells()[(0, 0)].horizontal_accuracy == pytest.approx(0.05)
        assert g.cells()[(10, 10)].horizontal_accuracy == pytest.approx(0.50)


class TestPersistenceRoundTrip:
    def test_empty_grid_roundtrip(self):
        out = sg.SignalGrid().to_dict()
        restored = sg.SignalGrid.from_dict(out)
        assert len(restored) == 0
        assert out["version"] == sg.GRID_SCHEMA_VERSION

    def test_populated_grid_roundtrip(self):
        g = sg.SignalGrid()
        for i in range(5):
            g.record(
                float(i), float(i),
                horizontal_accuracy=0.10 + i * 0.05,
                position_quality=2,
                wifi_signal=-50 - i,
                lte_signal=-70,
            )
        snapshot = g.to_dict()
        restored = sg.SignalGrid.from_dict(snapshot)
        assert len(restored) == len(g)
        for key, cell in g.cells().items():
            r = restored.cells()[key]
            assert r.n == cell.n
            assert r.horizontal_accuracy == pytest.approx(cell.horizontal_accuracy)
            assert r.position_quality == pytest.approx(cell.position_quality)
            assert r.wifi_signal == pytest.approx(cell.wifi_signal)
            assert r.lte_signal == pytest.approx(cell.lte_signal)

    def test_from_dict_tolerates_garbage(self):
        # Corrupt or partial Store data should never crash; just dropped cells.
        bad = {
            "version": sg.GRID_SCHEMA_VERSION,
            "cell_m": sg.CELL_M,
            "cells": {
                "5,10":  {"n": 3, "ha": 0.12, "pq": 3, "wifi": -55, "lte": -70},
                "bad":   {"n": 1, "ha": 0.10},        # un-parseable key
                "1,1":   None,                         # not a dict
                "2,2":   {"n": 1, "ha": "garbage"},    # un-parseable value
            },
        }
        g = sg.SignalGrid.from_dict(bad)
        # Only the valid key (5,10) survives — the un-parseable key gets
        # dropped; (2,2) keeps n but has ha=None because "garbage" coerces
        # to None.
        assert (5, 10) in g.cells()
        assert g.cells()[(5, 10)].horizontal_accuracy == pytest.approx(0.12)
        if (2, 2) in g.cells():
            assert g.cells()[(2, 2)].horizontal_accuracy is None

    def test_from_dict_none_returns_empty_grid(self):
        g = sg.SignalGrid.from_dict(None)
        assert len(g) == 0

    def test_from_dict_discards_on_cell_size_mismatch(self):
        # Simulates an upgrade where the integration's CELL_M changed —
        # any stored cells need to be discarded because their bin keys
        # would land in the wrong world coordinates at the new resolution.
        legacy = {
            "version": sg.GRID_SCHEMA_VERSION,
            "cell_m": sg.CELL_M * 2,  # deliberately wrong
            "cells": {
                "0,0":  {"n": 5, "ha": 0.04},
                "10,5": {"n": 3, "ha": 0.18},
            },
        }
        g = sg.SignalGrid.from_dict(legacy)
        assert len(g) == 0

    def test_from_dict_discards_legacy_blob_without_cell_m(self):
        # v1 blobs (pre-cell_m field) can't be trusted at a different bin
        # size, so the load path discards them too.
        legacy = {
            "version": 1,
            "cells": {"3,4": {"n": 2, "ha": 0.05}},
        }
        g = sg.SignalGrid.from_dict(legacy)
        assert len(g) == 0
