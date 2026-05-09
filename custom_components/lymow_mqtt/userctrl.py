"""userCtrl post-state expectations for the command watchdog.

After publishing userCtrl=N, the firmware should transition robotStatus
or workStatus to one of the values listed here within ~2.5s. If not,
treat as silently rejected (arch.md §11 — firmware silently ignores
invalid commands in some states).

Variant selection (which userCtrl int to publish for a given button
press) used to live here as `pick_pause_variant` / `pick_resume_variant`,
but that decision is now part of the lawn-mower state matrix —
see `state_matrix.py`. This module is just the watchdog-expectations table.
"""
from __future__ import annotations

from .const import (
    USER_CTRL_CLEAN,
    USER_CTRL_DOCK,
    USER_CTRL_FORCE_REINIT,
    USER_CTRL_PAUSE,
    USER_CTRL_PAUSE_DOCK,
    USER_CTRL_QUERY_MAP,
    USER_CTRL_QUERY_RUN_TIME_CONFIG,
    USER_CTRL_QUERY_SCHEDULES,
    USER_CTRL_RECHARGE_DOCK,
    USER_CTRL_RESUME,
    USER_CTRL_RESUME_DOCK,
    WORK_STATUS_CHARGING,
    WORK_STATUS_DOCKING,
    WORK_STATUS_MOWING,
    WORK_STATUS_PAUSE,
    WORK_STATUS_PAUSE_DOCKING,
    WORK_STATUS_RESUME,
    WORK_STATUS_WAITING,
)


# Expected post-state for command watchdog.
# After publishing userCtrl=N, the firmware should transition robotStatus
# or workStatus to one of these values within ~2.5s. If not, treat as
# silently rejected.
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
