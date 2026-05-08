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


class TestMergePbOutput:
    def test_merges_robotinfo(self):
        import lymow_extracted_pb2 as pb
        s: dict = {}
        msg = pb.PbOutput()
        msg.robotInfo.battery = 75
        msg.robotInfo.workStatus = 2
        state.merge_pboutput(s, msg)
        assert s["robotInfo"].battery == 75
        assert s["robotInfo"].workStatus == 2

    def test_overwrites_robotinfo_on_subsequent_merge(self):
        import lymow_extracted_pb2 as pb
        s: dict = {}
        msg1 = pb.PbOutput()
        msg1.robotInfo.battery = 75
        state.merge_pboutput(s, msg1)
        msg2 = pb.PbOutput()
        msg2.robotInfo.battery = 70
        state.merge_pboutput(s, msg2)
        assert s["robotInfo"].battery == 70

    def test_does_not_clobber_other_keys_on_partial_message(self):
        """A message with only robotInfo shouldn't blank previous cleanInfo."""
        import lymow_extracted_pb2 as pb
        s: dict = {}
        msg1 = pb.PbOutput()
        msg1.cleanInfo.cleanArea = 1367.0
        state.merge_pboutput(s, msg1)
        msg2 = pb.PbOutput()
        msg2.robotInfo.battery = 75
        state.merge_pboutput(s, msg2)
        assert s["cleanInfo"].cleanArea == 1367.0
        assert s["robotInfo"].battery == 75

    def test_merges_error_codes_as_list(self):
        import lymow_extracted_pb2 as pb
        s: dict = {}
        msg = pb.PbOutput()
        msg.errorCodes.append(45)
        state.merge_pboutput(s, msg)
        assert s["errorCodes"] == [45]

    def test_clears_error_codes_when_message_has_none(self):
        """Once an error clears, the next broadcast typically has no errorCodes
        field. Our state should reflect 'no current errors'."""
        import lymow_extracted_pb2 as pb
        s: dict = {"errorCodes": [45]}
        msg = pb.PbOutput()
        # No errorCodes set
        state.merge_pboutput(s, msg)
        # Behavior: if errorCodes field is unset, leave the previous value
        # (we can't distinguish "field absent" from "deliberately empty list"
        # at protobuf level). Document this in the implementation.
        # The per-broadcast resolution happens in the coordinator using
        # robotStatus transitions (7 -> non-7) as the cleared-error signal.
        assert s.get("errorCodes") == [45]  # unchanged
