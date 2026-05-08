"""Config flow for Lymow MQTT integration.

Steps:
1. Region picker
2. Auth method (native SRP vs federated OAuth)
3a. SRP form (email + password)
3b. OAuth paste form (URL or bare code from failed myapp:// redirect)
4. Device picker (one config entry per mower)

Reauth flow re-uses steps 3a/3b based on the original auth method.
"""
from __future__ import annotations

import logging
import urllib.parse
from typing import Any

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.data_entry_flow import FlowResult
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .auth import CognitoAuth, LymowAuthError
from .const import (
    CONF_EMAIL,
    CONF_PASSWORD,
    CONF_REGION,
    COGNITO_CONFIG,
    DOMAIN,
    REGIONS,
)
from .rest import LymowREST

_LOGGER = logging.getLogger(__name__)

# Internal flow keys
_CONF_AUTH_METHOD = "auth_method"
_CONF_OAUTH_CODE = "oauth_code"
_CONF_THING_NAME = "thing_name"

_AUTH_METHODS = {
    "srp": "Email + password",
    "oauth": "Sign in with Google or Apple (federated)",
}


def _build_oauth_url(region: str) -> str:
    """Build the Cognito hosted-UI link for federated login.

    The federated client only accepts myapp://callback/ as redirect_uri
    (verified empirically — see spec §6.1). The hosted-UI domain is
    region-specific and read from COGNITO_CONFIG.
    """
    cfg = COGNITO_CONFIG[region]
    qs = urllib.parse.urlencode({
        "client_id": cfg["client_id"],
        "response_type": "code",
        "scope": "openid aws.cognito.signin.user.admin",
        "redirect_uri": "myapp://callback/",
    })
    return f"https://{cfg['hosted_ui_domain']}/login?{qs}"


def _extract_oauth_code(pasted: str) -> str | None:
    """Extract the auth code from either a full myapp://callback URL or a bare code."""
    pasted = pasted.strip()
    if pasted.startswith("myapp://"):
        try:
            parsed = urllib.parse.urlparse(pasted)
            qs = urllib.parse.parse_qs(parsed.query)
            return qs.get("code", [None])[0]
        except Exception:
            return None
    if "code=" in pasted:
        try:
            qs = urllib.parse.parse_qs(pasted.split("?", 1)[1] if "?" in pasted else pasted)
            return qs.get("code", [None])[0]
        except Exception:
            return None
    # Otherwise treat the input as the bare code
    return pasted if pasted else None


class LymowConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Config flow for the Lymow MQTT integration."""

    VERSION = 1

    def __init__(self) -> None:
        self._region: str | None = None
        self._auth_method: str | None = None
        self._auth: CognitoAuth | None = None
        self._email: str | None = None
        self._password: str | None = None
        self._devices: list[dict] = []
        self._reauth_entry: config_entries.ConfigEntry | None = None

    # ── Step 1: Region ──────────────────────────────────────────

    async def async_step_user(self, user_input: dict | None = None) -> FlowResult:
        if user_input is not None:
            self._region = user_input[CONF_REGION]
            return await self.async_step_auth_method()
        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema({vol.Required(CONF_REGION): vol.In(REGIONS)}),
        )

    # ── Step 2: Auth method ────────────────────────────────────

    async def async_step_auth_method(self, user_input: dict | None = None) -> FlowResult:
        if user_input is not None:
            self._auth_method = user_input[_CONF_AUTH_METHOD]
            if self._auth_method == "srp":
                return await self.async_step_srp()
            return await self.async_step_oauth()
        return self.async_show_form(
            step_id="auth_method",
            data_schema=vol.Schema({vol.Required(_CONF_AUTH_METHOD): vol.In(_AUTH_METHODS)}),
        )

    # ── Step 3a: SRP form ──────────────────────────────────────

    async def async_step_srp(self, user_input: dict | None = None) -> FlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            session = async_get_clientsession(self.hass)
            auth = CognitoAuth(self._region, session)
            try:
                await auth.login_srp(user_input[CONF_EMAIL], user_input[CONF_PASSWORD])
                await auth.get_aws_credentials()
            except LymowAuthError as e:
                _LOGGER.warning("SRP login failed: %s", e)
                errors["base"] = "auth_failed"
            else:
                self._auth = auth
                self._email = user_input[CONF_EMAIL]
                self._password = user_input[CONF_PASSWORD]
                # If this is a reauth, update the existing entry instead of
                # creating a new one.
                if self._reauth_entry is not None:
                    return await self._async_finish_reauth()
                return await self.async_step_pick_device()
        return self.async_show_form(
            step_id="srp",
            data_schema=vol.Schema({
                vol.Required(CONF_EMAIL): str,
                vol.Required(CONF_PASSWORD): str,
            }),
            errors=errors,
        )

    # ── Step 3b: OAuth paste ───────────────────────────────────

    async def async_step_oauth(self, user_input: dict | None = None) -> FlowResult:
        errors: dict[str, str] = {}
        oauth_url = _build_oauth_url(self._region)

        if user_input is not None:
            code = _extract_oauth_code(user_input[_CONF_OAUTH_CODE])
            if not code:
                errors["base"] = "no_code_found"
            else:
                session = async_get_clientsession(self.hass)
                auth = CognitoAuth(self._region, session)
                try:
                    await auth.exchange_oauth_code(code)
                    await auth.get_aws_credentials()
                except LymowAuthError as e:
                    _LOGGER.warning("OAuth exchange failed: %s", e)
                    errors["base"] = "oauth_failed"
                else:
                    self._auth = auth
                    if self._reauth_entry is not None:
                        return await self._async_finish_reauth()
                    return await self.async_step_pick_device()

        return self.async_show_form(
            step_id="oauth",
            data_schema=vol.Schema({vol.Required(_CONF_OAUTH_CODE): str}),
            description_placeholders={"oauth_url": oauth_url},
            errors=errors,
        )

    # ── Step 4: Device picker ──────────────────────────────────

    async def async_step_pick_device(self, user_input: dict | None = None) -> FlowResult:
        if not self._devices:
            session = async_get_clientsession(self.hass)
            rest = LymowREST(self._region, self._auth, session)
            try:
                self._devices = await rest.get_device_list()
            except Exception as e:
                _LOGGER.exception("Failed to list devices: %s", e)
                return self.async_abort(reason="device_list_failed")
            if not self._devices:
                return self.async_abort(reason="no_devices")

        if user_input is not None:
            thing_name = user_input[_CONF_THING_NAME]
            await self.async_set_unique_id(thing_name)
            self._abort_if_unique_id_configured()
            data = {
                CONF_REGION: self._region,
                _CONF_THING_NAME: thing_name,
                _CONF_AUTH_METHOD: self._auth_method,
                **self._auth.to_dict(),
            }
            if self._auth_method == "srp":
                data[CONF_EMAIL] = self._email
                data[CONF_PASSWORD] = self._password
            return self.async_create_entry(
                title=f"Lymow {thing_name[-6:]}",
                data=data,
            )

        choices = {
            d.get("deviceThingName") or d.get("thingName") or d.get("name"):
            d.get("deviceName") or d.get("displayName") or d.get("name") or "(unnamed)"
            for d in self._devices
        }
        return self.async_show_form(
            step_id="pick_device",
            data_schema=vol.Schema({vol.Required(_CONF_THING_NAME): vol.In(choices)}),
        )

    # ── Reauth ──────────────────────────────────────────────────

    async def async_step_reauth(self, entry_data: dict[str, Any]) -> FlowResult:
        self._reauth_entry = self.hass.config_entries.async_get_entry(self.context["entry_id"])
        if self._reauth_entry:
            self._region = self._reauth_entry.data[CONF_REGION]
            self._auth_method = self._reauth_entry.data.get(_CONF_AUTH_METHOD, "srp")
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(self, user_input: dict | None = None) -> FlowResult:
        if self._auth_method == "srp":
            return await self.async_step_srp()
        return await self.async_step_oauth()

    async def _async_finish_reauth(self) -> FlowResult:
        """Update the existing entry's stored auth and finish the reauth flow."""
        assert self._reauth_entry is not None
        assert self._auth is not None
        new_data = {
            **self._reauth_entry.data,
            CONF_REGION: self._region,
            _CONF_AUTH_METHOD: self._auth_method,
            **self._auth.to_dict(),
        }
        if self._auth_method == "srp":
            new_data[CONF_EMAIL] = self._email
            new_data[CONF_PASSWORD] = self._password
        self.hass.config_entries.async_update_entry(self._reauth_entry, data=new_data)
        await self.hass.config_entries.async_reload(self._reauth_entry.entry_id)
        return self.async_abort(reason="reauth_successful")
