"""Tests for state.py — state merge, active-config inheritance, geometry."""
from __future__ import annotations

import pytest

from lymow_mqtt import state


class TestPointInPolygon:
    def test_point_inside_unit_square(self):
        square = [(0, 0), (1, 0), (1, 1), (0, 1)]
        assert state.point_in_polygon(0.5, 0.5, square) is True

    def test_point_outside_unit_square(self):
        square = [(0, 0), (1, 0), (1, 1), (0, 1)]
        assert state.point_in_polygon(2.0, 0.5, square) is False
        assert state.point_in_polygon(-1.0, 0.5, square) is False
        assert state.point_in_polygon(0.5, -1.0, square) is False
        assert state.point_in_polygon(0.5, 2.0, square) is False

    def test_concave_polygon(self):
        # An L-shape (inverted)
        l_shape = [(0, 0), (3, 0), (3, 1), (1, 1), (1, 3), (0, 3)]
        # Inside the foot
        assert state.point_in_polygon(2.0, 0.5, l_shape) is True
        # Inside the leg
        assert state.point_in_polygon(0.5, 2.0, l_shape) is True
        # In the notch (outside)
        assert state.point_in_polygon(2.0, 2.0, l_shape) is False

    def test_empty_polygon_returns_false(self):
        assert state.point_in_polygon(0.5, 0.5, []) is False
        assert state.point_in_polygon(0.5, 0.5, [(0, 0), (1, 0)]) is False  # degenerate
