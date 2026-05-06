"""
test_lymow_oauth.py - Read-only Lymow API smoke test using OAuth tokens
acquired via exchange_code.py.

Loads tokens from tokens.json, injects them into CognitoAuth (skipping
SRP login, which is unavailable for federated/Google-linked accounts),
then exercises every read-only endpoint.

Sends NO commands to the mower.

Note on shadow access:
  AWS IoT Shadow REST endpoints (/things/.../shadow) return 403 for
  this app's Identity Pool role, even though SigV4 is signed correctly.
  The Lymow Android app appears to receive shadow data over MQTT-over-WSS
  rather than via the Shadow REST API. Until that path is implemented,
  the shadow / get_full_state calls below will return empty dicts.
"""

import asyncio
import json
import sys
from datetime import UTC, datetime, timedelta

import aiohttp

sys.path.insert(0, "./custom_components")
from lymow.api import CognitoAuth, LymowClient  # noqa: E402


async def main() -> None:
    with open("tokens.json") as f:
        tokens = json.load(f)

    region = tokens.get("region", "us-east-2")

    async with aiohttp.ClientSession() as session:
        # CognitoAuth with pre-acquired tokens (skip SRP).
        auth = CognitoAuth(region, session)
        auth.id_token      = tokens["id_token"]
        auth.access_token  = tokens["access_token"]
        auth.refresh_token = tokens["refresh_token"]
        auth._token_expiry = (
            datetime.now(UTC) + timedelta(seconds=tokens.get("expires_in", 3600))
        )

        print("=== EXCHANGE TOKENS FOR AWS CREDENTIALS ===")
        await auth.get_aws_credentials()
        print(f"AccessKeyId acquired: {auth.access_key_id[:12]}...")
        print(f"AWS creds expire at:  {auth._creds_expiry}")

        client = LymowClient(region, auth, session)

        print("\n=== DEVICE LIST ===")
        devices = await client.get_device_list()
        print(json.dumps(devices, indent=2, default=str))

        if not devices:
            print("No devices on this account.")
            return

        d = devices[0]
        # Real field name is 'deviceThingName'; fall back to legacy aliases.
        thing = (
            d.get("deviceThingName")
            or d.get("thingName")
            or d.get("thing_name")
            or d.get("deviceId")
        )
        print(f"\nUsing first device: {thing}")

        print("\n=== DEVICE INFO ===")
        print(json.dumps(await client.get_device_info(thing), indent=2, default=str))

        print("\n=== DEVICE FEATURES ===")
        print(json.dumps(await client.get_device_feature(thing), indent=2, default=str))

        print("\n=== SHADOW (main)  [expected 403 - see module docstring] ===")
        print(json.dumps(await client.get_shadow(thing), indent=2, default=str))

        print(f"\n=== SHADOW ({thing}-shadow)  [expected 403] ===")
        print(json.dumps(
            await client.get_named_shadow(thing, f"{thing}-shadow"),
            indent=2, default=str,
        ))

        print(f"\n=== SHADOW ({thing}-extended-shadow)  [expected 403] ===")
        print(json.dumps(
            await client.get_named_shadow(thing, f"{thing}-extended-shadow"),
            indent=2, default=str,
        ))

        print("\n=== MOW HISTORY (last 5) ===")
        print(json.dumps(
            await client.get_clean_history(thing, size=5),
            indent=2, default=str,
        ))

        print("\n=== CHECK UPDATE ===")
        print(json.dumps(await client.check_update(thing), indent=2, default=str))


if __name__ == "__main__":
    asyncio.run(main())
