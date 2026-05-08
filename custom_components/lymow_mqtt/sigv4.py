"""AWS SigV4 signing helpers — header form for REST + query-string for MQTT WSS.

Header form: signs a request via Authorization + x-amz-date headers.
Query-string form (presigned URL): signs the URL itself via X-Amz-* params.

The Lymow IoT WSS connection uses the query-string form (arch.md §4b);
REST API Gateway uses Cognito access tokens directly (no SigV4 — see auth.py).
SigV4 here is for: AWS IoT WSS connect, IoT Data plane HTTPS (legacy/unused),
and S3 GETs against the user-data bucket (future feature).
"""
from __future__ import annotations

import hashlib
import hmac
import urllib.parse
from datetime import UTC, datetime

_IOT_SERVICE = "iotdevicegateway"


def _sign(key: bytes, msg: str) -> bytes:
    return hmac.new(key, msg.encode(), hashlib.sha256).digest()


def signing_key(secret: str, datestamp: str, region: str, service: str) -> bytes:
    """Derive the SigV4 signing key for (datestamp, region, service)."""
    k = _sign(("AWS4" + secret).encode(), datestamp)
    k = _sign(k, region)
    k = _sign(k, service)
    return _sign(k, "aws4_request")


def sigv4_headers(
    method: str,
    url: str,
    payload: bytes,
    service: str,
    region: str,
    access_key: str,
    secret_key: str,
    session_token: str | None = None,
) -> dict[str, str]:
    """Header-form SigV4. Returns headers including Authorization."""
    parsed = urllib.parse.urlparse(url)
    host = parsed.netloc
    canonical_uri = parsed.path or "/"
    canonical_qs = parsed.query

    now = datetime.now(UTC)
    amz_date = now.strftime("%Y%m%dT%H%M%SZ")
    date_stamp = now.strftime("%Y%m%d")
    body_hash = hashlib.sha256(payload).hexdigest()

    hdrs: dict[str, str] = {
        "host": host,
        "x-amz-date": amz_date,
        "x-amz-content-sha256": body_hash,
    }
    if session_token:
        hdrs["x-amz-security-token"] = session_token

    signed_list = sorted(hdrs)
    canonical_hdrs = "".join(f"{k}:{hdrs[k]}\n" for k in signed_list)
    signed_headers = ";".join(signed_list)

    canonical_req = "\n".join([
        method.upper(), canonical_uri, canonical_qs,
        canonical_hdrs, signed_headers, body_hash,
    ])

    scope = f"{date_stamp}/{region}/{service}/aws4_request"
    string_to_sign = "\n".join([
        "AWS4-HMAC-SHA256", amz_date, scope,
        hashlib.sha256(canonical_req.encode()).hexdigest(),
    ])

    sig = hmac.new(
        signing_key(secret_key, date_stamp, region, service),
        string_to_sign.encode(),
        hashlib.sha256,
    ).hexdigest()

    return {
        "Authorization": (
            f"AWS4-HMAC-SHA256 Credential={access_key}/{scope}, "
            f"SignedHeaders={signed_headers}, Signature={sig}"
        ),
        "x-amz-date": amz_date,
        "x-amz-content-sha256": body_hash,
        **({"x-amz-security-token": session_token} if session_token else {}),
    }


def presigned_ws_path(
    host: str,
    region: str,
    access_key: str,
    secret_key: str,
    session_token: str | None,
    now: datetime | None = None,
    expires_seconds: int = 86400,
) -> str:
    """Query-string SigV4 for AWS IoT MQTT-over-WSS connect.

    Returns the path component (`/mqtt?...`) for paho's ws_set_options().
    The session_token is appended UNSIGNED as X-Amz-Security-Token —
    this is the AWS IoT idiom.

    Ported from harness.py:presigned_ws_path. arch.md §4b.
    """
    if now is None:
        now = datetime.now(UTC)
    amz_date = now.strftime("%Y%m%dT%H%M%SZ")
    datestamp = now.strftime("%Y%m%d")
    scope = f"{datestamp}/{region}/{_IOT_SERVICE}/aws4_request"

    qs_pairs = {
        "X-Amz-Algorithm": "AWS4-HMAC-SHA256",
        "X-Amz-Credential": f"{access_key}/{scope}",
        "X-Amz-Date": amz_date,
        "X-Amz-Expires": str(expires_seconds),
        "X-Amz-SignedHeaders": "host",
    }
    canonical_qs = "&".join(
        f"{urllib.parse.quote(k, safe='')}={urllib.parse.quote(v, safe='')}"
        for k, v in sorted(qs_pairs.items())
    )

    canonical_req = "\n".join([
        "GET", "/mqtt", canonical_qs,
        f"host:{host}\n", "host",
        hashlib.sha256(b"").hexdigest(),
    ])
    string_to_sign = "\n".join([
        "AWS4-HMAC-SHA256", amz_date, scope,
        hashlib.sha256(canonical_req.encode()).hexdigest(),
    ])
    sig = hmac.new(
        signing_key(secret_key, datestamp, region, _IOT_SERVICE),
        string_to_sign.encode(),
        hashlib.sha256,
    ).hexdigest()

    final_qs = canonical_qs + f"&X-Amz-Signature={sig}"
    if session_token:
        final_qs += "&X-Amz-Security-Token=" + urllib.parse.quote(session_token, safe="")
    return f"/mqtt?{final_qs}"
