"""Lymow protocol layer — protobuf encode/decode + JSON envelope wrapping.

Wire format on /pbinput, /pboutput, /notify-app:
    JSON {"message": "<base64-encoded-protobuf-bytes>"}

This module is pure functions, fully unit-testable without HA.
"""
from __future__ import annotations

import base64
import json

from . import lymow_extracted_pb2 as pb

PB_VERSION_4_9 = 40  # PbVersion.PB_VERSION_4_9 — required on every PbInput


def wrap_envelope(raw: bytes) -> str:
    """Wrap raw protobuf bytes in the JSON envelope used on AWS IoT topics."""
    return json.dumps({"message": base64.b64encode(raw).decode("ascii")})


def unwrap_envelope(envelope_bytes: bytes) -> bytes:
    """Strip the JSON envelope, return raw protobuf bytes."""
    envelope = json.loads(envelope_bytes.decode("utf-8"))
    return base64.b64decode(envelope["message"])


def encode_userctrl(user_ctrl: int) -> bytes:
    """Encode a minimal PbInput { userCtrl: N, version: 40 }.

    Used for pause, resume, dock, recharge_dock, queries, cancel_task,
    and most other commands that don't need extra payload fields.
    """
    pb_in = pb.PbInput()
    pb_in.userCtrl = user_ctrl
    pb_in.version = PB_VERSION_4_9
    return pb_in.SerializeToString()
