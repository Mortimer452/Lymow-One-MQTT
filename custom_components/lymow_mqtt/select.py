"""Lymow select entities — accuracy-guard action.

One entity: which command the RTK accuracy guard dispatches when it
trips. "Dock" (default) drives the mower home along its channel; "Pause"
stops it in place — faster but leaves it parked mid-lawn until the user
intervenes. HA-side setting stored in config-entry options.
"""
from __future__ import annotations

from homeassistant.components.select import SelectEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .accuracy_guard import ACTION_DOCK, ACTION_PAUSE
from .const import CONF_GUARD_ACTION, DOMAIN
from .coordinator import LymowCoordinator
from .entity_base import LymowEntity


class LymowGuardActionSelect(LymowEntity, SelectEntity):
    """What the accuracy guard does when it trips: dock or pause."""

    _attr_translation_key = "guard_action"
    _attr_entity_category = EntityCategory.CONFIG
    _attr_icon = "mdi:home-alert"
    _attr_options = [ACTION_DOCK, ACTION_PAUSE]

    def __init__(self, coordinator: LymowCoordinator) -> None:
        super().__init__(coordinator, "guard_action")

    @property
    def available(self) -> bool:
        # Local setting — editable even while the mower is offline.
        return True

    @property
    def current_option(self) -> str:
        return self.coordinator.guard_config.action

    async def async_select_option(self, option: str) -> None:
        self.coordinator.set_guard_option(CONF_GUARD_ACTION, option)


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coord: LymowCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([LymowGuardActionSelect(coord)])
