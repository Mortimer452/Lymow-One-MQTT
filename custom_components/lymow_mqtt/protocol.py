"""Lymow protocol layer — protobuf encode/decode + JSON envelope wrapping.

Wire format on /pbinput, /pboutput, /notify-app:
    JSON {"message": "<base64-encoded-protobuf-bytes>"}

This module is pure functions, fully unit-testable without HA.
"""
from __future__ import annotations

import base64
import json
import re
import struct
from dataclasses import dataclass, field

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


def encode_upload_robot_config() -> bytes:
    """Encode the L3 wakeup payload that triggers a robotConfig broadcast.

    Per arch.md §7a Layer 3, sending PbInput with
    `debugSetting.uploadRobotConfig=true` makes the firmware re-broadcast its
    current `robotConfig` (containing rrConfig auto-recharge thresholds among
    other things). No userCtrl required — the firmware reacts to the debug
    flag directly.

    Used at integration startup so entities backed by `robotConfig` (the
    auto-recharge switch) become available without waiting for a state-burst.
    """
    pb_in = pb.PbInput()
    pb_in.version = PB_VERSION_4_9
    pb_in.debugSetting.uploadRobotConfig = True
    return pb_in.SerializeToString()


def encode_set_rr_config(
    *,
    enable_rr: bool,
    recharge_bat: int | None,
    resume_bat: int | None,
    period_start_hour: int | None,
    period_start_minute: int | None,
    period_end_hour: int | None,
    period_end_minute: int | None,
) -> bytes:
    """Encode the no-userCtrl `setRR` payload (arch.md §6g).

    Writes `robotConfig.rrConfig` with the supplied fields and triggers a
    confirmation broadcast via `debugSetting.uploadRobotConfig=true`. The
    firmware applies the new rrConfig within ~1s and broadcasts back the
    updated PbRobotConfig so we can verify the write took.

    Caller is responsible for "carry-forward" preservation: read the current
    rrConfig from coordinator state, mutate only the field(s) being changed,
    pass the rest unchanged so they aren't reset to firmware defaults.
    Verified via spike_set_rrconfig.py round-trip on 2026-05-10.
    """
    pb_in = pb.PbInput()
    pb_in.version = PB_VERSION_4_9
    # Deliberately NO userCtrl — setRR is the no-userCtrl pattern (§6g).
    rr = pb_in.robotConfig.rrConfig
    rr.enableRr = enable_rr
    if recharge_bat is not None:
        rr.rechargeBat = recharge_bat
    if resume_bat is not None:
        rr.resumeBat = resume_bat
    if period_start_hour is not None:
        rr.resumePeriodStart.hour = period_start_hour
    if period_start_minute is not None:
        rr.resumePeriodStart.minute = period_start_minute
    if period_end_hour is not None:
        rr.resumePeriodEnd.hour = period_end_hour
    if period_end_minute is not None:
        rr.resumePeriodEnd.minute = period_end_minute
    pb_in.debugSetting.uploadRobotConfig = True
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
    """Decode a protobuf varint. Capped at 10 bytes per the spec (64-bit max)."""
    r, s = 0, 0
    for _ in range(10):
        b = buf[pos]
        pos += 1
        r |= (b & 0x7F) << s
        if not (b & 0x80):
            return r, pos
        s += 7
    raise ValueError("varint exceeds 10 bytes")


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


