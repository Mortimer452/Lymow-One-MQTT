"""Lymow number entities — user-editable rrConfig thresholds.

Two entities live here:

- `LymowRechargeThresholdNumber` — `rrConfig.rechargeBat`. Battery
  percentage below which the firmware auto-docks mid-task.
- `LymowResumeThresholdNumber` — `rrConfig.resumeBat`. Battery percentage
  at which the firmware auto-resumes a saved task from the dock.

Both gate `available` on a populated `rrConfig` for the same reason as
the auto-recharge switch: without a known current value to carry forward,
a write would clobber the user's other settings with defaults. Once
rrConfig has been observed once (typically within a second of HA startup
via the L3 wakeup), the entities become operable.

Cross-validation (recharge < resume) lives here in `async_set_native_value`
— HA doesn't coordinate between two number entities natively. A write
that would invert the relationship raises a HomeAssistantError so the
user gets a clear error in the frontend rather than a silent firmware
rejection or a surprise reordering.
"""
from __future__ import annotations

from typing import Any

from homeassistant.components.number import (
    NumberDeviceClass,
    NumberEntity,
    NumberMode,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    PERCENTAGE,
    EntityCategory,
    UnitOfLength,
    UnitOfTime,
)
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import CONF_GUARD_HOLD_MIN, CONF_GUARD_THRESHOLD, DOMAIN
from .coordinator import LymowCoordinator
from .entity_base import LymowEntity


def _rrconfig(coordinator: LymowCoordinator):
    """Return the cached rrConfig sub-message, or None if not yet observed."""
    rc = coordinator.state_dict.get("robotConfig")
    if rc is None or not rc.HasField("rrConfig"):
        return None
    return rc.rrConfig


class _LymowRrThresholdNumber(LymowEntity, NumberEntity):
    """Shared base for the recharge / resume battery-threshold entities.

    Subclasses set the entity key, translation_key, min/max bounds, and
    override `_current_value` to pull the right rrConfig field, plus
    `_apply` to dispatch to the right coordinator command.
    """

    _attr_entity_category = EntityCategory.CONFIG
    _attr_device_class = NumberDeviceClass.BATTERY
    _attr_native_unit_of_measurement = PERCENTAGE
    _attr_native_step = 1
    _attr_mode = NumberMode.SLIDER

    @property
    def available(self) -> bool:
        if not super().available:
            return False
        return _rrconfig(self.coordinator) is not None

    @property
    def native_value(self) -> float | None:
        return self._current_value()

    def _current_value(self) -> int | None:
        raise NotImplementedError

    async def _apply(self, value: int) -> None:
        raise NotImplementedError


class LymowRechargeThresholdNumber(_LymowRrThresholdNumber):
    """`rrConfig.rechargeBat` — battery % below which the mower auto-docks."""

    _attr_translation_key = "recharge_threshold"
    _attr_icon = "mdi:battery-arrow-down"
    _attr_native_min_value = 5
    _attr_native_max_value = 50

    def __init__(self, coordinator: LymowCoordinator) -> None:
        super().__init__(coordinator, "recharge_threshold")

    def _current_value(self) -> int | None:
        rr = _rrconfig(self.coordinator)
        if rr is None or not rr.HasField("rechargeBat"):
            return None
        return int(rr.rechargeBat)

    async def async_set_native_value(self, value: float) -> None:
        new = int(round(value))
        rr = _rrconfig(self.coordinator)
        if rr is not None and rr.HasField("resumeBat") and new >= int(rr.resumeBat):
            raise HomeAssistantError(
                f"Recharge threshold ({new}%) must be lower than the "
                f"resume threshold ({int(rr.resumeBat)}%). Lower the resume "
                f"threshold first, or pick a smaller recharge value."
            )
        await self.coordinator.cmd_set_recharge_threshold(new)


