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
    WORK_STATUS_PAUSE,
    WORK_STATUS_PAUSE_DOCKING,
    WORK_STATUS_RESUME,
    WORK_STATUS_WAITING,
    WORK_STATUS_ZONE_PARTITION,
)
from .coordinator import LymowCoordinator
from .entity_base import LymowEntity

_FEATURES = (
    LawnMowerEntityFeature.START_MOWING
    | LawnMowerEntityFeature.PAUSE
    | LawnMowerEntityFeature.DOCK
)


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coord: LymowCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([LymowMower(coord)])


class LymowMower(LymowEntity, LawnMowerEntity):
    """Lymow robot mower entity."""

    _attr_name = None  # use device name
    _attr_supported_features = _FEATURES

    def __init__(self, coordinator: LymowCoordinator) -> None:
        super().__init__(coordinator, "mower")

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
        """Start mow OR resume from paused, depending on current state."""
        s = self.coordinator.state_dict.get("robotInfo")
        if s and s.workStatus in (WORK_STATUS_PAUSE, WORK_STATUS_PAUSE_DOCKING):
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
