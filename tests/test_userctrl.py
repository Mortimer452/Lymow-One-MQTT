"""Tests for userctrl.py — watchdog post-state expectations.

The pause/resume variant pickers that used to live in userctrl.py have
moved to state_matrix.py — see tests/test_state_matrix.py for variant
selection coverage. This file just tests the watchdog table.
"""
from __future__ import annotations

from lymow_mqtt import userctrl
from lymow_mqtt.const import (
    USER_CTRL_PAUSE,
    USER_CTRL_QUERY_MAP,
    USER_CTRL_RECHARGE_DOCK,
    WORK_STATUS_PAUSE,
)


class TestExpectedPostStates:
    def test_pause_expects_paused_state(self):
        assert WORK_STATUS_PAUSE in userctrl.EXPECTED_POST_STATES[USER_CTRL_PAUSE]

    def test_recharge_dock_expects_docking_or_charging(self):
        states = userctrl.EXPECTED_POST_STATES[USER_CTRL_RECHARGE_DOCK]
        assert any(s in states for s in (4, 5))  # DOCKING or CHARGING

    def test_query_commands_have_no_expected_state(self):
        # Query commands return empty set — watchdog skips them
        assert userctrl.EXPECTED_POST_STATES.get(USER_CTRL_QUERY_MAP) == set()
