"""Lymow switch entities."""
from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.util import dt as dt_util

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


class LymowDockOnErrorSwitch(LymowEntity, SwitchEntity):
    """Toggle the firmware's auto-return-to-dock-on-error feature.

    Backed by `robotConfig.dockOnError` (PbRobotConfig field 22). When ON,
    the mower automatically returns to the dock if it enters an error state
    and no user action is taken within ~30 minutes (fixed firmware-side
    timeout). A device-firmware setting — the official app added a UI toggle
    for it in 3.0.7/3.0.8, but the field has existed since ≥3.0.5.

    Pairs with the RTK Accuracy Guard as a second "don't leave the mower
    stuck outside" layer: the guard reacts to degraded RTK while mowing;
    this reacts to any error state that lingers unattended.
    """

    _attr_translation_key = "dock_on_error"
    _attr_entity_category = EntityCategory.CONFIG
    _attr_icon = "mdi:home-import-outline"

    def __init__(self, coordinator: LymowCoordinator) -> None:
        super().__init__(coordinator, "dock_on_error")

    @property
    def available(self) -> bool:
        # Needs a robotConfig broadcast to know the current state. Unlike
        # the rrConfig entities we don't gate on rrConfig specifically —
        # dockOnError is a top-level robotConfig scalar, present whenever
        # robotConfig has been received.
        if not super().available:
            return False
        return self.coordinator.state_dict.get("robotConfig") is not None

    @property
    def is_on(self) -> bool | None:
        rc = self.coordinator.state_dict.get("robotConfig")
        if rc is None:
            return None
        # proto3 explicit-presence: absence means the firmware hasn't
        # reported it yet — treat as unknown rather than False.
        if not rc.HasField("dockOnError"):
            return None
        return bool(rc.dockOnError)

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self.coordinator.cmd_set_dock_on_error(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self.coordinator.cmd_set_dock_on_error(False)


class LymowAutoHeadlightsSwitch(LymowEntity, SwitchEntity):
    """Keep the mower's headlight window pinned to sunset → sunrise.

    The app's "Headlight mode" is a fixed daily window stored on the device
    (`robotConfig.openLedTime` / `closeLedTime`, UTC — arch.md §6i). With
    this ON, the integration re-writes that window every night at 3 AM local
    using HA's configured location, and once immediately when switched on
    or at startup. OFF just stops writing — the mower keeps the last
    window it was given, and the app can still edit it by hand.

    HA-side setting stored in config-entry options.
    """

    _attr_translation_key = "auto_headlights"
    _attr_entity_category = EntityCategory.CONFIG
    _attr_icon = "mdi:car-light-high"

    def __init__(self, coordinator: LymowCoordinator) -> None:
        super().__init__(coordinator, "auto_headlights")

    @property
    def available(self) -> bool:
        # Local setting — editable even while the mower is offline; a
        # missed write self-heals at the next nightly run.
        return True

    @property
    def is_on(self) -> bool:
        return self.coordinator.auto_headlights_enabled

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Readback of the window the mower currently holds, in local time.

        Sourced from the device's last robotConfig broadcast (not from what
        we computed), so it reflects what actually took. Absent until a
        robotConfig carrying the LED times has been received.
        """
        attrs: dict[str, Any] = {
            "description": (
                "Every night at 3:00 AM, sets the mower's headlight window "
                "to today's sunset (on) and sunrise (off) for this Home "
                "Assistant location. Off leaves the mower's current window "
                "untouched."
            )
        }
        rc = self.coordinator.state_dict.get("robotConfig")
        if rc is not None and rc.HasField("openLedTime") and rc.HasField("closeLedTime"):
            attrs["headlights_on"] = _utc_hm_to_local(
                rc.openLedTime.hour, rc.openLedTime.minute
            )
            attrs["headlights_off"] = _utc_hm_to_local(
                rc.closeLedTime.hour, rc.closeLedTime.minute
            )
        return attrs

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self.coordinator.async_set_auto_headlights(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self.coordinator.async_set_auto_headlights(False)


def _utc_hm_to_local(hour: int, minute: int) -> str:
    """Render a device-side UTC hour/minute as today's local HH:MM."""
    today_utc = datetime.now(UTC).replace(
        hour=hour, minute=minute, second=0, microsecond=0
    )
    return dt_util.as_local(today_utc).strftime("%H:%M")


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coord: LymowCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([
        LymowAutoRechargeSwitch(coord),
        LymowAccuracyGuardSwitch(coord),
        LymowDockOnErrorSwitch(coord),
        LymowAutoHeadlightsSwitch(coord),
    ])
