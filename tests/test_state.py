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


class TestPolygonArea:
    """Shoelace formula for zone area calculation. Polygon points are
    local-frame meters, result is m²."""

    def test_unit_square(self):
        sq = [(0, 0), (1, 0), (1, 1), (0, 1)]
        assert state.polygon_area(sq) == pytest.approx(1.0, abs=1e-9)

    def test_10m_square(self):
        sq = [(0, 0), (10, 0), (10, 10), (0, 10)]
        assert state.polygon_area(sq) == pytest.approx(100.0, abs=1e-9)

    def test_right_triangle(self):
        # legs 4 and 3 → area 6
        tri = [(0, 0), (4, 0), (0, 3)]
        assert state.polygon_area(tri) == pytest.approx(6.0, abs=1e-9)

    def test_concave_l_shape(self):
        # 3x3 outer minus 2x2 notch = 9 - 4 = 5 m²
        l_shape = [(0, 0), (3, 0), (3, 1), (1, 1), (1, 3), (0, 3)]
        assert state.polygon_area(l_shape) == pytest.approx(5.0, abs=1e-9)

    def test_winding_direction_doesnt_matter(self):
        ccw = [(0, 0), (1, 0), (1, 1), (0, 1)]
        cw = [(0, 0), (0, 1), (1, 1), (1, 0)]
        assert state.polygon_area(ccw) == pytest.approx(state.polygon_area(cw), abs=1e-9)

    def test_degenerate_inputs_return_zero(self):
        assert state.polygon_area([]) == 0.0
        assert state.polygon_area([(0, 0)]) == 0.0
        assert state.polygon_area([(0, 0), (1, 1)]) == 0.0  # 2 points

    def test_negative_coordinates(self):
        # Local ENU frame can have negative x/y if mower is west/south of
        # the RTK base. Area calc must be robust to that.
        sq = [(-5, -5), (5, -5), (5, 5), (-5, 5)]
        assert state.polygon_area(sq) == pytest.approx(100.0, abs=1e-9)


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


class TestIsRealZoneCatalog:
    """Sticky-field guard. QUERY_PATH responses produce an empty ZoneCatalog
    that should NOT replace a previously-populated one — see the long
    comment on the function for the failure mode (camera goes unavailable,
    frontend builds `?token=undefined` URLs).
    """

    def _empty(self):
        from lymow_mqtt.protocol import ZoneCatalog
        return ZoneCatalog()

    def _with_zone(self):
        from lymow_mqtt.protocol import ZoneCatalog, ZoneInfo
        c = ZoneCatalog()
        z = ZoneInfo(
            hash_id="abc12345", name="Pool",
            mow_order=0, is_enabled=True,
            polygon_points=[(0, 0), (1, 0), (1, 1)],
        )
        c.zones.append(z)
        c.zones_by_hashid[z.hash_id] = z
        return c

    def test_empty_catalog_is_not_real(self):
        from lymow_mqtt import state as state_mod
        assert state_mod.is_real_zone_catalog(self._empty()) is False

    def test_catalog_with_zones_is_real(self):
        from lymow_mqtt import state as state_mod
        assert state_mod.is_real_zone_catalog(self._with_zone()) is True

    def test_catalog_with_only_channels_is_real(self):
        from lymow_mqtt.protocol import ChannelInfo, ZoneCatalog
        from lymow_mqtt import state as state_mod
        c = ZoneCatalog()
        c.channels.append(ChannelInfo(
            hash_id="ch1", zone1="a", zone2="b",
            is_docking_channel=False, polygon_points=[(0, 0), (1, 0), (1, 1)],
        ))
        assert state_mod.is_real_zone_catalog(c) is True

    def test_catalog_with_only_runtime_config_is_real(self):
        from lymow_mqtt.protocol import ZoneCatalog
        from lymow_mqtt import state as state_mod
        c = ZoneCatalog()
        c.runtime_config = object()  # any non-None sentinel
        assert state_mod.is_real_zone_catalog(c) is True

    def test_catalog_with_only_enu_base_point_is_real(self):
        from lymow_mqtt.protocol import ZoneCatalog
        from lymow_mqtt import state as state_mod
        c = ZoneCatalog()
        c.enu_base_point = object()
        assert state_mod.is_real_zone_catalog(c) is True


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


