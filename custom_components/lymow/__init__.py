"""Lymow Robot Mower integration."""

from __future__ import annotations

import logging
from datetime import timedelta

import voluptuous as vol
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import CognitoAuth, LymowClient
from .const import (
    CLEAN_MODE_OPTIONS,
    CONF_EMAIL,
    CONF_PASSWORD,
    CONF_REGION,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    SERVICE_SET_BLADE,
    SERVICE_SET_SCHEDULE,
    SERVICE_START_ZONE,
)
from .coordinator import LymowCoordinator

_LOGGER = logging.getLogger(__name__)

PLATFORMS = [
    Platform.LAWN_MOWER,
    Platform.SENSOR,
    Platform.BINARY_SENSOR,
    Platform.SELECT,
    Platform.NUMBER,
    Platform.CAMERA,
]


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Lymow from a config entry."""
    email      = entry.data[CONF_EMAIL]
    password   = entry.data[CONF_PASSWORD]
    region     = entry.data[CONF_REGION]
    thing_name = entry.data["thing_name"]

    session = async_get_clientsession(hass)
    auth    = CognitoAuth(region, session)

    # Restore stored tokens to avoid re-login on every HA restart
    if entry.data.get("refresh_token"):
        auth.from_dict(entry.data)
        try:
            await auth.ensure_valid(email, password)
        except Exception:
            _LOGGER.warning("Stored tokens invalid for %s — re-logging in", thing_name)
            await auth.login(email, password)
            await auth.get_aws_credentials()
    else:
        await auth.login(email, password)
        await auth.get_aws_credentials()

    client = LymowClient(region, auth, session)

    scan_interval = entry.options.get("scan_interval", DEFAULT_SCAN_INTERVAL)
    coordinator   = LymowCoordinator(
        hass=hass,
        auth=auth,
        client=client,
        thing_name=thing_name,
        email=email,
        password=password,
    )
    coordinator.update_interval = timedelta(seconds=scan_interval)

    # Store reference so entity platforms can find it
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator

    # First refresh + static device info
    await coordinator.async_config_entry_first_refresh()
    await coordinator.async_refresh_device_info()

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    # ── Register services (only once, even with multiple robots) ──────────

    def _coord_for_call(call: ServiceCall) -> LymowCoordinator | None:
        entry_id = call.data.get("entry_id")
        if entry_id:
            return hass.data[DOMAIN].get(entry_id)
        coords = list(hass.data[DOMAIN].values())
        return coords[0] if coords else None

    if not hass.services.has_service(DOMAIN, SERVICE_START_ZONE):
        async def handle_start_zone(call: ServiceCall) -> None:
            if coord := _coord_for_call(call):
                zone_ids = call.data.get("zone_ids") or None
                await coord.async_start_mow(zone_ids=zone_ids)

        hass.services.async_register(
            DOMAIN, SERVICE_START_ZONE, handle_start_zone,
            schema=vol.Schema({
                vol.Optional("entry_id"): str,
                vol.Optional("zone_ids", default=[]): [str],
            }),
        )

    if not hass.services.has_service(DOMAIN, SERVICE_SET_BLADE):
        async def handle_set_blade(call: ServiceCall) -> None:
            if coord := _coord_for_call(call):
                await coord.async_set_blade_height(call.data["height_mm"])

        hass.services.async_register(
            DOMAIN, SERVICE_SET_BLADE, handle_set_blade,
            schema=vol.Schema({
                vol.Optional("entry_id"): str,
                vol.Required("height_mm"): vol.All(int, vol.Range(min=20, max=60)),
            }),
        )

    if not hass.services.has_service(DOMAIN, SERVICE_SET_SCHEDULE):
        async def handle_set_schedule(call: ServiceCall) -> None:
            if coord := _coord_for_call(call):
                await coord.async_set_schedule(call.data["schedules"])

        hass.services.async_register(
            DOMAIN, SERVICE_SET_SCHEDULE, handle_set_schedule,
            schema=vol.Schema({
                vol.Optional("entry_id"): str,
                vol.Required("schedules"): [
                    vol.Schema({
                        vol.Required("day"):       vol.All(int, vol.Range(min=0, max=6)),
                        vol.Required("startHour"): vol.All(int, vol.Range(min=0, max=23)),
                        vol.Required("startMin"):  vol.All(int, vol.Range(min=0, max=59)),
                        vol.Required("duration"):  vol.All(int, vol.Range(min=1, max=1440)),
                    })
                ],
            }),
        )

    # Persist updated tokens after successful setup
    hass.config_entries.async_update_entry(
        entry,
        data={**entry.data, **auth.to_dict()},
    )

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        hass.data[DOMAIN].pop(entry.entry_id, None)
    return unloaded
