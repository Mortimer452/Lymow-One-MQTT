"""Shared base class for Lymow entities."""
from __future__ import annotations

from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, MANUFACTURER
from .coordinator import LymowCoordinator


class LymowEntity(CoordinatorEntity[LymowCoordinator]):
    """Base for all Lymow entities."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: LymowCoordinator, key: str) -> None:
        super().__init__(coordinator)
        self._key = key
        self._attr_unique_id = f"{coordinator.thing_name}_{key}"

    @property
    def device_info(self) -> DeviceInfo:
        s = self.coordinator.state_dict
        di = s.get("deviceInfo")
        sw = di.softwareVersion if di and di.HasField("softwareVersion") else None
        return DeviceInfo(
            identifiers={(DOMAIN, self.coordinator.thing_name)},
            manufacturer=MANUFACTURER,
            model="Lymow One",
            sw_version=sw,
            name=f"Lymow {self.coordinator.thing_name[-6:]}",
        )

    @property
    def available(self) -> bool:
        return self.coordinator.is_online
