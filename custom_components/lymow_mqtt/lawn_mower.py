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
    def activity(self) -> LawnMowerActivity:
        s = self.coordinator.state_dict.get("robotInfo")
        if s is None:
            return LawnMowerActivity.ERROR
        ws = s.workStatus
        if ws in (
            WORK_STATUS_MOWING,
            WORK_STATUS_RESUME,
            WORK_STATUS_ZONE_PARTITION,
            WORK_STATUS_ESCAPING,
        ):
            return LawnMowerActivity.MOWING
        if ws in (WORK_STATUS_PAUSE, WORK_STATUS_PAUSE_DOCKING):
            return LawnMowerActivity.PAUSED
        if ws == WORK_STATUS_DOCKING:
            return LawnMowerActivity.RETURNING
        if ws in (
            WORK_STATUS_WAITING,
            WORK_STATUS_CHARGING,
            WORK_STATUS_CHARGING_FULL,
            WORK_STATUS_NONE,
        ):
            return LawnMowerActivity.DOCKED
        if ws in (WORK_STATUS_ERROR, WORK_STATUS_EMERGENCY_STOP):
            return LawnMowerActivity.ERROR
        return LawnMowerActivity.DOCKED  # fallback for unexpected states

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
