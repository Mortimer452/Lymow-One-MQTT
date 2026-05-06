"""
Lymow async API client.

Auth:    pycognito (handles USER_SRP_AUTH — the only flow enabled on the Lymow Cognito client)
REST:    aiohttp + SigV4 (no boto3 required at runtime)
IoT:     AWS IoT Data HTTPS (SigV4 signed)
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import urllib.parse
from datetime import UTC, datetime, timedelta
from typing import Any

import aiohttp

# pycognito handles the SRP challenge exchange transparently.
# It uses boto3 under the hood only for the auth calls (not for IoT/API Gateway).
try:
    from pycognito import Cognito as _PyCognito
    _HAS_PYCOGNITO = True
except ImportError:
    _HAS_PYCOGNITO = False

from .const import API_ENDPOINTS, COGNITO_CONFIG

_LOGGER = logging.getLogger(__name__)


# ─────────────────────────────────────────────
# SigV4 (no external deps)
# ─────────────────────────────────────────────

def _sign(key: bytes, msg: str) -> bytes:
    return hmac.new(key, msg.encode(), hashlib.sha256).digest()

def _signing_key(secret: str, date: str, region: str, service: str) -> bytes:
    k = _sign(("AWS4" + secret).encode(), date)
    k = _sign(k, region)
    k = _sign(k, service)
    return _sign(k, "aws4_request")

def _sigv4_headers(
    method: str,
    url: str,
    payload: bytes,
    service: str,
    region: str,
    access_key: str,
    secret_key: str,
    session_token: str | None = None,
) -> dict[str, str]:
    parsed       = urllib.parse.urlparse(url)
    host         = parsed.netloc
    canonical_uri = parsed.path or "/"
    canonical_qs  = parsed.query

    now        = datetime.now(UTC)
    amz_date   = now.strftime("%Y%m%dT%H%M%SZ")
    date_stamp = now.strftime("%Y%m%d")
    body_hash  = hashlib.sha256(payload).hexdigest()

    hdrs: dict[str, str] = {
        "host":                  host,
        "x-amz-date":            amz_date,
        "x-amz-content-sha256":  body_hash,
    }
    if session_token:
        hdrs["x-amz-security-token"] = session_token

    signed_list     = sorted(hdrs)
    canonical_hdrs  = "".join(f"{k}:{hdrs[k]}\n" for k in signed_list)
    signed_headers  = ";".join(signed_list)

    canonical_req = "\n".join([
        method.upper(), canonical_uri, canonical_qs,
        canonical_hdrs, signed_headers, body_hash,
    ])

    scope         = f"{date_stamp}/{region}/{service}/aws4_request"
    string_to_sign = "\n".join([
        "AWS4-HMAC-SHA256", amz_date, scope,
        hashlib.sha256(canonical_req.encode()).hexdigest(),
    ])

    sig = hmac.new(
        _signing_key(secret_key, date_stamp, region, service),
        string_to_sign.encode(),
        hashlib.sha256,
    ).hexdigest()

    return {
        "Authorization": (
            f"AWS4-HMAC-SHA256 Credential={access_key}/{scope}, "
            f"SignedHeaders={signed_headers}, Signature={sig}"
        ),
        "x-amz-date":            amz_date,
        "x-amz-content-sha256":  body_hash,
        **({"x-amz-security-token": session_token} if session_token else {}),
    }


# ─────────────────────────────────────────────
# Cognito auth via pycognito (USER_SRP_AUTH)
# ─────────────────────────────────────────────

class CognitoAuth:
    """
    Handles Cognito SRP login + Identity Pool credential exchange.

    Uses pycognito for the SRP challenge (USER_SRP_AUTH — the only flow
    enabled on the Lymow Cognito app client).
    The Identity Pool exchange and everything else is done with plain aiohttp.
    """

    def __init__(self, region: str, session: aiohttp.ClientSession) -> None:
        if not _HAS_PYCOGNITO:
            raise LymowAuthError(
                "pycognito is required: pip install pycognito"
            )
        self._region  = region
        self._session = session
        self._cfg     = COGNITO_CONFIG[region]

        self.id_token:      str | None = None
        self.access_token:  str | None = None
        self.refresh_token: str | None = None
        self._token_expiry: datetime | None = None

        self.access_key_id:     str | None = None
        self.secret_access_key: str | None = None
        self.session_token:     str | None = None
        self._creds_expiry:     datetime | None = None

        self._email:    str | None = None
        self._password: str | None = None

    # ── SRP login via pycognito ──────────────

    async def login(self, email: str, password: str) -> None:
        """
        Authenticate with USER_SRP_AUTH via pycognito.
        pycognito calls are blocking/sync — we run them in a thread executor
        so we don't block the HA event loop.
        """
        _LOGGER.debug("SRP login: %s @ %s", email, self._region)

        def _do_srp() -> tuple[str, str, str]:
            u = _PyCognito(
                user_pool_id=self._cfg["user_pool_id"],
                client_id=self._cfg["client_id"],
                user_pool_region=self._region,
                username=email,
            )
            u.authenticate(password=password)
            return u.id_token, u.access_token, u.refresh_token

        try:
            loop = asyncio.get_event_loop()
            id_t, acc_t, ref_t = await loop.run_in_executor(None, _do_srp)
        except Exception as e:
            raise LymowAuthError(f"SRP login failed: {e}") from e

        self.id_token      = id_t
        self.access_token  = acc_t
        self.refresh_token = ref_t
        self._token_expiry = datetime.now(UTC) + timedelta(hours=1)
        self._email        = email
        self._password     = password
        _LOGGER.debug("SRP login OK")

    async def refresh(self) -> None:
        """Refresh tokens using the stored refresh token."""
        if not self.refresh_token or not self._email:
            raise LymowAuthError("No refresh token — re-login required")
        _LOGGER.debug("Refreshing Cognito tokens")

        def _do_refresh() -> tuple[str, str]:
            u = _PyCognito(
                user_pool_id=self._cfg["user_pool_id"],
                client_id=self._cfg["client_id"],
                user_pool_region=self._region,
                username=self._email,
                id_token=self.id_token,
                refresh_token=self.refresh_token,
                access_token=self.access_token,
            )
            u.renew_access_token()
            return u.id_token, u.access_token

        try:
            loop = asyncio.get_event_loop()
            id_t, acc_t = await loop.run_in_executor(None, _do_refresh)
        except Exception as e:
            raise LymowAuthError(f"Token refresh failed: {e}") from e

        self.id_token     = id_t
        self.access_token = acc_t
        self._token_expiry = datetime.now(UTC) + timedelta(hours=1)

    # ── Identity Pool → AWS credentials ──────

    async def get_aws_credentials(self) -> None:
        """Exchange IdToken for temporary AWS credentials (SigV4 signing)."""
        if not self.id_token:
            raise LymowAuthError("No IdToken — call login() first")

        logins = {
            f"cognito-idp.{self._region}.amazonaws.com/{self._cfg['user_pool_id']}": self.id_token
        }

        # Step 1: get Identity ID
        id_url = f"https://cognito-identity.{self._region}.amazonaws.com/"
        id_hdrs = {
            "Content-Type": "application/x-amz-json-1.1",
            "X-Amz-Target": "AWSCognitoIdentityService.GetId",
        }
        async with self._session.post(id_url, json={
            "IdentityPoolId": self._cfg["identity_pool_id"],
            "Logins": logins,
        }, headers=id_hdrs) as r:
            data = await r.json(content_type=None)
            if r.status != 200:
                raise LymowAuthError(f"GetId failed: {data}")
            identity_id = data["IdentityId"]

        # Step 2: get credentials for that identity
        async with self._session.post(id_url, json={
            "IdentityId": identity_id,
            "Logins": logins,
        }, headers={**id_hdrs, "X-Amz-Target": "AWSCognitoIdentityService.GetCredentialsForIdentity"}) as r:
            data = await r.json(content_type=None)
            if r.status != 200:
                raise LymowAuthError(f"GetCredentialsForIdentity failed: {data}")
            c = data["Credentials"]

        self.access_key_id     = c["AccessKeyId"]
        self.secret_access_key = c["SecretKey"]
        self.session_token     = c["SessionToken"]
        exp = c["Expiration"]
        self._creds_expiry = (
            datetime.fromtimestamp(exp, UTC) if isinstance(exp, (int, float)) else None
        )
        _LOGGER.debug("AWS credentials OK, expire: %s", self._creds_expiry)

    # ── Token lifecycle ───────────────────────

    def _tokens_expiring(self) -> bool:
        if not self._token_expiry:
            return True
        return datetime.now(UTC) >= (self._token_expiry - timedelta(minutes=5))

    def _creds_expiring(self) -> bool:
        if not self._creds_expiry:
            return True
        return datetime.now(UTC) >= (self._creds_expiry - timedelta(minutes=10))

    async def ensure_valid(self, email: str | None = None, password: str | None = None) -> None:
        """Refresh tokens/credentials proactively before they expire."""
        _email    = email    or self._email
        _password = password or self._password

        if self._tokens_expiring():
            if self.refresh_token:
                try:
                    await self.refresh()
                except LymowAuthError:
                    # Refresh failed — fall back to full re-login
                    if _email and _password:
                        await self.login(_email, _password)
                    else:
                        raise
            elif _email and _password:
                await self.login(_email, _password)
            else:
                raise LymowAuthError("Tokens expired and no credentials available")

        if self._creds_expiring():
            await self.get_aws_credentials()

    # ── Serialization ─────────────────────────

    def to_dict(self) -> dict:
        return {
            "id_token":          self.id_token,
            "access_token":      self.access_token,
            "refresh_token":     self.refresh_token,
            "access_key_id":     self.access_key_id,
            "secret_access_key": self.secret_access_key,
            "session_token":     self.session_token,
            "_email":            self._email,
        }

    def from_dict(self, d: dict) -> None:
        self.id_token          = d.get("id_token")
        self.access_token      = d.get("access_token")
        self.refresh_token     = d.get("refresh_token")
        self.access_key_id     = d.get("access_key_id")
        self.secret_access_key = d.get("secret_access_key")
        self.session_token     = d.get("session_token")
        self._email            = d.get("_email")


# ─────────────────────────────────────────────
# Lymow REST + IoT client
# ─────────────────────────────────────────────

class LymowClient:
    def __init__(self, region: str, auth: CognitoAuth, session: aiohttp.ClientSession) -> None:
        self._region  = region
        self._auth    = auth
        self._session = session
        self._ep      = API_ENDPOINTS[region]

    # ── Auth helpers ─────────────────────────

    def _rest_headers(self, extra: dict | None = None) -> dict:
        """
        REST API Gateway uses Cognito AccessToken in Authorization header.
        Verified from APK source: Authorization = getAuthSession().accessToken
        No Bearer prefix, no SigV4.
        """
        h = {
            "Content-Type":    "application/json",
            "Accept-Encoding": "gzip, deflate, br",
            "Authorization":   self._auth.access_token,
        }
        if extra:
            h.update(extra)
        return h

    def _iot_headers(self, method: str, url: str, payload: bytes) -> dict:
        """IoT Data endpoint uses SigV4 with Identity Pool credentials."""
        return {
            **_sigv4_headers(
                method=method, url=url, payload=payload,
                service="iotdata", region=self._region,
                access_key=self._auth.access_key_id,
                secret_key=self._auth.secret_access_key,
                session_token=self._auth.session_token,
            ),
            "Content-Type": "application/json",
        }

    # ── REST API calls ────────────────────────

    async def _api_get(self, api: str, path: str) -> Any:
        url = self._ep[api] + path
        async with self._session.get(url, headers=self._rest_headers()) as r:
            text = await r.text()
            if r.status >= 400:
                _LOGGER.warning("GET %s%s → %s: %s", api, path, r.status, text)
                return None
            try:
                return json.loads(text)
            except json.JSONDecodeError:
                return text

    async def _api_post(self, api: str, path: str, payload: dict) -> Any:
        url  = self._ep[api] + path
        body = json.dumps(payload).encode()
        async with self._session.post(url, data=body, headers=self._rest_headers()) as r:
            text = await r.text()
            if r.status >= 400:
                _LOGGER.warning("POST %s%s → %s: %s", api, path, r.status, text)
                return None
            try:
                return json.loads(text)
            except json.JSONDecodeError:
                return text

    # ── IoT Shadow ────────────────────────────

    async def _iot(self, method: str, path: str, payload: dict | None = None) -> Any:
        url  = f"https://{self._ep['iotDomain']}{path}"
        body = json.dumps(payload).encode() if payload else b""
        hdrs = self._iot_headers(method, url, body)
        async with self._session.request(method, url, data=body, headers=hdrs) as r:
            text = await r.text()
            if r.status == 404:
                return None
            if r.status >= 400:
                _LOGGER.warning("IoT %s %s → %s: %s", method, path, r.status, text)
                return None
            try:
                return json.loads(text)
            except json.JSONDecodeError:
                return text

    # ── Device management ─────────────────────

    async def get_device_list(self) -> list[dict]:
        """
        Real endpoint from APK source:
        apiGet("deviceBindingApi", "/device-list-query?p=validation")
        Response is a JSON array of device objects with 'thingName' field.
        """
        data = await self._api_get("deviceBindingApi", "/device-list-query?p=validation")
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            for k in ("data", "items", "devices", "list"):
                if isinstance(data.get(k), list):
                    return data[k]
        return []

    async def get_device_info(self, thing_name: str) -> dict:
        """GET /get-device-info with deviceThingName as query param."""
        data = await self._api_get(
            "deviceProfileApi", f"/get-device-info?deviceThingName={thing_name}"
        )
        return data or {}

    async def get_device_feature(self, thing_name: str) -> dict:
        data = await self._api_get(
            "deviceProfileApi", f"/get-device-feature?deviceThingName={thing_name}"
        )
        return data or {}

    async def get_clean_history(self, thing_name: str, page: int = 1, size: int = 10) -> list[dict]:
        # Note: this endpoint lives on s3Api, NOT deviceProfileApi (verified from APK decompile).
        # Returns {clean_history: [...], page, has_more, total_records, clean_summary}.
        data = await self._api_get(
            "s3Api",
            f"/get-clean-history-collect?deviceThingName={thing_name}&page={page}&pageSize={size}",
        )
        if isinstance(data, dict):
            if isinstance(data.get("clean_history"), list):
                return data["clean_history"]
            for k in ("data", "items", "list"):
                if isinstance(data.get(k), list):
                    return data[k]
        if isinstance(data, list):
            return data
        return []

    async def get_backup_map(self, thing_name: str) -> dict | None:
        """Fetch saved map from S3 via proxy API."""
        return await self._api_get("s3Api", f"/get-backup-map?deviceThingName={thing_name}")

    async def check_update(self, thing_name: str) -> dict:
        """Check for OTA firmware updates.

        APK source uses GET with deviceThingName query string, NOT POST with body.
        """
        data = await self._api_get(
            "checkUpdateApi", f"/check-update?deviceThingName={thing_name}"
        )
        return data or {}

    async def check_force_update(self) -> dict:
        """Check if app/firmware force update is required."""
        data = await self._api_post("deviceBindingApi", "/check-app-force-update", {})
        return data or {}

    # ── IoT Shadow state + commands ───────────

    @staticmethod
    def _reported(data: Any) -> dict:
        if not isinstance(data, dict):
            return {}
        return data.get("state", {}).get("reported", {})

    async def get_shadow(self, thing_name: str) -> dict:
        return self._reported(await self._iot("GET", f"/things/{thing_name}/shadow"))

    async def get_named_shadow(self, thing_name: str, shadow_name: str) -> dict:
        return self._reported(
            await self._iot("GET", f"/things/{thing_name}/shadow?name={shadow_name}")
        )

    async def update_shadow(self, thing_name: str, desired: dict) -> bool:
        result = await self._iot(
            "POST", f"/things/{thing_name}/shadow", {"state": {"desired": desired}}
        )
        return result is not None

    async def get_full_state(self, thing_name: str) -> dict:
        """
        Merge all known shadows. Named shadows from APK:
        '{thing}-shadow' and '{thing}-extended-shadow'
        Main shadow wins on key conflicts.
        """
        ext2 = await self.get_named_shadow(thing_name, f"{thing_name}-extended-shadow")
        ext1 = await self.get_named_shadow(thing_name, f"{thing_name}-shadow")
        main = await self.get_shadow(thing_name)
        return {**ext2, **ext1, **main}

    # ── Robot commands ────────────────────────
    # workStatus values sent as integers (RobotStatus enum)

    async def cmd_start_mow(self, thing_name: str, zone_ids: list[str] | None = None) -> bool:
        desired: dict = {"workStatus": 2}  # ROBOT_STATUS_CLEANING
        if zone_ids:
            desired["cleanZoneIds"] = zone_ids     # real field name
            desired["goZoneHashIds"] = zone_ids
        return await self.update_shadow(thing_name, desired)

    async def cmd_pause(self, thing_name: str) -> bool:
        return await self.update_shadow(thing_name, {"workStatus": 3})  # ROBOT_STATUS_PAUSE

    async def cmd_resume(self, thing_name: str) -> bool:
        return await self.update_shadow(thing_name, {"workStatus": 8})  # ROBOT_STATUS_RESUME

    async def cmd_dock(self, thing_name: str) -> bool:
        return await self.update_shadow(thing_name, {"workStatus": 4})  # ROBOT_STATUS_DOCKING

    async def cmd_stop(self, thing_name: str) -> bool:
        return await self.update_shadow(thing_name, {"workStatus": 1})  # ROBOT_STATUS_WAITING

    async def cmd_set_blade_height(self, thing_name: str, height_mm: int) -> bool:
        # Both fields set for compatibility (BLE side uses cutHeight, cloud uses cuttingHeight)
        return await self.update_shadow(thing_name, {
            "cutHeight":     height_mm,
            "cuttingHeight": height_mm,
        })

    async def cmd_set_clean_mode(self, thing_name: str, mode: str) -> bool:
        """mode: one of CLEAN_MODE_* constants"""
        return await self.update_shadow(thing_name, {"cleanMode": mode})

    async def cmd_set_schedule(self, thing_name: str, schedules: list[dict]) -> bool:
        return await self.update_shadow(thing_name, {"schedules": schedules})


# ─────────────────────────────────────────────
# Exceptions
# ─────────────────────────────────────────────

class LymowError(Exception):
    """Base Lymow error."""

class LymowAuthError(LymowError):
    """Authentication error."""

class LymowAPIError(LymowError):
    """API call error."""
