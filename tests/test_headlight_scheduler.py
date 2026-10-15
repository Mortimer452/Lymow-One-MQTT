"""Tests for headlight_scheduler.py — pure helpers behind the auto-headlights switch.

The HA glue (astral sunrise/sunset lookup, midnight time tracking, MQTT
publish) lives in the coordinator and isn't unit-testable here (no
homeassistant in the test env). What IS tested is the one piece of logic
that can silently go wrong: turning aware sunrise/sunset datetimes into
the UTC hour/minute pairs the firmware stores in openLedTime/closeLedTime.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

import pytest
from lymow_mqtt import headlight_scheduler as hs

CDT = timezone(timedelta(hours=-5))


class TestWindowFromSun:
    def test_headlights_on_at_sunset_off_at_sunrise_in_utc(self):
        """Central-time sunset 20:30 / sunrise 06:00 -> 01:30 / 11:00 UTC —
        exactly the window observed on the wire in
        passive_captures/capture_20260508_184353.json."""
        sunset = datetime(2026, 5, 8, 20, 30, 12, tzinfo=CDT)
        sunrise = datetime(2026, 5, 8, 6, 0, 45, tzinfo=CDT)
        w = hs.window_from_sun(sunrise=sunrise, sunset=sunset)
        assert (w.open_hour, w.open_minute) == (1, 30)
        assert (w.close_hour, w.close_minute) == (11, 0)

    def test_seconds_are_truncated_not_rounded(self):
        """Mirror the app's getUTCMinutes(): 20:30:59 -> minute 30."""
        sunset = datetime(2026, 5, 8, 20, 30, 59, tzinfo=UTC)
        sunrise = datetime(2026, 5, 8, 6, 59, 59, tzinfo=UTC)
        w = hs.window_from_sun(sunrise=sunrise, sunset=sunset)
        assert (w.open_hour, w.open_minute) == (20, 30)
        assert (w.close_hour, w.close_minute) == (6, 59)

    def test_rejects_naive_datetimes(self):
        """A naive datetime has no offset to convert from — refusing beats
        silently writing local hours into a UTC field."""
        with pytest.raises(ValueError):
            hs.window_from_sun(
                sunrise=datetime(2026, 5, 8, 6, 0),
                sunset=datetime(2026, 5, 8, 20, 30, tzinfo=UTC),
            )


class TestNightlyUpdateHour:
    def test_nightly_update_runs_at_3am_local(self):
        assert hs.NIGHTLY_UPDATE_HOUR == 3

    def test_nightly_update_hour_avoids_dst_transition_window(self):
        """US clocks jump at 02:00 local (spring: 02:00→03:00, so 02:xx never
        exists; fall: 01:xx repeats). EU jumps at 01:00 UTC. Any hour in
        01:00–02:59 can fire twice or not at all on a changeover night, so
        the nightly write must sit outside that window."""
        assert hs.NIGHTLY_UPDATE_HOUR not in (1, 2)
        assert 0 <= hs.NIGHTLY_UPDATE_HOUR <= 23
