"""Lymow sensor entities. Primary + diagnostic sensors share this file.

Per spec §4, sensors expose raw enum ints for work_status / robot_status
(no friendly-label mapping). Numeric sensors use HA's built-in unit
conversion via device_class + native_unit_of_measurement +
suggested_unit_of_measurement + suggested_display_precision.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    PERCENTAGE,
    EntityCategory,
    UnitOfArea,
    UnitOfLength,
    UnitOfSpeed,
    UnitOfTime,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity

from . import state as state_mod
from .const import DOMAIN, WARNING_CODE_LABELS, error_label, work_status_label
from .coordinator import LymowCoordinator
from .entity_base import LymowEntity
from .protocol import ZoneInfo


@dataclass(frozen=True, kw_only=True)
class LymowSensorDesc(SensorEntityDescription):
    """Sensor descriptor with a value-extraction callable."""

    value_fn: Callable[[dict[str, Any]], Any] = lambda s: None


# ─────────────────────────────────────────────
# Value extractors
# ─────────────────────────────────────────────

def _battery(s):
    ri = s.get("robotInfo")
    return ri.battery if ri else None


def _work_status(s):
    ri = s.get("robotInfo")
    if ri is None:
        return None
    return work_status_label(ri.workStatus)


def _robot_status(s):
    ri = s.get("robotInfo")
    if ri is None:
        return None
    return work_status_label(ri.robotStatus)


def _current_zone(s):
    return state_mod.derive_current_zone(s)


def _task_progress(s):
    ci = s.get("cleanInfo")
    if ci is None or not ci.HasField("cleanPercent"):
        return None
    return ci.cleanPercent * 100


def _mow_time(s):
    ci = s.get("cleanInfo")
    return ci.cleanTime if ci and ci.HasField("cleanTime") else None


def _next_schedule(s):
    """Compute datetime of next upcoming scheduled run."""
    schedules = s.get("schedules") or []
    if not schedules:
        return None
    now = datetime.now(UTC)
    candidates = []
    for sch in schedules:
        if sch.is_disabled:
            continue
        for day in sch.days_of_week:
            # day: 0=Sun, 6=Sat (per arch.md §5e)
            # Python weekday: 0=Mon, 6=Sun -> shift
            python_day = (day + 6) % 7
            days_ahead = (python_day - now.weekday()) % 7
            run_dt = now.replace(
                hour=sch.hour, minute=sch.minute, second=0, microsecond=0
            )
            if days_ahead == 0 and run_dt <= now:
                days_ahead = 7
            run_dt = run_dt + timedelta(days=days_ahead)
            candidates.append(run_dt)
    return min(candidates) if candidates else None


def _error_message(s):
    codes = s.get("errorCodes") or []
    if not codes:
        return "OK"
    return error_label(codes[0])


def _error_code(s):
    codes = s.get("errorCodes") or []
    return codes[0] if codes else 0


def _warning_code(s):
    codes = s.get("warningCodes") or []
    return codes[0] if codes else 0


def _rtk_quality(s):
    li = s.get("localizationInfo")
    if li is None or not li.HasField("positionQuality"):
        return None
    # PbLocalizationInfo.positionQuality enum (decompiled.js:388820-388873).
    # NOTE: this is the 4-value LocalQuality enum used inside localizationInfo,
    # NOT the 3-value RtkStatus enum (RTK_NOT_READY/FLOAT/FIX) that lives on
    # PbRtkDiagnosticL1.rtkStatus. The two are easy to confuse — different
    # field, different enum.
    return {
        0: "No signal",
        1: "GPS only",   # SINGLE_POINT — standard GPS, no RTK lock
        2: "Float fix",  # FLOAT_FIXED — RTK sub-meter
        3: "Fixed cm",   # FIXED — RTK centimeter
    }.get(li.positionQuality, f"unknown ({li.positionQuality})")


def _horizontal_accuracy(s):
    li = s.get("localizationInfo")
    return (
        li.horizontalAccuracy
        if li and li.HasField("horizontalAccuracy")
        else None
    )


def _wifi_signal(s):
    ri = s.get("robotInfo")
    return ri.wifiSignalQuality if ri else None


def _lte_signal(s):
    ri = s.get("robotInfo")
    return ri.lteSignalQuality if ri else None


def _firmware(s):
    di = s.get("deviceInfo")
    return (
        di.softwareVersion if di and di.HasField("softwareVersion") else None
    )


def _ip_address(s):
    di = s.get("deviceInfo")
    if di and di.HasField("ipAddress") and di.ipAddress:
        return di.ipAddress
    return s.get("rest_ip_address")


def _task_area(s):
    ci = s.get("cleanInfo")
    return ci.cleanArea if ci and ci.HasField("cleanArea") else None


def _total_mapped_area(s):
    ci = s.get("cleanInfo")
    return ci.mapArea if ci and ci.HasField("mapArea") else None


def _last_mow_duration(s):
    cr = s.get("last_clean_report")
    if not cr:
        return None
    ci = cr.cleanInfo
    return ci.cleanTime if ci.HasField("cleanTime") else None


def _last_mow_battery_used(s):
    cr = s.get("last_clean_report")
    return cr.usedBattery if cr and cr.HasField("usedBattery") else None


def _last_mow_zones(s):
    cr = s.get("last_clean_report")
    if not cr:
        return None
    zone_ids = list(cr.cleanInfo.areaInfo.cleanZoneIds)
    if not zone_ids:
        return None
    catalog = s.get("zone_catalog")
    if not catalog:
        return ", ".join(zone_ids)
    names = [
        catalog.zones_by_hashid[h].name
        if h in catalog.zones_by_hashid
        else h
        for h in zone_ids
    ]
    return ", ".join(names)


def _last_mow_end_type(s):
    cr = s.get("last_clean_report")
    if not cr or not cr.HasField("mowEndType"):
        return None
    return {1: "Normal", 2: "Cancel"}.get(
        cr.mowEndType, f"Unknown ({cr.mowEndType})"
    )


def _cut_speed(s):
    cfg = state_mod.active_cut_config(s)
    cs = cfg["cut_speed"]
    return {3: "Eco", 4: "Standard", 5: "Power", 6: "Turbo"}.get(cs)


def _cut_height(s):
    return state_mod.active_cut_config(s)["cut_height"]


def _move_speed(s):
    return state_mod.active_cut_config(s)["move_speed"]


# ─────────────────────────────────────────────
# Descriptors — primary
# ─────────────────────────────────────────────

PRIMARY_SENSORS: tuple[LymowSensorDesc, ...] = (
    LymowSensorDesc(
        key="battery",
        translation_key="battery",
        device_class=SensorDeviceClass.BATTERY,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=PERCENTAGE,
        value_fn=_battery,
    ),
    LymowSensorDesc(
        key="work_status",
        translation_key="work_status",
        value_fn=_work_status,
    ),
    LymowSensorDesc(
        key="robot_status",
        translation_key="robot_status",
        value_fn=_robot_status,
    ),
    LymowSensorDesc(
        key="current_zone",
        translation_key="current_zone",
        icon="mdi:map-marker",
        value_fn=_current_zone,
    ),
    LymowSensorDesc(
        key="task_progress",
        translation_key="task_progress",
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=PERCENTAGE,
        suggested_display_precision=0,
        value_fn=_task_progress,
    ),
    LymowSensorDesc(
        key="mow_time",
        translation_key="mow_time",
        device_class=SensorDeviceClass.DURATION,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfTime.MINUTES,
        value_fn=_mow_time,
    ),
    LymowSensorDesc(
        key="next_schedule",
        translation_key="next_schedule",
        device_class=SensorDeviceClass.TIMESTAMP,
        value_fn=_next_schedule,
    ),
    LymowSensorDesc(
        key="error_message",
        translation_key="error_message",
        icon="mdi:alert-circle-outline",
        value_fn=_error_message,
    ),
    LymowSensorDesc(
        key="last_mow_duration",
        translation_key="last_mow_duration",
        device_class=SensorDeviceClass.DURATION,
        native_unit_of_measurement=UnitOfTime.MINUTES,
        value_fn=_last_mow_duration,
    ),
)

# ─────────────────────────────────────────────
# Descriptors — diagnostic
# ─────────────────────────────────────────────

DIAGNOSTIC_SENSORS: tuple[LymowSensorDesc, ...] = (
    LymowSensorDesc(
        key="cut_speed",
        translation_key="cut_speed",
        entity_category=EntityCategory.DIAGNOSTIC,
        icon="mdi:fan",
        value_fn=_cut_speed,
    ),
    LymowSensorDesc(
        key="cut_height",
        translation_key="cut_height",
        entity_category=EntityCategory.DIAGNOSTIC,
        device_class=SensorDeviceClass.DISTANCE,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfLength.MILLIMETERS,
        suggested_display_precision=1,
        value_fn=_cut_height,
    ),
    LymowSensorDesc(
        key="move_speed",
        translation_key="move_speed",
        entity_category=EntityCategory.DIAGNOSTIC,
        device_class=SensorDeviceClass.SPEED,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfSpeed.METERS_PER_SECOND,
        suggested_display_precision=1,
        value_fn=_move_speed,
    ),
    LymowSensorDesc(
        key="error_code",
        translation_key="error_code",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=_error_code,
    ),
    LymowSensorDesc(
        key="warning_code",
        translation_key="warning_code",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=_warning_code,
    ),
    LymowSensorDesc(
        key="rtk_quality",
        translation_key="rtk_quality",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=_rtk_quality,
    ),
    LymowSensorDesc(
        key="horizontal_accuracy",
        translation_key="horizontal_accuracy",
        entity_category=EntityCategory.DIAGNOSTIC,
        device_class=SensorDeviceClass.DISTANCE,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfLength.METERS,
        suggested_display_precision=2,
        value_fn=_horizontal_accuracy,
    ),
    LymowSensorDesc(
        key="wifi_signal",
        translation_key="wifi_signal",
        entity_category=EntityCategory.DIAGNOSTIC,
        device_class=SensorDeviceClass.SIGNAL_STRENGTH,
        native_unit_of_measurement="dBm",
        value_fn=_wifi_signal,
    ),
    LymowSensorDesc(
        key="lte_signal",
        translation_key="lte_signal",
        entity_category=EntityCategory.DIAGNOSTIC,
        device_class=SensorDeviceClass.SIGNAL_STRENGTH,
        native_unit_of_measurement="dBm",
        value_fn=_lte_signal,
    ),
    LymowSensorDesc(
        key="firmware",
        translation_key="firmware",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=_firmware,
    ),
    LymowSensorDesc(
        key="ip_address",
        translation_key="ip_address",
        entity_category=EntityCategory.DIAGNOSTIC,
        icon="mdi:ip-network",
        value_fn=_ip_address,
    ),
    LymowSensorDesc(
        key="task_area",
        translation_key="task_area",
        entity_category=EntityCategory.DIAGNOSTIC,
        device_class=SensorDeviceClass.AREA,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfArea.SQUARE_METERS,
        suggested_display_precision=0,
        value_fn=_task_area,
    ),
    LymowSensorDesc(
        key="total_mapped_area",
        translation_key="total_mapped_area",
        entity_category=EntityCategory.DIAGNOSTIC,
        device_class=SensorDeviceClass.AREA,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfArea.SQUARE_METERS,
        suggested_display_precision=0,
        value_fn=_total_mapped_area,
    ),
    LymowSensorDesc(
        key="last_mow_battery_used",
        translation_key="last_mow_battery_used",
        entity_category=EntityCategory.DIAGNOSTIC,
        native_unit_of_measurement=PERCENTAGE,
        value_fn=_last_mow_battery_used,
    ),
    LymowSensorDesc(
        key="last_mow_zones",
        translation_key="last_mow_zones",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=_last_mow_zones,
    ),
    LymowSensorDesc(
        key="last_mow_end_type",
        translation_key="last_mow_end_type",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=_last_mow_end_type,
    ),
)


# ─────────────────────────────────────────────
# Entity class
# ─────────────────────────────────────────────

class LymowSensor(LymowEntity, SensorEntity):
    """Generic Lymow sensor."""

    entity_description: LymowSensorDesc

    def __init__(
        self, coordinator: LymowCoordinator, desc: LymowSensorDesc
    ) -> None:
        super().__init__(coordinator, desc.key)
        self.entity_description = desc

    @property
    def native_value(self):
        return self.entity_description.value_fn(self.coordinator.state_dict)

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        # warning_code: expose all_codes + label list
        if self.entity_description.key == "warning_code":
            codes = self.coordinator.state_dict.get("warningCodes") or []
            return {
                "all_codes": codes,
                "labels": [
                    WARNING_CODE_LABELS.get(c, f"unknown_{c}") for c in codes
                ],
            }
        # next_schedule: expose full schedule list
        if self.entity_description.key == "next_schedule":
            schedules = self.coordinator.state_dict.get("schedules") or []
            return {
                "schedules": [
                    {
                        "id": s.id,
                        "days": s.days_of_week,
                        "hour": s.hour,
                        "minute": s.minute,
                        "disabled": s.is_disabled,
                        "zones": s.zone_hash_ids,
                    }
                    for s in schedules
                ],
            }
        return None


# ─────────────────────────────────────────────
# Per-zone sensor: last-mowed timestamp + metadata
# ─────────────────────────────────────────────


class LymowZoneSensor(LymowEntity, SensorEntity, RestoreEntity):
    """One sensor per zone — state is the last mow timestamp.

    State updates when a `cleanReport` arrives whose `cleanZoneIds` list
    contains this zone's hashId. Persists across HA restarts via
    RestoreEntity (timestamp + mow_count + last_session_minutes).

    Entity_id pattern: `sensor.lymow_<thing_short>_zone_<zone_name_slug>`.
    Friendly name: "Lymow <thing_short> Zone <Zone Name>".
    Unique_id is anchored to the zone hashId, so renaming a zone in the
    Lymow app preserves the entity (and any automations referencing it).
    """

    _attr_has_entity_name = True
    _attr_device_class = SensorDeviceClass.TIMESTAMP

    def __init__(self, coordinator: LymowCoordinator, hash_id: str, name: str) -> None:
        super().__init__(coordinator, f"zone_{hash_id}")
        self._hash_id = hash_id
        # Live values overlay restored values; restored persists across
        # HA restarts so the entity isn't blank if the integration loads
        # before the next cleanReport.
        self._last_mowed_ts: datetime | None = None
        self._mow_count: int = 0
        self._last_session_minutes: float | None = None
        # Track the cleanReport object we've already counted, so we don't
        # double-increment if the coordinator notifies us multiple times
        # for the same report.
        self._last_processed_report: Any = None
        # _attr_name is set from the catalog name and refreshed on each
        # coordinator update (zone renames in the app propagate here).
        self._attr_name = f"Zone {name}"

    async def async_added_to_hass(self) -> None:
        """Restore last-known state from before the previous HA restart."""
        await super().async_added_to_hass()
        last = await self.async_get_last_state()
        if last and last.state not in (None, "unknown", "unavailable"):
            try:
                self._last_mowed_ts = datetime.fromisoformat(last.state)
            except (TypeError, ValueError):
                pass
            attrs = last.attributes or {}
            try:
                self._mow_count = int(attrs.get("mow_count", 0) or 0)
            except (TypeError, ValueError):
                self._mow_count = 0
            v = attrs.get("last_session_minutes")
            try:
                self._last_session_minutes = float(v) if v is not None else None
            except (TypeError, ValueError):
                self._last_session_minutes = None

    @callback
    def _handle_coordinator_update(self) -> None:
        """Refresh entity name from catalog + count this zone in any new cleanReport."""
        catalog = self.coordinator.state_dict.get("zone_catalog")
        if catalog is not None:
            zone = catalog.zones_by_hashid.get(self._hash_id)
            if zone is not None:
                # Pick up zone-name renames from the app
                self._attr_name = f"Zone {zone.name}"

        report = self.coordinator.state_dict.get("last_clean_report")
        if report is not None and report is not self._last_processed_report:
            self._last_processed_report = report
            try:
                zone_ids = list(report.cleanInfo.areaInfo.cleanZoneIds)
            except (AttributeError, TypeError):
                zone_ids = []
            if self._hash_id in zone_ids:
                self._last_mowed_ts = datetime.now(UTC)
                self._mow_count += 1
                # cleanTime is int32 minutes — same field/unit as the existing
                # last_mow_duration sensor (UnitOfTime.MINUTES). No conversion
                # needed; storing the raw value.
                ci = getattr(report, "cleanInfo", None)
                if ci is not None and ci.HasField("cleanTime"):
                    self._last_session_minutes = ci.cleanTime

        super()._handle_coordinator_update()

    @property
    def native_value(self) -> datetime | None:
        return self._last_mowed_ts

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Per-zone metadata + computed flags.

        Reads catalog live each time (no caching of zone metadata) so
        is_enabled / area reflect current state — zone toggles in the
        app propagate immediately on the next coordinator update.

        `area` is converted to the user's preferred unit (m² metric, ft²
        imperial) based on `hass.config.units`. HA's automatic unit
        conversion only works on sensor state values, not attributes, so
        we do it manually here. `area_unit` carries the unit string for
        users / template authors who want to know which it is without
        re-checking hass.config.units themselves.
        """
        out: dict[str, Any] = {
            "hash_id": self._hash_id,
            "mow_count": self._mow_count,
            "last_session_minutes": self._last_session_minutes,
            "is_enabled": None,
            "area": None,
            "area_unit": None,
            "mower_in_zone": False,
        }
        catalog = self.coordinator.state_dict.get("zone_catalog")
        if catalog is None:
            return out
        zone = catalog.zones_by_hashid.get(self._hash_id)
        if zone is None:
            # Zone deleted from the app — entity stays in registry but
            # we have nothing to compute. Caller can clean up via "Reset"
            # on the device card.
            return out
        out["is_enabled"] = zone.is_enabled
        if zone.polygon_points:
            area_m2 = state_mod.polygon_area(zone.polygon_points)
            # Lazy import — HA's unit conversion utilities aren't available
            # in the pure-Python test environment.
            from homeassistant.const import UnitOfArea
            from homeassistant.util.unit_conversion import AreaConverter
            from homeassistant.util.unit_system import METRIC_SYSTEM

            if self.hass.config.units is METRIC_SYSTEM:
                out["area"] = round(area_m2, 1)
                out["area_unit"] = UnitOfArea.SQUARE_METERS
            else:
                out["area"] = round(
                    AreaConverter.convert(
                        area_m2,
                        UnitOfArea.SQUARE_METERS,
                        UnitOfArea.SQUARE_FEET,
                    ),
                    1,
                )
                out["area_unit"] = UnitOfArea.SQUARE_FEET
            pose = self.coordinator.state_dict.get("pose")
            if pose is not None and hasattr(pose, "x") and hasattr(pose, "y"):
                out["mower_in_zone"] = state_mod.point_in_polygon(
                    pose.x, pose.y, zone.polygon_points
                )
        return out


