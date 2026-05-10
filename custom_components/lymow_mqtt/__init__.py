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
    Platform.DEVICE_TRACKER,
    Platform.SWITCH,
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
        # Modern HA convention: device targets arrive in call.target["device_id"].
        # Fallback to call.data for backwards-compat or unusual call patterns.
        device_id: str | None = None
        target = getattr(call, "target", None) or {}
        target_ids = target.get("device_id") if isinstance(target, dict) else None
        if target_ids:
            if isinstance(target_ids, str):
                device_id = target_ids
            elif isinstance(target_ids, (list, set, tuple)) and target_ids:
                device_id = next(iter(target_ids))
        if not device_id:
            data_device = call.data.get("device_id")
            if isinstance(data_device, str):
                device_id = data_device
            elif isinstance(data_device, list) and data_device:
                device_id = data_device[0]
        if not device_id:
            return None
        device_reg = dr.async_get(hass)
        device = device_reg.async_get(device_id)
        if not device:
            return None
        # The device's identifiers contain (DOMAIN, thing_name)
        for domain, thing_name in device.identifiers:
            if domain == DOMAIN:
                for coord in hass.data.get(DOMAIN, {}).values():
                    if getattr(coord, "thing_name", None) == thing_name:
                        return coord
        return None

    async def _handle_start_zones(call: ServiceCall) -> None:
        from homeassistant.exceptions import HomeAssistantError
        from . import state as state_mod
        coord = _coord_for_device_id(call)
        if not coord:
            raise HomeAssistantError(
                "No Lymow device targeted. Pick the mower in the Targets "
                "section of the service call."
            )
        # Accept either friendly zone names or raw hashIds — both go through
        # the resolver which checks the catalog and converts names to hashIds.
        raw_zones = call.data.get("zones", [])
        # Coerce a single string to a one-element list (HA's developer-tools
        # YAML editor parses `zones: Pool` as a scalar, not a list).
        if isinstance(raw_zones, str):
            raw_zones = [raw_zones]
        catalog = coord.state_dict.get("zone_catalog")
        try:
            hash_ids = state_mod.resolve_zones(catalog, raw_zones)
        except ValueError as e:
            raise HomeAssistantError(str(e)) from e
        if not hash_ids:
            raise HomeAssistantError(
                "No valid zones provided. Use the lawn_mower 'Start' button "
                "to mow the default rotation."
            )
        await coord.cmd_start(zone_hash_ids=hash_ids)

    async def _handle_dock_cancel_task(call: ServiceCall) -> None:
        from homeassistant.exceptions import HomeAssistantError
        coord = _coord_for_device_id(call)
        if not coord:
            raise HomeAssistantError("No Lymow device targeted.")
        await coord.cmd_dock_cancel_task()

    async def _handle_cancel_task(call: ServiceCall) -> None:
        from homeassistant.exceptions import HomeAssistantError
        coord = _coord_for_device_id(call)
        if not coord:
            raise HomeAssistantError("No Lymow device targeted.")
        await coord.cmd_force_reinit()

    # Schemas validate `call.data` only. The device_id arrives via call.target
    # (per services.yaml `target:` block) and is read inside the handler via
    # _coord_for_device_id. Schemas also accept device_id in data for
    # backwards-compat with older call styles.
    hass.services.async_register(
        DOMAIN,
        "start_zones",
        _handle_start_zones,
        schema=vol.Schema(
            {
                vol.Optional("device_id"): vol.Any(str, [str]),
                vol.Required("zones"): vol.Any(str, [str]),
            }
        ),
    )
    hass.services.async_register(
        DOMAIN,
        "dock_cancel_task",
        _handle_dock_cancel_task,
        schema=vol.Schema({vol.Optional("device_id"): vol.Any(str, [str])}),
    )
    hass.services.async_register(
        DOMAIN,
        "cancel_task",
        _handle_cancel_task,
        schema=vol.Schema({vol.Optional("device_id"): vol.Any(str, [str])}),
    )
