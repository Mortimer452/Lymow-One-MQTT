"""Lymow select platform — Clean Mode."""

from __future__ import annotations

from homeassistant.components.select import SelectEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import CLEAN_MODE_OPTIONS, DOMAIN, F_CLEAN_MODE
from .coordinator import LymowCoordinator
from .entity_base import LymowEntity


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coord: LymowCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([LymowCleanModeSelect(coord)], update_before_add=False)


class LymowCleanModeSelect(LymowEntity, SelectEntity):
    """Select entity for mowing mode."""

    _attr_name    = "Mow Mode"
    _attr_icon    = "mdi:grass"
    _attr_options = CLEAN_MODE_OPTIONS  # real protobuf string values

    def __init__(self, coordinator: LymowCoordinator) -> None:
        super().__init__(coordinator, "clean_mode_select")

    @property
    def current_option(self) -> str | None:
        return (self.coordinator.data or {}).get(F_CLEAN_MODE)

    async def async_select_option(self, option: str) -> None:
        await self.coordinator.async_set_clean_mode(option)
