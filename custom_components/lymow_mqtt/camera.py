"""
Lymow camera platform.

Two camera entities:
  1. LymowMapCamera  — SVG map rendered from zone/obstacle shadow data (always available)
  2. LymowRTSPCamera — live video stream from the robot's onboard camera (requires local network)

RTSP URL format (verified from APK source):
  rtsp://<ipAddress>:10022/h264ESVideoTest
  where ipAddress comes from the fwVersion.ipAddress shadow field.

HA cannot snapshot RTSP natively without go2rtc or ffmpeg.
LymowRTSPCamera exposes the URL via attributes so it can be used with go2rtc:

  go2rtc:
    streams:
      lymow:
        - "rtsp://{{ states('sensor.lymow_ip_address') }}:10022/h264ESVideoTest"
"""

from __future__ import annotations

import json
import logging
import math
from typing import Any

from homeassistant.components.camera import Camera, CameraEntityFeature
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import (
    DOMAIN,
    F_CLEAN_ZONE_IDS,
    F_CUT_ZONE_ID,
    F_GO_ZONE_IDS,
    F_MAP_AREA,
    F_OBS_MAP,
    RTK_STATUS_LABELS,
    RTSP_PATH,
    RTSP_PORT,
    WORK_STATUS_OFFLINE,
)
from .coordinator import LymowCoordinator
from .entity_base import LymowEntity

_LOGGER = logging.getLogger(__name__)


def _get_robot_ip(data: dict) -> str | None:
    """
    Extract the robot's local WiFi IP from the shadow state.
    Priority: fwVersion.ipAddress → netDetailInfo.wifiIp → top-level ipAddress.
    """
    return (
        (data.get("fwVersion") or {}).get("ipAddress")
        or (data.get("netDetailInfo") or {}).get("wifiIp")
        or data.get("ipAddress")
    )


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coord: LymowCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(
        [LymowMapCamera(coord), LymowRTSPCamera(coord)],
        update_before_add=False,
    )


class LymowMapCamera(LymowEntity, Camera):
    """Camera that renders the Lymow lawn map as SVG."""

    _attr_name         = "Map"
    _attr_icon         = "mdi:map"
    _attr_content_type = "image/svg+xml"
    _attr_supported_features = CameraEntityFeature(0)

    def __init__(self, coordinator: LymowCoordinator) -> None:
        LymowEntity.__init__(self, coordinator, "map")
        Camera.__init__(self)
        self._backup_map: dict | None = None

    @property
    def available(self) -> bool:
        # Camera is always available (shows placeholder when offline)
        return self.coordinator.last_update_success

    async def async_camera_image(self, width: int | None = None, height: int | None = None) -> bytes | None:
        d = self.coordinator.data or {}
        work_status = d.get("workStatus", WORK_STATUS_OFFLINE)

        # Primary map source: obsMap from shadow
        obs_map = d.get(F_OBS_MAP)

        # Fetch backup map from API once if shadow has no map
        if not obs_map and not self._backup_map:
            self._backup_map = await self.coordinator.async_refresh_map()

        map_data = obs_map or self._backup_map

        svg = render_svg(
            map_data=map_data,
            robot_pos=d.get("position") or d.get("robotPosition") or d.get("locData"),
            active_zone_id=d.get(F_CUT_ZONE_ID),
            queued_zone_ids=set(d.get(F_GO_ZONE_IDS) or d.get(F_CLEAN_ZONE_IDS) or []),
            work_status=work_status,
            rtk_status=d.get("rtkStatus"),
            battery=d.get("battery"),
        )
        return svg.encode("utf-8")

    @property
    def extra_state_attributes(self) -> dict:
        d = self.coordinator.data or {}
        attrs: dict = {}
        if area := d.get(F_MAP_AREA):
            attrs["map_area_m2"] = area
        if obs := d.get(F_OBS_MAP):
            # Expose raw map as JSON for external integrations / dashboards
            attrs["obs_map_json"] = json.dumps(obs) if not isinstance(obs, str) else obs
        if pos := d.get("position") or d.get("robotPosition"):
            attrs["robot_position"] = pos
        return attrs


# ─────────────────────────────────────────────────────────────────────────────
# SVG renderer
# ─────────────────────────────────────────────────────────────────────────────

_CANVAS   = 500
_PADDING  = 24

