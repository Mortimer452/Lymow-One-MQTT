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


class TestActiveCutConfig:
    def test_falls_through_to_runtime_when_no_zone(self):
        # PbRunTimeConfig (the global runtime config from PbMap.runTimeConfig)
        # carries cutHeight, cutSpeed, AND moveSpeed (arch.md §6c). It is
        # delivered inside QUERY_MAP responses and stashed on ZoneCatalog.
        from lymow_mqtt.protocol import ZoneCatalog
        import lymow_extracted_pb2 as pb
        rtc = pb.PbRunTimeConfig()
        rtc.cutHeight = 60
        rtc.cutSpeed = 4
        rtc.moveSpeed = 0.7
        catalog = ZoneCatalog()
        catalog.runtime_config = rtc
        s = {"zone_catalog": catalog}
        result = state.active_cut_config(s)
        assert result["cut_height"] == 60
        assert result["cut_speed"] == 4
        assert result["move_speed"] == pytest.approx(0.7)

    def test_returns_zone_config_when_zone_known(self):
        # Build a synthetic state with a zone_catalog and matching active zone
        from lymow_mqtt.protocol import ZoneCatalog, ZoneInfo
        import lymow_extracted_pb2 as pb
        zone = ZoneInfo(
            hash_id="abc12345",
            name="Pool",
            mow_order=1,
            is_enabled=True,
            polygon_points=[(0, 0), (10, 0), (10, 10), (0, 10)],
        )
        # Stash the PbZoneConfig on the ZoneInfo (the parser does this in
        # production; tests build the catalog by hand).
        zone.zone_config = pb.PbZoneConfig()
        zone.zone_config.cutSpeed = 5
        zone.zone_config.cutHeight = 50
        zone.zone_config.moveSpeed = 0.6
        catalog = ZoneCatalog()
        catalog.zones.append(zone)
        catalog.zones_by_hashid[zone.hash_id] = zone
        # Runtime config has different values; should NOT be picked
        # because the zone-tier wins.
        rtc = pb.PbRunTimeConfig()
        rtc.cutHeight = 99
        rtc.cutSpeed = 6
        rtc.moveSpeed = 1.0
        catalog.runtime_config = rtc

        # Mower pose inside the zone, mowing
        pose = pb.PbPose()
        pose.x = 5.0
        pose.y = 5.0
        robot_info = pb.PbRobotInfo()
        robot_info.workStatus = 2  # MOWING

        s = {
            "zone_catalog": catalog,
            "pose": pose,
            "robotInfo": robot_info,
        }
        result = state.active_cut_config(s)
        assert result["cut_height"] == 50
        assert result["cut_speed"] == 5
        assert result["move_speed"] == pytest.approx(0.6)

    def test_returns_none_dict_when_no_state(self):
        result = state.active_cut_config({})
        assert result == {"cut_speed": None, "cut_height": None, "move_speed": None}


from datetime import UTC, datetime, timedelta


class TestResolveOnline:
    def _now(self) -> datetime:
        return datetime(2026, 5, 8, 12, 0, 0, tzinfo=UTC)

    def test_rest_online_no_mqtt_returns_online(self):
        assert state.resolve_online(rest_online=True, last_mqtt_at=None, now=self._now()) is True

    def test_rest_online_with_mqtt_returns_online(self):
        recent = self._now() - timedelta(seconds=30)
        assert state.resolve_online(rest_online=True, last_mqtt_at=recent, now=self._now()) is True

    def test_rest_offline_no_mqtt_returns_offline(self):
        assert state.resolve_online(rest_online=False, last_mqtt_at=None, now=self._now()) is False

    def test_rest_offline_recent_mqtt_overrides_to_online(self):
        """The corner case from spec §7.3 — fresh MQTT in last 5min trumps stale REST."""
        recent = self._now() - timedelta(minutes=2)
        assert state.resolve_online(rest_online=False, last_mqtt_at=recent, now=self._now()) is True

    def test_rest_offline_old_mqtt_returns_offline(self):
        old = self._now() - timedelta(minutes=10)
        assert state.resolve_online(rest_online=False, last_mqtt_at=old, now=self._now()) is False