@dataclass
class ZoneInfo:
    """One go-zone from the catalog."""

    hash_id: str
    name: str
    mow_order: int  # 0 if not in active task
    is_enabled: bool
    polygon_points: list[tuple[float, float]]  # local-frame (x, y) pairs
    # Optional per-zone config (PbZoneConfig). Populated by parse_zone_catalog
    # when the inner PbZone carries a zoneConfig sub-message. Used by
    # state.active_cut_config for the per-zone tier of the inheritance walker.
    zone_config: object | None = None


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
    # PbRunTimeConfig from PbMap.runTimeConfig (field 13). Carries cutHeight,
    # cutSpeed, moveSpeed for the global runtime tier of active_cut_config.
    # Different from PbRobotConfig (which has rcCutHeight/rcCutSpeed but no
    # moveSpeed) — the harness reads from this field at parse_btmap_payload
    # (harness.py:1064-1079).
    runtime_config: object | None = None
    # PbMap.enuBasePoint (field 7) — surveyed RTK base GPS, anchor of the
    # local ENU frame. Set during initial RTK base survey; the dock is a
    # separate physical unit. Used by the GPS device_tracker entities to
    # convert pose (local meters) into live mower lat/lon. See arch.md §8c.
    enu_base_point: object | None = None


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
    catalog = ZoneCatalog()
    HASHID_RE = re.compile(r"^[A-Za-z0-9_]{4,16}$")

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
            # Parse field 2 (zoneConfig) into a PbZoneConfig if present so
            # state.active_cut_config can read per-zone overrides via HasField.
            if 2 in zone_msg:
                try:
                    zc_bytes = zone_msg[2][0][1]
                    zc = pb.PbZoneConfig()
                    zc.ParseFromString(zc_bytes)
                    zi.zone_config = zc
                except Exception:
                    zi.zone_config = None
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

    # Inner.field_13: PbRunTimeConfig — global cutHeight/cutSpeed/moveSpeed.
    # Distinct from PbRobotConfig; this is the source the harness reads for
    # the "Runtime Config" panel (harness.py:1064-1079, arch.md §6 - cut config).
    if 13 in inner:
        try:
            rtc_bytes = inner[13][0][1]
            rtc = pb.PbRunTimeConfig()
            rtc.ParseFromString(rtc_bytes)
            catalog.runtime_config = rtc
        except Exception:
            catalog.runtime_config = None

    # Inner.field_7: PbRobotLLACoords — RTK base station GPS anchor of the
    # local ENU frame (arch.md §8c). Sticky once captured; the coordinator
    # promotes this to its own state slot so it survives QUERY_PATH responses
    # which arrive in the same btMap branch but carry no PbMap structure.
    if 7 in inner:
        try:
            ebp_bytes = inner[7][0][1]
            ebp = pb.PbRobotLLACoords()
            ebp.ParseFromString(ebp_bytes)
            catalog.enu_base_point = ebp
        except Exception:
            catalog.enu_base_point = None

    return catalog


# ---------------------------------------------------------------------------
# Schedule decoder
# ---------------------------------------------------------------------------

_DAYS_NAMES = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"]


@dataclass
class ScheduleConfig:
    """Per-zone runtime override inside a PbSchedule (PbScheduleConfig)."""

    hash_id: str
    cut_height: int | None
    move_speed: float | None
    clean_dir: int | None


@dataclass
class ScheduleInfo:
    """Decoded weekly schedule entry (arch.md §5e)."""

    id: int
    days_of_week: list[int]  # 0=Sun, 1=Mon, ..., 6=Sat
    hour: int
    minute: int
    is_repeated: bool
    is_disabled: bool
    is_angle_offset: bool
    mow_angle: int
    timezone_offset: int  # UTC hour offset (-12..+14)
    zone_hash_ids: list[str]  # in mowOrder order
    zones: list[dict] = field(default_factory=list)  # full zone basic-info dicts
    config: list[ScheduleConfig] = field(default_factory=list)


def _decode_schedule_task(buf: bytes) -> dict:
    """Walk one PbSchedule's wire-format payload (harness.py:961).

    Schema recovered from decompiled.js:397250-397528 + lymow_extracted.proto:
        1 dayOfWeek    repeated enum (packed)
        2 hour         int32
        3 minute       int32
        4 isRepeated   bool
        5 zonesInfo    repeated PbZoneBasicInfo
        6 id           uint32
        7 timeZone     int32 (sign-extended as 10-byte int64 varint)
        8 isDisabled   bool
        9 isAngleOffset bool
        10 mowAngle    int32
        11 config      repeated PbScheduleConfig
    """
    f = _wire_parse(buf)
    out: dict = {
        "dayOfWeek": [],
        "dayNames": [],
        "hour": f.get(2, [(None, 0)])[0][1] if 2 in f else 0,
        "minute": f.get(3, [(None, 0)])[0][1] if 3 in f else 0,
        "isRepeated": bool(f.get(4, [(None, 0)])[0][1]) if 4 in f else False,
        "id": f.get(6, [(None, 0)])[0][1] if 6 in f else 0,
        "timeZone": 0,
        "isDisabled": bool(f.get(8, [(None, 0)])[0][1]) if 8 in f else False,
        "isAngleOffset": bool(f.get(9, [(None, 0)])[0][1]) if 9 in f else False,
        "mowAngle": f.get(10, [(None, 0)])[0][1] if 10 in f else 0,
        "zones": [],
        "config": [],
    }
    # dayOfWeek (field 1) — packed-repeated enum varints inside a single LEN payload.
    if 1 in f:
        for tag, val in f[1]:
            if tag != "L":
                continue
            day_buf = val
            pos = 0
            while pos < len(day_buf):
                d, pos = _wire_varint(day_buf, pos)
                out["dayOfWeek"].append(d)
                if 0 <= d < 7:
                    out["dayNames"].append(_DAYS_NAMES[d])
    # timeZone (field 7) — int32 sign-extended over 10-byte int64 varint.
    if 7 in f:
        raw = f[7][0][1]
        if raw > 0x7FFFFFFFFFFFFFFF:
            raw -= 1 << 64
        out["timeZone"] = raw
    # zonesInfo (field 5) — repeated PbZoneBasicInfo.
    if 5 in f:
        for tag, val in f[5]:
            if tag != "L":
                continue
            try:
                out["zones"].append(_parse_pbzone_basicinfo(val))
            except Exception:
                pass
    # config (field 11) — repeated PbScheduleConfig.
    if 11 in f:
        for tag, val in f[11]:
            if tag != "L":
                continue
            try:
                cfg = _wire_parse(val)
                entry: dict = {
                    "hashId": _wire_str(cfg[1][0][1]) if 1 in cfg else "",
                    "cutHeight": cfg.get(2, [(None, None)])[0][1] if 2 in cfg else None,
                    "moveSpeed": None,
                    "cleanDir": cfg.get(4, [(None, None)])[0][1] if 4 in cfg else None,
                }
                if 3 in cfg:
                    entry["moveSpeed"] = struct.unpack("<f", cfg[3][0][1])[0]
                out["config"].append(entry)
            except Exception:
                pass
    return out


