"""State management — merging MQTT broadcasts, deriving derived sensors,
geometry helpers, active-config inheritance.

All pure functions. The coordinator owns the state dict; this module
just provides the merge and derivation logic.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from .const import ACTIVE_TASK_WORK_STATUSES


def point_in_polygon(x: float, y: float, polygon: list[tuple[float, float]]) -> bool:
    """Ray-casting point-in-polygon test.

    Per arch.md §12, the official app uses this for "currently in zone"
    derivation (decompiled.js:490631). Returns True if the point is inside
    the polygon (boundary points may be inside or outside; behavior is
    not strictly defined by ray casting and we don't care for our use).

    Polygon is a list of (x, y) tuples; first and last need not be
    identical (we treat as a closed polygon).
    """
    n = len(polygon)
    if n < 3:
        return False
    inside = False
    j = n - 1
    for i in range(n):
        xi, yi = polygon[i]
        xj, yj = polygon[j]
        # Check whether the ray from (x, y) going right crosses edge (j, i)
        if (yi > y) != (yj > y):
            x_intersect = (xj - xi) * (y - yi) / (yj - yi) + xi
            if x < x_intersect:
                inside = not inside
        j = i
    return inside


def merge_pboutput(state_dict: dict[str, Any], msg) -> None:
    """Merge populated submessages from a PbOutput into the state dict.

    Only fields present in the message are touched. Submessages are
    fully replaced (the firmware sends complete substructures, not
    deltas). Repeated fields (errorCodes, warningCodes) are replaced
    when the field is populated, left untouched otherwise — the
    coordinator clears them based on robotStatus transitions, since
    protobuf can't distinguish "absent field" from "empty list" reliably.
    """
    # Iterate fields actually populated in the message
    populated_names = {fd.name for fd, _ in msg.ListFields()}

    if "robotInfo" in populated_names:
        state_dict["robotInfo"] = msg.robotInfo
    if "cleanInfo" in populated_names:
        state_dict["cleanInfo"] = msg.cleanInfo
    if "pose" in populated_names:
        state_dict["pose"] = msg.pose
    if "localizationInfo" in populated_names:
        state_dict["localizationInfo"] = msg.localizationInfo
    if "deviceInfo" in populated_names:
        state_dict["deviceInfo"] = msg.deviceInfo
    if "btMap" in populated_names:
        state_dict["btMap"] = msg.btMap
    if "cleanReport" in populated_names:
        state_dict["cleanReport"] = msg.cleanReport
    if "schedule" in populated_names:
        state_dict["schedule"] = msg.schedule
    if "robotConfig" in populated_names:
        state_dict["robotConfig"] = msg.robotConfig
    if "debugSetting" in populated_names:
        state_dict["debugSetting"] = msg.debugSetting
    if "wifiConfigRes" in populated_names:
        state_dict["wifiConfigRes"] = msg.wifiConfigRes
    if "netDetailInfo" in populated_names:
        state_dict["netDetailInfo"] = msg.netDetailInfo
    if "chargingStationLoc" in populated_names:
        state_dict["chargingStationLoc"] = msg.chargingStationLoc

    # Repeated fields: replace when present, leave alone otherwise
    if "errorCodes" in populated_names:
        state_dict["errorCodes"] = list(msg.errorCodes)
    if "warningCodes" in populated_names:
        state_dict["warningCodes"] = list(msg.warningCodes)


def derive_current_zone(state_dict: dict[str, Any]) -> str | None:
    """Derive 'which zone is the mower physically in right now'.

    Returns:
        Zone name if the mower is inside a zone polygon
        Channel descriptor (e.g. "Pool → Front yard" or "→ dock") if in a corridor
        None if idle, or in transit, or zone catalog unavailable

    Per arch.md §12, this is what the official app does. Pose is in local
    map frame matching the polygon coordinates (both are mower-local meters
    relative to the dock origin).
    """
    pose = state_dict.get("pose")
    catalog = state_dict.get("zone_catalog")  # populated by coordinator after parse_zone_catalog
    robot_info = state_dict.get("robotInfo")
    if not pose or not catalog or not robot_info:
        return None

    work_status = getattr(robot_info, "workStatus", 0)
    if work_status not in ACTIVE_TASK_WORK_STATUSES:
        return None

    # Try go-zones first, ordered by mowOrder>0 (active task) then others
    zones = sorted(catalog.zones, key=lambda z: (z.mow_order == 0, z.mow_order))
    for zone in zones:
        if zone.polygon_points and point_in_polygon(pose.x, pose.y, zone.polygon_points):
            return zone.name

    # Then channels — handle dock approach specially
    for ch in catalog.channels:
        if ch.polygon_points and point_in_polygon(pose.x, pose.y, ch.polygon_points):
            if ch.is_docking_channel:
                return "→ dock"
            zone1_name = catalog.zones_by_hashid.get(ch.zone1)
            zone2_name = catalog.zones_by_hashid.get(ch.zone2)
            n1 = zone1_name.name if zone1_name else ch.zone1
            n2 = zone2_name.name if zone2_name else ch.zone2
            return f"{n1} → {n2}"

    return None  # mower is somewhere between defined polygons (rare)


def _zone_at_pose(state_dict: dict[str, Any]):
    """Return the ZoneInfo the mower is currently inside, or None."""
    pose = state_dict.get("pose")
    catalog = state_dict.get("zone_catalog")
    if not pose or not catalog:
        return None
    for zone in catalog.zones:
        if zone.polygon_points and point_in_polygon(pose.x, pose.y, zone.polygon_points):
            return zone
    return None


def _has_field(msg, name: str) -> bool:
    """HasField with graceful fallback for fields that don't track presence."""
    try:
        return msg.HasField(name)
    except (ValueError, AttributeError):
        # Proto3 implicit-presence scalar; treat truthy values as "set".
        return bool(getattr(msg, name, None))


def active_cut_config(state_dict: dict[str, Any]) -> dict[str, Any]:
    """Walk schedule_config -> zone_config -> runtime_config and return active cut params.

    Per arch.md §6c: cut height/speed values cascade from per-task schedule
    overrides down to the global runtime config. Move speed only ever comes
    from the per-zone or schedule-override tiers; PbRobotConfig has no
    global moveSpeed field.

    Returns dict with keys: cut_speed (int 3-6 or None), cut_height (int mm),
    move_speed (float m/s or None).
    """
    result: dict[str, Any] = {"cut_speed": None, "cut_height": None, "move_speed": None}

    current_zone = _zone_at_pose(state_dict)
    active_schedule = state_dict.get("active_schedule")  # PbSchedule of currently-running task, if any

    # Tier 1: schedule override per-zone
    if current_zone is not None and active_schedule is not None:
        for sc in getattr(active_schedule, "config", []):
            if sc.hashId == current_zone.hash_id:
                if _has_field(sc, "cutHeight"):
                    result["cut_height"] = sc.cutHeight
                if _has_field(sc, "moveSpeed"):
                    result["move_speed"] = sc.moveSpeed
                # PbScheduleConfig has no cutSpeed, fall through to zone for that
                break

    # Tier 2: per-zone PbZoneConfig from catalog
    if current_zone is not None:
        zc = getattr(current_zone, "zone_config", None)
        if zc is not None:
            if result["cut_speed"] is None and _has_field(zc, "cutSpeed"):
                result["cut_speed"] = zc.cutSpeed
            if result["cut_height"] is None and _has_field(zc, "cutHeight"):
                result["cut_height"] = zc.cutHeight
            if result["move_speed"] is None and _has_field(zc, "moveSpeed"):
                result["move_speed"] = zc.moveSpeed

    # Tier 3: global runtime config (PbRobotConfig — note rc-prefixed fields,
    # and there is no moveSpeed at this tier)
    rc = state_dict.get("runtime_config")
    if rc is not None:
        if result["cut_speed"] is None and _has_field(rc, "rcCutSpeed"):
            result["cut_speed"] = rc.rcCutSpeed
        if result["cut_height"] is None and _has_field(rc, "rcCutHeight"):
            result["cut_height"] = rc.rcCutHeight

    return result


def resolve_online(
    rest_online: bool,
    last_mqtt_at: datetime | None,
    now: datetime | None = None,
    mqtt_recency_window: timedelta = timedelta(minutes=5),
) -> bool:
    """Combine REST and MQTT signals into a single online truth.

    Per spec §7.3: REST is authoritative, BUT a fresh MQTT broadcast
    (< mqtt_recency_window old) overrides a stale REST -> offline reading.
    """
    if rest_online:
        return True
    # REST says offline. Check for fresh MQTT activity.
    if last_mqtt_at is None:
        return False
    if now is None:
        now = datetime.now(UTC)
    if (now - last_mqtt_at) < mqtt_recency_window:
        return True  # MQTT activity overrides stale REST
    return False
