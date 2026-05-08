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

from . import protocol, state, userctrl
from .auth import CognitoAuth
from .const import (
    API_ENDPOINTS,
    DOMAIN,
    USER_CTRL_CLEAN,
    USER_CTRL_DOCK,
    USER_CTRL_FORCE_REINIT,
    USER_CTRL_QUERY_MAP,
    USER_CTRL_QUERY_SCHEDULES,
    USER_CTRL_RECHARGE_DOCK,
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

        # MQTT client (constructed in async_setup)
        self.mqtt: MqttClient | None = None

        # Background tasks
        self._rest_poll_task: asyncio.Task | None = None

        # Watchdog support: an Event the dispatch coroutine waits on,
        # set by the inbound MQTT handler whenever new state lands. The
        # waiter clears it before each await so we don't miss a set()
        # that happens between checks.
        self._state_event = asyncio.Event()

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

        # Kick off the REST poll task
        self._rest_poll_task = self.hass.async_create_task(self._rest_poll_loop())

    async def async_unload(self) -> None:
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
                self._state["zone_catalog"] = protocol.parse_zone_catalog(msg.btMap)
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
        if msg.cleanReport.ByteSize() > 0:
            self._state["last_clean_report"] = msg.cleanReport
            self.hass.bus.async_fire(
                f"{DOMAIN}_task_complete",
                {
                    "thing_name": self.thing_name,
                    "mow_end_type": msg.cleanReport.mowEndType,
                },
            )

        # Notify watchdog waiters. We only set() here; the waiter does the
        # clear() before each await so a set() between predicate-check and
        # wait() can't be lost.
        self._state_event.set()

        # Notify HA entities
        self.async_set_updated_data(self._state)

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
        """Bridged from paho's on_disconnect — runs in asyncio."""
        _LOGGER.warning(
            "MQTT disconnected for %s; paho will auto-reconnect", self.thing_name
        )
        # paho handles reconnect internally; if it fails persistently we rely
        # on the next _do_rest_poll cycle to detect offline. A more aggressive
        # cred-refresh + manual reconnect can be added here if needed.

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

    async def cmd_pause(self) -> None:
        """User tapped Pause. Refresh state, pick variant, publish, watchdog."""
        # Pre-flight QUERY_MAP for fresh state
        await self._publish_userctrl(USER_CTRL_QUERY_MAP, with_query_map_flag=True)
        await self._wait_for_state(set(), timeout=_QUERY_MAP_WAIT_SECONDS)
        ri = self._state.get("robotInfo")
        if ri is None:
            raise HomeAssistantError("No state — try again in a moment")
        try:
            variant = userctrl.pick_pause_variant(ri.workStatus)
        except ValueError as e:
            raise HomeAssistantError(str(e)) from e
        if variant is None:
            return  # already paused, no-op
        await self._publish_userctrl(variant)
        ok = await self._wait_for_state(
            userctrl.EXPECTED_POST_STATES[variant],
            timeout=_COMMAND_WATCHDOG_SECONDS,
        )
        if not ok:
            raise HomeAssistantError(
                "Lymow ignored the command. Mower may be in a state that doesn't allow this."
            )

    async def cmd_resume(self) -> None:
        await self._publish_userctrl(USER_CTRL_QUERY_MAP, with_query_map_flag=True)
        await self._wait_for_state(set(), timeout=_QUERY_MAP_WAIT_SECONDS)
        ri = self._state.get("robotInfo")
        if ri is None:
            raise HomeAssistantError("No state — try again in a moment")
        try:
            variant = userctrl.pick_resume_variant(ri.workStatus)
        except ValueError as e:
            raise HomeAssistantError(str(e)) from e
        if variant is None:
            # Not paused — interpret as "start"
            await self.cmd_start()
            return
        await self._publish_userctrl(variant)
        ok = await self._wait_for_state(
            userctrl.EXPECTED_POST_STATES[variant],
            timeout=_COMMAND_WATCHDOG_SECONDS,
        )
        if not ok:
            raise HomeAssistantError(
                "Resume command not confirmed within watchdog window"
            )

    async def cmd_start(self, zone_hash_ids: list[str] | None = None) -> None:
        """Start fresh mow on default rotation, or on specific zones."""
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

    async def cmd_dock_recharge(self) -> None:
        """Dock + KEEP task progress (the safer default)."""
        await self._publish_userctrl(USER_CTRL_RECHARGE_DOCK)
        ok = await self._wait_for_state(
            userctrl.EXPECTED_POST_STATES[USER_CTRL_RECHARGE_DOCK],
            timeout=_COMMAND_WATCHDOG_SECONDS,
        )
        if not ok:
            raise HomeAssistantError(
                "Dock command not confirmed within watchdog window"
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
