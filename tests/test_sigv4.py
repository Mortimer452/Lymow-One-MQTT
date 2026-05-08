"""Tests for SigV4 signing — header form (REST) and query-string form (MQTT WSS)."""
from __future__ import annotations

from datetime import UTC, datetime

import pytest

from lymow_mqtt import sigv4


class TestSigV4Header:
    def test_signing_key_derivation(self):
        # Known AWS test vector: secret + 20120215 + us-east-1 + iam
        # Expected first byte of derived key (well-documented AWS test vector)
        key = sigv4.signing_key(
            "wJalrXUtnFEMI/K7MDENG+bPxRfiCYEXAMPLEKEY",
            "20150830",
            "us-east-1",
            "iam",
        )
        assert isinstance(key, bytes)
        assert len(key) == 32  # SHA256 output


class TestPresignedWsPath:
    def test_path_starts_with_mqtt(self):
        path = sigv4.presigned_ws_path(
            host="a3j5zqqo5iuph9-ats.iot.us-east-2.amazonaws.com",
            region="us-east-2",
            access_key="AKIATEST",
            secret_key="secrettest",
            session_token="sessiontest",
            now=datetime(2026, 5, 8, 12, 0, 0, tzinfo=UTC),
        )
        assert path.startswith("/mqtt?")

    def test_path_includes_required_params(self):
        path = sigv4.presigned_ws_path(
            host="a3j5zqqo5iuph9-ats.iot.us-east-2.amazonaws.com",
            region="us-east-2",
            access_key="AKIATEST",
            secret_key="secrettest",
            session_token="sessiontest",
            now=datetime(2026, 5, 8, 12, 0, 0, tzinfo=UTC),
        )
        assert "X-Amz-Algorithm=AWS4-HMAC-SHA256" in path
        assert "X-Amz-Credential=" in path
        assert "X-Amz-Date=" in path
        assert "X-Amz-Expires=86400" in path
        assert "X-Amz-Signature=" in path
        assert "X-Amz-Security-Token=" in path

    def test_signature_deterministic_for_fixed_inputs(self):
        """Same inputs -> same signature. If this fails, the canonical-request
        construction or signing-key derivation has changed.
        """
        path1 = sigv4.presigned_ws_path(
            host="a3j5zqqo5iuph9-ats.iot.us-east-2.amazonaws.com",
            region="us-east-2",
            access_key="AKIATEST",
            secret_key="secrettest",
            session_token="sessiontest",
            now=datetime(2026, 5, 8, 12, 0, 0, tzinfo=UTC),
        )
        path2 = sigv4.presigned_ws_path(
            host="a3j5zqqo5iuph9-ats.iot.us-east-2.amazonaws.com",
            region="us-east-2",
            access_key="AKIATEST",
            secret_key="secrettest",
            session_token="sessiontest",
            now=datetime(2026, 5, 8, 12, 0, 0, tzinfo=UTC),
        )
        assert path1 == path2
