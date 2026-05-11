"""Tests for protocol.py — protobuf encode/decode and envelope handling."""
from __future__ import annotations

import base64
import json

import pytest

from lymow_mqtt import protocol  # noqa: F401  (will fail until module exists)


class TestEnvelope:
    def test_wrap_produces_json_with_base64_message(self):
        result = protocol.wrap_envelope(b"hello")
        envelope = json.loads(result)
        assert "message" in envelope
        assert base64.b64decode(envelope["message"]) == b"hello"

    def test_unwrap_recovers_original_bytes(self):
        envelope_bytes = json.dumps({"message": base64.b64encode(b"world").decode()}).encode()
        result = protocol.unwrap_envelope(envelope_bytes)
        assert result == b"world"

    def test_unwrap_handles_extra_whitespace(self):
        # Real captures have tabs/newlines around fields
        envelope = b'{\n\t"message" : "aGVsbG8="\n}'
        assert protocol.unwrap_envelope(envelope) == b"hello"


class TestEncodeUserCtrl:
    def test_simple_userctrl_pause(self):
        """USER_CTRL_PAUSE = 3 produces the documented 4-byte protobuf."""
        raw = protocol.encode_userctrl(3)
        # PbInput { userCtrl: 3, version: 40 } = bytes "10 28 28 03"
        # field 2 (version) tag = 0x10, value 40 = 0x28
        # field 5 (userCtrl) tag = 0x28, value 3 = 0x03
        assert raw == bytes([0x10, 0x28, 0x28, 0x03])

    def test_userctrl_dock_recharge(self):
        """USER_CTRL_RECHARGE_DOCK = 33 produces a 4-byte protobuf."""
        raw = protocol.encode_userctrl(33)
        assert raw == bytes([0x10, 0x28, 0x28, 0x21])  # 33 = 0x21


class TestEncodeQueryMap:
    def test_query_map_includes_btmap_query_flag(self):
        raw = protocol.encode_query_map()
        # Decode it back to verify
        import lymow_extracted_pb2 as pb
        msg = pb.PbInput()
        msg.ParseFromString(raw)
        assert msg.userCtrl == 19
        assert msg.version == 40
        assert msg.btMap.queryMap is True


class TestEncodeUploadRobotConfig:
    def test_encodes_l3_wakeup_payload(self):
        """L3 wakeup is `version=40 + debugSetting.uploadRobotConfig=true`,
        no userCtrl. Triggers a robotConfig broadcast (arch.md §7a Layer 3)."""
        import lymow_extracted_pb2 as pb
        raw = protocol.encode_upload_robot_config()
        msg = pb.PbInput()
        msg.ParseFromString(raw)
        assert msg.version == 40
        assert not msg.HasField("userCtrl") or msg.userCtrl == 0
        assert msg.debugSetting.uploadRobotConfig is True
        # Should NOT also set uploadTaskConfig — that's a different concern
        assert not msg.debugSetting.HasField("uploadTaskConfig") or not msg.debugSetting.uploadTaskConfig


