"""State management — merging MQTT broadcasts, deriving derived sensors,
geometry helpers, active-config inheritance.

All pure functions. The coordinator owns the state dict; this module
just provides the merge and derivation logic.
"""
from __future__ import annotations

from typing import Any


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
