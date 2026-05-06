"""Lymow LawnMower platform."""

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
    DOCKED_STATUSES,
    DOMAIN,
    ERROR_STATUSES,
    MOWING_STATUSES,
    PAUSED_STATUSES,
    RETURNING_STATUSES,
    WORK_STATUS_OFFLINE,
    error_label,
    F_ERROR_CODE,
    F_ERROR_CODES,
    F_CLEAN_ZONE_IDS,
    F_GO_ZONE_IDS,
    F_CUT_ZONE_ID,
    F_CLEAN_AREA,
    F_MAP_AREA,
    F_RTK_STATUS,
    RTK_STATUS_LABELS,
)
from .coordinator import LymowCoordinator
from .entity_base import LymowEntity

FEATURES = (
    LawnMowerEntityFeature.START_MOWING
    | LawnMowerEntityFeature.PAUSE
    | LawnMowerEntityFeature.DOCK
)


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coord: LymowCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([LymowMower(coord)], update_before_add=False)


class LymowMower(LymowEntity, LawnMowerEntity):
    """Lymow robot mower entity."""

    _attr_name            = None  # use device name as entity name
    _attr_supported_features = FEATURES

    def __init__(self, coordinator: LymowCoordinator) -> None:
        super().__init__(coordinator, "mower")

    @property
    def activity(self) -> LawnMowerActivity:
        status = self.coordinator.work_status
        if status in MOWING_STATUSES:
            return LawnMowerActivity.MOWING
        if status in RETURNING_STATUSES:
            return LawnMowerActivity.RETURNING
        if status in DOCKED_STATUSES:
            return LawnMowerActivity.DOCKED
        if status in PAUSED_STATUSES:
            return LawnMowerActivity.PAUSED
        if status in ERROR_STATUSES:
            return LawnMowerActivity.ERROR
        if status == WORK_STATUS_OFFLINE:
            return LawnMowerActivity.ERROR
        return LawnMowerActivity.ERROR

    @property
    def extra_state_attributes(self) -> dict:
        d = self.coordinator.data or {}
        attrs: dict = {}

        # Active zones
        if zone := d.get(F_CUT_ZONE_ID):
            attrs["current_zone_id"] = zone
        if zones := d.get(F_GO_ZONE_IDS) or d.get(F_CLEAN_ZONE_IDS):
            attrs["queued_zone_ids"] = zones

        # Session stats
        if area := d.get(F_CLEAN_AREA):
            attrs["session_area_m2"] = area
        if total := d.get(F_MAP_AREA):
            attrs["total_map_area_m2"] = total

        # RTK GPS
        if rtk := d.get(F_RTK_STATUS):
            attrs["rtk_status"]       = RTK_STATUS_LABELS.get(rtk, rtk)
            attrs["rtk_status_code"]  = rtk

        # Errors
        err = d.get(F_ERROR_CODE)
        if err is not None and err != 0:
            attrs["error_code"]    = err
            attrs["error_message"] = error_label(err)
        if errs := d.get(F_ERROR_CODES):
            attrs["error_codes"] = errs

        # Work status as human label (for automation use)
        attrs["work_status_code"] = self.coordinator.work_status

        return attrs

    async def async_start_mowing(self) -> None:
        await self.coordinator.async_start_mow()

    async def async_pause(self) -> None:
        await self.coordinator.async_pause()

    async def async_dock(self) -> None:
        await self.coordinator.async_dock()