class LymowResumeThresholdNumber(_LymowRrThresholdNumber):
    """`rrConfig.resumeBat` — battery % at which the mower auto-resumes."""

    _attr_translation_key = "resume_threshold"
    _attr_icon = "mdi:battery-arrow-up"
    _attr_native_min_value = 50
    _attr_native_max_value = 100

    def __init__(self, coordinator: LymowCoordinator) -> None:
        super().__init__(coordinator, "resume_threshold")

    def _current_value(self) -> int | None:
        rr = _rrconfig(self.coordinator)
        if rr is None or not rr.HasField("resumeBat"):
            return None
        return int(rr.resumeBat)

    async def async_set_native_value(self, value: float) -> None:
        new = int(round(value))
        rr = _rrconfig(self.coordinator)
        if rr is not None and rr.HasField("rechargeBat") and new <= int(rr.rechargeBat):
            raise HomeAssistantError(
                f"Resume threshold ({new}%) must be higher than the "
                f"recharge threshold ({int(rr.rechargeBat)}%). Raise the "
                f"recharge threshold first, or pick a larger resume value."
            )
        await self.coordinator.cmd_set_resume_threshold(new)


class _LymowGuardNumber(LymowEntity, NumberEntity):
    """Shared base for accuracy-guard option numbers.

    Unlike the rrConfig numbers above, these are HA-side settings stored
    in config-entry options — no firmware round-trip, no availability
    gate (editable even while the mower is offline).
    """

    _attr_entity_category = EntityCategory.CONFIG

    @property
    def available(self) -> bool:
        return True


class LymowGuardThresholdNumber(_LymowGuardNumber):
    """Accuracy-guard trip threshold — meters of horizontalAccuracy.

    Default 1.0 m: field history shows healthy mowing at p90 < 1.0 m and
    real base-station failures at meter-to-hundreds-of-meters scale.
    """

    _attr_translation_key = "guard_threshold"
    _attr_icon = "mdi:crosshairs-gps"
    # DISTANCE device_class + native meters = HA's built-in unit conversion,
    # same as the horizontal_accuracy sensor this threshold is compared
    # against — imperial users see/enter feet, the option stays meters.
    _attr_device_class = NumberDeviceClass.DISTANCE
    _attr_native_min_value = 0.1
    _attr_native_max_value = 10.0
    _attr_native_step = 0.1
    _attr_native_unit_of_measurement = UnitOfLength.METERS
    _attr_mode = NumberMode.BOX

    def __init__(self, coordinator: LymowCoordinator) -> None:
        super().__init__(coordinator, "guard_threshold")

    @property
    def native_value(self) -> float:
        return self.coordinator.guard_config.threshold_m

    async def async_set_native_value(self, value: float) -> None:
        # Two decimals, not one: HA hands us native meters, and an imperial
        # entry (e.g. 3 ft = 0.9144 m) shouldn't quantize to a whole 0.1 m.
        self.coordinator.set_guard_option(CONF_GUARD_THRESHOLD, round(value, 2))


class LymowGuardHoldNumber(_LymowGuardNumber):
    """How long degradation must persist before the guard acts.

    Default 3 min: long enough to ride out blip-level RTK jitter
    (observed: sub-minute dips), short enough to bound how far a blind
    mower can wander.
    """

    _attr_translation_key = "guard_hold_time"
    _attr_icon = "mdi:timer-sand"
    _attr_native_min_value = 1
    _attr_native_max_value = 15
    _attr_native_step = 1
    _attr_native_unit_of_measurement = UnitOfTime.MINUTES
    _attr_mode = NumberMode.SLIDER

    def __init__(self, coordinator: LymowCoordinator) -> None:
        super().__init__(coordinator, "guard_hold_time")

    @property
    def native_value(self) -> float:
        return self.coordinator.guard_config.hold_s / 60.0

    async def async_set_native_value(self, value: float) -> None:
        self.coordinator.set_guard_option(CONF_GUARD_HOLD_MIN, int(round(value)))


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coord: LymowCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([
        LymowRechargeThresholdNumber(coord),
        LymowResumeThresholdNumber(coord),
        LymowGuardThresholdNumber(coord),
        LymowGuardHoldNumber(coord),
    ])
