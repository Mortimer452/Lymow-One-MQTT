"""Shared base entity for all Lymow platforms."""

from __future__ import annotations

from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, MANUFACTURER
from .coordinator import LymowCoordinator


class LymowEntity(CoordinatorEntity[LymowCoordinator]):
    """Base class — wires up device_info and unique_id prefix."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: LymowCoordinator, unique_suffix: str) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.thing_name}_{unique_suffix}"

    @property
    def device_info(self) -> dict:
        info = self.coordinator.device_info_data
        return {
            "identifiers":   {(DOMAIN, self.coordinator.thing_name)},
            "name":          self.coordinator.config_entry.data.get(
                                 "device_name", f"Lymow {self.coordinator.thing_name}"
                             ),
            "manufacturer":  MANUFACTURER,
            "model":         info.get("model") or info.get("deviceType") or "Robot Mower",
            "sw_version":    (
                info.get("fwVersion")
                or self.coordinator.data.get("fwVersion")
                or info.get("firmwareVersion")
            ),
            "serial_number": info.get("serialNumber") or self.coordinator.thing_name,
        }

    @property
    def available(self) -> bool:
        return self.coordinator.last_update_success and self.coordinator.is_online
