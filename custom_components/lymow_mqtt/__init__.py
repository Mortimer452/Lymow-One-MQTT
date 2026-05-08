"""Lymow MQTT integration entry point."""
from __future__ import annotations

import logging

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .auth import CognitoAuth
from .const import CONF_EMAIL, CONF_PASSWORD, CONF_REGION, DOMAIN
from .coordinator import LymowCoordinator
from .rest import LymowREST

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [
    Platform.LAWN_MOWER,
    Platform.SENSOR,
    Platform.BINARY_SENSOR,
    Platform.CAMERA,
]

# Internal entry-data keys (must match config_flow.py)
_CONF_AUTH_METHOD = "auth_method"
_CONF_THING_NAME = "thing_name"


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Lymow MQTT from a config entry."""
    region = entry.data[CONF_REGION]
    thing_name = entry.data[_CONF_THING_NAME]

    session = async_get_clientsession(hass)
    auth = CognitoAuth(region, session)
    auth.from_dict(entry.data)

    # Restore email/password if SRP (needed for offline re-login if refresh
    # token rotates). The federated path can't re-login without user
    # interaction, so reauth is triggered by the ensure_valid() failure below.
    if entry.data.get(_CONF_AUTH_METHOD) == "srp":
        auth._email = entry.data.get(CONF_EMAIL)
        auth._password = entry.data.get(CONF_PASSWORD)

    try:
        await auth.ensure_valid()
    except Exception as e:
        _LOGGER.warning(
            "Auth invalid for %s; triggering reauth: %s", thing_name, e
        )
        raise ConfigEntryAuthFailed("Token refresh failed") from e

    rest = LymowREST(region, auth, session)
    coordinator = LymowCoordinator(hass, auth, rest, thing_name, region)
    await coordinator.async_setup()

    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN][entry.entry_id] = coordinator

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    _register_services(hass)

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    coordinator: LymowCoordinator | None = hass.data.get(DOMAIN, {}).pop(
        entry.entry_id, None
    )
    if coordinator is not None:
        await coordinator.async_unload()
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


def _register_services(hass: HomeAssistant) -> None:
    """Register service handlers (idempotent — only registers once per HA run)."""
    if hass.services.has_service(DOMAIN, "start_zones"):
        return

    def _coord_for_device_id(call: ServiceCall) -> LymowCoordinator | None:
        device_id = call.data.get("device_id")
        if not device_id:
            return None
        if isinstance(device_id, list):
            device_id = device_id[0] if device_id else None
        if not device_id:
            return None
        device_reg = dr.async_get(hass)
        device = device_reg.async_get(device_id)
        if not device:
            return None
        # The device's identifiers contain (DOMAIN, thing_name)
        for domain, thing_name in device.identifiers:
            if domain == DOMAIN:
                # Find the coordinator for this thing_name
                for coord in hass.data.get(DOMAIN, {}).values():
                    if getattr(coord, "thing_name", None) == thing_name:
                        return coord
        return None

    async def _handle_start_zones(call: ServiceCall) -> None:
        coord = _coord_for_device_id(call)
        if not coord:
            _LOGGER.warning("start_zones: no coordinator for device_id=%s", call.data.get("device_id"))
            return
        zone_ids = call.data.get("zone_ids", [])
        await coord.cmd_start(zone_hash_ids=zone_ids)

    async def _handle_dock_cancel_task(call: ServiceCall) -> None:
        coord = _coord_for_device_id(call)
        if not coord:
            _LOGGER.warning("dock_cancel_task: no coordinator for device_id=%s", call.data.get("device_id"))
            return
        await coord.cmd_dock_cancel_task()

    async def _handle_cancel_task(call: ServiceCall) -> None:
        coord = _coord_for_device_id(call)
        if not coord:
            _LOGGER.warning("cancel_task: no coordinator for device_id=%s", call.data.get("device_id"))
            return
        await coord.cmd_force_reinit()

    hass.services.async_register(
        DOMAIN,
        "start_zones",
        _handle_start_zones,
        schema=vol.Schema(
            {
                vol.Required("device_id"): vol.Any(str, [str]),
                vol.Required("zone_ids"): [str],
            }
        ),
    )
    hass.services.async_register(
        DOMAIN,
        "dock_cancel_task",
        _handle_dock_cancel_task,
        schema=vol.Schema({vol.Required("device_id"): vol.Any(str, [str])}),
    )
    hass.services.async_register(
        DOMAIN,
        "cancel_task",
        _handle_cancel_task,
        schema=vol.Schema({vol.Required("device_id"): vol.Any(str, [str])}),
    )
