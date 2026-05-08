"""Tests for userctrl.py — command registry."""
from __future__ import annotations

import pytest

from lymow_mqtt import userctrl
from lymow_mqtt.const import (
    USER_CTRL_PAUSE, USER_CTRL_PAUSE_DOCK, USER_CTRL_RESUME, USER_CTRL_RESUME_DOCK,
    USER_CTRL_DOCK, USER_CTRL_RECHARGE_DOCK, USER_CTRL_FORCE_REINIT,
    WORK_STATUS_MOWING, WORK_STATUS_PAUSE, WORK_STATUS_DOCKING, WORK_STATUS_PAUSE_DOCKING,
)


class TestPickPauseVariant:
    def test_pause_from_mowing_returns_3(self):
        assert userctrl.pick_pause_variant(WORK_STATUS_MOWING) == USER_CTRL_PAUSE

    def test_pause_from_docking_returns_21(self):
        assert userctrl.pick_pause_variant(WORK_STATUS_DOCKING) == USER_CTRL_PAUSE_DOCK

    def test_pause_from_paused_state_returns_none_noop(self):
        assert userctrl.pick_pause_variant(WORK_STATUS_PAUSE) is None
        assert userctrl.pick_pause_variant(WORK_STATUS_PAUSE_DOCKING) is None

    def test_pause_from_unsupported_state_raises(self):
        # Mower in CHARGING (5) cannot be paused
        with pytest.raises(ValueError):
            userctrl.pick_pause_variant(5)


class TestPickResumeVariant:
    def test_resume_from_pause_returns_4(self):
        assert userctrl.pick_resume_variant(WORK_STATUS_PAUSE) == USER_CTRL_RESUME

    def test_resume_from_pause_docking_returns_22(self):
        assert userctrl.pick_resume_variant(WORK_STATUS_PAUSE_DOCKING) == USER_CTRL_RESUME_DOCK

    def test_resume_from_already_active_state_returns_none_noop(self):
        assert userctrl.pick_resume_variant(WORK_STATUS_MOWING) is None
        assert userctrl.pick_resume_variant(WORK_STATUS_DOCKING) is None


class TestExpectedPostStates:
    def test_pause_expects_paused_state(self):
        assert WORK_STATUS_PAUSE in userctrl.EXPECTED_POST_STATES[USER_CTRL_PAUSE]

    def test_recharge_dock_expects_docking_or_charging(self):
        states = userctrl.EXPECTED_POST_STATES[USER_CTRL_RECHARGE_DOCK]
        assert any(s in states for s in (4, 5))  # DOCKING or CHARGING

    def test_query_commands_have_no_expected_state(self):
        # Query commands return empty set — watchdog skips them
        from lymow_mqtt.const import USER_CTRL_QUERY_MAP
        assert userctrl.EXPECTED_POST_STATES.get(USER_CTRL_QUERY_MAP) == set()