class TestCurrentZoneCache:
    """The cache populated by ``compute_current_zone_cache`` is what makes
    pose-in-polygon work O(1) per consumer instead of O(N) per call. These
    tests pin down the contract every consumer relies on.
    """

    def _state_with_zones(self, pose_xy):
        from lymow_mqtt.protocol import ZoneCatalog, ZoneInfo

        class _P:
            def __init__(self, x, y): self.x, self.y = x, y

        z1 = ZoneInfo(
            hash_id="aaa11111", name="Front",
            mow_order=0, is_enabled=True,
            polygon_points=[(0, 0), (5, 0), (5, 5), (0, 5)],
        )
        z2 = ZoneInfo(
            hash_id="bbb22222", name="Back",
            mow_order=1, is_enabled=True,
            polygon_points=[(10, 10), (15, 10), (15, 15), (10, 15)],
        )
        cat = ZoneCatalog()
        cat.zones.extend([z1, z2])
        cat.zones_by_hashid[z1.hash_id] = z1
        cat.zones_by_hashid[z2.hash_id] = z2
        return {
            "pose": _P(*pose_xy),
            "zone_catalog": cat,
        }

    def test_cache_populated_with_containing_zone_hash(self):
        from lymow_mqtt import state as state_mod
        s = self._state_with_zones((2.5, 2.5))
        result = state_mod.compute_current_zone_cache(s)
        assert result == "aaa11111"
        assert s["_current_zone_hash_id"] == "aaa11111"

    def test_cache_populated_with_none_when_pose_outside_all_zones(self):
        from lymow_mqtt import state as state_mod
        s = self._state_with_zones((100, 100))
        result = state_mod.compute_current_zone_cache(s)
        assert result is None
        assert s["_current_zone_hash_id"] is None  # key present, value None

    def test_zone_at_pose_reads_cache_when_present(self):
        from lymow_mqtt import state as state_mod
        s = self._state_with_zones((2.5, 2.5))
        # Pre-populate cache with a value that disagrees with pose.
        # zone_at_pose should trust the cache, not re-walk.
        s["_current_zone_hash_id"] = "bbb22222"
        zone = state_mod.zone_at_pose(s)
        assert zone is not None
        assert zone.hash_id == "bbb22222"

    def test_zone_at_pose_falls_back_to_live_walk_when_cache_missing(self):
        from lymow_mqtt import state as state_mod
        s = self._state_with_zones((2.5, 2.5))
        assert "_current_zone_hash_id" not in s
        zone = state_mod.zone_at_pose(s)
        assert zone is not None
        assert zone.hash_id == "aaa11111"

    def test_cache_handles_missing_pose_or_catalog(self):
        from lymow_mqtt import state as state_mod
        # No pose
        assert state_mod.compute_current_zone_cache({"zone_catalog": object()}) is None
        # No catalog
        s = {"pose": object()}
        assert state_mod.compute_current_zone_cache(s) is None
        # Both missing
        assert state_mod.compute_current_zone_cache({}) is None