_COLOR_BG         = "#111827"
_COLOR_LAWN       = "#1a3a1a"
_COLOR_BOUNDARY   = "#4ade80"
_COLOR_ZONE_IDLE  = "#22c55e"
_COLOR_ZONE_ACTIVE = "#86efac"
_COLOR_OBSTACLE   = "#ef4444"
_COLOR_ROBOT      = "#f97316"
_COLOR_PATH       = "#fde68a"
_COLOR_STATUS_BG  = {
    2: "#16a34a",   # mowing
    4: "#2563eb",   # docking
    5: "#d97706",   # charging
    7: "#dc2626",   # error
    3: "#9333ea",   # pause
    12: "#0891b2",  # charging full
    13: "#dc2626",  # emergency stop
    1: "#6b7280",   # waiting
    0: "#6b7280",   # idle
    -1: "#374151",  # offline
}
_STATUS_LABELS = {
    -1: "OFFLINE", 0: "IDLE", 1: "WAITING", 2: "MOWING",
    3: "PAUSED", 4: "DOCKING", 5: "CHARGING", 6: "REMOTE",
    7: "ERROR", 8: "RESUMING", 9: "MAPPING", 10: "DOCKING",
    11: "UPDATING", 12: "CHARGED", 13: "E-STOP", 14: "ESCAPING",
}


