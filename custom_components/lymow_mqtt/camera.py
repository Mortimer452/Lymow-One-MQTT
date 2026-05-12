"""Lymow camera entities.

Two entities live here:

- `LymowRtspCamera` — RTSP stream from the robot's onboard camera at
  rtsp://<ip>:10022/h264ESVideoTest (arch.md §4d). The IP is derived
  from MQTT deviceInfo broadcasts or REST /get-device-info polls.
  stream_source updates whenever the IP changes.

- `LymowMapCamera` — server-rendered top-down lawn map (PNG snapshot,
  not a stream). Combines:
  * Zone outlines with task / current-zone status colouring (orange
    for the zone the mower is physically inside, green for zones in
    the current task, plain for everything else).
  * A heat overlay coloured by the EWMA-smoothed
    `horizontal_accuracy` accumulated per cell in
    `coordinator.signal_grid`. Cells with no samples are simply not
    drawn.
  * A bottom-left legend explaining the heat-color ramp.
  Available whenever the zone catalog has been populated (one shot at
  startup via QUERY_MAP). The signal grid may be empty (no mowing
  yet) — in that case zone outlines + markers render without heat.
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
    """Server-rendered combined map: zone status + signal-quality heat.

    Pulls happen at whatever cadence the Lovelace card chose (~10s by
    default for Picture Entity cards). State changes more slowly than
    that in passive mode, so most pulls produce an identical PNG.
    """

    _attr_name = "Map"

    def __init__(self, coordinator: LymowCoordinator) -> None:
        LymowEntity.__init__(self, coordinator, "map")
        Camera.__init__(self)

    @property
    def available(self) -> bool:
        # Standard online check first, then gate on having at least one
        # zone in the catalog. Without zones there's nothing meaningful
        # to anchor the heat layer / outlines to.
        if not super().available:
            return False
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
            _render_map_kwargs,
            catalog,
            pose,
            dock,
            current_zone,
            task_active,
            self.coordinator.signal_grid,
            _sg.CELL_M,
            width or _MAP_DEFAULT_WIDTH,
            height or _MAP_DEFAULT_HEIGHT,
        )


def _render_map_kwargs(
    catalog,
    pose,
    dock,
    current_zone,
    task_active,
    signal_grid,
    cell_m,
    width,
    height,
) -> bytes | None:
    """Adapter so the executor-job call site can pass positional args.

    `map_render.render_map` takes ``signal_grid`` / ``cell_m`` / ``width``
    / ``height`` as keyword-only, which `async_add_executor_job` can't
    target directly (it only forwards positional args).
    """
    return map_render.render_map(
        catalog,
        pose,
        dock,
        current_zone,
        task_active,
        signal_grid=signal_grid,
        cell_m=cell_m,
        width=width,
        height=height,
    )


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coord: LymowCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([
        LymowRtspCamera(coord),
        LymowMapCamera(coord),
    ])