class TestEncodeSetRrConfig:
    """The no-userCtrl `setRR` payload (arch.md §6g) — verified via
    spike_set_rrconfig.py round-trip on 2026-05-10."""

    def test_no_user_ctrl(self):
        """setRR is the no-userCtrl pattern — firmware reacts to a populated
        robotConfig.rrConfig field, not a userCtrl int."""
        import lymow_extracted_pb2 as pb
        raw = protocol.encode_set_rr_config(
            enable_rr=True,
            recharge_bat=15, resume_bat=75,
            period_start_hour=15, period_start_minute=30,
            period_end_hour=2, period_end_minute=30,
        )
        msg = pb.PbInput()
        msg.ParseFromString(raw)
        # version is set to 40
        assert msg.version == 40
        # userCtrl deliberately absent
        assert not msg.HasField("userCtrl") or msg.userCtrl == 0

    def test_rrconfig_round_trip(self):
        """All five rrConfig fields make it into the payload intact."""
        import lymow_extracted_pb2 as pb
        raw = protocol.encode_set_rr_config(
            enable_rr=True,
            recharge_bat=20, resume_bat=70,
            period_start_hour=15, period_start_minute=30,
            period_end_hour=2, period_end_minute=30,
        )
        msg = pb.PbInput()
        msg.ParseFromString(raw)
        rr = msg.robotConfig.rrConfig
        assert rr.enableRr is True
        assert rr.rechargeBat == 20
        assert rr.resumeBat == 70
        assert rr.resumePeriodStart.hour == 15
        assert rr.resumePeriodStart.minute == 30
        assert rr.resumePeriodEnd.hour == 2
        assert rr.resumePeriodEnd.minute == 30

    def test_upload_robot_config_flag_set(self):
        """The uploadRobotConfig debug flag is required for the firmware to
        echo back its updated robotConfig — without it, the write may apply
        but we'd have no way to confirm."""
        import lymow_extracted_pb2 as pb
        raw = protocol.encode_set_rr_config(
            enable_rr=False,
            recharge_bat=15, resume_bat=75,
            period_start_hour=None, period_start_minute=None,
            period_end_hour=None, period_end_minute=None,
        )
        msg = pb.PbInput()
        msg.ParseFromString(raw)
        assert msg.debugSetting.uploadRobotConfig is True

    def test_optional_fields_omitted(self):
        """Passing None for a field should leave it unset on the wire."""
        import lymow_extracted_pb2 as pb
        raw = protocol.encode_set_rr_config(
            enable_rr=True,
            recharge_bat=None, resume_bat=None,
            period_start_hour=None, period_start_minute=None,
            period_end_hour=None, period_end_minute=None,
        )
        msg = pb.PbInput()
        msg.ParseFromString(raw)
        rr = msg.robotConfig.rrConfig
        assert rr.enableRr is True
        # Optional fields should not be set
        assert not rr.HasField("rechargeBat")
        assert not rr.HasField("resumeBat")
        assert not rr.HasField("resumePeriodStart")
        assert not rr.HasField("resumePeriodEnd")

    def test_enable_rr_false_round_trips(self):
        """proto3 has implicit-presence quirks for bools — confirm a False
        value actually gets serialized (not optimized out as default)."""
        import lymow_extracted_pb2 as pb
        raw = protocol.encode_set_rr_config(
            enable_rr=False,
            recharge_bat=15, resume_bat=75,
            period_start_hour=15, period_start_minute=30,
            period_end_hour=2, period_end_minute=30,
        )
        msg = pb.PbInput()
        msg.ParseFromString(raw)
        # PbRRConfig.enableRr is `optional bool` — explicit-presence — so
        # False is distinguishable from "unset" via HasField
        assert msg.robotConfig.rrConfig.HasField("enableRr")
        assert msg.robotConfig.rrConfig.enableRr is False


class TestEncodeStartZones:
    def test_start_zones_two_zones_in_order(self):
        raw = protocol.encode_start_zones(["aaaa1111", "bbbb2222"])
        import lymow_extracted_pb2 as pb
        msg = pb.PbInput()
        msg.ParseFromString(raw)
        assert msg.userCtrl == 1
        assert msg.version == 40
        assert len(msg.map.goZones) == 2
        assert msg.map.goZones[0].basicInfo.hashId == "aaaa1111"
        assert msg.map.goZones[0].basicInfo.mowOrder == 1
        assert msg.map.goZones[1].basicInfo.hashId == "bbbb2222"
        assert msg.map.goZones[1].basicInfo.mowOrder == 2

    def test_start_zones_empty_list_is_default_rotation(self):
        raw = protocol.encode_start_zones([])
        import lymow_extracted_pb2 as pb
        msg = pb.PbInput()
        msg.ParseFromString(raw)
        assert msg.userCtrl == 1
        assert msg.version == 40
        assert len(msg.map.goZones) == 0


from tests.conftest import load_fixture


class TestDecodePbOutput:
    def test_decodes_state_broadcast_fixture(self):
        envelope = load_fixture("state_broadcast.bin")
        msg = protocol.decode_pboutput_envelope(envelope)
        # State broadcasts always carry robotInfo
        assert msg.robotInfo.ByteSize() > 0

    def test_populated_fields_returns_field_names(self):
        envelope = load_fixture("state_broadcast.bin")
        msg = protocol.decode_pboutput_envelope(envelope)
        names = protocol.populated_fields(msg)
        assert "robotInfo" in names
        # version is sometimes set, sometimes not, don't assert
        assert isinstance(names, list)

    def test_query_map_response_has_btmap(self):
        envelope = load_fixture("query_map_response.bin")
        msg = protocol.decode_pboutput_envelope(envelope)
        if msg.btMap.ByteSize() == 0:
            pytest.skip("Fixture is state-echo only, no btMap to check")
        assert "btMap" in protocol.populated_fields(msg)