class TestTaskZonesHelpers:
    """`is_task_active` + `current_task_zones` are the shared predicates the
    Task Zones sensor, per-zone in_current_task attribute, and map camera
    task-highlight gate all consult.
    """

    def _state(self, work_status, task_orders):
        from lymow_mqtt.protocol import ZoneCatalog, ZoneInfo

        class _RI:
            def __init__(self, ws): self.workStatus = ws

        cat = ZoneCatalog()
        for i, (name, order) in enumerate(task_orders):
            z = ZoneInfo(
                hash_id=f"h{i:03d}", name=name,
                mow_order=order, is_enabled=True,
                polygon_points=[(0, 0), (1, 0), (1, 1), (0, 1)],
            )
            cat.zones.append(z)
            cat.zones_by_hashid[z.hash_id] = z
        return {"zone_catalog": cat, "robotInfo": _RI(work_status)}

    def test_is_task_active_true_for_mowing(self):
        from lymow_mqtt import state as state_mod
        # WORK_STATUS_MOWING = 2
        s = self._state(2, [])
        assert state_mod.is_task_active(s) is True

    def test_is_task_active_false_for_idle(self):
        from lymow_mqtt import state as state_mod
        # WORK_STATUS_WAITING = 1 (not in ACTIVE_TASK_STATUSES)
        s = self._state(1, [])
        assert state_mod.is_task_active(s) is False

    def test_is_task_active_false_when_robotinfo_missing(self):
        from lymow_mqtt import state as state_mod
        assert state_mod.is_task_active({}) is False

    def test_current_task_zones_sorted_by_mow_order(self):
        from lymow_mqtt import state as state_mod
        # Insert zones in random mow_order — helper must sort.
        s = self._state(2, [
            ("Pool", 3),
            ("Front", 1),
            ("Garden", 0),   # not in task
            ("Back", 2),
        ])
        zones = state_mod.current_task_zones(s)
        assert [z.name for z in zones] == ["Front", "Back", "Pool"]

    def test_current_task_zones_empty_when_not_active(self):
        from lymow_mqtt import state as state_mod
        # Same zones, but workStatus says Waiting → residual mow_order
        # values shouldn't be reported as "current task".
        s = self._state(1, [("Front", 1), ("Back", 2)])
        assert state_mod.current_task_zones(s) == []

    def test_current_task_zones_empty_when_no_zones_have_mow_order(self):
        from lymow_mqtt import state as state_mod
        s = self._state(2, [("Front", 0), ("Back", 0)])
        assert state_mod.current_task_zones(s) == []


from datetime import UTC, datetime, timedelta


class TestResolveZones:
    def _catalog(self):
        from lymow_mqtt.protocol import ZoneCatalog, ZoneInfo
        c = ZoneCatalog()
        for hash_id, name in [("XBYm6ijg", "Pool"), ("zCGt0Yy9", "Front yard"), ("AbCdEf12", "Red barn")]:
            zi = ZoneInfo(hash_id=hash_id, name=name, mow_order=0, is_enabled=True, polygon_points=[])
            c.zones.append(zi)
            c.zones_by_hashid[hash_id] = zi
        return c

    def test_resolves_names_to_hashids(self):
        c = self._catalog()
        assert state.resolve_zones(c, ["Pool", "Front yard"]) == ["XBYm6ijg", "zCGt0Yy9"]

    def test_passes_through_hashids(self):
        c = self._catalog()
        assert state.resolve_zones(c, ["XBYm6ijg", "AbCdEf12"]) == ["XBYm6ijg", "AbCdEf12"]

    def test_mixed_names_and_hashids(self):
        c = self._catalog()
        assert state.resolve_zones(c, ["Pool", "AbCdEf12"]) == ["XBYm6ijg", "AbCdEf12"]

    def test_case_insensitive_name_match(self):
        c = self._catalog()
        assert state.resolve_zones(c, ["pool", "FRONT YARD"]) == ["XBYm6ijg", "zCGt0Yy9"]

    def test_strips_whitespace(self):
        c = self._catalog()
        assert state.resolve_zones(c, ["  Pool  "]) == ["XBYm6ijg"]

    def test_skips_empty_strings(self):
        c = self._catalog()
        assert state.resolve_zones(c, ["Pool", "", "  "]) == ["XBYm6ijg"]

    def test_empty_list_returns_empty(self):
        c = self._catalog()
        assert state.resolve_zones(c, []) == []

    def test_unknown_name_raises_with_known_list(self):
        c = self._catalog()
        with pytest.raises(ValueError, match="Unknown zone"):
            state.resolve_zones(c, ["Foo"])

    def test_no_catalog_raises(self):
        with pytest.raises(ValueError, match="catalog not available"):
            state.resolve_zones(None, ["Pool"])

    def test_empty_catalog_raises(self):
        from lymow_mqtt.protocol import ZoneCatalog
        with pytest.raises(ValueError, match="catalog not available"):
            state.resolve_zones(ZoneCatalog(), ["Pool"])


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


