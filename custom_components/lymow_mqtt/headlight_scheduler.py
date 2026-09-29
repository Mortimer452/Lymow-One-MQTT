"""Pure helpers behind the auto-headlights switch.

The mower's "Headlight mode" (app: NightModeScreen) is a fixed daily
window stored on the device as `robotConfig.openLedTime` / `closeLedTime`,
each a `PbTimeZone {hour, minute}` in **UTC** (decompiled.js:328737).
The auto-headlights feature re-writes that window every night at
NIGHTLY_UPDATE_HOUR local so the lights come on at that day's sunset and
go off at sunrise.

This module is deliberately free of Home Assistant imports so the
conversion can be unit-tested. The HA glue — astral lookup, the nightly
time-change listener, and the MQTT publish — lives in coordinator.py.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

# Local hour at which the nightly re-write fires. 03:00 deliberately sits
# outside the 01:00–02:59 window where DST changeovers happen (US clocks
# jump at 02:00 local; EU at 01:00 UTC), so the listener fires exactly
# once on changeover nights instead of twice (fall back) or never (spring
# forward). Midnight would also be safe, but 3 AM keeps clear of any
# date-rollover edge cases in the sun lookup as well.
NIGHTLY_UPDATE_HOUR = 3


@dataclass(frozen=True)
class HeadlightWindow:
    """UTC hour/minute pairs ready for `protocol.encode_set_night_mode`."""

    open_hour: int
    open_minute: int
    close_hour: int
    close_minute: int


def _utc_hm(dt: datetime, label: str) -> tuple[int, int]:
    if dt.tzinfo is None or dt.utcoffset() is None:
        raise ValueError(f"{label} must be timezone-aware")
    u = dt.astimezone(UTC)
    # Truncate seconds like the app's getUTCHours()/getUTCMinutes().
    return u.hour, u.minute


def window_from_sun(*, sunrise: datetime, sunset: datetime) -> HeadlightWindow:
    """Headlights ON at sunset, OFF at sunrise — both converted to UTC h/m."""
    open_h, open_m = _utc_hm(sunset, "sunset")
    close_h, close_m = _utc_hm(sunrise, "sunrise")
    return HeadlightWindow(open_h, open_m, close_h, close_m)
