"""Lymow REST API client. Uses Cognito access_token in Authorization header.

Endpoints documented in arch.md §4a. The integration only uses a small
subset:
- /device-list-query (config flow only)
- /get-device-info (config flow + 15-min online poll)
- /get-clean-history-collect (one-time backfill at install, optional)
"""
from __future__ import annotations

import json
import logging
from typing import Any

import aiohttp

from .auth import CognitoAuth
from .const import API_ENDPOINTS

_LOGGER = logging.getLogger(__name__)


class LymowREST:
    def __init__(
        self,
        region: str,
        auth: CognitoAuth,
        session: aiohttp.ClientSession,
    ) -> None:
        self._region = region
        self._auth = auth
        self._session = session
        self._ep = API_ENDPOINTS[region]

    def _headers(self) -> dict:
        return {
            "Content-Type": "application/json",
            "Accept-Encoding": "gzip, deflate, br",
            "Authorization": self._auth.access_token,
        }

    async def _get(self, api: str, path: str) -> Any:
        await self._auth.ensure_valid()
        url = self._ep[api] + path
        async with self._session.get(url, headers=self._headers()) as r:
            text = await r.text()
            if r.status >= 400:
                _LOGGER.warning("GET %s%s -> %s: %s", api, path, r.status, text)
                return None
            try:
                return json.loads(text)
            except json.JSONDecodeError:
                return text

    async def get_device_list(self) -> list[dict]:
        """List devices bound to this account."""
        data = await self._get("deviceBindingApi", "/device-list-query?p=validation")
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            for k in ("data", "items", "devices", "list"):
                if isinstance(data.get(k), list):
                    return data[k]
        return []

    async def get_device_info(self, thing_name: str) -> dict:
        """IP, MAC, fw versions, deviceState (online/offline)."""
        data = await self._get(
            "deviceProfileApi",
            f"/get-device-info?deviceThingName={thing_name}",
        )
        return data or {}

    async def get_clean_history(
        self, thing_name: str, page: int = 0, size: int = 10
    ) -> list[dict]:
        """Mow-history records. page is 0-indexed (page=0 is most recent)."""
        data = await self._get(
            "s3Api",
            f"/get-clean-history-collect?deviceThingName={thing_name}&page={page}&pageSize={size}",
        )
        if isinstance(data, dict) and isinstance(data.get("clean_history"), list):
            return data["clean_history"]
        if isinstance(data, list):
            return data
        return []
