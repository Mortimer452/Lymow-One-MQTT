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


# ---------------------------------------------------------------------------
# Wire-format walker — ported from harness.py
# ---------------------------------------------------------------------------
# These low-level helpers parse raw protobuf bytes without a generated schema.
# Necessary for fields (e.g. PbSchedule, PbZoneBasicInfo) where the compiled
# pb2 module is an empty placeholder due to .proto declaration quirks.

def _wire_varint(buf: bytes, pos: int) -> tuple[int, int]:
    r, s = 0, 0
    while True:
        b = buf[pos]
        pos += 1
        r |= (b & 0x7F) << s
        if not (b & 0x80):
            return r, pos
        s += 7


def _wire_parse(buf: bytes) -> dict:
    """Walk a raw protobuf message, returning {field_no: [(wire_kind, value), ...]}.

    wire_kind is one of: "v" (varint), "f64", "f32", "L" (length-delimited bytes).
    """
    out: dict[int, list[tuple[str, object]]] = {}
    p = 0
    while p < len(buf):
        try:
            tag, p = _wire_varint(buf, p)
        except IndexError:
            break
        fno, wt = tag >> 3, tag & 7
        if wt == 0:
            v, p = _wire_varint(buf, p)
            out.setdefault(fno, []).append(("v", v))
        elif wt == 1:
            v = buf[p:p + 8]
            p += 8
            out.setdefault(fno, []).append(("f64", v))
        elif wt == 2:
            ln, p = _wire_varint(buf, p)
            out.setdefault(fno, []).append(("L", buf[p:p + ln]))
            p += ln
        elif wt == 5:
            v = buf[p:p + 4]
            p += 4
            out.setdefault(fno, []).append(("f32", v))
        else:
            break
    return out


def _wire_str(buf: bytes) -> str | None:
    """Decode a length-delimited blob as UTF-8 string. Return None if not printable."""
    try:
        s = buf.decode("utf-8")
        if s.isprintable():
            return s
    except UnicodeDecodeError:
        pass
    return None


def _parse_pbzone_basicinfo(buf: bytes) -> dict:
    """Walk PbZoneBasicInfo wire format (harness.py:904).

    Schema (from lymow_extracted.proto):
      1 type        int32
      2 name        string
      3 hashId      string
      4 isEnabled   bool
      5 polygon     PbPolygon (repeated PbPoint points = 1)
      6 zoneRename  string
      7 updateTime  uint64
      8 mowOrder    int32
      9 mowOrderTextPos PbPoint
    """
    import struct

    f = _wire_parse(buf)
    out: dict[str, object] = {
        "type": f.get(1, [(None, None)])[0][1] if 1 in f else None,
        "name": _wire_str(f[2][0][1]) if 2 in f else "",
        "hashId": _wire_str(f[3][0][1]) if 3 in f else "",
        "isEnabled": bool(f.get(4, [(None, 0)])[0][1]) if 4 in f else None,
        "zoneRename": _wire_str(f[6][0][1]) if 6 in f else "",
        "updateTime": f.get(7, [(None, 0)])[0][1] if 7 in f else None,
        "mowOrder": f.get(8, [(None, 0)])[0][1] if 8 in f else 0,
        "polygon": [],
        "textPos": None,
    }
    # polygon (field 5) → PbPolygon { repeated PbPoint points = 1 }
    if 5 in f:
        try:
            poly_msg = _wire_parse(f[5][0][1])
            for t, v in poly_msg.get(1, []):
                if t != "L":
                    continue
                pt_msg = _wire_parse(v)
                if 1 in pt_msg and 2 in pt_msg:
                    x = struct.unpack("<f", pt_msg[1][0][1])[0]
                    y = struct.unpack("<f", pt_msg[2][0][1])[0]
                    out["polygon"].append((x, y))  # type: ignore[attr-defined]
        except Exception:
            pass
    # mowOrderTextPos (field 9) → PbPoint
    if 9 in f:
        try:
            pt_msg = _wire_parse(f[9][0][1])
            if 1 in pt_msg and 2 in pt_msg:
                out["textPos"] = {
                    "x": struct.unpack("<f", pt_msg[1][0][1])[0],
                    "y": struct.unpack("<f", pt_msg[2][0][1])[0],
                }
        except Exception:
            pass
    return out


# ---------------------------------------------------------------------------
# Zone catalog parser
# ---------------------------------------------------------------------------

from dataclasses import dataclass, field


@dataclass
class ZoneInfo:
    """One go-zone from the catalog."""

    hash_id: str
    name: str
    mow_order: int  # 0 if not in active task
    is_enabled: bool
    polygon_points: list[tuple[float, float]]  # local-frame (x, y) pairs


