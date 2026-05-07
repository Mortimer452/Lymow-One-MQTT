"""Config flow for Lymow."""

from __future__ import annotations

import logging
from typing import Any

import aiohttp
import voluptuous as vol

from homeassistant import config_entries
from homeassistant.data_entry_flow import FlowResult
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import CognitoAuth, LymowAuthError, LymowClient
from .const import CONF_EMAIL, CONF_PASSWORD, CONF_REGION, DOMAIN, REGIONS

_LOGGER = logging.getLogger(__name__)


class LymowConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Config flow for Lymow integration."""

    VERSION = 1

    def __init__(self) -> None:
        self._auth:     CognitoAuth | None = None
        self._devices:  list[dict]         = []
        self._email     = ""
        self._password  = ""
        self._region    = "eu-west-1"

    async def async_step_user(self, user_input: dict | None = None) -> FlowResult:
        errors: dict[str, str] = {}

        if user_input:
            email    = user_input[CONF_EMAIL]
            password = user_input[CONF_PASSWORD]
            region   = user_input[CONF_REGION]

            try:
                session = async_get_clientsession(self.hass)
                auth    = CognitoAuth(region, session)
                await auth.login(email, password)
                await auth.get_aws_credentials()

                client  = LymowClient(region, auth, session)
                devices = await client.get_device_list()

                self._auth     = auth
                self._devices  = devices
                self._email    = email
                self._password = password
                self._region   = region

            except LymowAuthError:
                errors["base"] = "invalid_auth"
            except aiohttp.ClientConnectionError:
                errors["base"] = "cannot_connect"
            except Exception:
                _LOGGER.exception("Unexpected error during Lymow auth")
                errors["base"] = "unknown"
            else:
                if not self._devices:
                    return self.async_abort(reason="no_devices")
                if len(self._devices) == 1:
                    return await self._create_entry(self._devices[0])
                return await self.async_step_select_device()

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema({
                vol.Required(CONF_EMAIL):    str,
                vol.Required(CONF_PASSWORD): str,
                vol.Required(CONF_REGION, default="eu-west-1"): vol.In(REGIONS),
            }),
            errors=errors,
        )

    async def async_step_select_device(self, user_input: dict | None = None) -> FlowResult:
        if user_input:
            thing = user_input["thing_name"]
            device = next((d for d in self._devices if _thing_name(d) == thing), self._devices[0])
            return await self._create_entry(device)

        choices = {_thing_name(d): _label(d) for d in self._devices}
        return self.async_show_form(
            step_id="select_device",
            data_schema=vol.Schema({vol.Required("thing_name"): vol.In(choices)}),
        )

    async def _create_entry(self, device: dict) -> FlowResult:
        thing = _thing_name(device)
        await self.async_set_unique_id(f"{DOMAIN}_{thing}")
        self._abort_if_unique_id_configured()

        return self.async_create_entry(
            title=_label(device),
            data={
                CONF_EMAIL:      self._email,
                CONF_PASSWORD:   self._password,
                CONF_REGION:     self._region,
                "thing_name":    thing,
                "device_name":   _label(device),
                "refresh_token": self._auth.refresh_token,
                "id_token":      self._auth.id_token,
            },
        )

    @staticmethod
    def async_get_options_flow(entry: config_entries.ConfigEntry) -> LymowOptionsFlow:
        return LymowOptionsFlow(entry)


class LymowOptionsFlow(config_entries.OptionsFlow):
    def __init__(self, entry: config_entries.ConfigEntry) -> None:
        self._entry = entry

    async def async_step_init(self, user_input: dict | None = None) -> FlowResult:
        from .const import DEFAULT_SCAN_INTERVAL
        if user_input:
            return self.async_create_entry(title="", data=user_input)
        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema({
                vol.Required(
                    "scan_interval",
                    default=self._entry.options.get("scan_interval", DEFAULT_SCAN_INTERVAL),
                ): vol.All(int, vol.Range(min=10, max=300)),
            }),
        )


def _thing_name(d: dict) -> str:
    return d.get("deviceThingName") or d.get("thingName") or d.get("thing_name") or d.get("deviceId") or d.get("id") or str(d)

def _label(d: dict) -> str:
    n = d.get("deviceName") or d.get("name") or d.get("alias") or _thing_name(d)
    return f"Lymow {n}"