def render_svg(
    map_data: dict | None,
    robot_pos: Any,
    active_zone_id: str | None,
    queued_zone_ids: set[str],
    work_status: int,
    rtk_status: int | None = None,
    battery: int | None = None,
) -> str:
    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" '
        f'viewBox="0 0 {_CANVAS} {_CANVAS}" width="{_CANVAS}" height="{_CANVAS}">',
        f'<rect width="{_CANVAS}" height="{_CANVAS}" fill="{_COLOR_BG}"/>',
    ]

    if not map_data:
        parts += _placeholder()
        parts.append("</svg>")
        return "\n".join(parts)

    # ── Collect all geometry points ──────────────────────────────────────────
    all_pts: list[tuple[float, float]] = []

    boundary = _get_boundary(map_data)
    zones    = _get_zones(map_data)
    obstacles = _get_obstacles(map_data)
    robot_path = _get_path(map_data)

    for pts in [boundary, *[_zone_pts(z) for z in zones], *obstacles]:
        all_pts.extend(pts)
    if robot_pos:
        pt = _to_point(robot_pos)
        if pt:
            all_pts.append(pt)

    if not all_pts:
        parts += _placeholder()
        parts.append("</svg>")
        return "\n".join(parts)

    # ── Transform: fit all points into canvas ────────────────────────────────
    min_x = min(p[0] for p in all_pts)
    max_x = max(p[0] for p in all_pts)
    min_y = min(p[1] for p in all_pts)
    max_y = max(p[1] for p in all_pts)
    w = max_x - min_x or 1
    h = max_y - min_y or 1
    scale = (_CANVAS - _PADDING * 2) / max(w, h)

    def tx(x: float) -> str:
        return f"{(x - min_x) * scale + _PADDING:.1f}"

    def ty(y: float) -> str:
        return f"{(y - min_y) * scale + _PADDING:.1f}"

    def poly(pts: list[tuple[float, float]]) -> str:
        return " ".join(f"{tx(x)},{ty(y)}" for x, y in pts)

    # ── Draw lawn fill (boundary) ─────────────────────────────────────────────
    if len(boundary) >= 3:
        parts.append(
            f'<polygon points="{poly(boundary)}" '
            f'fill="{_COLOR_LAWN}" stroke="{_COLOR_BOUNDARY}" '
            f'stroke-width="2"/>'
        )

    # ── Draw robot path ───────────────────────────────────────────────────────
    path_pts = robot_path[-300:]
    if len(path_pts) >= 2:
        d_attr = " ".join(
            f"{'M' if i == 0 else 'L'}{tx(p[0])},{ty(p[1])}"
            for i, p in enumerate(path_pts)
        )
        parts.append(
            f'<path d="{d_attr}" fill="none" stroke="{_COLOR_PATH}" '
            f'stroke-width="1.5" stroke-opacity="0.5" stroke-dasharray="3 2"/>'
        )

    # ── Draw obstacles ────────────────────────────────────────────────────────
    for obs_pts in obstacles:
        if len(obs_pts) >= 2:
            parts.append(
                f'<polygon points="{poly(obs_pts)}" '
                f'fill="{_COLOR_OBSTACLE}" fill-opacity="0.35" '
                f'stroke="{_COLOR_OBSTACLE}" stroke-width="1"/>'
            )

    # ── Draw zones ────────────────────────────────────────────────────────────
    zone_alphas = ["88", "aa", "cc", "bb", "99", "77"]
    for i, zone in enumerate(zones):
        z_pts = _zone_pts(zone)
        if len(z_pts) < 3:
            continue
        z_id   = _zone_id(zone)
        is_active  = z_id == active_zone_id
        is_queued  = z_id in queued_zone_ids
        alpha  = "dd" if is_active else ("aa" if is_queued else zone_alphas[i % len(zone_alphas)])
        color  = _COLOR_ZONE_ACTIVE if (is_active or is_queued) else _COLOR_ZONE_IDLE
        stroke_w = "2.5" if is_active else "1.5"

        cx = sum(p[0] for p in z_pts) / len(z_pts)
        cy = sum(p[1] for p in z_pts) / len(z_pts)
        z_name = zone.get("name") or zone.get("zoneName") or z_id or str(i + 1)

        parts.append(
            f'<polygon points="{poly(z_pts)}" '
            f'fill="{color}{alpha}" stroke="{color}" stroke-width="{stroke_w}"/>'
        )
        parts.append(
            f'<text x="{tx(cx)}" y="{ty(cy)}" text-anchor="middle" '
            f'dominant-baseline="middle" font-size="11" '
            f'font-family="sans-serif" fill="white" font-weight="bold">'
            f'{z_name}</text>'
        )

    # ── Draw robot ────────────────────────────────────────────────────────────
    if robot_pos:
        rpt = _to_point(robot_pos)
        if rpt:
            rx, ry = float(tx(rpt[0])), float(ty(rpt[1]))
            heading = 0
            if isinstance(robot_pos, dict):
                heading = robot_pos.get("heading") or robot_pos.get("angle") or 0

            # Glow
            parts.append(
                f'<circle cx="{rx:.1f}" cy="{ry:.1f}" r="14" '
                f'fill="{_COLOR_ROBOT}" fill-opacity="0.25"/>'
            )
            # Body
            parts.append(
                f'<circle cx="{rx:.1f}" cy="{ry:.1f}" r="9" '
                f'fill="{_COLOR_ROBOT}" stroke="white" stroke-width="2"/>'
            )
            # Direction arrow
            ang = math.radians(heading)
            ax = rx + 13 * math.sin(ang)
            ay = ry - 13 * math.cos(ang)
            parts.append(
                f'<line x1="{rx:.1f}" y1="{ry:.1f}" '
                f'x2="{ax:.1f}" y2="{ay:.1f}" '
                f'stroke="white" stroke-width="2.5" '
                f'stroke-linecap="round"/>'
            )

    # ── HUD: status badge ─────────────────────────────────────────────────────
    status_color = _COLOR_STATUS_BG.get(work_status, "#374151")
    status_label = _STATUS_LABELS.get(work_status, "?")
    parts += [
        f'<rect x="6" y="6" width="90" height="22" rx="5" fill="{status_color}" fill-opacity="0.92"/>',
        f'<text x="51" y="20" text-anchor="middle" dominant-baseline="middle" '
        f'font-size="11" font-family="sans-serif" fill="white" '
        f'font-weight="bold">{status_label}</text>',
    ]

    # ── HUD: battery ─────────────────────────────────────────────────────────
    if battery is not None:
        bat_color = "#4ade80" if battery > 30 else ("#facc15" if battery > 15 else "#f87171")
        parts += [
            f'<rect x="104" y="6" width="52" height="22" rx="5" '
            f'fill="#1f2937" fill-opacity="0.9"/>',
            f'<text x="130" y="20" text-anchor="middle" dominant-baseline="middle" '
            f'font-size="11" font-family="sans-serif" fill="{bat_color}" '
            f'font-weight="bold">🔋{battery}%</text>',
        ]

    # ── HUD: RTK badge ────────────────────────────────────────────────────────
    if rtk_status is not None:
        rtk_labels = {0: "RTK ✗", 1: "RTK ~", 2: "RTK ✓"}
        rtk_colors = {0: "#dc2626", 1: "#d97706", 2: "#16a34a"}
        parts += [
            f'<rect x="164" y="6" width="52" height="22" rx="5" '
            f'fill="{rtk_colors.get(rtk_status, "#374151")}" fill-opacity="0.9"/>',
            f'<text x="190" y="20" text-anchor="middle" dominant-baseline="middle" '
            f'font-size="11" font-family="sans-serif" fill="white" '
            f'font-weight="bold">{rtk_labels.get(rtk_status, "RTK ?")}</text>',
        ]

    parts.append("</svg>")
    return "\n".join(parts)


