"""
Lymow async API client.
SigV4 signing implemented without boto3 (only aiohttp required).
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import urllib.parse
from datetime import UTC, datetime, timedelta
from typing import Any

import aiohttp

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
# Cognito auth (pure HTTP, no boto3)
# ─────────────────────────────────────────────

class CognitoAuth:
    def __init__(self, region: str, session: aiohttp.ClientSession) -> None:
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

    # ── Cognito IDP ──────────────────────────

    async def _idp(self, target: str, body: dict) -> dict:
        url = f"https://cognito-idp.{self._region}.amazonaws.com/"
        headers = {
            "Content-Type": "application/x-amz-json-1.1",
            "X-Amz-Target": f"AWSCognitoIdentityProviderService.{target}",
        }
        async with self._session.post(url, json=body, headers=headers) as r:
            data = await r.json(content_type=None)
            if r.status != 200:
                raise LymowAuthError(f"Cognito {target}: {data}")
            return data

    async def login(self, email: str, password: str) -> None:
        _LOGGER.debug("Cognito login: %s @ %s", email, self._region)
        data = await self._idp("InitiateAuth", {
            "AuthFlow": "USER_PASSWORD_AUTH",
            "AuthParameters": {"USERNAME": email, "PASSWORD": password},
            "ClientId": self._cfg["client_id"],
        })
        self._store_tokens(data["AuthenticationResult"])

    async def refresh(self) -> None:
        if not self.refresh_token:
            raise LymowAuthError("No refresh token")
        data = await self._idp("InitiateAuth", {
            "AuthFlow": "REFRESH_TOKEN_AUTH",
            "AuthParameters": {"REFRESH_TOKEN": self.refresh_token},
            "ClientId": self._cfg["client_id"],
        })
        r = data["AuthenticationResult"]
        self.id_token    = r["IdToken"]
        self.access_token = r["AccessToken"]
        self._token_expiry = datetime.now(UTC) + timedelta(seconds=r.get("ExpiresIn", 3600))

    def _store_tokens(self, r: dict) -> None:
        self.id_token      = r["IdToken"]
        self.access_token  = r["AccessToken"]
        self.refresh_token = r.get("RefreshToken", self.refresh_token)
        self._token_expiry = datetime.now(UTC) + timedelta(seconds=r.get("ExpiresIn", 3600))

    # ── Cognito Identity ──────────────────────

    async def _identity(self, target: str, body: dict) -> dict:
        url = f"https://cognito-identity.{self._region}.amazonaws.com/"
        headers = {
            "Content-Type": "application/x-amz-json-1.1",
            "X-Amz-Target": f"AWSCognitoIdentityService.{target}",
        }
        async with self._session.post(url, json=body, headers=headers) as r:
            data = await r.json(content_type=None)
            if r.status != 200:
                raise LymowAuthError(f"Cognito Identity {target}: {data}")
            return data

    async def get_aws_credentials(self) -> None:
        if not self.id_token:
            raise LymowAuthError("No IdToken — call login() first")
        logins = {
            f"cognito-idp.{self._region}.amazonaws.com/{self._cfg['user_pool_id']}": self.id_token
        }
        identity = await self._identity("GetId", {
            "IdentityPoolId": self._cfg["identity_pool_id"],
            "Logins": logins,
        })
        creds_data = await self._identity("GetCredentialsForIdentity", {
            "IdentityId": identity["IdentityId"],
            "Logins": logins,
        })
        c = creds_data["Credentials"]
        self.access_key_id     = c["AccessKeyId"]
        self.secret_access_key = c["SecretKey"]
        self.session_token     = c["SessionToken"]
        exp = c["Expiration"]
        self._creds_expiry = (
            datetime.fromtimestamp(exp, UTC) if isinstance(exp, (int, float))
            else None
        )
        _LOGGER.debug("AWS credentials obtained, expire: %s", self._creds_expiry)

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
        if self._tokens_expiring():
            if self.refresh_token:
                await self.refresh()
            elif email and password:
                await self.login(email, password)
            else:
                raise LymowAuthError("Tokens expired and no credentials to re-login")
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
        }

    def from_dict(self, d: dict) -> None:
        self.id_token          = d.get("id_token")
        self.access_token      = d.get("access_token")
        self.refresh_token     = d.get("refresh_token")
        self.access_key_id     = d.get("access_key_id")
        self.secret_access_key = d.get("secret_access_key")
        self.session_token     = d.get("session_token")


# ─────────────────────────────────────────────
# Lymow REST + IoT client
# ─────────────────────────────────────────────

class LymowClient:
    def __init__(self, region: str, auth: CognitoAuth, session: aiohttp.ClientSession) -> None:
        self._region  = region
        self._auth    = auth
        self._session = session
        self._ep      = API_ENDPOINTS[region]

    # ── SigV4 helpers ────────────────────────

    def _hdrs(self, method: str, url: str, payload: bytes, service: str = "execute-api") -> dict:
        return _sigv4_headers(
            method=method, url=url, payload=payload,
            service=service, region=self._region,
            access_key=self._auth.access_key_id,
            secret_key=self._auth.secret_access_key,
            session_token=self._auth.session_token,
        )

    async def _api(self, api: str, path: str, method: str = "POST", payload: dict | None = None) -> Any:
        url  = self._ep[api] + path
        body = json.dumps(payload).encode() if payload else b""
        hdrs = {**self._hdrs(method, url, body), "Content-Type": "application/json"}
        async with self._session.request(method, url, data=body, headers=hdrs) as r:
            text = await r.text()
            if r.status >= 400:
                _LOGGER.warning("REST %s%s → %s: %s", api, path, r.status, text)
                return None
            try:
                return json.loads(text)
            except json.JSONDecodeError:
                return text

    async def _iot(self, method: str, path: str, payload: dict | None = None) -> Any:
        url  = f"https://{self._ep['iotDomain']}{path}"
        body = json.dumps(payload).encode() if payload else b""
        hdrs = {**self._hdrs(method, url, body, "iotdata"), "Content-Type": "application/json"}
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
        data = await self._api("deviceBindingApi", "/get-device-list", method="GET")
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            for k in ("data", "items", "devices", "list"):
                if isinstance(data.get(k), list):
                    return data[k]
        return []

    async def get_device_info(self, thing_name: str) -> dict:
        data = await self._api("deviceProfileApi", "/get-device-info", payload={"thingName": thing_name})
        return data or {}

    async def get_clean_history(self, thing_name: str, page: int = 1, size: int = 10) -> list[dict]:
        data = await self._api("deviceProfileApi", "/get-clean-history-collect",
                               payload={"thingName": thing_name, "page": page, "pageSize": size})
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            for k in ("data", "items", "list"):
                if isinstance(data.get(k), list):
                    return data[k]
        return []

    async def get_backup_map(self, thing_name: str) -> dict | None:
        return await self._api("s3Api", "/get-backup-map", payload={"thingName": thing_name})

    async def check_update(self, thing_name: str, current_fw: str) -> dict:
        return await self._api("checkUpdateApi", "/check-update",
                               payload={"thingName": thing_name, "currentVersion": current_fw}) or {}

    # ── IoT Shadow ────────────────────────────

    async def _shadow_reported(self, data: Any) -> dict:
        """Extract reported state from shadow response."""
        if not isinstance(data, dict):
            return {}
        return data.get("state", {}).get("reported", {})

    async def get_shadow(self, thing_name: str) -> dict:
        data = await self._iot("GET", f"/things/{thing_name}/shadow")
        return await self._shadow_reported(data)

    async def get_named_shadow(self, thing_name: str, shadow_name: str) -> dict:
        data = await self._iot("GET", f"/things/{thing_name}/shadow?name={shadow_name}")
        return await self._shadow_reported(data)

    async def update_shadow(self, thing_name: str, desired: dict) -> bool:
        result = await self._iot("POST", f"/things/{thing_name}/shadow",
                                 {"state": {"desired": desired}})
        return result is not None

    async def get_full_state(self, thing_name: str) -> dict:
        """
        Merge all known shadows (low-priority first, main shadow wins).
        Named shadows from APK: '{thing}-shadow', '{thing}-extended-shadow'
        """
        ext2 = await self.get_named_shadow(thing_name, f"{thing_name}-extended-shadow")
        ext1 = await self.get_named_shadow(thing_name, f"{thing_name}-shadow")
        main = await self.get_shadow(thing_name)

        merged: dict = {}
        merged.update(ext2)
        merged.update(ext1)
        merged.update(main)  # main shadow wins conflicts
        return merged

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
