"""Tests for state_matrix.py — the lawn-mower decision table.

The matrix is the single source of truth for:
  (work_status, robot_status, is_recharging) → (activity, button-actions)

These tests parametrize over both real-world cases and the bug we
specifically refactored to fix (rs=PAUSE, ws=MOWING).
"""
from __future__ import annotations

import pytest

from lymow_mqtt import state_matrix
from lymow_mqtt.const import (
    USER_CTRL_CLEAN,
    USER_CTRL_PAUSE,
    USER_CTRL_PAUSE_DOCK,
    USER_CTRL_RECHARGE_DOCK,
    USER_CTRL_RESUME,
    USER_CTRL_RESUME_DOCK,
    WORK_STATUS_CHARGING,
    WORK_STATUS_CHARGING_FULL,
    WORK_STATUS_DOCKING,
    WORK_STATUS_EMERGENCY_STOP,
    WORK_STATUS_ERROR,
    WORK_STATUS_ESCAPING,
    WORK_STATUS_MOWING,
    WORK_STATUS_NONE,
    WORK_STATUS_PAUSE,
    WORK_STATUS_PAUSE_DOCKING,
    WORK_STATUS_RESUME,
    WORK_STATUS_WAITING,
    WORK_STATUS_ZONE_PARTITION,
)


class TestPriorityOrder:
    """Physical state (robot_status) overrides task intent (work_status)
    for the four states where they can disagree."""

    def test_rs_error_overrides_ws_mowing(self):
        """Mid-mow blade jam: rs=ERROR but ws=MOWING (firmware lag).
        Activity must reflect physical truth."""
        row = state_matrix.lookup(
            work_status=WORK_STATUS_MOWING,
            robot_status=WORK_STATUS_ERROR,
            is_recharging=False,
        )
        assert row.activity == "error"
        # Pause clears the error per arch.md §6b
        assert row.pause == USER_CTRL_PAUSE

    def test_rs_pause_overrides_ws_mowing(self):
        """The bug this refactor fixed: paused mid-mow but ws hasn't caught up.

        Pre-refactor, the cascading-if checked ws only and reported MOWING.
        Now the matrix sees rs=PAUSE first and reports PAUSED, with
        Start routing to RESUME (not CLEAN, which would reset task progress).
        """
        row = state_matrix.lookup(
            work_status=WORK_STATUS_MOWING,
            robot_status=WORK_STATUS_PAUSE,
            is_recharging=False,
        )
        assert row.activity == "paused"
        assert row.start_mowing == USER_CTRL_RESUME, "Start must Resume, not fresh-start"
        assert row.pause is None, "Pause hidden (already paused)"
        assert row.dock == USER_CTRL_RECHARGE_DOCK

    def test_rs_charging_overrides_ws_docking(self):
        """Mower has arrived at dock: rs=CHARGING but ws may still say DOCKING."""
        row = state_matrix.lookup(
            work_status=WORK_STATUS_DOCKING,
            robot_status=WORK_STATUS_CHARGING,
            is_recharging=True,
        )
        assert row.activity == "docked"
        assert row.start_mowing == USER_CTRL_RESUME, "Saved task → Resume"

    def test_rs_emergency_stop_blocks_all_buttons(self):
        row = state_matrix.lookup(
            work_status=WORK_STATUS_MOWING,
            robot_status=WORK_STATUS_EMERGENCY_STOP,
            is_recharging=False,
        )
        assert row.activity == "error"
        assert row.start_mowing is None
        assert row.pause is None
        assert row.dock is None


class TestChargingForkOnIsRecharging:
    """The is_recharging flag forks the Start-button behavior between
    'resume the saved task' and 'start a fresh mow' — this is the PR #2
    bug-class. Matrix exposes both rows side-by-side."""

    def test_charging_with_saved_task_resumes(self):
        row = state_matrix.lookup(
            work_status=WORK_STATUS_CHARGING,
            robot_status=WORK_STATUS_CHARGING,
            is_recharging=True,
        )
        assert row.start_mowing == USER_CTRL_RESUME, "Saved task → Resume"

    def test_charging_idle_starts_fresh(self):
        row = state_matrix.lookup(
            work_status=WORK_STATUS_CHARGING,
            robot_status=WORK_STATUS_CHARGING,
            is_recharging=False,
        )
        assert row.start_mowing == USER_CTRL_CLEAN, "No saved task → fresh CLEAN"

    def test_charging_full_with_saved_task_resumes(self):
        row = state_matrix.lookup(
            work_status=WORK_STATUS_CHARGING_FULL,
            robot_status=WORK_STATUS_CHARGING_FULL,
            is_recharging=True,
        )
        assert row.start_mowing == USER_CTRL_RESUME

    def test_charging_full_idle_starts_fresh(self):
        row = state_matrix.lookup(
            work_status=WORK_STATUS_CHARGING_FULL,
            robot_status=WORK_STATUS_CHARGING_FULL,
            is_recharging=False,
        )
        assert row.start_mowing == USER_CTRL_CLEAN


