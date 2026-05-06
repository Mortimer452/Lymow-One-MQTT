"""Lymow sensor platform."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import PERCENTAGE, UnitOfArea, UnitOfLength, UnitOfTime
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import (
    CLEAN_MODE_ADAPTIVE_ZIGZAG,
    CLEAN_MODE_CHESS_BOARD,
    CLEAN_MODE_PERIMETER_ONLY,
    CLEAN_MODE_ZIGZAG,
    DOMAIN,
    F_BATTERY,
    F_CLEAN_AREA,
    F_CLEAN_MODE,
    F_CUT_HEIGHT,
    F_CUTTING_HEIGHT,
    F_FW_VERSION,
    F_LTE_SIGNAL,
    F_MCU_VERSION,
    F_NET_DETAIL,
    F_RTK_STATUS,
    F_WIFI_SIGNAL,
    NET_SIM_SIGNAL,
    NET_WIFI_SIGNAL,
    RTK_STATUS_LABELS,
    WORK_STATUS_OFFLINE,
    error_label,
    F_ERROR_CODE,
)
from .coordinator import LymowCoordinator
from .entity_base import LymowEntity

# Human-readable labels for cleanMode string values
CLEAN_MODE_LABELS: dict[str, str] = {
    CLEAN_MODE_ZIGZAG:          "Zigzag",
    CLEAN_MODE_CHESS_BOARD:     "Chess Board",
    CLEAN_MODE_PERIMETER_ONLY:  "Perimeter Only",
    CLEAN_MODE_ADAPTIVE_ZIGZAG: "Adaptive Zigzag",
}

# Human-readable work status labels (integer → string)
WORK_STATUS_LABELS: dict[int, str] = {
    -1: "Offline",
    0:  "Idle",
    1:  "Waiting",
    2:  "Mowing",
    3:  "Paused",
    4:  "Docking",
    5:  "Charging",
    6:  "Remote Control",
    7:  "Error",
    8:  "Resuming",
    9:  "Zone Partitioning",
    10: "Pause Docking",
    11: "Updating",
    12: "Fully Charged",
    13: "Emergency Stop",
    14: "Escaping",
    15: "RTT Test",
}


@dataclass(frozen=True, kw_only=True)
class LymowSensorDesc(SensorEntityDescription):
    # How to get the raw value from coordinator.data
    # Can be a simple key string or a callable(data: dict) -> Any
    value_source: str | Callable[[dict], Any] = ""
    # Optional transform applied to the raw value before storing
    transform: Callable[[Any], Any] | None = None


def _net(key: str) -> Callable[[dict], Any]:
    """Helper: extract a key from the nested netDetailInfo dict."""
    return lambda d: (d.get(F_NET_DETAIL) or {}).get(key)


SENSORS: tuple[LymowSensorDesc, ...] = (
    # ── Status ──────────────────────────────────────────────────────────
    LymowSensorDesc(
        key="work_status",
        name="Status",
        icon="mdi:robot-mower",
        value_source="workStatus",
        transform=lambda v: WORK_STATUS_LABELS.get(v, f"Unknown ({v})"),
    ),
    LymowSensorDesc(
        key="error",
        name="Error",
        icon="mdi:alert-circle-outline",
        value_source=F_ERROR_CODE,
        transform=lambda v: error_label(v) if v else "None",
        entity_registry_enabled_default=False,
    ),

    # ── Battery ─────────────────────────────────────────────────────────
    LymowSensorDesc(
        key="battery",
        name="Battery",
        native_unit_of_measurement=PERCENTAGE,
        device_class=SensorDeviceClass.BATTERY,
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:battery",
        value_source=F_BATTERY,
    ),

    # ── Mowing ──────────────────────────────────────────────────────────
    LymowSensorDesc(
        key="clean_mode",
        name="Mow Mode",
        icon="mdi:grass",
        value_source=F_CLEAN_MODE,
        transform=lambda v: CLEAN_MODE_LABELS.get(v, v),
    ),
    LymowSensorDesc(
        key="blade_height",
        name="Blade Height",
        native_unit_of_measurement=UnitOfLength.MILLIMETERS,
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:scissors-cutting",
        # cuttingHeight preferred (cloud shadow), fallback to cutHeight (BLE shadow)
        value_source=lambda d: d.get(F_CUTTING_HEIGHT) or d.get(F_CUT_HEIGHT),
    ),
    LymowSensorDesc(
        key="session_area",
        name="Session Mowed Area",
        native_unit_of_measurement=UnitOfArea.SQUARE_METERS,
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:map-check",
        value_source=F_CLEAN_AREA,
    ),

    # ── GPS / RTK ────────────────────────────────────────────────────────
    LymowSensorDesc(
        key="rtk_status",
        name="RTK GPS",
        icon="mdi:satellite-uplink",
        value_source=F_RTK_STATUS,
        transform=lambda v: RTK_STATUS_LABELS.get(v, f"Unknown ({v})"),
    ),
    LymowSensorDesc(
        key="rtk_precision",
        name="RTK Precision",
        native_unit_of_measurement=UnitOfLength.METERS,
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:crosshairs-gps",
        value_source=lambda d: (d.get("rtkDiagnosticL1") or {}).get("precision"),
        entity_registry_enabled_default=False,
    ),
    LymowSensorDesc(
        key="rtk_satellites",
        name="RTK Satellites",
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:satellite-variant",
        value_source=lambda d: (d.get("rtkDiagnosticL1") or {}).get("satelliteCount"),
        entity_registry_enabled_default=False,
    ),

    # ── Connectivity ─────────────────────────────────────────────────────
    LymowSensorDesc(
        key="wifi_signal",
        name="WiFi Signal",
        native_unit_of_measurement="dBm",
        device_class=SensorDeviceClass.SIGNAL_STRENGTH,
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:wifi",
        # Try top-level wifiSignalQuality first, then nested netDetailInfo.wifiSignal
        value_source=lambda d: d.get(F_WIFI_SIGNAL) or _net(NET_WIFI_SIGNAL)(d),
        entity_registry_enabled_default=False,
    ),
    LymowSensorDesc(
        key="lte_signal",
        name="4G Signal",
        native_unit_of_measurement="dBm",
        device_class=SensorDeviceClass.SIGNAL_STRENGTH,
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:signal-4g",
        value_source=lambda d: d.get(F_LTE_SIGNAL) or _net(NET_SIM_SIGNAL)(d),
        entity_registry_enabled_default=False,
    ),
    LymowSensorDesc(
        key="wifi_name",
        name="WiFi Network",
        icon="mdi:wifi-settings",
        value_source=_net("wifiName"),
        entity_registry_enabled_default=False,
    ),
    LymowSensorDesc(
        key="sim_iccid",
        name="SIM ICCID",
        icon="mdi:sim",
        value_source=_net("simIccid"),
        entity_registry_enabled_default=False,
    ),

    # ── Firmware ─────────────────────────────────────────────────────────
    LymowSensorDesc(
        key="fw_version",
        name="Firmware",
        icon="mdi:chip",
        value_source=F_FW_VERSION,
        entity_registry_enabled_default=False,
    ),
    LymowSensorDesc(
        key="mcu_version",
        name="MCU Version",
        icon="mdi:memory",
        value_source=F_MCU_VERSION,
        entity_registry_enabled_default=False,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coord: LymowCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(
        [LymowSensor(coord, desc) for desc in SENSORS],
        update_before_add=False,
    )


class LymowSensor(LymowEntity, SensorEntity):
    """Generic Lymow sensor."""

    entity_description: LymowSensorDesc

    def __init__(self, coordinator: LymowCoordinator, desc: LymowSensorDesc) -> None:
        super().__init__(coordinator, desc.key)
        self.entity_description = desc

    @property
    def native_value(self) -> Any:
        d = self.coordinator.data or {}
        src = self.entity_description.value_source

        raw = src(d) if callable(src) else d.get(src)
        if raw is None:
            return None

        if fn := self.entity_description.transform:
            return fn(raw)
        return raw
