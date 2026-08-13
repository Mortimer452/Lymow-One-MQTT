"""Tests for accuracy_guard.py — the RTK accuracy guard state machine.

Pure state machine, no HA imports. The coordinator feeds it one call per
pboutput merge; it decides when the mower is navigating on untrustworthy
localization and should be sent home (or paused).

Key design points under test (arch.md §5d, capture_20260813_150340):
- Arm on the first locNodeStatus=RUNNING of a task — accuracy values
  before that describe a converging/sleeping receiver, not RTK health.
- Degraded = h_acc above threshold OR locNode left RUNNING. positionQuality
  is deliberately NOT an input: it lags locNodeStatus by tens of seconds.
- Sustained-hold before tripping; latch so one incident fires one action.
"""
from __future__ import annotations

from lymow_mqtt.accuracy_guard import ACTION_RESUME, AccuracyGuard, GuardConfig
from lymow_mqtt.const import (
    LOC_NODE_INITIALIZING,
    LOC_NODE_RUNNING,
    LOC_NODE_WAITING,
    WORK_STATUS_CHARGING,
    WORK_STATUS_DOCKING,
    WORK_STATUS_MOWING,
    WORK_STATUS_PAUSE,
    WORK_STATUS_WAITING,
)

CFG = GuardConfig(enabled=True, threshold_m=1.0, hold_s=180.0, action="dock")
HOLD = 180.0


def _mow(guard, now, loc_node=LOC_NODE_RUNNING, h_acc=0.05, cfg=CFG,
         work_status=WORK_STATUS_MOWING):
    return guard.evaluate(
        now=now, work_status=work_status,
        loc_node_status=loc_node, h_acc=h_acc, config=cfg,
    )


class TestArming:
    def test_starts_disarmed(self):
        assert AccuracyGuard().armed is False

    def test_does_not_arm_during_convergence(self):
        """WAITING/INITIALIZING with garbage accuracy is the normal dock
        wake-up sequence (28 m observed) — never arm, never trip, no
        matter how long it takes."""
        g = AccuracyGuard()
        for t in range(0, 1200, 30):
            node = LOC_NODE_WAITING if t < 300 else LOC_NODE_INITIALIZING
            assert _mow(g, float(t), loc_node=node, h_acc=28.4) is None
        assert g.armed is False

    def test_arms_on_first_running(self):
        g = AccuracyGuard()
        _mow(g, 0.0, loc_node=LOC_NODE_INITIALIZING, h_acc=28.4)
        _mow(g, 30.0, loc_node=LOC_NODE_RUNNING, h_acc=0.015)
        assert g.armed is True

    def test_convergence_time_does_not_leak_into_hold(self):
        """10 min of pre-arm garbage must not pre-charge the hold timer:
        arming with good accuracy trips nothing."""
        g = AccuracyGuard()
        for t in range(0, 600, 30):
            _mow(g, float(t), loc_node=LOC_NODE_INITIALIZING, h_acc=28.4)
        assert _mow(g, 600.0, loc_node=LOC_NODE_RUNNING, h_acc=0.015) is None
        assert _mow(g, 600.0 + HOLD + 1, h_acc=0.015) is None

    def test_disabled_never_arms_or_trips(self):
        cfg = GuardConfig(enabled=False, threshold_m=1.0, hold_s=HOLD, action="dock")
        g = AccuracyGuard()
        _mow(g, 0.0, cfg=cfg)
        assert g.armed is False
        for t in range(0, 1200, 30):
            assert _mow(g, float(t), h_acc=50.0, cfg=cfg) is None


