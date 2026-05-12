"""Lymow camera entities.

Two entities live here:

- `LymowRtspCamera` — RTSP stream from the robot's onboard camera at
  rtsp://<ip>:10022/h264ESVideoTest (arch.md §4d). The IP is derived
  from MQTT deviceInfo broadcasts (post-completion bursts) or REST
  /get-device-info polls. stream_source updates whenever the IP changes.

- `LymowMapCamera` — server-rendered top-down lawn map (PNG) showing
  zones, channels, dock, and live mower position with task / current-zone
  highlighting per arch.md §8b. No live video — just a freshly-rendered
  image each time HA pulls. Available whenever the zone catalog has been
  populated (one shot at startup via QUERY_MAP).
"""
from __future__ import annotations

import logging

from homeassistant.components.camera import Camera, CameraEntityFeature
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import map_render, signal_grid as _sg, state
from .const import ACTIVE_TASK_STATUSES, DOMAIN, RTSP_PATH, RTSP_PORT
from .coordinator import LymowCoordinator
from .entity_base import LymowEntity

_LOGGER = logging.getLogger(__name__)

# Default render dimensions when HA doesn't supply width/height.
_MAP_DEFAULT_WIDTH = 1024
_MAP_DEFAULT_HEIGHT = 768


def _resolve_ip(coordinator: LymowCoordinator) -> str | None:
    s = coordinator.state_dict
    di = s.get("deviceInfo")
    if di and di.HasField("ipAddress") and di.ipAddress:
        return di.ipAddress
    return s.get("rest_ip_address")


class LymowRtspCamera(LymowEntity, Camera):
    """RTSP video stream from the robot's onboard camera."""

    _attr_name = "Camera"
    _attr_supported_features = CameraEntityFeature.STREAM

    def __init__(self, coordinator: LymowCoordinator) -> None:
        LymowEntity.__init__(self, coordinator, "camera")
        Camera.__init__(self)

    async def stream_source(self) -> str | None:
        ip = _resolve_ip(self.coordinator)
        if not ip:
            return None
        return f"rtsp://{ip}:{RTSP_PORT}/{RTSP_PATH}"

    async def async_camera_image(
        self, width: int | None = None, height: int | None = None
    ) -> bytes | None:
        # HA's frontend uses stream_source via go2rtc / ffmpeg;
        # snapshot is unavailable without that pipeline.
        return None


class LymowMapCamera(LymowEntity, Camera):
    """Server-rendered lawn map (PNG snapshot, no stream).

    Pulls happen at whatever cadence the Lovelace card chose (~10s by
    default for Picture Entity cards). The underlying mower state changes
    much more slowly than that in this integration's passive design, so
    most pulls produce an identical PNG — which is fine.
    """

    _attr_name = "Map"

    def __init__(self, coordinator: LymowCoordinator) -> None:
        LymowEntity.__init__(self, coordinator, "map")
        Camera.__init__(self)

    @property
    def available(self) -> bool:
        # Standard online check from the base class first.
        if not super().available:
            return False
        # Then gate on having at least one zone in the catalog. Without
        # zones there's nothing meaningful to render.
        s = self.coordinator.state_dict
        catalog = s.get("zone_catalog")
        return catalog is not None and len(getattr(catalog, "zones", [])) > 0

    async def async_camera_image(
        self, width: int | None = None, height: int | None = None
    ) -> bytes | None:
        s = self.coordinator.state_dict
        catalog = s.get("zone_catalog")
        if catalog is None or not getattr(catalog, "zones", None):
            return None

        pose = s.get("pose")
        dock = s.get("chargingStationLoc")
        current_zone = state.derive_current_zone(s)
        ri = s.get("robotInfo")
        work_status = getattr(ri, "workStatus", None) if ri is not None else None
        task_active = work_status in ACTIVE_TASK_STATUSES

        # Pillow is sync and polygon rasterization can take a few ms on
        # busy maps — keep it out of the event loop.
        return await self.hass.async_add_executor_job(
            map_render.render_map,
            catalog,
            pose,
            dock,
            current_zone,
            task_active,
            width or _MAP_DEFAULT_WIDTH,
            height or _MAP_DEFAULT_HEIGHT,
        )


class LymowSignalMapCamera(LymowEntity, Camera):
    """Heat-map view of signal quality across the property.

    Renders a top-down PNG with each spatial cell colored by the EWMA
    of `horizontal_accuracy` observed there. Built from the accumulator
    in `coordinator.signal_grid` — see `signal_grid.py` for the data
    model and `map_render.render_signal_map` for the rendering.

    Currently only horizontal_accuracy is visualized; the coordinator
    accumulates four metrics (RTK quality, horizontal accuracy, WiFi,
    LTE) and additional heat layers can be added later without re-mowing.
    """

    _attr_name = "Signal map"

    def __init__(self, coordinator: LymowCoordinator) -> None:
        LymowEntity.__init__(self, coordinator, "signal_map")
        Camera.__init__(self)

    @property
    def available(self) -> bool:
        if not super().available:
            return False
        # Same gate as the map camera — without a zone catalog we have no
        # frame of reference for the heat cells. The grid itself may still
        # be empty (no mowing yet), in which case the render is a zone
        # outline with no heat overlay — informative enough for a v1.
        s = self.coordinator.state_dict
        catalog = s.get("zone_catalog")
        return catalog is not None and len(getattr(catalog, "zones", [])) > 0

    async def async_camera_image(
        self, width: int | None = None, height: int | None = None
    ) -> bytes | None:
        s = self.coordinator.state_dict
        catalog = s.get("zone_catalog")
        if catalog is None or not getattr(catalog, "zones", None):
            return None
        return await self.hass.async_add_executor_job(
            map_render.render_signal_map,
            catalog,
            s.get("pose"),
            s.get("chargingStationLoc"),
            self.coordinator.signal_grid,
            _sg.CELL_M,
            width or _MAP_DEFAULT_WIDTH,
            height or _MAP_DEFAULT_HEIGHT,
        )


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coord: LymowCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([
        LymowRtspCamera(coord),
        LymowMapCamera(coord),
        LymowSignalMapCamera(coord),
    ])