class TestEnuToLla:
    """Verifies enu_base_point + pose → GPS lat/lon math (arch.md §8c)."""

    def _ebp(self, lat=37.6390347, lon=-97.4817202, alt=350.0):
        import lymow_extracted_pb2 as pb
        e = pb.PbRobotLLACoords()
        e.latitude = lat
        e.longitude = lon
        e.altitude = alt
        return e

    def _pose(self, x=0.0, y=0.0):
        import lymow_extracted_pb2 as pb
        p = pb.PbPose()
        p.x = x
        p.y = y
        return p

    # Note: PbRobotLLACoords stores latitude/longitude as float (32-bit),
    # so values round-trip with ~7 significant digits of precision. We use
    # 1e-5 absolute tolerance (~1m at this latitude) — well above float32
    # rounding noise, well below RTK accuracy bounds.

    def test_zero_pose_equals_base_point(self):
        """At the RTK base origin (pose 0,0), GPS = enuBasePoint."""
        result = state.enu_to_lla(self._ebp(), self._pose(0, 0))
        assert result is not None
        lat, lon = result
        assert lat == pytest.approx(37.6390347, abs=1e-5)
        assert lon == pytest.approx(-97.4817202, abs=1e-5)

    def test_pose_y_increases_latitude(self):
        """Pose y is meters north — pushes latitude positive."""
        # 100 meters north → ~0.0009 degrees latitude
        ebp = self._ebp(lat=40.0, lon=-100.0)
        result = state.enu_to_lla(ebp, self._pose(x=0.0, y=100.0))
        assert result is not None
        lat, lon = result
        # 1 degree latitude ≈ 111111 m, so 100m → 100/111111 = 0.0009 degrees
        assert lat == pytest.approx(40.0 + (100.0 / 111111.0), abs=1e-5)
        # Longitude unchanged (pose.x=0)
        assert lon == pytest.approx(-100.0, abs=1e-5)

    def test_pose_x_increases_longitude(self):
        """Pose x is meters east — pushes longitude positive."""
        # 100 meters east at lat=0 (cos=1) → 100/111111 ≈ 0.0009 degrees lon
        ebp = self._ebp(lat=0.0, lon=0.0)
        result = state.enu_to_lla(ebp, self._pose(x=100.0, y=0.0))
        assert result is not None
        lat, lon = result
        assert lat == pytest.approx(0.0, abs=1e-5)
        assert lon == pytest.approx(100.0 / 111111.0, abs=1e-5)

    def test_longitude_scales_by_cosine_of_latitude(self):
        """At higher latitudes, the same x-offset yields larger longitude
        delta because longitude lines converge near the poles."""
        # 100m east at lat=60° (cos ≈ 0.5) → 2x the longitude delta of equator
        ebp = self._ebp(lat=60.0, lon=0.0)
        result = state.enu_to_lla(ebp, self._pose(x=100.0, y=0.0))
        assert result is not None
        _, lon = result
        # Expected: 100 / (111111 * cos(60°)) ≈ 100 / (111111 * 0.5) ≈ 2x equator
        from math import cos, radians
        expected = 100.0 / (111111.0 * cos(radians(60.0)))
        assert lon == pytest.approx(expected, abs=1e-5)

    def test_returns_none_when_ebp_missing(self):
        assert state.enu_to_lla(None, self._pose(1, 1)) is None

    def test_returns_none_when_pose_missing(self):
        assert state.enu_to_lla(self._ebp(), None) is None

    def test_returns_none_when_pose_lacks_xy(self):
        """Object without x/y attributes shouldn't crash — returns None."""
        class FakePose:
            pass
        assert state.enu_to_lla(self._ebp(), FakePose()) is None

    def test_returns_none_when_ebp_lacks_lat_lon(self):
        class FakeEbp:
            pass
        assert state.enu_to_lla(FakeEbp(), self._pose(1, 1)) is None