class TestActiveTaskStates:
    """ws ∈ {MOWING, RESUME, ZONE_PARTITION, ESCAPING} all map to
    Activity=mowing with Pause + Dock buttons."""

    @pytest.mark.parametrize("ws", [
        WORK_STATUS_MOWING,
        WORK_STATUS_RESUME,
        WORK_STATUS_ZONE_PARTITION,
        WORK_STATUS_ESCAPING,
    ])
    def test_active_states_show_mowing_with_pause_and_dock(self, ws):
        row = state_matrix.lookup(
            work_status=ws,
            robot_status=ws,  # match ws to avoid hitting an rs override
            is_recharging=False,
        )
        assert row.activity == "mowing"
        assert row.pause == USER_CTRL_PAUSE
        assert row.dock == USER_CTRL_RECHARGE_DOCK
        assert row.start_mowing is None, "Already mowing — Start hidden"


class TestDockingState:
    def test_returning_to_dock_shows_only_pause(self):
        row = state_matrix.lookup(
            work_status=WORK_STATUS_DOCKING,
            robot_status=WORK_STATUS_DOCKING,
            is_recharging=False,
        )
        assert row.activity == "returning"
        assert row.pause == USER_CTRL_PAUSE_DOCK, "PAUSE_DOCK(21) variant for in-transit"
        assert row.dock is None, "Already docking — Dock would be redundant"
        assert row.start_mowing is None


class TestPauseDockingState:
    def test_paused_during_dock_resumes_dock_approach(self):
        row = state_matrix.lookup(
            work_status=WORK_STATUS_PAUSE_DOCKING,
            robot_status=WORK_STATUS_PAUSE_DOCKING,
            is_recharging=False,
        )
        assert row.activity == "paused"
        assert row.start_mowing == USER_CTRL_RESUME_DOCK, "Resume the dock approach, not the mow"
        assert row.pause is None
        # Dock not offered — it's redundant when already approaching dock paused
        assert row.dock is None


class TestIdleStates:
    @pytest.mark.parametrize("ws", [WORK_STATUS_WAITING, WORK_STATUS_NONE])
    def test_idle_offers_only_start(self, ws):
        row = state_matrix.lookup(
            work_status=ws,
            robot_status=ws,
            is_recharging=False,
        )
        assert row.activity == "docked"
        assert row.start_mowing == USER_CTRL_CLEAN
        assert row.pause is None
        assert row.dock is None


class TestDefaultRow:
    def test_unknown_combo_falls_through_to_default(self):
        # Use values outside any defined row — UPDATING(11) and RTT(15)
        row = state_matrix.lookup(
            work_status=11,
            robot_status=15,
            is_recharging=False,
        )
        assert row.activity is None  # → HA "Unknown"
        assert row.start_mowing is None
        assert row.pause is None
        assert row.dock is None


class TestFeaturesDerivation:
    """features_for() reads the action columns and sets the corresponding
    LawnMowerEntityFeature flags — derived, not stored."""

    def test_features_for_mowing_row_includes_pause_and_dock(self):
        # Need HA imports for this test; skip if running outside HA.
        pytest.importorskip("homeassistant.components.lawn_mower")
        from homeassistant.components.lawn_mower import LawnMowerEntityFeature

        row = state_matrix.lookup(
            work_status=WORK_STATUS_MOWING,
            robot_status=WORK_STATUS_MOWING,
            is_recharging=False,
        )
        f = state_matrix.features_for(row)
        assert f & LawnMowerEntityFeature.PAUSE
        assert f & LawnMowerEntityFeature.DOCK
        assert not (f & LawnMowerEntityFeature.START_MOWING)

    def test_features_for_default_row_is_empty(self):
        pytest.importorskip("homeassistant.components.lawn_mower")
        from homeassistant.components.lawn_mower import LawnMowerEntityFeature

        f = state_matrix.features_for(state_matrix.DEFAULT_ROW)
        assert f == LawnMowerEntityFeature(0)


class TestMatrixCoverage:
    """Sanity: every row has either a match condition or a sensible default,
    and rows are well-formed."""

    def test_every_row_has_a_note(self):
        """Notes document the why — required for matrix maintenance."""
        for row in state_matrix.STATE_MATRIX:
            assert row.note, f"Row missing note: {row}"

    def test_every_row_has_at_least_one_match_condition(self):
        """A row with all wildcards would catch everything — that's
        what DEFAULT_ROW is for. Defensive check that no in-table row
        accidentally becomes a swallow-all."""
        for row in state_matrix.STATE_MATRIX:
            has_match = (
                row.work_status is not None
                or row.robot_status is not None
                or row.is_recharging is not None
            )
            assert has_match, f"Row with no match conditions: {row}"

    def test_every_action_int_appears_in_expected_post_states(self):
        """The watchdog can only confirm actions it knows the expected
        post-state for. If a row references a userCtrl that's absent
        from EXPECTED_POST_STATES, the watchdog would be a no-op."""
        from lymow_mqtt.userctrl import EXPECTED_POST_STATES
        for row in state_matrix.STATE_MATRIX:
            for action in (row.start_mowing, row.pause, row.dock):
                if action is not None:
                    assert action in EXPECTED_POST_STATES, (
                        f"userCtrl={action} in row {row.note!r} not in "
                        f"EXPECTED_POST_STATES — watchdog won't know "
                        f"what state to expect"
                    )
