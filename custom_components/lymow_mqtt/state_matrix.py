"""Lawn-mower state decision matrix.

A lookup table from `(work_status, robot_status, is_recharging)` →
`(activity, button-actions)`. Replaces the previous if/elif cascades in
lawn_mower.py and the variant pickers in userctrl.py.

Reading order matters — first match wins, so place more-specific or
higher-priority rows first. Wildcards (`None` in a match column) skip
the comparison for that field.

Why a matrix vs. cascading ifs:
- Adding a new edge case is one row, not a careful re-ordering of priorities.
- Coverage is visible at a glance; missing combos fall through to DEFAULT_ROW.
- Trivially parametrize-testable.

Pure module — no homeassistant imports — so test runners can import it
without HA stubs. The entity layer wraps activity strings into
LawnMowerActivity values.
"""
from __future__ import annotations

from dataclasses import dataclass

from .const import (
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

# ─────────────────────────────────────────────────────────────────────────
# Activity strings — match the LawnMowerActivity enum's string values
# (LawnMowerActivity is a StrEnum in HA). Kept as strings here so this
# module stays HA-import-free; the entity layer wraps them.
# ─────────────────────────────────────────────────────────────────────────
ACTIVITY_MOWING = "mowing"
ACTIVITY_PAUSED = "paused"
ACTIVITY_DOCKED = "docked"
ACTIVITY_RETURNING = "returning"
ACTIVITY_ERROR = "error"


@dataclass(frozen=True, kw_only=True, slots=True)
class StateRow:
    """One row of the lawn-mower decision matrix.

    Match columns (None means "any"):
      work_status   — match against PbRobotInfo.workStatus
      robot_status  — match against PbRobotInfo.robotStatus
      is_recharging — match against PbRobotInfo.isRecharging (bool)

    Outcome columns:
      activity      — value of LawnMowerEntity.activity (string from
                      ACTIVITY_*; None means HA "unknown")
      start_mowing  — userCtrl int published when HA fires
                      async_start_mowing; None hides the Start button
      pause         — userCtrl int published on async_pause; None hides Pause
      dock          — userCtrl int published on async_dock; None hides Dock
      note          — human-readable rationale (free-form, for grep / audit)
    """

    work_status: int | None = None
    robot_status: int | None = None
    is_recharging: bool | None = None
    activity: str | None = None
    start_mowing: int | None = None
    pause: int | None = None
    dock: int | None = None
    note: str = ""


# ─────────────────────────────────────────────────────────────────────────
# THE MATRIX
# ─────────────────────────────────────────────────────────────────────────
# Match columns:
#   work_status   — PbRobotInfo.workStatus  (WORK_STATUS_* enum)
#   robot_status  — PbRobotInfo.robotStatus (same enum; rs is the
#                   physical-truth field, ws is the task-intent field)
#   is_recharging — PbRobotInfo.isRecharging — True ONLY during a mid-task
#                   recharge cycle, NOT for end-of-task charging
#
# Outcome columns (every button column = userCtrl int to publish, or None
# to hide that button):
#   activity      — LawnMowerEntity.activity string
#   start_mowing  — userCtrl for async_start_mowing (HA's Start/Play button)
#   pause         — userCtrl for async_pause (HA's Pause button)
#   dock          — userCtrl for async_dock (HA's Dock button —
#                   the safer "keep progress" variant; the destructive
#                   "abandon task" dock is the lymow_mqtt.dock_cancel_task
#                   service, not a button)
#
# Priority: rows are evaluated top-to-bottom, first match wins. Physical
# state (robot_status) is checked before task intent (work_status) for
# states where they can disagree (ERROR, EMERGENCY_STOP, PAUSE, CHARGING).
STATE_MATRIX: list[StateRow] = [
    # ─────────────────────────────────────────────────────────────────
    # 1. Physical errors override task intent (rs is authoritative)
    # ─────────────────────────────────────────────────────────────────
    StateRow(
        robot_status=WORK_STATUS_ERROR,
        activity=ACTIVITY_ERROR,
        pause=USER_CTRL_PAUSE,
        note="rs=ERROR — Pause button doubles as 'Clear Error' (arch.md §6b)",
    ),
    StateRow(
        work_status=WORK_STATUS_ERROR,
        activity=ACTIVITY_ERROR,
        pause=USER_CTRL_PAUSE,
        note="ws=ERROR (rare, normally rs is set first) — same handling",
    ),
    StateRow(
        robot_status=WORK_STATUS_EMERGENCY_STOP,
        activity=ACTIVITY_ERROR,
        note="rs=EMERGENCY_STOP — physical e-stop, no buttons (user must reset on mower)",
    ),
    StateRow(
        work_status=WORK_STATUS_EMERGENCY_STOP,
        activity=ACTIVITY_ERROR,
        note="ws=EMERGENCY_STOP — same",
    ),

    # ─────────────────────────────────────────────────────────────────
    # 2. Physical pause is authoritative over task intent
    #    (the bug the cascade-of-ifs missed: rs=PAUSE while ws=MOWING)
    # ─────────────────────────────────────────────────────────────────
    StateRow(
        robot_status=WORK_STATUS_PAUSE,
        activity=ACTIVITY_PAUSED,
        start_mowing=USER_CTRL_RESUME,
        dock=USER_CTRL_RECHARGE_DOCK,
        note="rs=PAUSE — Start sends RESUME(4), Dock keeps progress",
    ),
    StateRow(
        robot_status=WORK_STATUS_PAUSE_DOCKING,
        activity=ACTIVITY_PAUSED,
        start_mowing=USER_CTRL_RESUME_DOCK,
        note="rs=PAUSE_DOCKING — Start sends RESUME_DOCK(22) to continue dock approach",
    ),
    StateRow(
        work_status=WORK_STATUS_PAUSE,
        activity=ACTIVITY_PAUSED,
        start_mowing=USER_CTRL_RESUME,
        dock=USER_CTRL_RECHARGE_DOCK,
        note="ws=PAUSE — same as rs=PAUSE (mirror for ws-first transitions)",
    ),
    StateRow(
        work_status=WORK_STATUS_PAUSE_DOCKING,
        activity=ACTIVITY_PAUSED,
        start_mowing=USER_CTRL_RESUME_DOCK,
        note="ws=PAUSE_DOCKING — same as rs=PAUSE_DOCKING",
    ),

    # ─────────────────────────────────────────────────────────────────
    # 3. Charging at dock — fork on isRecharging (saved task vs idle)
    # ─────────────────────────────────────────────────────────────────
    StateRow(
        robot_status=WORK_STATUS_CHARGING,
        is_recharging=True,
        activity=ACTIVITY_DOCKED,
        start_mowing=USER_CTRL_RESUME,
        note="CHARGING + saved task → Start RESUMES the saved mow (PR #2 fix)",
    ),
    StateRow(
        robot_status=WORK_STATUS_CHARGING_FULL,
        is_recharging=True,
        activity=ACTIVITY_DOCKED,
        start_mowing=USER_CTRL_RESUME,
        note="CHARGING_FULL + saved task → Start RESUMES",
    ),
    StateRow(
        robot_status=WORK_STATUS_CHARGING,
        activity=ACTIVITY_DOCKED,
        start_mowing=USER_CTRL_CLEAN,
        note="CHARGING idle (no saved task) → Start fires fresh CLEAN(1)",
    ),
    StateRow(
        robot_status=WORK_STATUS_CHARGING_FULL,
        activity=ACTIVITY_DOCKED,
        start_mowing=USER_CTRL_CLEAN,
        note="CHARGING_FULL idle → Start fires fresh CLEAN(1)",
    ),

    # ─────────────────────────────────────────────────────────────────
    # 4. Active task states (ws is authoritative — rs typically agrees)
    # ─────────────────────────────────────────────────────────────────
    StateRow(
        work_status=WORK_STATUS_MOWING,
        activity=ACTIVITY_MOWING,
        pause=USER_CTRL_PAUSE,
        dock=USER_CTRL_RECHARGE_DOCK,
        note="ws=MOWING — active mow, Pause(3) and Dock-keep(33)",
    ),
    StateRow(
        work_status=WORK_STATUS_RESUME,
        activity=ACTIVITY_MOWING,
        pause=USER_CTRL_PAUSE,
        dock=USER_CTRL_RECHARGE_DOCK,
        note="ws=RESUME — transient state right after Resume cmd; treat as MOWING",
    ),
    StateRow(
        work_status=WORK_STATUS_ZONE_PARTITION,
        activity=ACTIVITY_MOWING,
        pause=USER_CTRL_PAUSE,
        dock=USER_CTRL_RECHARGE_DOCK,
        note="ws=ZONE_PARTITION — perimeter cut substate, treat as MOWING",
    ),
    StateRow(
        work_status=WORK_STATUS_ESCAPING,
        activity=ACTIVITY_MOWING,
        pause=USER_CTRL_PAUSE,
        dock=USER_CTRL_RECHARGE_DOCK,
        note="ws=ESCAPING — recovering from stuck/obstacle, still active",
    ),

    # ─────────────────────────────────────────────────────────────────
    # 5. Returning to dock
    # ─────────────────────────────────────────────────────────────────
    StateRow(
        work_status=WORK_STATUS_DOCKING,
        activity=ACTIVITY_RETURNING,
        pause=USER_CTRL_PAUSE_DOCK,
        note="ws=DOCKING — Pause sends PAUSE_DOCK(21); Dock would be redundant",
    ),

    # ─────────────────────────────────────────────────────────────────
    # 6. Idle (workStatus says waiting/none, robotStatus didn't trigger
    #    earlier rules — i.e., not on dock charging)
    # ─────────────────────────────────────────────────────────────────
    StateRow(
        work_status=WORK_STATUS_WAITING,
        activity=ACTIVITY_DOCKED,
        start_mowing=USER_CTRL_CLEAN,
        note="ws=WAITING idle — Start fires fresh CLEAN(1)",
    ),
    StateRow(
        work_status=WORK_STATUS_NONE,
        activity=ACTIVITY_DOCKED,
        start_mowing=USER_CTRL_CLEAN,
        note="ws=NONE idle — same as WAITING",
    ),

    # ─────────────────────────────────────────────────────────────────
    # 7. Catch-all default — UPDATING(11), RTT(15), REMOTE_CONTROL(6),
    #    or any unhandled combo. Activity=Unknown, no buttons shown.
    # ─────────────────────────────────────────────────────────────────
]

# Sentinel returned when no row matches (and from lookup() if robotInfo
# is missing entirely). Activity=None renders as "Unknown" in HA;
# no actions means features=0 (no buttons advertised).
DEFAULT_ROW = StateRow(
    activity=None,
    note="default — unhandled (ws,rs,isRech) combo; HA shows Unknown, no buttons",
)


def lookup(
    *, work_status: int, robot_status: int, is_recharging: bool
) -> StateRow:
    """Find the first row whose non-None match columns all equal the inputs.

    None in any of `row.work_status` / `row.robot_status` /
    `row.is_recharging` is a wildcard for that column.
    """
    for row in STATE_MATRIX:
        if row.work_status is not None and row.work_status != work_status:
            continue
        if row.robot_status is not None and row.robot_status != robot_status:
            continue
        if row.is_recharging is not None and row.is_recharging != is_recharging:
            continue
        return row
    return DEFAULT_ROW


def features_for(row: StateRow):
    """Derive supported_features from which action columns are populated.

    Returns a LawnMowerEntityFeature flag combination. Imported lazily so
    the module stays HA-import-free for unit tests.
    """
    from homeassistant.components.lawn_mower import LawnMowerEntityFeature

    f = LawnMowerEntityFeature(0)
    if row.start_mowing is not None:
        f |= LawnMowerEntityFeature.START_MOWING
    if row.pause is not None:
        f |= LawnMowerEntityFeature.PAUSE
    if row.dock is not None:
        f |= LawnMowerEntityFeature.DOCK
    return f
