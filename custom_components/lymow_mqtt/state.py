"""State management — merging MQTT broadcasts, deriving derived sensors,
geometry helpers, active-config inheritance.

All pure functions. The coordinator owns the state dict; this module
just provides the merge and derivation logic.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from math import cos, radians
from typing import Any

from .const import ACTIVE_TASK_STATUSES


def enu_to_lla(ebp, pose) -> tuple[float, float] | None:
    """Convert a local ENU-frame pose to GPS lat/lon using the RTK base anchor.

    Flat-earth approximation, accurate to a few cm at residential lawn scale
    (< ~1 km from the anchor). For higher precision, do a proper ENU→ECEF→LLA
    transform — not needed for this use case. See arch.md §8c.

    `ebp` is a PbRobotLLACoords (latitude/longitude/altitude floats). `pose`
    is a PbPose (x = meters east, y = meters north, both relative to the
    RTK base station). Returns None if either input is missing or malformed.
    """
    if ebp is None or pose is None:
        return None
    if not (hasattr(pose, "x") and hasattr(pose, "y")):
        return None
    if not (hasattr(ebp, "latitude") and hasattr(ebp, "longitude")):
        return None
    base_lat = ebp.latitude
    lat = base_lat + (pose.y / 111111.0)
    lon = ebp.longitude + (pose.x / (111111.0 * cos(radians(base_lat))))
    return (lat, lon)


def polygon_area(polygon: list[tuple[float, float]]) -> float:
    """Compute the area of a 2D polygon using the shoelace formula.

    Polygon points are local-frame meters (the same x/y coordinate space
    that the mower's pose lives in), so the result is square meters.
    Handles concave shapes correctly. Returns 0.0 for degenerate inputs
    (fewer than 3 points).
    """
    n = len(polygon)
    if n < 3:
        return 0.0
    s = 0.0
    for i in range(n):
        x1, y1 = polygon[i]
        x2, y2 = polygon[(i + 1) % n]
        s += x1 * y2 - x2 * y1
    return abs(s) * 0.5


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


def is_real_zone_catalog(new_catalog) -> bool:
    """Heuristic: did `parse_zone_catalog` actually walk a PbMap-bearing blob?

    A QUERY_PATH response shares the btMap branch with QUERY_MAP but carries
    only path data — no PbMap structure inside. ``parse_zone_catalog``
    returns an *empty* ``ZoneCatalog`` for those (no zones, no channels,
    no runtime_config, no enu_base_point). The official app's heartbeat
    fires QUERY_PATH bursts every time the user opens it, so without this
    guard the integration's cached catalog gets stomped to empty every
    couple of seconds, making the map camera entity flip to unavailable
    and dropping the camera entity's access_token from state attributes
    (which produces the `token=undefined` URL the HA frontend then
    fails to authenticate).

    Returns True iff the parsed catalog has at least one signal that it
    came from a real PbMap. Mirrors the conditional already applied to
    ``enu_base_point`` in the coordinator. See the
    ``project_btmap_sticky_fields`` project memory + arch.md §8c.
    """
    return bool(
        new_catalog.zones
        or new_catalog.channels
        or new_catalog.runtime_config is not None
        or new_catalog.enu_base_point is not None
    )


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
        new_rc = msg.robotConfig
        existing_rc = state_dict.get("robotConfig")
        if existing_rc is None:
            cloned = new_rc.__class__()
            cloned.CopyFrom(new_rc)
            state_dict["robotConfig"] = cloned
        else:
            # rrConfig.enableRr is `optional bool` and the firmware does not
            # serialize it on the wire when the device-side value is the
            # default (false). proto3 MergeFrom can't tell "field absent on
            # wire" from "value is false" — both look identical — so it would
            # preserve a stale cached enableRr=true forever once the cache
            # picked one up. Clearing rrConfig before the outer MergeFrom
            # forces a full replace of just the rrConfig sub-message; the
            # other rrConfig fields (rechargeBat / resumeBat / resumePeriod*)
            # are always fully echoed when rrConfig is present in the wire
            # payload, so replacing rrConfig wholesale is safe. Other
            # robotConfig fields (audioVolume, camLedStatus, etc.) keep the
            # sticky-merge behavior. See arch.md §6e + the project memory
            # `project_rrconfig_replace_semantics`.
            if new_rc.HasField("rrConfig"):
                existing_rc.ClearField("rrConfig")
            existing_rc.MergeFrom(new_rc)
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


# Sentinel key for the cached "which zone polygon contains the live pose"
# hash_id. Populated by `compute_current_zone_cache` at pboutput-merge time
# and consumed by `zone_at_pose` / `derive_current_zone` / per-zone entity
# attributes so we don't run N polygon-tests N times per state refresh.
# The leading underscore signals "internal derived state, not for export".
_CURRENT_ZONE_HASH_KEY = "_current_zone_hash_id"


def compute_current_zone_cache(state_dict: dict[str, Any]) -> str | None:
    """Run the pose-in-polygon walk once and stash the result in state.

    Called by the coordinator after each pboutput merge. All downstream
    consumers (current_zone sensor, per-zone "mower_in_zone" attribute,
    active_cut_config, map camera task-highlight gate) then look up the
    cached value instead of independently re-walking the catalog. For a
    typical 15-zone yard that's the difference between 1 polygon test per
    pboutput and 15+ per refresh cycle.

    Storage shape: ``state_dict[_CURRENT_ZONE_HASH_KEY]`` is set to the
    hash_id of the containing zone, or None if pose is outside every
    polygon. Key absent means "cache not populated yet" — consumers fall
    back to a live walk in that case (e.g. unit tests that bypass the
    coordinator).
    """
    pose = state_dict.get("pose")
    catalog = state_dict.get("zone_catalog")
    if pose is None or catalog is None:
        state_dict[_CURRENT_ZONE_HASH_KEY] = None
        return None
    # Mirror derive_current_zone's sort: in the (theoretical) case where
    # polygons overlap, prefer the in-task zone so the cache result is
    # consistent with what derive_current_zone would have returned on its
    # own. Zones don't overlap in practice but the sort is cheap.
    zones = sorted(catalog.zones, key=lambda z: (z.mow_order == 0, z.mow_order))
    for zone in zones:
        if zone.polygon_points and point_in_polygon(pose.x, pose.y, zone.polygon_points):
            state_dict[_CURRENT_ZONE_HASH_KEY] = zone.hash_id
            return zone.hash_id
    state_dict[_CURRENT_ZONE_HASH_KEY] = None
    return None


def zone_at_pose(state_dict: dict[str, Any]):
    """Return the ZoneInfo whose polygon contains the live pose, or None.

    Reads the cache populated by ``compute_current_zone_cache`` when the
    coordinator's pboutput handler has run. Falls back to a live walk if
    the cache is absent (test fixtures, fresh state_dict bypassing the
    coordinator). The fall-back path does NOT update the cache —
    coordinator ownership of cache writes is the only invariant we
    maintain to keep the fast path predictable.
    """
    catalog = state_dict.get("zone_catalog")
    if catalog is None:
        return None
    if _CURRENT_ZONE_HASH_KEY in state_dict:
        cached = state_dict[_CURRENT_ZONE_HASH_KEY]
        if cached is None:
            return None
        return catalog.zones_by_hashid.get(cached)
    # Cache not populated — fall back to a live walk. Same sort as the
    # cache writer so the result is consistent across both paths.
    pose = state_dict.get("pose")
    if pose is None:
        return None
    zones = sorted(catalog.zones, key=lambda z: (z.mow_order == 0, z.mow_order))
    for zone in zones:
        if zone.polygon_points and point_in_polygon(pose.x, pose.y, zone.polygon_points):
            return zone
    return None


def is_task_active(state_dict: dict[str, Any]) -> bool:
    """True iff robotInfo.workStatus says a mow task is currently underway.

    Single source of truth for the "is there an in-progress task right now"
    predicate. Used by:
      - The Task Zones sensor (shows current task or None).
      - Per-zone ``in_current_task`` attribute.
      - The map camera's green-zone task-highlight gate.

    Mirrors the ``ACTIVE_TASK_STATUSES`` set: Mowing / Pause / Docking /
    Error / Resume / ZonePartition / PauseDocking / Escaping. A
    freshly-completed task has its workStatus reset to Waiting, so residual
    mow_order values left in the catalog don't get treated as "still active".
    """
    ri = state_dict.get("robotInfo")
    if ri is None:
        return False
    return getattr(ri, "workStatus", 0) in ACTIVE_TASK_STATUSES


def current_task_zones(state_dict: dict[str, Any]) -> list:
    """Return ZoneInfo objects in the current mow task, ordered by mow_order.

    Empty list when no task is active or no zones have ``mow_order > 0``.
    Both the Task Zones sensor (which joins names for its state) and any
    other future "what's queued" consumer should read from this — it's the
    canonical "current task" definition.
    """
    if not is_task_active(state_dict):
        return []
    catalog = state_dict.get("zone_catalog")
    if catalog is None:
        return []
    task_zones = [z for z in catalog.zones if z.mow_order > 0]
    task_zones.sort(key=lambda z: z.mow_order)
    return task_zones


def derive_current_zone(state_dict: dict[str, Any]) -> str | None:
    """Derive 'which zone is the mower physically in right now'.

    Returns:
        Zone name if the mower is inside a zone polygon
        Channel descriptor (e.g. "Pool → Front yard" or "→ dock") if in a corridor
        None if idle, or in transit, or zone catalog unavailable

    Per arch.md §12, this is what the official app does. Pose is in local
    map frame matching the polygon coordinates (both are mower-local meters
    relative to the dock origin).

    Implementation note: the zone polygon walk is shared with all other
    consumers via ``zone_at_pose`` (cache-fed). Channels aren't cached —
    they're walked here on each call. The workStatus gate is local because
    the cache stores spatial truth regardless of task state ("mower_in_zone"
    is useful idle too).
    """
    pose = state_dict.get("pose")
    catalog = state_dict.get("zone_catalog")
    robot_info = state_dict.get("robotInfo")
    if not pose or not catalog or not robot_info:
        return None

    work_status = getattr(robot_info, "workStatus", 0)
    if work_status not in ACTIVE_TASK_STATUSES:
        return None

    zone = zone_at_pose(state_dict)
    if zone is not None:
        return zone.name

    # Channel walk — distinct geometry from zones; not cached.
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

    current_zone = zone_at_pose(state_dict)
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


def resolve_zones(catalog, inputs: list[str]) -> list[str]:
    """Convert a list of zone names or hashIds into canonical hashIds.

    For each input string:
      - Empty/whitespace → silently skipped
      - Matches a known hashId exactly → kept as-is
      - Matches a known zone name (case-insensitive, whitespace-trimmed) → resolved
      - Otherwise → raises ValueError listing known zones

    Used by the lymow_mqtt.start_zones service handler so users can pass
    friendly zone names instead of opaque firmware hashIds.

    Raises ValueError on unknown input or when catalog is unavailable.
    """
    if catalog is None or not catalog.zones:
        raise ValueError(
            "Zone catalog not available yet — wait for the integration to "
            "fetch zones from the mower (a few seconds after startup, "
            "or fire a command to trigger a refresh)."
        )

    name_to_hash = {z.name.strip().lower(): z.hash_id for z in catalog.zones if z.name}
    hashid_set = {z.hash_id for z in catalog.zones}

    resolved: list[str] = []
    for raw in inputs or []:
        s = (raw or "").strip()
        if not s:
            continue
        if s in hashid_set:
            resolved.append(s)
            continue
        looked_up = name_to_hash.get(s.lower())
        if looked_up is not None:
            resolved.append(looked_up)
            continue
        known = sorted({z.name for z in catalog.zones if z.name})
        raise ValueError(
            f"Unknown zone: {raw!r}. Known zones: {', '.join(known)}"
        )
    return resolved


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
