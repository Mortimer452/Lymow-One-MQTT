"""Lymow lawn_mower entity — start/pause/dock with smart variant dispatch."""
from __future__ import annotations

from homeassistant.components.lawn_mower import (
    LawnMowerActivity,
    LawnMowerEntity,
    LawnMowerEntityFeature,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import (
    DOMAIN,
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
from .coordinator import LymowCoordinator
from .entity_base import LymowEntity

# All possible features the entity can advertise. supported_features (below)
# returns a subset of these depending on current state.
_ALL_FEATURES = (
    LawnMowerEntityFeature.START_MOWING
    | LawnMowerEntityFeature.PAUSE
    | LawnMowerEntityFeature.DOCK
)
_NO_FEATURES = LawnMowerEntityFeature(0)


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coord: LymowCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([LymowMower(coord)])


class LymowMower(LymowEntity, LawnMowerEntity):
    """Lymow robot mower entity."""

    _attr_name = None  # use device name

    def __init__(self, coordinator: LymowCoordinator) -> None:
        super().__init__(coordinator, "mower")

    @property
    def supported_features(self) -> LawnMowerEntityFeature:
        """Show only the buttons that make sense for the current state.

        Matrix:
          - WAITING / NONE / CHARGING / CHARGING_FULL → Start (start fresh mow)
          - MOWING / RESUME / ZONE_PARTITION / ESCAPING → Pause + Dock
          - PAUSE                  → Start (resumes) + Dock
          - DOCKING                → Pause (Dock would be redundant)
          - PAUSE_DOCKING          → Start (resumes the dock approach)
          - ERROR                  → Pause (also clears the error per arch.md §6b)
          - EMERGENCY_STOP / UPDATING / RTT / REMOTE_CONTROL → no buttons
            (mower is in a state where remote control isn't appropriate)

        For destructive actions (cancel-task with stop-in-place, dock that
        abandons task progress), use the dedicated services
        lymow_mqtt.cancel_task and lymow_mqtt.dock_cancel_task.
        """
        ri = self.coordinator.state_dict.get("robotInfo")
        if ri is None:
            return _NO_FEATURES
        ws = ri.workStatus
        rs = ri.robotStatus

        # Error state — only "Clear Error" via Pause
        if rs == WORK_STATUS_ERROR or ws == WORK_STATUS_ERROR:
            return LawnMowerEntityFeature.PAUSE
        # Emergency stop / firmware update / factory test / remote control
        # — hide all buttons; user must intervene physically or via service
        if rs == WORK_STATUS_EMERGENCY_STOP or ws == WORK_STATUS_EMERGENCY_STOP:
            return _NO_FEATURES
        # Active task — pause or recall
        if ws in (
            WORK_STATUS_MOWING,
            WORK_STATUS_RESUME,
            WORK_STATUS_ZONE_PARTITION,
            WORK_STATUS_ESCAPING,
        ):
            return LawnMowerEntityFeature.PAUSE | LawnMowerEntityFeature.DOCK
        # Paused mid-mow — Start routes to resume, Dock sends home
        if ws == WORK_STATUS_PAUSE:
            return LawnMowerEntityFeature.START_MOWING | LawnMowerEntityFeature.DOCK
        # Heading to dock — Dock would be redundant
        if ws == WORK_STATUS_DOCKING:
            return LawnMowerEntityFeature.PAUSE
        # Paused while docking — Start routes to resume the dock approach
        if ws == WORK_STATUS_PAUSE_DOCKING:
            return LawnMowerEntityFeature.START_MOWING
        # Idle / charging — only Start makes sense
        if ws in (
            WORK_STATUS_WAITING,
            WORK_STATUS_NONE,
            WORK_STATUS_CHARGING,
            WORK_STATUS_CHARGING_FULL,
        ):
            return LawnMowerEntityFeature.START_MOWING
        # Anything else (UPDATING, RTT, REMOTE_CONTROL) — hide buttons
        return _NO_FEATURES

    @property
    def activity(self) -> LawnMowerActivity | None:
        """Map (workStatus, robotStatus) to one of HA's 5 LawnMowerActivity buckets.

        Priority order (first match wins):
          1. robotStatus ∈ {ERROR, EMERGENCY_STOP}      → ERROR (physical fault overrides task intent)
          2. robotStatus ∈ {CHARGING, CHARGING_FULL}    → DOCKED (physically at dock charging)
          3. workStatus ∈ {PAUSE, PAUSE_DOCKING}        → PAUSED
          4. workStatus ∈ {MOWING, RESUME, ZONE_PARTITION, ESCAPING}  → MOWING
          5. workStatus = DOCKING                        → RETURNING
          6. workStatus = WAITING                        → DOCKED
          7. else (NONE, REMOTE_CONTROL, UPDATING, RTT) → None (Unknown)

        Returning None gives HA's "Unknown" state for firmware states that
        don't fit any of the 5 LawnMowerActivity buckets cleanly.
        """
        ri = self.coordinator.state_dict.get("robotInfo")
        if ri is None:
            return None
        ws = ri.workStatus
        rs = ri.robotStatus

        # 1. Physical error states override task intent
        if rs in (WORK_STATUS_ERROR, WORK_STATUS_EMERGENCY_STOP):
            return LawnMowerActivity.ERROR
        # 2. Charging at dock — even if task intent is still "Docking" mid-recharge
        if rs in (WORK_STATUS_CHARGING, WORK_STATUS_CHARGING_FULL):
            return LawnMowerActivity.DOCKED
        # 3. Paused (either variant)
        if ws in (WORK_STATUS_PAUSE, WORK_STATUS_PAUSE_DOCKING):
            return LawnMowerActivity.PAUSED
        # 4. Active task states
        if ws in (
            WORK_STATUS_MOWING,
            WORK_STATUS_RESUME,
            WORK_STATUS_ZONE_PARTITION,
            WORK_STATUS_ESCAPING,
        ):
            return LawnMowerActivity.MOWING
        # 5. Returning to dock
        if ws == WORK_STATUS_DOCKING:
            return LawnMowerActivity.RETURNING
        # 6. Idle on dock not charging
        if ws == WORK_STATUS_WAITING:
            return LawnMowerActivity.DOCKED
        # 7. Anything else (NONE, REMOTE_CONTROL, UPDATING, RTT) — show Unknown
        return None

    async def async_start_mowing(self) -> None:
        """Start mow OR resume, depending on current state.

        Three resume cases dispatch to cmd_resume (which sends the right
        firmware userCtrl variant — 4 or 22 — based on state):
          1. Paused mid-mow (workStatus = PAUSE)
          2. Paused mid-dock (workStatus = PAUSE_DOCKING)
          3. Mid-task recharge dock with task saved (workStatus = CHARGING
             or CHARGING_FULL AND isRecharging = True). Critical: without
             this check, a "save progress" dock followed by Start in HA
             would send USER_CTRL_CLEAN (1) which silently RESETS task
             progress. The official app handles this case via Resume.

        Otherwise (idle on dock, no saved task) → fresh start via cmd_start.
        """
        s = self.coordinator.state_dict.get("robotInfo")
        if s is None:
            await self.coordinator.cmd_start()
            return
        is_recharging = bool(getattr(s, "isRecharging", False))
        is_paused = s.workStatus in (WORK_STATUS_PAUSE, WORK_STATUS_PAUSE_DOCKING)
        is_saved_recharge = (
            is_recharging
            and s.workStatus in (WORK_STATUS_CHARGING, WORK_STATUS_CHARGING_FULL)
        )
        if is_paused or is_saved_recharge:
            await self.coordinator.cmd_resume()
        else:
            await self.coordinator.cmd_start()

    async def async_pause(self) -> None:
        await self.coordinator.cmd_pause()

    async def async_dock(self) -> None:
        """Dock and KEEP task progress.

        Use the lymow_mqtt.dock_cancel_task service for the destructive
        variant that abandons the task.
        """
        await self.coordinator.cmd_dock_recharge()
