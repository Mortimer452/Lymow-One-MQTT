"""userCtrl command registry — logical command -> (variant picker, expected post-state).

The pause/resume verbs come in matched pairs depending on the current
state machine (arch.md §6d). The integration picks the right variant
from live robotInfo at command time.

EXPECTED_POST_STATES is consulted by the coordinator's watchdog after
publishing to verify the command actually took effect (arch.md §11 —
firmware silently ignores invalid commands).
"""
from __future__ import annotations

from .const import (
    USER_CTRL_CLEAN, USER_CTRL_DOCK, USER_CTRL_PAUSE, USER_CTRL_RESUME,
    USER_CTRL_PAUSE_DOCK, USER_CTRL_RESUME_DOCK, USER_CTRL_RECHARGE_DOCK,
    USER_CTRL_FORCE_REINIT, USER_CTRL_QUERY_MAP, USER_CTRL_QUERY_SCHEDULES,
    USER_CTRL_QUERY_RUN_TIME_CONFIG,
    WORK_STATUS_MOWING, WORK_STATUS_PAUSE, WORK_STATUS_DOCKING,
    WORK_STATUS_PAUSE_DOCKING, WORK_STATUS_RESUME,
    WORK_STATUS_WAITING, WORK_STATUS_CHARGING,
    WORK_STATUS_ZONE_PARTITION, WORK_STATUS_ESCAPING,
)


def pick_pause_variant(work_status: int) -> int | None:
    """Pick the right pause userCtrl from current work status.

    Returns None if the mower is already in a paused state (no-op for HA UX).
    Raises ValueError if the state doesn't support pausing.
    """
    # Verified pauseable states only. ZONE_PARTITION (9) and ESCAPING (14)
    # are in ACTIVE_TASK_WORK_STATUSES but pause behavior in those substates
    # is unverified — firmware may silently ignore. Add to this list once
    # characterized in arch.md §6d.
    if work_status in (WORK_STATUS_MOWING, WORK_STATUS_RESUME):
        return USER_CTRL_PAUSE
    if work_status == WORK_STATUS_DOCKING:
        return USER_CTRL_PAUSE_DOCK
    if work_status in (WORK_STATUS_PAUSE, WORK_STATUS_PAUSE_DOCKING):
        return None
    raise ValueError(
        f"Cannot pause from work_status={work_status} (not a pauseable state)"
    )


def pick_resume_variant(work_status: int) -> int | None:
    """Pick the right resume userCtrl from current work status.

    Returns None if the mower isn't paused (no-op).
    """
    if work_status == WORK_STATUS_PAUSE:
        return USER_CTRL_RESUME
    if work_status == WORK_STATUS_PAUSE_DOCKING:
        return USER_CTRL_RESUME_DOCK
    if work_status in (WORK_STATUS_MOWING, WORK_STATUS_DOCKING, WORK_STATUS_RESUME):
        return None
    raise ValueError(
        f"Cannot resume from work_status={work_status} (not a paused state)"
    )


# Expected post-state for command watchdog.
# After publishing userCtrl=N, the firmware should transition robotStatus
# to one of these values within ~2.5s. If not, treat as silently rejected.
EXPECTED_POST_STATES: dict[int, set[int]] = {
    # Action commands
    USER_CTRL_CLEAN: {WORK_STATUS_MOWING, WORK_STATUS_RESUME},
    USER_CTRL_PAUSE: {WORK_STATUS_PAUSE},
    USER_CTRL_RESUME: {WORK_STATUS_MOWING, WORK_STATUS_RESUME},
    USER_CTRL_DOCK: {WORK_STATUS_DOCKING, WORK_STATUS_WAITING, WORK_STATUS_CHARGING},
    USER_CTRL_PAUSE_DOCK: {WORK_STATUS_PAUSE_DOCKING},
    USER_CTRL_RESUME_DOCK: {WORK_STATUS_DOCKING},
    USER_CTRL_RECHARGE_DOCK: {WORK_STATUS_DOCKING, WORK_STATUS_CHARGING},
    USER_CTRL_FORCE_REINIT: {WORK_STATUS_WAITING},
    # Query commands — watchdog skips them (state doesn't transition)
    USER_CTRL_QUERY_MAP: set(),
    USER_CTRL_QUERY_SCHEDULES: set(),
    USER_CTRL_QUERY_RUN_TIME_CONFIG: set(),
}
