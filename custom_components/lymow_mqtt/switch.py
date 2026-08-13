"""Lymow switch entities."""
from __future__ import annotations

from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import CONF_GUARD_ENABLED, DOMAIN
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
            # rrConfig hasn't arrived yet — paired with the `available` gate
            # below, this branch is unreachable in practice, but stays as a
            # belt-and-suspenders default.
            return None
        # rrConfig is in the cache. The firmware omits `enableRr` from the
        # wire when the value is the proto3 default (False) — so the absence
        # of the field IS the "disabled" signal, not an "unknown" signal.
        # Compare with the receive-side fix in state.py (rrConfig is now
        # replaced rather than merged for exactly this reason).
        return bool(rc.rrConfig.enableRr)

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self.coordinator.cmd_set_auto_recharge(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self.coordinator.cmd_set_auto_recharge(False)


class LymowAccuracyGuardSwitch(LymowEntity, SwitchEntity):
    """Enable the RTK accuracy guard (accuracy_guard.py).

    When ON, the integration watches horizontalAccuracy while mowing and
    docks (or pauses) the mower on sustained degradation — protection
    against a failing RTK base station letting the mower wander out of
    bounds. Off by default: it commands the mower autonomously, so it
    must be an explicit user choice.

    HA-side setting stored in config-entry options — no firmware traffic.
    """

    _attr_translation_key = "accuracy_guard"
    _attr_entity_category = EntityCategory.CONFIG
    _attr_icon = "mdi:satellite-uplink"
    # HA has no native per-entity help text; a static attribute shows in
    # the more-info dialog's Attributes section.
    _attr_extra_state_attributes = {
        "description": (
            "Watches RTK accuracy while mowing and docks or pauses the "
            "mower when accuracy stays degraded past the hold time — "
            "protection against a failing RTK base station. Arms itself "
            "once localization reports Running after each task start."
        )
    }

    def __init__(self, coordinator: LymowCoordinator) -> None:
        super().__init__(coordinator, "accuracy_guard")

    @property
    def available(self) -> bool:
        # Local setting — editable even while the mower is offline.
        return True

    @property
    def is_on(self) -> bool:
        return self.coordinator.guard_config.enabled

    async def async_turn_on(self, **kwargs: Any) -> None:
        self.coordinator.set_guard_option(CONF_GUARD_ENABLED, True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        self.coordinator.set_guard_option(CONF_GUARD_ENABLED, False)


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coord: LymowCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([
        LymowAutoRechargeSwitch(coord),
        LymowAccuracyGuardSwitch(coord),
    ])
