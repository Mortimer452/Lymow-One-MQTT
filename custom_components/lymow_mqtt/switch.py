"""Lymow switch entities."""
from __future__ import annotations

from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .coordinator import LymowCoordinator
from .entity_base import LymowEntity


class LymowAutoRechargeSwitch(LymowEntity, SwitchEntity):
    """Toggle the firmware's auto-recharge-and-resume feature.

    Backed by `robotConfig.rrConfig.enableRr`. When ON, the mower will
    autonomously dock when battery drops below `rechargeBat` percent and
    auto-resume the saved task when battery climbs back above `resumeBat`
    (within the configured time window). When OFF, the user (or HA
    automations) must manage dock-on-low-battery manually.

    The other rrConfig fields (battery thresholds, time window) aren't
    exposed as separate HA entities — users who want fine-grained control
    can build automations against `sensor.<mower>_battery` and the
    lawn_mower entity's actions. This switch is the simple "let the
    firmware handle it Y/N" choice that the official app exposes too.
    """

    _attr_translation_key = "auto_recharge"
    _attr_entity_category = EntityCategory.CONFIG
    _attr_icon = "mdi:battery-sync"

    def __init__(self, coordinator: LymowCoordinator) -> None:
        super().__init__(coordinator, "auto_recharge")

    @property
    def available(self) -> bool:
        """Available only once we've received a robotConfig with rrConfig.

        Without this guard, `cmd_set_auto_recharge` would have nothing to
        carry forward and would reset the user's other rrConfig fields to
        firmware defaults. Better to disable the switch until we know the
        current state.
        """
        if not super().available:
            return False
        rc = self.coordinator.state_dict.get("robotConfig")
        return rc is not None and rc.HasField("rrConfig")

    @property
    def is_on(self) -> bool | None:
        rc = self.coordinator.state_dict.get("robotConfig")
        if rc is None or not rc.HasField("rrConfig"):
            return None
        rr = rc.rrConfig
        if not rr.HasField("enableRr"):
            return None
        return bool(rr.enableRr)

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self.coordinator.cmd_set_auto_recharge(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self.coordinator.cmd_set_auto_recharge(False)


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coord: LymowCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([LymowAutoRechargeSwitch(coord)])
