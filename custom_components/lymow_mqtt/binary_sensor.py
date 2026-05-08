"""Lymow binary sensor entities."""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
    BinarySensorEntityDescription,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import (
    DOMAIN,
    WORK_STATUS_EMERGENCY_STOP,
    WORK_STATUS_ERROR,
)
from .coordinator import LymowCoordinator
from .entity_base import LymowEntity


@dataclass(frozen=True, kw_only=True)
class LymowBinarySensorDesc(BinarySensorEntityDescription):
    value_fn: Callable[[LymowCoordinator], bool] = lambda c: False


def _online(c: LymowCoordinator) -> bool:
    return c.is_online


def _charging(c: LymowCoordinator) -> bool:
    ri = c.state_dict.get("robotInfo")
    return bool(ri and ri.isCharging)


def _recharging(c: LymowCoordinator) -> bool:
    ri = c.state_dict.get("robotInfo")
    return bool(ri and ri.isRecharging)


def _error_active(c: LymowCoordinator) -> bool:
    ri = c.state_dict.get("robotInfo")
    return bool(ri and ri.robotStatus == WORK_STATUS_ERROR)


def _emergency_stop(c: LymowCoordinator) -> bool:
    ri = c.state_dict.get("robotInfo")
    return bool(ri and ri.robotStatus == WORK_STATUS_EMERGENCY_STOP)


BINARY_SENSORS: tuple[LymowBinarySensorDesc, ...] = (
    LymowBinarySensorDesc(
        key="online",
        translation_key="online",
        device_class=BinarySensorDeviceClass.CONNECTIVITY,
        value_fn=_online,
    ),
    LymowBinarySensorDesc(
        key="charging",
        translation_key="charging",
        device_class=BinarySensorDeviceClass.BATTERY_CHARGING,
        value_fn=_charging,
    ),
    LymowBinarySensorDesc(
        key="recharging",
        translation_key="recharging",
        entity_category=EntityCategory.DIAGNOSTIC,
        icon="mdi:battery-charging-outline",
        value_fn=_recharging,
    ),
    LymowBinarySensorDesc(
        key="error_active",
        translation_key="error_active",
        device_class=BinarySensorDeviceClass.PROBLEM,
        value_fn=_error_active,
    ),
    LymowBinarySensorDesc(
        key="emergency_stop",
        translation_key="emergency_stop",
        device_class=BinarySensorDeviceClass.PROBLEM,
        value_fn=_emergency_stop,
    ),
)


class LymowBinarySensor(LymowEntity, BinarySensorEntity):
    entity_description: LymowBinarySensorDesc

    def __init__(
        self,
        coordinator: LymowCoordinator,
        desc: LymowBinarySensorDesc,
    ) -> None:
        super().__init__(coordinator, desc.key)
        self.entity_description = desc

    @property
    def is_on(self) -> bool:
        return self.entity_description.value_fn(self.coordinator)


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coord: LymowCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([LymowBinarySensor(coord, d) for d in BINARY_SENSORS])
