"""Lymow RTSP camera entity.

Streams from rtsp://<ip>:10022/h264ESVideoTest (arch.md §4d).
The IP is derived from MQTT deviceInfo broadcasts (post-completion bursts)
or REST /get-device-info polls. stream_source updates whenever the IP
changes.
"""
from __future__ import annotations

import logging

from homeassistant.components.camera import Camera, CameraEntityFeature
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN, RTSP_PATH, RTSP_PORT
from .coordinator import LymowCoordinator
from .entity_base import LymowEntity

_LOGGER = logging.getLogger(__name__)


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


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coord: LymowCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([LymowRtspCamera(coord)])
