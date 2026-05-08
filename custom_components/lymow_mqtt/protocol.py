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


def encode_query_map() -> bytes:
    """Encode PbInput for USER_CTRL_QUERY_MAP (19) with btMap.queryMap=true.

    Without the btMap flag the mower returns nothing meaningful (arch.md §6d).
    Response is a full state echo plus zone-catalog blob (arch.md §8b).
    """
    pb_in = pb.PbInput()
    pb_in.userCtrl = 19
    pb_in.version = PB_VERSION_4_9
    pb_in.btMap.queryMap = True
    return pb_in.SerializeToString()


def encode_start_zones(zone_hash_ids: list[str]) -> bytes:
    """Encode PbInput for USER_CTRL_CLEAN (1) with optional goZones list.

    With an empty list, the firmware uses the default rotation (last-used
    zones from the device's catalog). With a populated list, each zone is
    given a sequential mowOrder (1, 2, ..., N) per arch.md §6d.
    """
    pb_in = pb.PbInput()
    pb_in.userCtrl = 1
    pb_in.version = PB_VERSION_4_9
    for i, hash_id in enumerate(zone_hash_ids, start=1):
        zone = pb_in.map.goZones.add()
        zone.basicInfo.hashId = hash_id
        zone.basicInfo.mowOrder = i
    return pb_in.SerializeToString()


def decode_pboutput(raw: bytes) -> pb.PbOutput:
    """Parse raw protobuf bytes as PbOutput. Raises on malformed input."""
    msg = pb.PbOutput()
    msg.ParseFromString(raw)
    return msg


def decode_pboutput_envelope(envelope_bytes: bytes) -> pb.PbOutput:
    """Decode either a JSON envelope or raw protobuf as PbOutput.

    Real /pboutput payloads come over the wire wrapped in a JSON envelope
    (`{"message": "<base64>"}`); test fixtures may be either format.
    """
    stripped = envelope_bytes.lstrip()
    if stripped.startswith(b"{"):
        return decode_pboutput(unwrap_envelope(envelope_bytes))
    return decode_pboutput(envelope_bytes)


def populated_fields(msg: pb.PbOutput) -> list[str]:
    """Return the names of fields actually present in the message.

    Used by state-merge to know which submessages to refresh in the
    coordinator's state dict.
    """
    return [field.name for field, _ in msg.ListFields()]
