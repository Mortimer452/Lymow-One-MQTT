"""RTK accuracy guard — send the mower home when localization degrades.

A pure state machine: the coordinator feeds it one ``evaluate()`` call per
pboutput merge and dispatches whatever action it returns. No HA imports,
no I/O — fully unit-testable (tests/test_accuracy_guard.py).

Why this exists: with a heat-glitched (or otherwise failing) RTK base
station, the mower keeps mowing on meter-scale accuracy and wanders out of
bounds. The guard watches ``horizontalAccuracy`` and calls for a dock or
pause when degradation is sustained.

Design notes (arch.md §5d, capture_20260813_150340):

- **Arm on the first ``locNodeStatus == RUNNING`` of a task.** Before
  that, the receiver is waking from dock sleep / converging and accuracy
  readings are garbage (~28 m observed) — there is no fixed "grace
  period"; the firmware announces readiness itself.
- **``positionQuality`` is deliberately not an input.** It lags
  ``locNodeStatus`` by tens of seconds (observed: RUNNING with 1.5 cm
  accuracy while quality still read SINGLE_POINT). ``horizontalAccuracy``
  is the truthful signal once the node is RUNNING.
- **The node leaving RUNNING mid-task is itself degradation** — the pose
  is no longer produced by a healthy localization stack, whatever the
  last accuracy reading said.
- **Latching**: one incident fires one action. The latch survives the
  commanded dock (DOCKING is still an active-task status) and clears when
  the task ends, so the next session starts fresh.
"""
from __future__ import annotations

from dataclasses import dataclass

from .const import ACTIVE_TASK_STATUSES, LOC_NODE_RUNNING, WORK_STATUS_MOWING

ACTION_DOCK = "dock"
ACTION_PAUSE = "pause"


@dataclass(frozen=True)
class GuardConfig:
    """User-facing knobs, sourced from config-entry options each call."""

    enabled: bool
    threshold_m: float
    hold_s: float
    action: str  # ACTION_DOCK | ACTION_PAUSE


class AccuracyGuard:
    """Tracks arm / degraded-since / latched across broadcasts."""

    def __init__(self) -> None:
        self._armed = False
        self._degraded_since: float | None = None
        self._latched = False

    @property
    def armed(self) -> bool:
        """True once locNodeStatus has reached RUNNING this task.

        The coordinator watches this flip to log the arm transition.
        """
        return self._armed

    def evaluate(
        self,
        *,
        now: float,
        work_status: int,
        loc_node_status: int | None,
        h_acc: float | None,
        config: GuardConfig,
    ) -> str | None:
        """Feed one broadcast; returns the action to dispatch, or None.

        ``now`` is any monotonic seconds value — only differences matter.
        """
        if not config.enabled:
            self._reset()
            return None

        if work_status not in ACTIVE_TASK_STATUSES:
            # Task over (charging / waiting / off): fresh slate for the
            # next session, which must re-arm via RUNNING.
            self._reset()
            return None

        if work_status != WORK_STATUS_MOWING:
            # Paused, docking, escaping, …: the mower isn't actively
            # navigating on this data. Freeze the hold timer but keep the
            # arm and latch — resuming re-enters evaluation immediately.
            self._degraded_since = None
            return None

        if not self._armed:
            if loc_node_status != LOC_NODE_RUNNING:
                return None  # convergence phase — however long it takes
            self._armed = True
            # Fall through: the arming broadcast itself gets evaluated
            # (it can already be degraded if the base is down at start).

        degraded = loc_node_status != LOC_NODE_RUNNING or (
            h_acc is not None and h_acc > config.threshold_m
        )
        if not degraded:
            self._degraded_since = None
            return None

        if self._latched:
            return None
        if self._degraded_since is None:
            self._degraded_since = now
            return None
        if now - self._degraded_since >= config.hold_s:
            self._latched = True
            self._degraded_since = None
            return config.action
        return None

    def _reset(self) -> None:
        self._armed = False
        self._degraded_since = None
        self._latched = False
