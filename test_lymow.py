"""
test_lymow.py — Preliminary test
pip install aiohttp pycognito
"""

import asyncio
import json
import sys

import aiohttp

sys.path.insert(0, "./custom_components")
from lymow.api import CognitoAuth, LymowClient

EMAIL    = "tua@email.com"    # ← Fill with account
PASSWORD = "tuaPassword"
REGION   = "eu-west-1"


async def main():
    async with aiohttp.ClientSession() as session:

        # 1. Login
        print("=== LOGIN ===")
        auth = CognitoAuth(REGION, session)
        await auth.login(EMAIL, PASSWORD)
        await auth.get_aws_credentials()
        print(f"✓ AccessToken: {auth.access_token[:50]}...")
        print(f"✓ AWS creds expire: {auth._creds_expiry}")

        client = LymowClient(REGION, auth, session)

        # 2. Device list
        print("\n=== DEVICE LIST ===")
        devices = await client.get_device_list()
        print(json.dumps(devices, indent=2))

        if not devices:
            print("Nessun device — usa l'account collegato al robot")
            return

        # Take firs robot
        d = devices[0]
        thing = d.get("thingName") or d.get("thing_name") or d.get("deviceId")
        print(f"\n→ Robot: {thing}")

        # 3. Device info
        print("\n=== DEVICE INFO ===")
        info = await client.get_device_info(thing)
        print(json.dumps(info, indent=2))

        # 4. Device features
        print("\n=== DEVICE FEATURES ===")
        feat = await client.get_device_feature(thing)
        print(json.dumps(feat, indent=2))

        # 5. Shadow principale
        print("\n=== SHADOW (main) ===")
        shadow = await client.get_shadow(thing)
        print(json.dumps(shadow, indent=2))

        # 6. Named shadows
        print(f"\n=== SHADOW ({thing}-shadow) ===")
        s1 = await client.get_named_shadow(thing, f"{thing}-shadow")
        print(json.dumps(s1, indent=2))

        print(f"\n=== SHADOW ({thing}-extended-shadow) ===")
        s2 = await client.get_named_shadow(thing, f"{thing}-extended-shadow")
        print(json.dumps(s2, indent=2))

        # 7. Complete state merged
        print("\n=== FULL STATE (merged) ===")
        full = await client.get_full_state(thing)
        print(json.dumps(full, indent=2))

        # 8. History
        print("\n=== MOW HISTORY (ultimi 5) ===")
        history = await client.get_clean_history(thing, size=5)
        print(json.dumps(history, indent=2))

        # 9. OTA check
        print("\n=== CHECK UPDATE ===")
        upd = await client.check_update(thing)
        print(json.dumps(upd, indent=2))


asyncio.run(main())