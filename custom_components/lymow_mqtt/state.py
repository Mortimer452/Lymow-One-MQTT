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

    For "sticky" submessages (cleanInfo, deviceInfo, robotConfig, etc.) we
    use protobuf MergeFrom so a broadcast that updates one field doesn't
    clobber others that weren't sent. Example: cleanInfo.mapArea is only
    populated occasionally; a broadcast carrying just cleanArea would
    otherwise wipe our cached mapArea. MergeFrom preserves it.

    For "snapshot" submessages (robotInfo, pose, localizationInfo,
    wifiConfigRes) we replace — the firmware sends a full snapshot
    every broadcast.

    Repeated fields (errorCodes, warningCodes) are replaced when the
    field is populated, left untouched otherwise. The coordinator
    explicitly clears errorCodes on robotStatus 7->non-7 transition.
    """
    populated_names = {fd.name for fd, _ in msg.ListFields()}

    # Snapshot submessages — full replace each broadcast
    if "robotInfo" in populated_names:
        state_dict["robotInfo"] = msg.robotInfo
    if "pose" in populated_names:
        state_dict["pose"] = msg.pose
    if "localizationInfo" in populated_names:
        state_dict["localizationInfo"] = msg.localizationInfo
    if "wifiConfigRes" in populated_names:
        state_dict["wifiConfigRes"] = msg.wifiConfigRes

    # Sticky submessages — merge so unset fields preserve previous values
    def _merge_sticky(name: str, source) -> None:
        existing = state_dict.get(name)
        if existing is None:
            # First time we see this submessage — clone it so we don't
            # alias the inbound message (which the caller may discard).
            cloned = source.__class__()
            cloned.CopyFrom(source)
            state_dict[name] = cloned
        else:
            existing.MergeFrom(source)

    if "cleanInfo" in populated_names:
        _merge_sticky("cleanInfo", msg.cleanInfo)
    if "deviceInfo" in populated_names:
        _merge_sticky("deviceInfo", msg.deviceInfo)
    if "btMap" in populated_names:
        # btMap is query-driven; the catalog blob is a one-shot reply.
        # Replace rather than merge — repeated fields would accumulate.
        state_dict["btMap"] = msg.btMap
    if "cleanReport" in populated_names:
        state_dict["cleanReport"] = msg.cleanReport
    if "schedule" in populated_names:
        # Replace — the schedules list comes back whole on QUERY_SCHEDULES,
        # we don't want to accumulate stale tasks.
        state_dict["schedule"] = msg.schedule
    if "robotConfig" in populated_names:
        _merge_sticky("robotConfig", msg.robotConfig)
    if "debugSetting" in populated_names:
        _merge_sticky("debugSetting", msg.debugSetting)
    if "netDetailInfo" in populated_names:
        _merge_sticky("netDetailInfo", msg.netDetailInfo)
    if "chargingStationLoc" in populated_names:
        state_dict["chargingStationLoc"] = msg.chargingStationLoc

    # Repeated fields: replace when present, leave alone otherwise.
    # NOTE: protobuf can't distinguish "field absent" from "deliberately empty list",
    # so an explicit empty errorCodes list looks the same as "no errorCodes field" here.
    # The coordinator (Phase 5) is responsible for clearing errorCodes when robotStatus
    # transitions from 7 (Error) to non-7 — see arch.md §7c "error_cleared" recipe.
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

    Per arch.md §6c, cut height/speed/move speed cascade from per-task
    schedule overrides → per-zone PbZoneConfig → global PbRunTimeConfig.

    Note on PbRunTimeConfig vs PbRobotConfig: the global "runtime config"
    that carries cutHeight, cutSpeed, AND moveSpeed is `PbRunTimeConfig`,
    nested at `PbMap.runTimeConfig` (delivered inside QUERY_MAP responses).
    `PbRobotConfig` is a different type (rcCutHeight, rcCutSpeed, no
    moveSpeed) used elsewhere. The zone catalog parser extracts PbRunTimeConfig
    onto `ZoneCatalog.runtime_config`; that is what we read here.

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

    # Tier 3: global PbRunTimeConfig (carried on ZoneCatalog from QUERY_MAP)
    catalog = state_dict.get("zone_catalog")
    rtc = getattr(catalog, "runtime_config", None) if catalog is not None else None
    if rtc is not None:
        if result["cut_speed"] is None and _has_field(rtc, "cutSpeed"):
            result["cut_speed"] = rtc.cutSpeed
        if result["cut_height"] is None and _has_field(rtc, "cutHeight"):
            result["cut_height"] = rtc.cutHeight
        if result["move_speed"] is None and _has_field(rtc, "moveSpeed"):
            result["move_speed"] = rtc.moveSpeed

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
    # Coerce naive datetimes to UTC — defensive, matches how callers should pass UTC-aware values.
    if last_mqtt_at.tzinfo is None:
        last_mqtt_at = last_mqtt_at.replace(tzinfo=UTC)
    if now is None:
        now = datetime.now(UTC)
    if (now - last_mqtt_at) < mqtt_recency_window:
        return True  # MQTT activity overrides stale REST
    return False