class TestTripping:
    def _armed_guard(self):
        g = AccuracyGuard()
        _mow(g, 0.0, h_acc=0.015)
        assert g.armed
        return g

    def test_healthy_session_never_trips(self):
        g = self._armed_guard()
        for t in range(30, 7200, 30):
            assert _mow(g, float(t), h_acc=0.3) is None

    def test_sustained_bad_accuracy_trips_once(self):
        g = self._armed_guard()
        assert _mow(g, 100.0, h_acc=2.5) is None            # hold starts
        assert _mow(g, 100.0 + HOLD - 1, h_acc=2.5) is None  # not yet
        assert _mow(g, 100.0 + HOLD + 1, h_acc=2.5) == "dock"
        # Latched: continuing degradation does not re-fire.
        assert _mow(g, 100.0 + HOLD + 60, h_acc=2.5) is None

    def test_brief_dip_resets_hold_timer(self):
        g = self._armed_guard()
        _mow(g, 100.0, h_acc=2.5)
        _mow(g, 200.0, h_acc=0.3)   # recovered — timer resets
        _mow(g, 300.0, h_acc=2.5)   # degraded again — fresh timer
        assert _mow(g, 300.0 + HOLD - 1, h_acc=2.5) is None
        assert _mow(g, 300.0 + HOLD + 1, h_acc=2.5) == "dock"

    def test_loc_node_leaving_running_trips(self):
        """Localization stack dying mid-task is degraded even if the last
        h_acc reading looked fine — the pose is no longer being produced
        by a running node."""
        g = self._armed_guard()
        assert _mow(g, 100.0, loc_node=LOC_NODE_INITIALIZING, h_acc=0.05) is None
        assert _mow(g, 100.0 + HOLD + 1, loc_node=LOC_NODE_INITIALIZING, h_acc=0.05) == "dock"

    def test_action_pause(self):
        cfg = GuardConfig(enabled=True, threshold_m=1.0, hold_s=HOLD, action="pause")
        g = AccuracyGuard()
        _mow(g, 0.0, h_acc=0.015, cfg=cfg)
        _mow(g, 100.0, h_acc=2.5, cfg=cfg)
        assert _mow(g, 100.0 + HOLD + 1, h_acc=2.5, cfg=cfg) == "pause"

    def test_threshold_is_exclusive(self):
        g = self._armed_guard()
        _mow(g, 100.0, h_acc=1.0)  # exactly at threshold — not degraded
        assert _mow(g, 100.0 + HOLD + 1, h_acc=1.0) is None

    def test_missing_accuracy_is_not_degraded(self):
        """A broadcast without localizationInfo says nothing — don't treat
        None as bad, don't crash."""
        g = self._armed_guard()
        assert _mow(g, 100.0, h_acc=None) is None
        assert _mow(g, 100.0 + HOLD + 1, h_acc=None) is None


class TestLifecycle:
    def _tripped_guard(self):
        g = AccuracyGuard()
        _mow(g, 0.0, h_acc=0.015)
        _mow(g, 100.0, h_acc=2.5)
        assert _mow(g, 100.0 + HOLD + 1, h_acc=2.5) == "dock"
        return g

    def test_latch_holds_through_docking(self):
        """While the commanded dock is underway (DOCKING is still an
        active-task status), the guard must not re-fire."""
        g = self._tripped_guard()
        t = 100.0 + HOLD + 30
        assert _mow(g, t, work_status=WORK_STATUS_DOCKING, h_acc=2.5) is None

    def test_task_end_resets_for_next_session(self):
        g = self._tripped_guard()
        _mow(g, 500.0, work_status=WORK_STATUS_CHARGING, h_acc=2.5)
        assert g.armed is False
        # Next session: must re-arm via RUNNING before evaluating again.
        assert _mow(g, 600.0, loc_node=LOC_NODE_WAITING, h_acc=30.0) is None
        assert g.armed is False
        _mow(g, 700.0, loc_node=LOC_NODE_RUNNING, h_acc=0.015)
        assert g.armed is True
        _mow(g, 800.0, h_acc=2.5)
        assert _mow(g, 800.0 + HOLD + 1, h_acc=2.5) == "dock"

    def test_waiting_resets_too(self):
        g = self._tripped_guard()
        _mow(g, 500.0, work_status=WORK_STATUS_WAITING)
        assert g.armed is False

    def test_guard_dock_does_not_auto_resume(self):
        """Auto-resume is a pause-action behavior only. After a dock trip
        the mower transits DOCKING (frozen) then CHARGING (reset) — good
        signal during the return must never emit a resume."""
        g = self._tripped_guard()  # action="dock"
        t = 100.0 + HOLD + 30
        for i in range(20):
            assert _mow(g, t + i * 30, work_status=WORK_STATUS_DOCKING, h_acc=0.05) is None


