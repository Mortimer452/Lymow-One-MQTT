"""Lymow number platform — Blade Height."""

from __future__ import annotations

from homeassistant.components.number import NumberEntity, NumberMode
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfLength
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN, F_CUT_HEIGHT, F_CUTTING_HEIGHT
from .coordinator import LymowCoordinator
from .entity_base import LymowEntity


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coord: LymowCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([LymowBladeHeight(coord)], update_before_add=False)


class LymowBladeHeight(LymowEntity, NumberEntity):
    """Blade height control (mm)."""

    _attr_name                        = "Blade Height"
    _attr_icon                        = "mdi:scissors-cutting"
    _attr_native_min_value            = 20
    _attr_native_max_value            = 60
    _attr_native_step                 = 5
    _attr_native_unit_of_measurement  = UnitOfLength.MILLIMETERS
    _attr_mode                        = NumberMode.SLIDER

    def __init__(self, coordinator: LymowCoordinator) -> None:
        super().__init__(coordinator, "blade_height")

    @property
    def native_value(self) -> float | None:
        d = self.coordinator.data or {}
        # Cloud shadow uses cuttingHeight, BLE uses cutHeight — prefer cloud value
        val = d.get(F_CUTTING_HEIGHT) or d.get(F_CUT_HEIGHT)
        return float(val) if val is not None else None

    async def async_set_native_value(self, value: float) -> None:
        await self.coordinator.async_set_blade_height(int(value))