# ─────────────────────────────────────────────
# Platform setup
# ─────────────────────────────────────────────


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coord: LymowCoordinator = hass.data[DOMAIN][entry.entry_id]

    # Fixed sensors — register immediately, available before the catalog lands
    async_add_entities(
        [LymowSensor(coord, d) for d in (*PRIMARY_SENSORS, *DIAGNOSTIC_SENSORS)]
    )

    # Per-zone sensors — discovered from the catalog, which arrives over MQTT
    # after async_setup_entry runs. Listen for coordinator updates and add
    # entities for any new zone hashIds we haven't seen yet. Same listener
    # also catches zones the user adds via the app post-install.
    seen_hash_ids: set[str] = set()

    @callback
    def _discover_zone_entities() -> None:
        catalog = coord.state_dict.get("zone_catalog")
        if catalog is None or not catalog.zones:
            return
        new: list[LymowZoneSensor] = []
        for zone in catalog.zones:
            if zone.hash_id in seen_hash_ids:
                continue
            new.append(LymowZoneSensor(coord, zone.hash_id, zone.name))
            seen_hash_ids.add(zone.hash_id)
        if new:
            async_add_entities(new)

    # Register the listener and try once immediately in case the catalog
    # already loaded between coordinator setup and platform setup (rare,
    # but possible — depends on QUERY_MAP response timing).
    entry.async_on_unload(coord.async_add_listener(_discover_zone_entities))
    _discover_zone_entities()
