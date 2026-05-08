"""DataUpdateCoordinator for Lymow."""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import CognitoAuth, LymowClient, LymowError
from .const import (
    DEFAULT_SCAN_INTERVAL,
    DEVICE_STATE_OFFLINE,
    DOMAIN,
    F_DEVICE_STATE,
    WORK_STATUS_OFFLINE,
)

_LOGGER = logging.getLogger(__name__)


class LymowCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Coordinator for a single Lymow robot."""

    def __init__(
        self,
        hass: HomeAssistant,
        auth: CognitoAuth,
        client: LymowClient,
        thing_name: str,
        email: str,
        password: str,
    ) -> None:
        self.auth       = auth
        self.client     = client
        self.thing_name = thing_name
        self._email     = email
        self._password  = password

        # Static info fetched once after setup
        self.device_info_data: dict = {}
        self.history: list[dict]    = []

        super().__init__(
            hass,
            _LOGGER,
            name=f"{DOMAIN}_{thing_name}",
            update_interval=timedelta(seconds=DEFAULT_SCAN_INTERVAL),
        )

    async def _async_update_data(self) -> dict[str, Any]:
        try:
            await self.auth.ensure_valid(self._email, self._password)
            state = await self.client.get_full_state(self.thing_name)

            if not state:
                _LOGGER.debug("Empty shadow for %s — marking offline", self.thing_name)
                return {
                    "workStatus":  WORK_STATUS_OFFLINE,
                    F_DEVICE_STATE: DEVICE_STATE_OFFLINE,
                    "isOnline":    False,
                }

            _LOGGER.debug("Shadow state %s: %s", self.thing_name, state)
            return state

        except LymowError as err:
            raise UpdateFailed(f"Lymow API error: {err}") from err
        except Exception as err:
            raise UpdateFailed(f"Unexpected error updating {self.thing_name}: {err}") from err

    # ── One-time fetches ─────────────────────

    async def async_refresh_device_info(self) -> None:
        try:
            self.device_info_data = await self.client.get_device_info(self.thing_name)
        except LymowError as err:
            _LOGGER.warning("Cannot fetch device info for %s: %s", self.thing_name, err)

    async def async_refresh_history(self, count: int = 10) -> list[dict]:
        try:
            await self.auth.ensure_valid(self._email, self._password)
            self.history = await self.client.get_clean_history(self.thing_name, size=count)
            return self.history
        except LymowError as err:
            _LOGGER.warning("History fetch failed: %s", err)
            return []

    async def async_refresh_map(self) -> dict | None:
        try:
            await self.auth.ensure_valid(self._email, self._password)
            return await self.client.get_backup_map(self.thing_name)
        except LymowError as err:
            _LOGGER.warning("Map fetch failed: %s", err)
            return None

    # ── Command shortcuts ─────────────────────
    # Each command refreshes coordinator state after sending.

    async def _cmd(self, coro) -> bool:
        await self.auth.ensure_valid(self._email, self._password)
        ok = await coro
        await self.async_request_refresh()
        return ok

    async def async_start_mow(self, zone_ids: list[str] | None = None) -> bool:
        return await self._cmd(self.client.cmd_start_mow(self.thing_name, zone_ids))

    async def async_pause(self) -> bool:
        return await self._cmd(self.client.cmd_pause(self.thing_name))

    async def async_resume(self) -> bool:
        return await self._cmd(self.client.cmd_resume(self.thing_name))

    async def async_dock(self) -> bool:
        return await self._cmd(self.client.cmd_dock(self.thing_name))

    async def async_stop(self) -> bool:
        return await self._cmd(self.client.cmd_stop(self.thing_name))

    async def async_set_blade_height(self, height_mm: int) -> bool:
        return await self._cmd(self.client.cmd_set_blade_height(self.thing_name, height_mm))

    async def async_set_clean_mode(self, mode: str) -> bool:
        return await self._cmd(self.client.cmd_set_clean_mode(self.thing_name, mode))

    async def async_set_schedule(self, schedules: list[dict]) -> bool:
        return await self._cmd(self.client.cmd_set_schedule(self.thing_name, schedules))

    # ── Helpers ──────────────────────────────

    @property
    def work_status(self) -> int:
        return self.data.get("workStatus", WORK_STATUS_OFFLINE) if self.data else WORK_STATUS_OFFLINE

    @property
    def is_online(self) -> bool:
        if not self.data:
            return False
        return (
            self.data.get("isOnline", False)
            or self.data.get(F_DEVICE_STATE) == "online"
            or self.data.get("workStatus", WORK_STATUS_OFFLINE) != WORK_STATUS_OFFLINE
        )
