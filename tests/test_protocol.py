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