@dataclass
class ChannelInfo:
    """Navigation corridor between zones / to dock."""

    hash_id: str
    zone1: str  # source zone hashId or "charging_area"
    zone2: str  # destination zone hashId
    is_docking_channel: bool
    polygon_points: list[tuple[float, float]]


@dataclass
class ZoneCatalog:
    zones: list[ZoneInfo] = field(default_factory=list)
    channels: list[ChannelInfo] = field(default_factory=list)
    zones_by_hashid: dict[str, ZoneInfo] = field(default_factory=dict)


def parse_zone_catalog(bt_map) -> ZoneCatalog:
    """Parse a PbBtMap from a QUERY_MAP response into a ZoneCatalog.

    Ported from harness.py:1034 (parse_btmap_payload). Walks the raw wire
    format because the .proto schema for the inner btMap → queryAck → field-3
    blob isn't fully wired through the compiled pb2 module.

    The btMap blob has two response shapes (arch.md §8b):
      - 26KB periodic broadcast: queryAck inner blob has repeated {x,y} pairs
        in field 1 (boundary polygons + paths).
      - 48KB QUERY_MAP response: queryAck inner blob is a PbMap with
        field 1 = repeated PbZone (with rich basicInfo: name, hashId, polygon,
        mowOrder), field 2 = repeated PbNoGoZone, field 3 = repeated PbChannel.

    Only the rich response shape produces a populated catalog.
    """
    import re as _re
    import struct

    catalog = ZoneCatalog()
    HASHID_RE = _re.compile(r"^[A-Za-z0-9_]{4,16}$")

    btmap_bytes = bt_map.SerializeToString()
    btmap = _wire_parse(btmap_bytes)

    # Direct path: btMap → queryAck (field 2) → inner blob (field 3 of queryAck)
    inner: dict = {}
    try:
        if 2 in btmap:
            qa = _wire_parse(btmap[2][0][1])
            if 3 in qa:
                inner = _wire_parse(qa[3][0][1])
    except Exception:
        return catalog

    # Extract zone metadata from inner.field_1 (repeated PbZone), where each
    # PbZone has PbZoneBasicInfo at its field 1.
    for t, v in inner.get(1, []):
        if t != "L":
            continue
        # Skip PbPoint entries (small + just two 4-byte fields).
        if len(v) <= 12:
            continue
        try:
            zone_msg = _wire_parse(v)
            if 1 not in zone_msg:
                continue
            bi = _parse_pbzone_basicinfo(zone_msg[1][0][1])
            if not bi.get("hashId") or not HASHID_RE.match(bi["hashId"]):
                continue
            zi = ZoneInfo(
                hash_id=bi["hashId"],
                name=bi.get("name") or bi.get("zoneRename") or bi["hashId"],
                mow_order=int(bi.get("mowOrder") or 0),
                is_enabled=bool(bi.get("isEnabled")) if bi.get("isEnabled") is not None else True,
                polygon_points=list(bi.get("polygon") or []),
            )
            catalog.zones.append(zi)
            catalog.zones_by_hashid[zi.hash_id] = zi
        except Exception:
            continue

    # Channels (inner.field_3): each has hashId/zone1/zone2/polygon/isDockingChannel
    for t, v in inner.get(3, []):
        if t != "L":
            continue
        try:
            ch_msg = _wire_parse(v)
            ch_hash = _wire_str(ch_msg[1][0][1]) if 1 in ch_msg else ""
            ch_z1 = _wire_str(ch_msg[2][0][1]) if 2 in ch_msg else ""
            ch_z2 = _wire_str(ch_msg[3][0][1]) if 3 in ch_msg else ""
            if not (ch_hash and HASHID_RE.match(ch_hash)):
                continue
            ch_poly: list[tuple[float, float]] = []
            if 5 in ch_msg:
                try:
                    poly_msg = _wire_parse(ch_msg[5][0][1])
                    for t2, v2 in poly_msg.get(1, []):
                        if t2 != "L":
                            continue
                        pt_msg = _wire_parse(v2)
                        if 1 in pt_msg and 2 in pt_msg:
                            x = struct.unpack("<f", pt_msg[1][0][1])[0]
                            y = struct.unpack("<f", pt_msg[2][0][1])[0]
                            ch_poly.append((x, y))
                except Exception:
                    pass
            is_dock = bool(ch_msg.get(6, [(None, 0)])[0][1]) if 6 in ch_msg else False
            catalog.channels.append(
                ChannelInfo(
                    hash_id=ch_hash,
                    zone1=ch_z1 or "",
                    zone2=ch_z2 or "",
                    is_docking_channel=is_dock,
                    polygon_points=ch_poly,
                )
            )
        except Exception:
            continue

    return catalog
