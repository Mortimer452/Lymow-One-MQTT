"""Lymow MQTT push-driven coordinator.

Owns the state dict, MQTT client, REST online poll, and command dispatch.
Coordinator update_interval is None (push-only) — broadcasts arrive via
the MQTT subscriber and call async_set_updated_data() directly.

Per spec §5, command dispatch fires QUERY_MAP first, picks the right
userCtrl variant from live robotInfo, publishes, and watchdogs the
expected state transition.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from . import protocol, state, state_matrix, userctrl
from .auth import CognitoAuth
from .const import (
    API_ENDPOINTS,
    DOMAIN,
    USER_CTRL_CLEAN,
    USER_CTRL_DOCK,
    USER_CTRL_FORCE_REINIT,
    USER_CTRL_QUERY_MAP,
    USER_CTRL_QUERY_SCHEDULES,
    WORK_STATUS_ERROR,
)
from .mqtt import MqttClient
from .rest import LymowREST

_LOGGER = logging.getLogger(__name__)

# How long to wait for a state-transition broadcast confirming a command
_COMMAND_WATCHDOG_SECONDS = 2.5
# QUERY_MAP pre-flight wait window
_QUERY_MAP_WAIT_SECONDS = 3.0
# REST online poll cadence
_REST_POLL_INTERVAL = timedelta(minutes=15)


class LymowCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Push-only coordinator for one Lymow mower."""

    def __init__(
        self,
        hass: HomeAssistant,
        auth: CognitoAuth,
        rest: LymowREST,
        thing_name: str,
        region: str,
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=f"{DOMAIN}_{thing_name}",
            update_interval=None,  # push-only; we never call _async_update_data on a timer
        )
        self.auth = auth
        self.rest = rest
        self.thing_name = thing_name
        self.region = region
        self.host = API_ENDPOINTS[region]["iotDomain"]

        # State dict — single source of truth
        self._state: dict[str, Any] = {}

        # Online tracking
        self._rest_online: bool = False
        self._last_mqtt_at: datetime | None = None

        # Track previous robotStatus so we can detect Error -> non-Error
        # transitions and clear stale errorCodes (arch.md §7c). The merge
        # layer can't distinguish "field absent" from "deliberately empty
        # list", so the recovery clear has to live here.
        self._prev_robot_status: int | None = None
        # Track previous workStatus so we can refire QUERY_MAP on
        # Waiting -> Mowing transitions when the catalog wasn't fully
        # populated by the startup fire.
        self._prev_work_status: int | None = None

        # Lazy QUERY_MAP retry — the startup fire sometimes returns only
        # the small state-echo half (no btMap catalog blob), leaving
        # zone_catalog empty and runtime_config None. We refire occasionally
        # until we have a complete catalog, capped to avoid unnecessary chatter.
        self._catalog_retry_count: int = 0
        self._last_catalog_query_at: datetime = datetime.now(UTC)

        # MQTT client (constructed in async_setup)
        self.mqtt: MqttClient | None = None

        # Background tasks
        self._rest_poll_task: asyncio.Task | None = None

        # Watchdog support: an Event the dispatch coroutine waits on,
        # set by the inbound MQTT handler whenever new state lands. The
        # waiter clears it before each await so we don't miss a set()
        # that happens between checks.
        self._state_event = asyncio.Event()

        # Reconnect-on-disconnect state. _shutting_down gates _handle_disconnect
        # so async_unload's intentional disconnect doesn't trigger a reconnect
        # storm. _reconnecting prevents overlapping reconnect attempts when
        # paho fires on_disconnect multiple times in quick succession.
        self._shutting_down: bool = False
        self._reconnecting: bool = False

    async def async_setup(self) -> None:
        """Connect MQTT, fire startup queries, kick off REST poll."""
        # Initial REST device-info call (also gives us the IP for camera)
        await self._do_rest_poll()

        self.mqtt = MqttClient(
            thing_name=self.thing_name,
            host=self.host,
            region=self.region,
            auth=self.auth,
            on_pboutput=self._handle_pboutput,
            on_notify_app=self._handle_notify_app,
            on_disconnect_async=self._handle_disconnect,
        )
        await self.mqtt.connect()

        # Fire startup queries — fire-and-forget, responses arrive on /pboutput.
        # QUERY_MAP gives us the zone catalog AND PbRunTimeConfig in one shot
        # (the latter for cut_height/move_speed/cut_speed sensors via
        # state.active_cut_config). QUERY_SCHEDULES gives the schedule list.
        # We previously also fired QUERY_RUN_TIME_CONFIG (51) but that returns
        # a PbRobotConfig (rcCutHeight/rcCutSpeed, no moveSpeed) which isn't
        # the source we need; dropped to keep startup quiet.
        await self._publish_userctrl(USER_CTRL_QUERY_MAP, with_query_map_flag=True)
        await self._publish_userctrl(USER_CTRL_QUERY_SCHEDULES)
        # L3 wakeup — triggers a robotConfig broadcast so the auto-recharge
        # switch can read its initial state without waiting for a state-burst.
        # 8 bytes, fires once. See arch.md §7a Layer 3.
        await self._publish_raw(protocol.encode_upload_robot_config())
        self._last_catalog_query_at = datetime.now(UTC)

        # Kick off the REST poll task
        self._rest_poll_task = self.hass.async_create_task(self._rest_poll_loop())

    async def async_unload(self) -> None:
        # Set shutdown flag BEFORE disconnecting so the disconnect callback
        # we're about to trigger doesn't kick off a reconnect attempt.
        self._shutting_down = True
        if self._rest_poll_task:
            self._rest_poll_task.cancel()
            try:
                await self._rest_poll_task
            except asyncio.CancelledError:
                pass
        if self.mqtt:
            await self.mqtt.disconnect()

    @property
    def state_dict(self) -> dict[str, Any]:
        """Read-only access to the merged state dict for entities."""
        return self._state

    @property
    def is_online(self) -> bool:
        """Resolve REST + MQTT online signals (spec §7.3)."""
        return state.resolve_online(
            rest_online=self._rest_online,
            last_mqtt_at=self._last_mqtt_at,
        )

    # ── Inbound message handlers ────────────────────────────────

    def _handle_pboutput(self, raw_envelope: bytes) -> None:
        """Called from asyncio loop (bridged from paho thread)."""
        try:
            msg = protocol.decode_pboutput_envelope(raw_envelope)
        except Exception:
            _LOGGER.exception("Failed to decode pboutput")
            return

        self._last_mqtt_at = datetime.now(UTC)
        state.merge_pboutput(self._state, msg)

        # errorCodes recovery (arch.md §7c): merge_pboutput can't tell
        # "field absent" from "empty list", so when robotStatus transitions
        # from 7 (Error) to anything else we wipe the stale codes here.
        new_ri = self._state.get("robotInfo")
        if new_ri is not None:
            new_robot_status = getattr(new_ri, "robotStatus", None)
            if (
                self._prev_robot_status == WORK_STATUS_ERROR
                and new_robot_status is not None
                and new_robot_status != WORK_STATUS_ERROR
            ):
                self._state["errorCodes"] = []
            self._prev_robot_status = new_robot_status

        # Cache parsed zone catalog whenever a btMap-bearing message arrives
        if msg.btMap.ByteSize() > 200:
            try:
                new_catalog = protocol.parse_zone_catalog(msg.btMap)
                self._state["zone_catalog"] = new_catalog
                # Promote enu_base_point to its own sticky state slot —
                # only update when the new catalog actually carries it.
                # QUERY_PATH responses share this branch but parse to an
                # empty catalog (no PbMap structure); without this conditional
                # we'd lose the dock-anchor every time the user opens the
                # app and triggers QUERY_PATH bursts. See arch.md §8c +
                # `project_btmap_sticky_fields` memory.
                ebp = getattr(new_catalog, "enu_base_point", None)
                if ebp is not None:
                    self._state["enu_base_point"] = ebp
            except Exception:
                _LOGGER.exception("Failed to parse zone catalog")

        # Cache decoded schedules for the next_schedule sensor + active config inheritance
        if msg.schedule.ByteSize() > 0:
            try:
                self._state["schedules"] = protocol.decode_schedules(msg.schedule)
            except Exception:
                _LOGGER.exception("Failed to decode schedules")

        # PbRobotConfig (msg.robotConfig) is a different message than PbRunTimeConfig
        # (the source state.active_cut_config wants). The cut/move config comes from
        # QUERY_MAP responses via state["zone_catalog"].runtime_config. We don't need
        # to cache PbRobotConfig as "runtime_config" — leaving the merged
        # state["robotConfig"] in place for any future consumers (rrConfig etc.).

        # cleanReport handling (arch.md §7d, spec §5.2)
        cleanreport_arrived = msg.cleanReport.ByteSize() > 0
        if cleanreport_arrived:
            self._state["last_clean_report"] = msg.cleanReport
            self.hass.bus.async_fire(
                f"{DOMAIN}_task_complete",
                {
                    "thing_name": self.thing_name,
                    "mow_end_type": msg.cleanReport.mowEndType,
                },
            )

        # Lazy catalog retry + workStatus-transition refresh + post-mow refresh
        # (so zone renames done in the app during a mow get picked up after).
        self._maybe_refire_query_map(cleanreport_arrived=cleanreport_arrived)

        # Notify watchdog waiters. We only set() here; the waiter does the
        # clear() before each await so a set() between predicate-check and
        # wait() can't be lost.
        self._state_event.set()

        # Notify HA entities
        self.async_set_updated_data(self._state)

    def _maybe_refire_query_map(self, cleanreport_arrived: bool = False) -> None:
        """Fire another QUERY_MAP under any of three conditions:

        1. Mower just transitioned into MOWING (workStatus 1 -> 2). Firmware
           is reliably awake; good moment to grab a fresh catalog. Resets
           the retry budget.
        2. cleanReport just arrived (a mow finished). Picks up any zone
           renames the user did in the app during the mow.
        3. Catalog is incomplete (no zones OR no runtime_config) AND last
           fire was >60s ago AND retry count <5 (lazy startup retry).

        On a complete catalog with no transition / cleanReport, returns early.
        """
        from .const import WORK_STATUS_MOWING, WORK_STATUS_WAITING

        ri = self._state.get("robotInfo")
        new_work_status = getattr(ri, "workStatus", None) if ri is not None else None
        transitioned_to_mowing = (
            self._prev_work_status == WORK_STATUS_WAITING
            and new_work_status == WORK_STATUS_MOWING
        )
        self._prev_work_status = new_work_status

        catalog = self._state.get("zone_catalog")
        catalog_complete = (
            catalog is not None
            and len(catalog.zones) > 0
            and catalog.runtime_config is not None
        )

        if catalog_complete and not transitioned_to_mowing and not cleanreport_arrived:
            return

        now = datetime.now(UTC)
        seconds_since_last = (now - self._last_catalog_query_at).total_seconds()

        should_refire = (
            transitioned_to_mowing
            or cleanreport_arrived
            or (self._catalog_retry_count < 5 and seconds_since_last > 60)
        )
        if not should_refire:
            return

        if transitioned_to_mowing or cleanreport_arrived:
            # Reset the retry budget on a real state event — the firmware
            # is freshly active and likely to send a complete catalog.
            self._catalog_retry_count = 0
        else:
            self._catalog_retry_count += 1

        self._last_catalog_query_at = now
        _LOGGER.debug(
            "Refiring QUERY_MAP (retry=%s, transition=%s, cleanReport=%s, complete=%s)",
            self._catalog_retry_count,
            transitioned_to_mowing,
            cleanreport_arrived,
            catalog_complete,
        )
        self.hass.async_create_task(
            self._publish_userctrl(USER_CTRL_QUERY_MAP, with_query_map_flag=True)
        )

    def _handle_notify_app(self, payload: dict) -> None:
        """JSON {deviceThingName, robotState: online|offline}."""
        rs = payload.get("robotState")
        if rs == "online":
            self._rest_online = True  # treat as evidence
        elif rs == "offline":
            self._rest_online = False
        # Re-evaluate the binary_sensor.online by triggering a coordinator update
        self.async_set_updated_data(self._state)

    def _handle_disconnect(self) -> None:
        """Bridged from paho's on_disconnect — runs in asyncio.

        Don't trust paho's auto-reconnect — our presigned URL has a 24h
        SigV4 X-Amz-Expires baked in (sigv4.py:101). After that window,
        paho would retry forever with an expired-signature URL and AWS
        would reject every handshake. By tearing down and reconnecting
        explicitly, we force `auth.ensure_valid()` and a freshly signed
        URL each time.

        The 24h URL TTL means most disconnects within a single day's
        run get the same end result via paho's retry — but >24h runs
        with a hard MQTT disconnect would silently fail without this.
        """
        if self._shutting_down or self._reconnecting:
            return
        _LOGGER.warning(
            "MQTT disconnected for %s — refreshing creds and reconnecting",
            self.thing_name,
        )
        self.hass.async_create_task(self._reconnect_with_fresh_creds())

    async def _reconnect_with_fresh_creds(self) -> None:
        """Tear down the MQTT client and reconnect with a fresh presigned URL.

        Loops with exponential backoff until reconnect succeeds or the
        integration is being unloaded. After a successful reconnect, fires
        a QUERY_MAP to refresh state since the disconnect window may have
        included broadcasts we missed.
        """
        if self._shutting_down or self._reconnecting:
            return
        self._reconnecting = True
        backoff = 5  # seconds; capped at 5 minutes below
        try:
            while not self._shutting_down:
                try:
                    if self.mqtt is None:
                        _LOGGER.error(
                            "MqttClient missing on %s; cannot reconnect",
                            self.thing_name,
                        )
                        return
                    await self.mqtt.disconnect()
                    if self._shutting_down:
                        return
                    await self.mqtt.connect()
                    _LOGGER.info(
                        "MQTT reconnected for %s with fresh URL", self.thing_name
                    )
                    # Refresh state after the gap
                    await self._publish_userctrl(
                        USER_CTRL_QUERY_MAP, with_query_map_flag=True
                    )
                    return
                except Exception as e:
                    if self._shutting_down:
                        return
                    _LOGGER.warning(
                        "Reconnect failed for %s: %s — retrying in %ss",
                        self.thing_name,
                        e,
                        backoff,
                    )
                    try:
                        await asyncio.sleep(backoff)
                    except asyncio.CancelledError:
                        return
                    backoff = min(backoff * 2, 300)
        finally:
            self._reconnecting = False

    # ── REST online poll ────────────────────────────────────────

    async def _rest_poll_loop(self) -> None:
        """Fire REST online check every 15 minutes."""
        while True:
            try:
                await asyncio.sleep(_REST_POLL_INTERVAL.total_seconds())
                await self._do_rest_poll()
            except asyncio.CancelledError:
                raise
            except Exception:
                _LOGGER.exception("REST poll cycle failed; will retry next interval")

    async def _do_rest_poll(self) -> None:
        """Fetch /get-device-info, update online + IP."""
        info = await self.rest.get_device_info(self.thing_name)
        if not info:
            self._rest_online = False
        else:
            ds = info.get("deviceState") or info.get("device_state") or "offline"
            self._rest_online = ds == "online"
            ip = info.get("ipAddress") or info.get("ip_address")
            if ip:
                self._state["rest_ip_address"] = ip
        self.async_set_updated_data(self._state)

    # ── Command dispatch ────────────────────────────────────────

    async def _publish_userctrl(
        self,
        user_ctrl: int,
        with_query_map_flag: bool = False,
        zone_hash_ids: list[str] | None = None,
    ) -> None:
        """Build and publish a PbInput. No watchdog (used for queries + internal calls)."""
        if not self.mqtt or not self.mqtt.is_connected:
            raise HomeAssistantError("MQTT not connected")
        if user_ctrl == USER_CTRL_QUERY_MAP and with_query_map_flag:
            raw = protocol.encode_query_map()
        elif user_ctrl == USER_CTRL_CLEAN and zone_hash_ids is not None:
            raw = protocol.encode_start_zones(zone_hash_ids)
        else:
            raw = protocol.encode_userctrl(user_ctrl)
        ok = self.mqtt.publish_pbinput(raw)
        if not ok:
            raise HomeAssistantError(f"MQTT publish failed for userCtrl={user_ctrl}")

    async def _publish_raw(self, raw: bytes) -> None:
        """Publish a pre-encoded PbInput payload. Used for non-userCtrl
        commands like the L3 wakeup and setRR config writes."""
        if not self.mqtt or not self.mqtt.is_connected:
            raise HomeAssistantError("MQTT not connected")
        ok = self.mqtt.publish_pbinput(raw)
        if not ok:
            raise HomeAssistantError("MQTT publish failed")

    async def cmd_set_auto_recharge(self, enabled: bool) -> None:
        """Toggle the firmware's auto-recharge-and-resume feature.

        Reads the current rrConfig from coordinator state to carry forward
        the user's other preferences (battery thresholds, time window) so
        toggling the switch doesn't reset them. Builds the no-userCtrl setRR
        payload (arch.md §6g) and publishes. The firmware applies the new
        config and broadcasts back the updated robotConfig within ~1s.

        Raises HomeAssistantError if no robotConfig has been received yet —
        we'd otherwise be writing default zero values for the carry-forward
        fields, nuking the user's settings.
        """
        rc = self._state.get("robotConfig")
        if rc is None or not rc.HasField("rrConfig"):
            raise HomeAssistantError(
                "No rrConfig in state yet — wait a few seconds for the "
                "mower to broadcast its current config, then retry."
            )
        rr = rc.rrConfig
        # Carry-forward must ALWAYS emit the PbTimeZone sub-messages — the
        # firmware uses REPLACE (not merge) semantics for those, so a setRR
        # without resumePeriodStart/End on the wire resets the user's saved
        # window to 00:00. The official app at decompiled.js:328846 always
        # includes both fields for exactly this reason. We default the inner
        # hour/minute to 0 when the device echoed an empty `{}` sub-message
        # (which means hour=0, minute=0 anyway), so the wire payload always
        # carries the full PbTimeZone. See arch.md §6g + the project memory
        # `project_rrconfig_replace_semantics`.
        ps = rr.resumePeriodStart if rr.HasField("resumePeriodStart") else None
        pe = rr.resumePeriodEnd   if rr.HasField("resumePeriodEnd")   else None
        raw = protocol.encode_set_rr_config(
            enable_rr=enabled,
            recharge_bat=rr.rechargeBat if rr.HasField("rechargeBat") else None,
            resume_bat=rr.resumeBat if rr.HasField("resumeBat") else None,
            period_start_hour=(ps.hour   if ps is not None else 0),
            period_start_minute=(ps.minute if ps is not None else 0),
            period_end_hour=(pe.hour     if pe is not None else 0),
            period_end_minute=(pe.minute   if pe is not None else 0),
        )
        await self._publish_raw(raw)

    async def _wait_for_state(self, expected: set[int], timeout: float) -> bool:
        """Wait until robotInfo.workStatus or robotStatus is in expected, or timeout.

        If `expected` is empty, this acts as a "wait for ANY state update"
        helper: it returns True at the first inbound broadcast (used as a
        post-QUERY_MAP settle) and False on timeout.

        The clear-before-await pattern ensures we never miss a set() that
        races our predicate check.
        """
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        any_update = not expected
        # Snapshot whatever update marker is meaningful — for the "any update"
        # mode we treat each new set() as a tick.
        while True:
            ri = self._state.get("robotInfo")
            if ri is not None and not any_update:
                ws = getattr(ri, "workStatus", None)
                rs = getattr(ri, "robotStatus", None)
                if ws in expected or rs in expected:
                    return True
            remaining = deadline - loop.time()
            if remaining <= 0:
                return False
            # Clear BEFORE awaiting so we don't miss the next set(). Anything
            # that fired between the predicate check and here will still be
            # latched on the event after we clear (because handlers won't
            # re-check until they fire again) — but the predicate check just
            # above already accounted for that state. The race window is the
            # gap between this clear() and the await, which is single-stepped
            # in the asyncio loop and therefore safe.
            self._state_event.clear()
            try:
                await asyncio.wait_for(self._state_event.wait(), timeout=remaining)
            except asyncio.TimeoutError:
                return False
            if any_update:
                return True

    async def cmd_button_press(self, button: str) -> None:
        """User tapped Start / Pause / Dock — matrix-driven dispatch.

        Pre-flight QUERY_MAP, look up the row for the fresh state, publish
        whichever userCtrl that row designates for this button, then watchdog
        the expected post-state. Replaces the old cmd_pause / cmd_resume /
        cmd_dock_recharge methods — those decisions now live in
        state_matrix.STATE_MATRIX.

        button must be one of: "start_mowing", "pause", "dock".
        """
        if button not in ("start_mowing", "pause", "dock"):
            raise ValueError(f"Unknown button: {button!r}")

        # Pre-flight QUERY_MAP so we pick the variant from fresh state
        await self._publish_userctrl(USER_CTRL_QUERY_MAP, with_query_map_flag=True)
        await self._wait_for_state(set(), timeout=_QUERY_MAP_WAIT_SECONDS)
        ri = self._state.get("robotInfo")
        if ri is None:
            raise HomeAssistantError("No state — try again in a moment")

        row = state_matrix.lookup(
            work_status=ri.workStatus,
            robot_status=ri.robotStatus,
            is_recharging=bool(getattr(ri, "isRecharging", False)),
        )
        action = getattr(row, button)
        if action is None:
            raise HomeAssistantError(
                f"Mower can't {button.replace('_', ' ')} from current state "
                f"(work_status={ri.workStatus}, robot_status={ri.robotStatus}). "
                f"State matrix says: {row.note or 'no action defined for this combo'}"
            )

        await self._publish_userctrl(action)
        expected = userctrl.EXPECTED_POST_STATES.get(action, set())
        if not expected:
            return  # nothing to watchdog for (e.g., query commands)
        ok = await self._wait_for_state(expected, timeout=_COMMAND_WATCHDOG_SECONDS)
        if not ok:
            raise HomeAssistantError(
                f"{button.replace('_', ' ').title()} command not confirmed "
                f"within watchdog window"
            )

    async def cmd_start(self, zone_hash_ids: list[str] | None = None) -> None:
        """Start a fresh mow on a specific zone list — service path only.

        The lawn_mower entity's Start button doesn't go through here; it
        goes through cmd_button_press which routes via the matrix (and
        for the WAITING-idle row, that ends up sending USER_CTRL_CLEAN
        with no zones, equivalent to this method's default behavior).

        This direct path exists because the lymow_mqtt.start_zones service
        needs to pass the zone_hash_ids list to encode_start_zones, and
        that's a different protobuf payload than a bare userCtrl.
        """
        await self._publish_userctrl(
            USER_CTRL_CLEAN, zone_hash_ids=zone_hash_ids or []
        )
        ok = await self._wait_for_state(
            userctrl.EXPECTED_POST_STATES[USER_CTRL_CLEAN],
            timeout=_COMMAND_WATCHDOG_SECONDS,
        )
        if not ok:
            raise HomeAssistantError(
                "Start command not confirmed within watchdog window"
            )

    async def cmd_dock_cancel_task(self) -> None:
        """Dock + ABANDON task. Service-only."""
        await self._publish_userctrl(USER_CTRL_DOCK)
        ok = await self._wait_for_state(
            userctrl.EXPECTED_POST_STATES[USER_CTRL_DOCK],
            timeout=_COMMAND_WATCHDOG_SECONDS,
        )
        if not ok:
            raise HomeAssistantError(
                "Dock-cancel command not confirmed within watchdog window"
            )

    async def cmd_force_reinit(self) -> None:
        """FORCE_REINIT — reset to WAITING, abandon task, stop in place."""
        await self._publish_userctrl(USER_CTRL_FORCE_REINIT)
        ok = await self._wait_for_state(
            userctrl.EXPECTED_POST_STATES[USER_CTRL_FORCE_REINIT],
            timeout=_COMMAND_WATCHDOG_SECONDS,
        )
        if not ok:
            raise HomeAssistantError(
                "Cancel-task command not confirmed within watchdog window"
            )
