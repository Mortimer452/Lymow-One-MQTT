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
        # btMap may be present (catalog reply) or absent (state-echo only)
        # but if it's present, ByteSize() > 200 indicates real content
        if msg.btMap.ByteSize() > 0:
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