class TestAutoResume:
    """Pause action: the guard keeps evaluating while ITS OWN pause is in
    effect and resumes mowing once accuracy recovers, sustained for the
    same hold time (debounce against marginal-signal cycling)."""

    PAUSE_CFG = GuardConfig(enabled=True, threshold_m=1.0, hold_s=HOLD, action="pause")

    def _guard_paused(self):
        """Arm, degrade, trip the pause at t=281."""
        g = AccuracyGuard()
        _mow(g, 0.0, h_acc=0.015, cfg=self.PAUSE_CFG)
        _mow(g, 100.0, h_acc=2.5, cfg=self.PAUSE_CFG)
        assert _mow(g, 100.0 + HOLD + 1, h_acc=2.5, cfg=self.PAUSE_CFG) == "pause"
        return g

    def _paused(self, g, now, h_acc, loc_node=LOC_NODE_RUNNING):
        return _mow(g, now, work_status=WORK_STATUS_PAUSE, h_acc=h_acc,
                    loc_node=loc_node, cfg=self.PAUSE_CFG)

    def test_sustained_recovery_resumes(self):
        g = self._guard_paused()
        assert self._paused(g, 300.0, h_acc=0.05) is None          # recovery starts
        assert self._paused(g, 300.0 + HOLD - 1, h_acc=0.05) is None
        assert self._paused(g, 300.0 + HOLD + 1, h_acc=0.05) == ACTION_RESUME

    def test_single_good_reading_does_not_resume(self):
        g = self._guard_paused()
        assert self._paused(g, 300.0, h_acc=0.05) is None
        assert self._paused(g, 330.0, h_acc=2.5) is None           # still bad — timer resets
        assert self._paused(g, 330.0 + HOLD + 1, h_acc=2.5) is None
        # Fresh sustained recovery needed from here
        assert self._paused(g, 700.0, h_acc=0.05) is None
        assert self._paused(g, 700.0 + HOLD + 1, h_acc=0.05) == ACTION_RESUME

    def test_missing_accuracy_does_not_resume(self):
        """No data is not recovery — never resume blind."""
        g = self._guard_paused()
        for i in range(20):
            assert self._paused(g, 300.0 + i * 30, h_acc=None) is None

    def test_loc_node_not_running_does_not_resume(self):
        g = self._guard_paused()
        for i in range(20):
            assert self._paused(g, 300.0 + i * 30, h_acc=0.05,
                                loc_node=LOC_NODE_INITIALIZING) is None

    def test_guard_can_trip_again_after_auto_resume(self):
        """Full cycle: pause → recover → resume → degrade again → pause."""
        g = self._guard_paused()
        self._paused(g, 300.0, h_acc=0.05)
        assert self._paused(g, 300.0 + HOLD + 1, h_acc=0.05) == ACTION_RESUME
        # Back to mowing; signal degrades again — fresh hold, fresh pause
        t = 600.0
        assert _mow(g, t, h_acc=2.5, cfg=self.PAUSE_CFG) is None
        assert _mow(g, t + HOLD + 1, h_acc=2.5, cfg=self.PAUSE_CFG) == "pause"

    def test_user_pause_never_auto_resumes(self):
        """A pause the guard did NOT initiate is the user's decision —
        pristine signal for any length of time must not resume it."""
        g = AccuracyGuard()
        _mow(g, 0.0, h_acc=0.015, cfg=self.PAUSE_CFG)  # armed, healthy
        for i in range(40):
            assert self._paused(g, 100.0 + i * 30, h_acc=0.05) is None

    def test_manual_resume_restores_protection(self):
        """User resumes while the guard awaits recovery: cancel the
        pending auto-resume and unlatch so the guard can protect again."""
        g = self._guard_paused()
        self._paused(g, 300.0, h_acc=0.05)  # recovery underway
        # User hits resume — mowing again, still degraded
        assert _mow(g, 400.0, h_acc=2.5, cfg=self.PAUSE_CFG) is None  # fresh hold
        assert _mow(g, 400.0 + HOLD + 1, h_acc=2.5, cfg=self.PAUSE_CFG) == "pause"

    def test_task_end_while_paused_resets(self):
        g = self._guard_paused()
        _mow(g, 400.0, work_status=WORK_STATUS_CHARGING, h_acc=0.05, cfg=self.PAUSE_CFG)
        assert g.armed is False
        # Good signal later must not emit a stray resume
        assert self._paused(g, 500.0, h_acc=0.05) is None

    def test_pause_freezes_evaluation_but_keeps_arm(self):
        """User-paused mid-task: no degradation accrues while parked, but
        the arm survives so resuming re-enters evaluation immediately."""
        g = AccuracyGuard()
        _mow(g, 0.0, h_acc=0.015)
        _mow(g, 100.0, h_acc=2.5)  # hold running
        # Pause before the hold elapses — evaluation freezes
        assert _mow(g, 150.0, work_status=WORK_STATUS_PAUSE, h_acc=2.5) is None
        assert _mow(g, 150.0 + HOLD * 2, work_status=WORK_STATUS_PAUSE, h_acc=2.5) is None
        assert g.armed is True
        # Resume with bad accuracy: fresh hold from resume, not from t=100
        assert _mow(g, 600.0, h_acc=2.5) is None
        assert _mow(g, 600.0 + HOLD - 1, h_acc=2.5) is None
        assert _mow(g, 600.0 + HOLD + 1, h_acc=2.5) == "dock"