class TestZoneCatalogParser:
    def test_parse_query_map_response_extracts_zones(self):
        envelope = load_fixture("query_map_response.bin")
        msg = protocol.decode_pboutput_envelope(envelope)
        # Skip if this fixture didn't include the btMap blob
        if msg.btMap.ByteSize() < 200:
            pytest.skip("Fixture is state-echo only, no zone catalog")
        catalog = protocol.parse_zone_catalog(msg.btMap)
        # Should find at least one zone
        assert len(catalog.zones) >= 1
        # Each zone has a hashId
        for z in catalog.zones:
            assert z.hash_id
            assert isinstance(z.name, str)
            assert isinstance(z.mow_order, int)

    def test_zones_by_hashid_lookup(self):
        envelope = load_fixture("query_map_response.bin")
        msg = protocol.decode_pboutput_envelope(envelope)
        if msg.btMap.ByteSize() < 200:
            pytest.skip("Fixture is state-echo only")
        catalog = protocol.parse_zone_catalog(msg.btMap)
        first_zone = catalog.zones[0]
        looked_up = catalog.zones_by_hashid.get(first_zone.hash_id)
        assert looked_up is first_zone

    def test_enu_base_point_extracted_from_synthetic_pbmap(self):
        """parse_zone_catalog reads PbMap.enuBasePoint (field 7) when present.

        The committed fixture is a small btMap without enuBasePoint, so we
        build a minimal PbBtMap → queryAck → PbMap synthetic to exercise
        the parser path. Mirrors the harness's empirical finding that the
        RTK base GPS lives inside the QUERY_MAP catalog.
        """
        import lymow_extracted_pb2 as pb

        # Build a real PbMap with just enuBasePoint and runtime_config set.
        pbmap = pb.PbMap()
        pbmap.enuBasePoint.latitude = 37.6390347
        pbmap.enuBasePoint.longitude = -97.4817202
        pbmap.enuBasePoint.altitude = 350.5
        inner_bytes = pbmap.SerializeToString()

        # Wrap in a queryAck (PbBtMap.queryAck = field 2, field 3 = inner bytes)
        # since parse_zone_catalog walks btMap → queryAck (field 2) → field 3.
        # Build minimal raw protobuf bytes for the wrapper.
        # Tag for field 3 (length-delimited) = (3 << 3) | 2 = 0x1A.
        from lymow_mqtt.protocol import _wire_varint  # noqa: F401  (just to confirm import)
        ln = len(inner_bytes)
        # Encode varint length manually (assumes <128 bytes is ok for tiny inner)
        def _varint(n):
            out = bytearray()
            while n > 0x7F:
                out.append((n & 0x7F) | 0x80)
                n >>= 7
            out.append(n & 0x7F)
            return bytes(out)
        qa_bytes = bytes([0x1A]) + _varint(ln) + inner_bytes

        # Build the outer PbBtMap with queryAck (field 2)
        btmap = pb.PbBtMap()
        # field 2 of PbBtMap is queryAck — we don't have its proto defined,
        # so reach in via raw serialize-merge: assemble a PbBtMap by parsing
        # a hand-rolled message that has field 2 = qa_bytes.
        # Tag for field 2 (length-delimited) = (2 << 3) | 2 = 0x12.
        outer_raw = bytes([0x12]) + _varint(len(qa_bytes)) + qa_bytes
        btmap.MergeFromString(outer_raw)

        catalog = protocol.parse_zone_catalog(btmap)
        assert catalog.enu_base_point is not None
        assert abs(catalog.enu_base_point.latitude - 37.6390347) < 1e-5
        assert abs(catalog.enu_base_point.longitude - (-97.4817202)) < 1e-5
        assert abs(catalog.enu_base_point.altitude - 350.5) < 1e-2

    def test_enu_base_point_none_when_pbmap_lacks_field(self):
        """Parser returns enu_base_point=None when the catalog has no field 7.

        This is the QUERY_PATH case — small btMap responses with path data
        only, no PbMap structure. The integration's coordinator depends on
        this signal to skip the sticky-state lift and preserve the prior
        enu_base_point across repeated QUERY_PATH responses.
        """
        envelope = load_fixture("query_map_response.bin")
        msg = protocol.decode_pboutput_envelope(envelope)
        catalog = protocol.parse_zone_catalog(msg.btMap)
        # Current fixture is small/sanitized and lacks enuBasePoint. If a
        # future fixture grows to include it, change this to skip-on-present.
        assert catalog.enu_base_point is None


