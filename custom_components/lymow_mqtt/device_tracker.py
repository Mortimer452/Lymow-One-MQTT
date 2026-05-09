"""Lymow device_tracker entities — RTK base station + live mower position.

The Lymow firmware never broadcasts global GPS coordinates directly
(`PbOutput.robotLlaCoords` is schema-only — no app or firmware emits it).
Instead, every position is in a local ENU frame anchored at the RTK base
station's surveyed GPS (`PbMap.enuBasePoint`, returned with QUERY_MAP
responses). See arch.md §8c.

This file derives both the RTK base GPS and the mower's live GPS:
- **RTK base**: read directly from `state["enu_base_point"]` once captured.
  Sticky for the lifetime of the integration; the coordinator only updates
  it when a fresh PbMap actually carries the field, so QUERY_PATH responses
  (which share the btMap branch) can't wipe it.
- **Mower**: `enu_base_point + pose` per pose broadcast (every ~1-2s during
  a mow, slower at rest). Math in `state.enu_to_lla`.

Both entities use `RestoreEntity` so HA restarts don't blank the position —
on startup we report the previously-cached coords until live data lands.
"""
from __future__ import annotations

from homeassistant.components.device_tracker import SourceType, TrackerEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity

from .const import DOMAIN
from .coordinator import LymowCoordinator
from .entity_base import LymowEntity
from .state import enu_to_lla


class _LymowTrackerBase(LymowEntity, TrackerEntity, RestoreEntity):
    """Base for Lymow trackers — restores last lat/lon on HA startup."""

    _attr_source_type = SourceType.GPS

    def __init__(self, coordinator: LymowCoordinator, key: str) -> None:
        super().__init__(coordinator, key)
        self._restored_lat: float | None = None
        self._restored_lon: float | None = None

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        last = await self.async_get_last_state()
        if last and last.attributes:
            try:
                lat = last.attributes.get("latitude")
                lon = last.attributes.get("longitude")
                if lat is not None:
                    self._restored_lat = float(lat)
                if lon is not None:
                    self._restored_lon = float(lon)
            except (TypeError, ValueError):
                pass


class LymowRtkBaseTracker(_LymowTrackerBase):
    """RTK base station GPS — surveyed once, sticky forever."""

    _attr_translation_key = "rtk_base"
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_icon = "mdi:satellite-uplink"

    @property
    def latitude(self) -> float | None:
        ebp = self.coordinator.state_dict.get("enu_base_point")
        if ebp is not None:
            return ebp.latitude
        return self._restored_lat

    @property
    def longitude(self) -> float | None:
        ebp = self.coordinator.state_dict.get("enu_base_point")
        if ebp is not None:
            return ebp.longitude
        return self._restored_lon


class LymowMowerTracker(_LymowTrackerBase):
    """Live mower GPS derived from enu_base_point + pose."""

    _attr_translation_key = "mower_position"
    _attr_icon = "mdi:robot-mower"

    def _live_coords(self) -> tuple[float, float] | None:
        ebp = self.coordinator.state_dict.get("enu_base_point")
        pose = self.coordinator.state_dict.get("pose")
        return enu_to_lla(ebp, pose)

    @property
    def latitude(self) -> float | None:
        live = self._live_coords()
        if live is not None:
            return live[0]
        return self._restored_lat

    @property
    def longitude(self) -> float | None:
        live = self._live_coords()
        if live is not None:
            return live[1]
        return self._restored_lon


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coord: LymowCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(
        [
            LymowRtkBaseTracker(coord, "rtk_base"),
            LymowMowerTracker(coord, "mower_position"),
        ]
    )