def _placeholder() -> list[str]:
    return [
        f'<text x="{_CANVAS // 2}" y="{_CANVAS // 2 - 10}" text-anchor="middle" '
        f'font-size="15" font-family="sans-serif" fill="#4b5563">Map not available</text>',
        f'<text x="{_CANVAS // 2}" y="{_CANVAS // 2 + 14}" text-anchor="middle" '
        f'font-size="11" font-family="sans-serif" fill="#374151">'
        f'Waiting for shadow data...</text>',
    ]


# ── Geometry extraction helpers ───────────────────────────────────────────────

def _get_boundary(m: dict) -> list[tuple[float, float]]:
    for key in ("boundary", "workArea", "outline", "perimeter", "area"):
        if v := m.get(key):
            return _extract_pts(v)
    return []

def _get_zones(m: dict) -> list[dict]:
    for key in ("zones", "workZones", "cutZones", "zoneList"):
        if v := m.get(key):
            return v if isinstance(v, list) else []
    return []

def _zone_pts(z: dict) -> list[tuple[float, float]]:
    for key in ("points", "vertices", "coordinates", "polygon", "outline"):
        if v := z.get(key):
            return _extract_pts(v)
    return _extract_pts(z)

def _zone_id(z: dict) -> str | None:
    return z.get("id") or z.get("zoneId") or z.get("hashId") or z.get("zoneHashId")

def _get_obstacles(m: dict) -> list[list[tuple[float, float]]]:
    obs = m.get("obstacles") or m.get("noGoZones") or m.get("nogoZones") or []
    if not isinstance(obs, list):
        return []
    result = []
    for o in obs:
        pts = _extract_pts(o.get("points") or o.get("vertices") or o) if isinstance(o, dict) else _extract_pts(o)
        if pts:
            result.append(pts)
    return result

def _get_path(m: dict) -> list[tuple[float, float]]:
    for key in ("path", "track", "trajectory"):
        if v := m.get(key):
            return _extract_pts(v)
    return []

def _extract_pts(obj: Any) -> list[tuple[float, float]]:
    if isinstance(obj, list):
        result = []
        for item in obj:
            pt = _to_point(item)
            if pt:
                result.append(pt)
        return result
    if isinstance(obj, dict):
        for key in ("points", "vertices", "coordinates", "coords"):
            if v := obj.get(key):
                return _extract_pts(v)
    return []

def _to_point(obj: Any) -> tuple[float, float] | None:
    if isinstance(obj, (list, tuple)) and len(obj) >= 2:
        try:
            return (float(obj[0]), float(obj[1]))
        except (TypeError, ValueError):
            return None
    if isinstance(obj, dict):
        for kx, ky in [("x", "y"), ("lon", "lat"), ("longitude", "latitude"),
                        ("lng", "lat"), ("e", "n")]:
            if kx in obj and ky in obj:
                try:
                    return (float(obj[kx]), float(obj[ky]))
                except (TypeError, ValueError):
                    pass
    return None


class LymowRTSPCamera(LymowEntity, Camera):
    """
    Exposes the robot's live video camera stream.

    The robot runs an RTSP server on port 10022. The URL is built dynamically
    from the robot's current local IP address (fwVersion.ipAddress in the shadow).

    HA cannot pull RTSP frames natively — this entity exposes the URL via the
    'rtsp_url' attribute so external tools (go2rtc, VLC, ffmpeg) can consume it.

    Recommended go2rtc config:
      go2rtc:
        streams:
          lymow:
            - "rtsp://{{ states('sensor.lymow_ip_address') }}:10022/h264ESVideoTest"
    """

    _attr_name         = "Live Camera"
    _attr_icon         = "mdi:cctv"
    _attr_content_type = "image/jpeg"
    _attr_supported_features = CameraEntityFeature(0)

    def __init__(self, coordinator: LymowCoordinator) -> None:
        LymowEntity.__init__(self, coordinator, "rtsp_camera")
        Camera.__init__(self)

    @property
    def available(self) -> bool:
        # Only mark available when we know the robot's IP.
        return bool(_get_robot_ip(self.coordinator.data or {}))

    @property
    def extra_state_attributes(self) -> dict:
        ip = _get_robot_ip(self.coordinator.data or {})
        if not ip:
            return {}
        return {
            "rtsp_url":  f"rtsp://{ip}:{RTSP_PORT}/{RTSP_PATH}",
            "robot_ip":  ip,
            "rtsp_port": RTSP_PORT,
        }

    async def async_camera_image(
        self, width: int | None = None, height: int | None = None
    ) -> bytes | None:
        """
        HA cannot snapshot RTSP without go2rtc/ffmpeg configured externally.
        Returns None — use the rtsp_url attribute to connect an external player.
        """
        return None
