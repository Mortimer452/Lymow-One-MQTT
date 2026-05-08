"""Verify no sensitive data in committed fixtures.

Decodes every fixture file in tests/fixtures/*.bin and asserts none of
the user's known-sensitive patterns appear. Pre-commit gate.
"""
from __future__ import annotations

import base64
import json
import re
import sys
from pathlib import Path

_HERE = Path(__file__).parent
_REPO_ROOT = _HERE.parent.parent
sys.path.insert(0, str(_REPO_ROOT / "custom_components" / "lymow_mqtt"))
import lymow_extracted_pb2 as pb  # noqa: E402

# Patterns that must NEVER appear in committed fixtures
SENSITIVE_PATTERNS = [
    re.compile(rb"device_3ba863e1d677"),       # original thing name
    re.compile(rb"60:FF:9E:D2:AD:77"),          # original MAC (with colons)
    re.compile(rb"60-FF-9E-D2-AD-77"),          # MAC with dashes
    re.compile(rb"Erebor", re.IGNORECASE),      # original WiFi SSID
    re.compile(rb"192\.168\."),                 # any RFC1918 IP leak
    re.compile(rb"10\.\d+\.\d+\.\d+"),
]


def check_file(path: Path) -> list[str]:
    """Return list of violation strings, or empty if clean."""
    violations = []
    raw = path.read_bytes()

    for pat in SENSITIVE_PATTERNS:
        if pat.search(raw):
            violations.append(f"{path.name}: matches {pat.pattern!r}")

    # If it's a JSON envelope, also inspect decoded protobuf string fields
    decoded_msg = None
    if raw.lstrip().startswith(b"{"):
        try:
            envelope = json.loads(raw.decode("utf-8"))
            inner = base64.b64decode(envelope["message"])
            msg = pb.PbOutput()
            msg.ParseFromString(inner)
            decoded_msg = msg
        except Exception:
            pass
    else:
        try:
            msg = pb.PbOutput()
            msg.ParseFromString(raw)
            decoded_msg = msg
        except Exception:
            pass

    if decoded_msg is not None and decoded_msg.deviceInfo.ByteSize() > 0:
        di = decoded_msg.deviceInfo
        if di.HasField("wifiSsid") and "Erebor" in di.wifiSsid:
            violations.append(f"{path.name}: wifiSsid not sanitized")
        if di.HasField("macAddress") and di.macAddress not in ("", "AA:BB:CC:DD:EE:FF"):
            violations.append(f"{path.name}: macAddress not sanitized ({di.macAddress!r})")
        if di.HasField("ipAddress") and di.ipAddress.startswith(("192.168.", "10.")):
            violations.append(f"{path.name}: ipAddress not sanitized ({di.ipAddress!r})")

    return violations


def main() -> int:
    fixtures = list(_HERE.glob("*.bin"))
    if not fixtures:
        print("No fixtures found; skipping check")
        return 0
    all_violations = []
    for f in fixtures:
        all_violations.extend(check_file(f))
    if all_violations:
        print("FIXTURE CLEANLINESS VIOLATIONS:")
        for v in all_violations:
            print(f"  - {v}")
        return 1
    print(f"All {len(fixtures)} fixtures clean.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
