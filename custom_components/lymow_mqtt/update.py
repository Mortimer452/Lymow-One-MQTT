"""Lymow firmware update entity.

Uses the same two-phase REST-driven OTA flow as the official app:
  1. GET checkUpdateApi/check-update  -> {latestVersion, releaseNote}
  2. GET createOtaJobApi/create-ota-job -> {jobId}
  3. Poll GET createOtaJobApi/get-ota-job-summary -> {status}
Meanwhile the firmware broadcasts workStatus=UPDATING (11) and
debugSetting.downloadProgress (0-100) over MQTT.
"""
from __future__ import annotations

import asyncio
import logging

from homeassistant.components.update import (
    UpdateDeviceClass,
    UpdateEntity,
    UpdateEntityFeature,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN, WORK_STATUS_UPDATING
from .coordinator import LymowCoordinator
from .entity_base import LymowEntity

_LOGGER = logging.getLogger(__name__)

_UPDATE_CHECK_INTERVAL = 6 * 3600  # 6 hours between REST checks
_OTA_POLL_INTERVAL = 10            # seconds between job-status polls
_OTA_TIMEOUT = 900                 # 15 minutes max


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: LymowCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([LymowUpdateEntity(coordinator)])


class LymowUpdateEntity(LymowEntity, UpdateEntity):
    """Firmware update entity for a Lymow mower."""

    _attr_device_class = UpdateDeviceClass.FIRMWARE
    _attr_supported_features = (
        UpdateEntityFeature.INSTALL
        | UpdateEntityFeature.PROGRESS
        | UpdateEntityFeature.RELEASE_NOTES
    )

    def __init__(self, coordinator: LymowCoordinator) -> None:
        super().__init__(coordinator, "firmware_update")
        self._attr_translation_key = "firmware_update"
        self._latest_version: str | None = None
        self._release_notes_text: str | None = None
        self._object_key: str | None = None
        self._ota_job_id: str | None = None
        self._ota_poll_task: asyncio.Task | None = None
        self._check_task: asyncio.Task | None = None

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self._check_task = self.hass.async_create_background_task(
            self._periodic_check_loop(),
            f"{self.coordinator.thing_name} firmware check",
        )

    async def async_will_remove_from_hass(self) -> None:
        for task in (self._check_task, self._ota_poll_task):
            if task:
                task.cancel()
        await super().async_will_remove_from_hass()

    # ── UpdateEntity properties ─────────────────────────────────

    @property
    def installed_version(self) -> str | None:
        di = self.coordinator.state_dict.get("deviceInfo")
        if di and di.HasField("softwareVersion"):
            return di.softwareVersion
        return self.coordinator.state_dict.get("rest_firmware_version")

    @property
    def latest_version(self) -> str | None:
        return self._latest_version

    @property
    def in_progress(self) -> bool | int:
        ri = self.coordinator.state_dict.get("robotInfo")
        ws = getattr(ri, "workStatus", None) if ri else None
        if ws == WORK_STATUS_UPDATING:
            ds = self.coordinator.state_dict.get("debugSetting")
            if ds and ds.HasField("downloadProgress") and ds.downloadProgress > 0:
                return min(ds.downloadProgress, 100)
            return True
        return self._ota_job_id is not None

    def release_notes(self) -> str | None:
        return self._release_notes_text

    # ── Install action ──────────────────────────────────────────

    async def async_install(
        self, version: str | None, backup: bool, **kwargs
    ) -> None:
        if not self._object_key:
            raise HomeAssistantError(
                "No firmware update available. Check again later."
            )
        job_id = await self.coordinator.rest.create_ota_job(
            self.coordinator.thing_name, self._object_key,
        )
        if not job_id:
            raise HomeAssistantError(
                "Failed to create OTA job — the REST API returned no jobId."
            )
        self._ota_job_id = job_id
        self.async_write_ha_state()

        if self._ota_poll_task and not self._ota_poll_task.done():
            self._ota_poll_task.cancel()
        self._ota_poll_task = self.hass.async_create_background_task(
            self._poll_ota_job(),
            f"{self.coordinator.thing_name} OTA poll",
        )

    # ── Background loops ────────────────────────────────────────

    async def _periodic_check_loop(self) -> None:
        while True:
            try:
                await self._do_update_check()
            except asyncio.CancelledError:
                return
            except Exception:
                _LOGGER.exception("Firmware update check failed")
            try:
                await asyncio.sleep(_UPDATE_CHECK_INTERVAL)
            except asyncio.CancelledError:
                return

    async def _do_update_check(self) -> None:
        data = await self.coordinator.rest.check_update(
            self.coordinator.thing_name,
        )
        if not data:
            return

        latest_fw = data.get("latestVersion")
        if not latest_fw:
            return

        installed = self.installed_version
        # App comparison logic (decompiled.js:1600588): if the installed
        # version + "_" appears inside the latestFw objectKey string,
        # versions match.  e.g. "v2.1.45_" in "v2.1.45_lymow_0.1.0" → same.
        if installed and f"{installed}_" in latest_fw:
            self._latest_version = installed
            self._object_key = None
            self._release_notes_text = None
        else:
            self._object_key = latest_fw
            # Extract display version by stripping the build suffix:
            #   "v2.1.48.1_20260528" → "v2.1.48.1"
            #   "v2.1.46_lymow_0.1.0" → "v2.1.46"
            self._latest_version = latest_fw.split("_", 1)[0]
            raw_notes = data.get("releaseNote", "")
            self._release_notes_text = (
                raw_notes.replace("\\n", "\n") if raw_notes else None
            )

        self.async_write_ha_state()

    async def _poll_ota_job(self) -> None:
        elapsed = 0
        try:
            while elapsed < _OTA_TIMEOUT and self._ota_job_id:
                await asyncio.sleep(_OTA_POLL_INTERVAL)
                elapsed += _OTA_POLL_INTERVAL

                data = await self.coordinator.rest.get_ota_job_summary(
                    self.coordinator.thing_name, self._ota_job_id,
                )
                if not data:
                    continue

                status = data.get("status", "")
                if status == "SUCCEEDED":
                    _LOGGER.info(
                        "Firmware update succeeded for %s",
                        self.coordinator.thing_name,
                    )
                    self._ota_job_id = None
                    await self._do_update_check()
                    return

                if status == "FAILED":
                    details = data.get("statusDetails") or {}
                    reason = (details.get("detailsMap") or {}).get(
                        "reason", "Unknown error"
                    )
                    _LOGGER.error(
                        "Firmware update failed for %s: %s",
                        self.coordinator.thing_name,
                        reason,
                    )
                    self._ota_job_id = None
                    self.async_write_ha_state()
                    return

            if self._ota_job_id:
                _LOGGER.error(
                    "Firmware update timed out for %s after %ss",
                    self.coordinator.thing_name,
                    _OTA_TIMEOUT,
                )
                self._ota_job_id = None
                self.async_write_ha_state()
        except asyncio.CancelledError:
            return