def decode_schedules(pb_schedules) -> list[ScheduleInfo]:
    """Decode PbSchedules into a list of ScheduleInfo.

    The compiled pb2 module's PbSchedule is an empty placeholder (the .proto
    field declarations don't survive proto2 compilation), so this walks each
    task's serialized bytes via the wire-format walker. Per the corrections
    note: PbSchedules has a single `tasks` field (repeated PbSchedule), not
    `schedules`.
    """
    out: list[ScheduleInfo] = []
    for task in pb_schedules.tasks:
        decoded = _decode_schedule_task(task.SerializeToString())
        # Order zone_hash_ids by mowOrder, falling back to insertion order.
        zones = decoded.get("zones") or []
        ordered_zones = sorted(
            (z for z in zones if z.get("hashId")),
            key=lambda z: int(z.get("mowOrder") or 0),
        )
        zone_hash_ids = [z["hashId"] for z in ordered_zones]
        configs = [
            ScheduleConfig(
                hash_id=c.get("hashId") or "",
                cut_height=c.get("cutHeight"),
                move_speed=c.get("moveSpeed"),
                clean_dir=c.get("cleanDir"),
            )
            for c in (decoded.get("config") or [])
        ]
        out.append(
            ScheduleInfo(
                id=int(decoded.get("id") or 0),
                days_of_week=list(decoded.get("dayOfWeek") or []),
                hour=int(decoded.get("hour") or 0),
                minute=int(decoded.get("minute") or 0),
                is_repeated=bool(decoded.get("isRepeated")),
                is_disabled=bool(decoded.get("isDisabled")),
                is_angle_offset=bool(decoded.get("isAngleOffset")),
                mow_angle=int(decoded.get("mowAngle") or 0),
                timezone_offset=int(decoded.get("timeZone") or 0),
                zone_hash_ids=zone_hash_ids,
                zones=zones,
                config=configs,
            )
        )
    return out


# ---------------------------------------------------------------------------
# Error / warning code extraction
# ---------------------------------------------------------------------------

_E_CODE_REGEX = re.compile(r"/E(\d+)-")


def extract_error_codes(msg: pb.PbOutput) -> list[int]:
    """Return the list of currently-active error codes.

    Per arch.md §7c, errorCodes is the preferred signal — populated
    when robotStatus=Error. The list is packed repeated int32 and can
    carry multiple simultaneous errors.
    """
    return list(msg.errorCodes)


def extract_warning_codes(msg: pb.PbOutput) -> list[int]:
    """Return the list of currently-active warning codes.

    Known: warningCodes=[4] is tip-over (arch.md §11). Other values
    likely exist; map as observed.
    """
    return list(msg.warningCodes)


def extract_error_from_debug_url(description: str) -> int | None:
    """Extract error code from a debugSetting.description S3 log URL.

    Format: s3://.../device_X/E<code>-v<fw>-<HH-MM>-log.zip
    Used as a fallback signal for error onset (arch.md §7c).
    """
    if not description:
        return None
    match = _E_CODE_REGEX.search(description)
    if match:
        return int(match.group(1))
    return None
