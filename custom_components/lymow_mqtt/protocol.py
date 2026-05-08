"""Lymow protocol layer — protobuf encode/decode + JSON envelope wrapping.

Wire format on /pbinput, /pboutput, /notify-app:
    JSON {"message": "<base64-encoded-protobuf-bytes>"}

This module is pure functions, fully unit-testable without HA.
"""
from __future__ import annotations

import base64
import json


def wrap_envelope(raw: bytes) -> str:
    """Wrap raw protobuf bytes in the JSON envelope used on AWS IoT topics."""
    return json.dumps({"message": base64.b64encode(raw).decode("ascii")})


def unwrap_envelope(envelope_bytes: bytes) -> bytes:
    """Strip the JSON envelope, return raw protobuf bytes."""
    envelope = json.loads(envelope_bytes.decode("utf-8"))
    return base64.b64decode(envelope["message"])