class TestScheduleDecoder:
    def test_decode_schedule_fixture(self):
        envelope = load_fixture("schedule_response.bin")
        msg = protocol.decode_pboutput_envelope(envelope)
        if msg.schedule.ByteSize() == 0:
            pytest.skip("Fixture has no schedule field")
        schedules = protocol.decode_schedules(msg.schedule)
        assert len(schedules) >= 1
        s = schedules[0]
        assert 0 <= s.hour <= 23
        assert 0 <= s.minute <= 59
        # Days are a list of ints 0-6
        assert all(0 <= d <= 6 for d in s.days_of_week)

    def test_negative_timezone_sign_extends_correctly(self):
        """Per arch.md §5e, negative timeZone values come over the wire
        as 10-byte int64 varints. -5 should decode as -5, not as a huge int.

        PbSchedule's compiled pb2 is an empty placeholder, so we construct
        the wire-format bytes directly and feed them into the walker via a
        PbSchedules wrapper.
        """
        import lymow_extracted_pb2 as pb

        # Hand-build a PbSchedule wire-format payload:
        #   field 2 (hour) varint: tag=0x10, value=14
        #   field 3 (minute) varint: tag=0x18, value=30
        #   field 6 (id) varint: tag=0x30, value=12345 (=0xb9 0x60 -> varint 0xb9 0x60 = ...)
        #   field 7 (timeZone) int32-as-int64 varint: -5 = 10-byte 0xfb...01
        # Compose:
        def _enc_varint(n: int) -> bytes:
            out = bytearray()
            v = n & ((1 << 64) - 1)
            while True:
                byte = v & 0x7F
                v >>= 7
                if v:
                    out.append(byte | 0x80)
                else:
                    out.append(byte)
                    return bytes(out)

        # Build PbSchedule contents:
        sched_payload = bytearray()
        sched_payload += b"\x10" + _enc_varint(14)  # hour=14
        sched_payload += b"\x18" + _enc_varint(30)  # minute=30
        sched_payload += b"\x30" + _enc_varint(12345)  # id=12345
        # timeZone (-5) as 10-byte 64-bit varint (sign-extended int64)
        sched_payload += b"\x38" + _enc_varint((-5) & ((1 << 64) - 1))

        # Wrap in PbSchedules: field 1 (tasks) is repeated PbSchedule, length-delimited
        sub_len = _enc_varint(len(sched_payload))
        outer = b"\x0a" + sub_len + bytes(sched_payload)

        pb_schedules = pb.PbSchedules()
        pb_schedules.ParseFromString(outer)
        assert len(pb_schedules.tasks) == 1

        schedules = protocol.decode_schedules(pb_schedules)
        assert schedules[0].timezone_offset == -5
        assert schedules[0].hour == 14
        assert schedules[0].minute == 30
        assert schedules[0].id == 12345


class TestErrorWarningExtraction:
    def test_extract_error_codes_returns_list(self):
        import lymow_extracted_pb2 as pb
        msg = pb.PbOutput()
        msg.errorCodes.append(45)
        codes = protocol.extract_error_codes(msg)
        assert codes == [45]

    def test_extract_error_codes_empty_when_absent(self):
        import lymow_extracted_pb2 as pb
        msg = pb.PbOutput()
        assert protocol.extract_error_codes(msg) == []

    def test_extract_error_from_debug_description_url(self):
        """Per arch.md §7c, debugSetting.description carries S3 URLs
        with E-code in the filename: E45-v2.1.45-..."""
        url = "s3://lymow-device-log-us-east-2/device_test/E45-v2.1.45-22-30-log.zip"
        assert protocol.extract_error_from_debug_url(url) == 45

    def test_extract_error_from_debug_description_no_code(self):
        assert protocol.extract_error_from_debug_url("") is None
        assert protocol.extract_error_from_debug_url("not a url") is None

    def test_extract_warning_codes(self):
        import lymow_extracted_pb2 as pb
        msg = pb.PbOutput()
        msg.warningCodes.append(4)
        assert protocol.extract_warning_codes(msg) == [4]
