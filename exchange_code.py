"""
exchange_code.py - Bootstrap Cognito OAuth tokens for accounts that use
federated identity (Sign in with Google / Apple).

Use this once when your Lymow account was created via "Sign in with Google"
(or Apple) and therefore has no password for SRP login.

Steps:

1. Open this URL in a browser, log in with Google/Apple, and accept access.
   When the browser fails to navigate to "myapp://callback/?code=..."
   (because the OS doesn't know that scheme), copy the auth code from the
   address bar.

   For us-east-2:
     https://us-auth.lymow.com/login?client_id=3ftv5jumkv375hic8dpdqodj8n
       &response_type=code&scope=openid+aws.cognito.signin.user.admin
       &redirect_uri=myapp%3A%2F%2Fcallback%2F

   The OAuth domain differs per region - see HOSTED_UI_DOMAIN below.

2. Paste the code into AUTH_CODE and set REGION, then run this script.

3. tokens.json gets written. Use it with test_lymow_oauth.py or any
   client that knows how to inject pre-acquired tokens into CognitoAuth.

Auth codes expire ~5 minutes after the OAuth flow completes - move quickly.
"""

import json
import sys
import urllib.error
import urllib.parse
import urllib.request

sys.path.insert(0, "./custom_components")
from lymow.const import COGNITO_CONFIG  # noqa: E402

# --- EDIT THESE ----------------------------------------------------------
AUTH_CODE = "PASTE_AUTH_CODE_HERE"
REGION    = "us-east-2"   # us-east-2 | eu-west-1 | ap-southeast-2 | ap-east-1
# -------------------------------------------------------------------------

# Per-region Cognito Hosted UI domains, extracted from the APK decompile.
# Most regions use a custom CloudFront-fronted domain; ap-east-1 uses the
# default Cognito-hosted form.
HOSTED_UI_DOMAIN = {
    "us-east-2":      "us-auth.lymow.com",
    "eu-west-1":      "eu-auth.lymow.com",
    "ap-southeast-2": "ap-auth.lymow.com",
    "ap-east-1":      "lymow.auth.ap-east-1.amazoncognito.com",
}

REDIRECT_URI = "myapp://callback/"


def main() -> None:
    if AUTH_CODE.startswith("PASTE"):
        print("ERROR: Edit AUTH_CODE in this script first.")
        print("\nTo get an auth code:")
        domain    = HOSTED_UI_DOMAIN[REGION]
        client_id = COGNITO_CONFIG[REGION]["client_id"]
        url = (
            f"https://{domain}/login"
            f"?client_id={client_id}"
            f"&response_type=code"
            f"&scope=openid+aws.cognito.signin.user.admin"
            f"&redirect_uri={urllib.parse.quote(REDIRECT_URI, safe='')}"
        )
        print(f"  Open: {url}")
        print("  Sign in, copy the code from the failed redirect URL bar.")
        sys.exit(1)

    domain    = HOSTED_UI_DOMAIN[REGION]
    client_id = COGNITO_CONFIG[REGION]["client_id"]
    token_url = f"https://{domain}/oauth2/token"

    body = urllib.parse.urlencode({
        "grant_type":   "authorization_code",
        "client_id":    client_id,
        "code":         AUTH_CODE,
        "redirect_uri": REDIRECT_URI,
    }).encode("utf-8")

    req = urllib.request.Request(
        token_url,
        data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )

    try:
        with urllib.request.urlopen(req) as resp:
            tokens = json.loads(resp.read())
    except urllib.error.HTTPError as e:
        print(f"HTTP {e.code} from {token_url}:")
        print(e.read().decode())
        sys.exit(1)

    print("=== TOKEN EXCHANGE SUCCEEDED ===")
    print(f"id_token (first 60 chars):      {tokens['id_token'][:60]}...")
    print(f"access_token (first 60 chars):  {tokens['access_token'][:60]}...")
    print(f"refresh_token (first 60 chars): {tokens['refresh_token'][:60]}...")
    print(f"expires_in:                     {tokens['expires_in']} seconds")
    print(f"token_type:                     {tokens['token_type']}")

    # Persist all of it plus the region so test_lymow_oauth.py knows
    # which region's API endpoints / Identity Pool to use.
    tokens["region"] = REGION
    with open("tokens.json", "w") as f:
        json.dump(tokens, f, indent=2)
    print("\nSaved to tokens.json (gitignored).")


if __name__ == "__main__":
    main()
