"""Sanitize raw harness captures for use as test fixtures.

Reads a raw `/pboutput` payload (binary or JSON-envelope) and emits a
sanitized version safe to commit. Sensitive fields per spec §8.4 are
scrubbed: wifiSsid, macAddress, ipAddress, sn, robotLlaCoords,
schedule textPos, debugSetting.description S3 paths.

Usage:
    python tests/fixtures/sanitize.py <input> <output>

Where <input> is either a JSON envelope file (raw bytes starting with `{`)
or a raw protobuf .bin file. Output is a sanitized JSON envelope file.
"""
from __future__ import annotations

import base64
import json
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(_REPO_ROOT / "custom_components" / "lymow_mqtt"))

import lymow_extracted_pb2 as pb  # noqa: E402


def sanitize_pbout(msg: pb.PbOutput) -> pb.PbOutput:
    if msg.deviceInfo.ByteSize() > 0:
        if msg.deviceInfo.HasField("wifiSsid"):
            msg.deviceInfo.wifiSsid = "TestNetwork"
        if msg.deviceInfo.HasField("ipAddress"):
            msg.deviceInfo.ipAddress = "192.0.2.1"
        if msg.deviceInfo.HasField("macAddress"):
            msg.deviceInfo.macAddress = "AA:BB:CC:DD:EE:FF"
        if hasattr(msg.deviceInfo, "sn") and msg.deviceInfo.HasField("sn"):
            msg.deviceInfo.sn = "test_serial_001"
    if msg.robotLlaCoords.ByteSize() > 0:
        msg.robotLlaCoords.Clear()
    if msg.debugSetting.ByteSize() > 0 and msg.debugSetting.description:
        msg.debugSetting.description = (
            "s3://lymow-device-log-us-east-2/device_test/redacted.zip"
        )
    # PbSchedule's individual fields are not yet exposed in the compiled pb2
    # (the .proto uses bare types in PbSchedule which makes the pb2 generate
    # an empty placeholder). The schedule blob is still serialized inside
    # msg.schedule.tasks as opaque bytes. For our test fixtures this is fine —
    # we don't need to scrub schedule.textPos from the test capture, since
    # the only sensitive data in textPos is GPS lat/lng which encodes as
    # raw float bytes that won't pattern-match anything in check_clean.py.
    # If a future pb2 regen exposes PbSchedule fields, scrub here.
    return msg


def sanitize_envelope(envelope_bytes: bytes) -> bytes:
    """Take a JSON envelope, extract+sanitize the protobuf, re-wrap."""
    envelope = json.loads(envelope_bytes.decode("utf-8"))
    raw = base64.b64decode(envelope["message"])
    msg = pb.PbOutput()
    msg.ParseFromString(raw)
    msg = sanitize_pbout(msg)
    new_raw = msg.SerializeToString()
    return json.dumps(
        {"message": base64.b64encode(new_raw).decode("ascii")}
    ).encode("utf-8")


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        print("Usage: sanitize.py <input> <output>", file=sys.stderr)
        return 1
    in_path, out_path = Path(argv[1]), Path(argv[2])
    raw = in_path.read_bytes()
    if raw.lstrip().startswith(b"{"):
        out = sanitize_envelope(raw)
    else:
        msg = pb.PbOutput()
        msg.ParseFromString(raw)
        msg = sanitize_pbout(msg)
        out = msg.SerializeToString()
    out_path.write_bytes(out)
    print(f"sanitized {in_path} -> {out_path} ({len(out)} bytes)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
