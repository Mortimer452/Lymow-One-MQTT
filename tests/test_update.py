"""Tests for the firmware update feature.

Covers:
- createOtaJobApi endpoints present in all regions
- Version comparison logic (matching the app's decompiled.js:1600588 algorithm)
- REST method URL construction
"""
from __future__ import annotations

import pytest
from lymow_mqtt.const import API_ENDPOINTS, REGIONS, WORK_STATUS_UPDATING


class TestCreateOtaJobApiEndpoints:
    """Verify createOtaJobApi is defined for every region."""

    @pytest.mark.parametrize("region", list(REGIONS.keys()))
    def test_region_has_createOtaJobApi(self, region: str) -> None:
        ep = API_ENDPOINTS[region]
        assert "createOtaJobApi" in ep, f"Missing createOtaJobApi for {region}"
        url = ep["createOtaJobApi"]
        assert url.startswith("https://"), f"Bad URL for {region}: {url}"
        assert url.endswith("/prod"), f"URL should end with /prod: {url}"
        assert region in url, f"URL should contain the region: {url}"

    @pytest.mark.parametrize("region", list(REGIONS.keys()))
    def test_region_has_checkUpdateApi(self, region: str) -> None:
        ep = API_ENDPOINTS[region]
        assert "checkUpdateApi" in ep, f"Missing checkUpdateApi for {region}"


class TestVersionComparison:
    """Exercise the version comparison algorithm from decompiled.js:1600588.

    The app checks: does `softwareVersion + "_"` appear inside the
    `latestVersion` string? If yes → same version (no update).
    If no → update available.
    """

    @staticmethod
    def _is_same_version(installed: str, latest_fw: str) -> bool:
        """Reproduce the app's compareLatestOTAVersions check."""
        return f"{installed}_" in latest_fw

    def test_same_version_matches_lymow_format(self) -> None:
        assert self._is_same_version("v2.1.45", "v2.1.45_lymow_0.1.0")

    def test_same_version_matches_date_format(self) -> None:
        assert self._is_same_version("v2.1.48.1", "v2.1.48.1_20260528")

    def test_different_version_does_not_match(self) -> None:
        assert not self._is_same_version("v2.1.45", "v2.1.46_lymow_0.1.0")

    def test_different_version_date_format(self) -> None:
        assert not self._is_same_version("v2.1.45", "v2.1.48.1_20260528")

    def test_major_version_bump(self) -> None:
        assert not self._is_same_version("v2.1.45", "v3.0.0_lymow_0.1.0")

    def test_partial_prefix_no_false_positive(self) -> None:
        # "v2.1.4" should NOT match "v2.1.45_lymow_0.1.0" — the app
        # appends "_" to prevent partial-prefix false positives.
        assert not self._is_same_version("v2.1.4", "v2.1.45_lymow_0.1.0")


class TestDisplayVersionExtraction:
    """The update entity strips the build suffix from the objectKey for display."""

    @staticmethod
    def _extract_display(latest_fw: str) -> str:
        return latest_fw.split("_", 1)[0]

    def test_strips_date_suffix(self) -> None:
        assert self._extract_display("v2.1.48.1_20260528") == "v2.1.48.1"

    def test_strips_lymow_suffix(self) -> None:
        assert self._extract_display("v2.1.46_lymow_0.1.0") == "v2.1.46"

    def test_no_suffix_returns_as_is(self) -> None:
        assert self._extract_display("v2.1.46") == "v2.1.46"


class TestWorkStatusUpdating:
    """Sanity check that WORK_STATUS_UPDATING is the expected value."""

    def test_value(self) -> None:
        assert WORK_STATUS_UPDATING == 11


class TestOtaObjectKey:
    """v2.1.50 OTA scheme (2026-08-13): check-update grew a `prefix` field
    ("rk3588/v2.1.50/") and the real S3 objectKey is prefix + latestVersion.
    Sending the bare latestVersion makes create-ota-job 500 ("UnknownError").
    Verified live: the joined key created job 8687eb46-... successfully
    (spike_create_ota_job.py). Pre-2.1.50 responses have no prefix and the
    bare key remains correct.
    """

    def test_joins_prefix_and_version(self) -> None:
        from lymow_mqtt.rest import build_ota_object_key
        assert build_ota_object_key(
            "v2.1.50_20260813_incremental", "rk3588/v2.1.50/"
        ) == "rk3588/v2.1.50/v2.1.50_20260813_incremental"

    def test_no_prefix_returns_bare_key(self) -> None:
        from lymow_mqtt.rest import build_ota_object_key
        assert build_ota_object_key("v2.1.48.1_20260528", None) == "v2.1.48.1_20260528"

    def test_empty_prefix_returns_bare_key(self) -> None:
        from lymow_mqtt.rest import build_ota_object_key
        assert build_ota_object_key("v2.1.48.1_20260528", "") == "v2.1.48.1_20260528"

    def test_prefix_without_trailing_slash_still_joins_cleanly(self) -> None:
        from lymow_mqtt.rest import build_ota_object_key
        assert build_ota_object_key(
            "v2.1.50_20260813_incremental", "rk3588/v2.1.50"
        ) == "rk3588/v2.1.50/v2.1.50_20260813_incremental"


class TestOtaCreateJobPath:
    """The objectKey now contains slashes — the query param MUST be
    URL-encoded (the app uses encodeURIComponent; %2F on the wire)."""

    def test_slashes_are_percent_encoded(self) -> None:
        from lymow_mqtt.rest import ota_create_job_path
        path = ota_create_job_path(
            "device_3ba863e1d677", "rk3588/v2.1.50/v2.1.50_20260813_incremental"
        )
        assert path == (
            "/create-ota-job?deviceThingName=device_3ba863e1d677"
            "&objectKey=rk3588%2Fv2.1.50%2Fv2.1.50_20260813_incremental"
        )

    def test_legacy_key_unchanged_on_the_wire(self) -> None:
        from lymow_mqtt.rest import ota_create_job_path
        path = ota_create_job_path("device_abc", "v2.1.48.1_20260528")
        assert path == (
            "/create-ota-job?deviceThingName=device_abc"
            "&objectKey=v2.1.48.1_20260528"
        )


class TestDisplayVersionNewFormat:
    """Display extraction and version comparison operate on latestVersion
    (NOT the joined objectKey) and must handle the new _incremental suffix."""

    def test_display_strips_incremental_suffix(self) -> None:
        latest = "v2.1.50_20260813_incremental"
        assert latest.split("_", 1)[0] == "v2.1.50"

    def test_installed_2150_matches_new_format(self) -> None:
        assert "v2.1.50_" in "v2.1.50_20260813_incremental"

    def test_installed_2149_does_not_match(self) -> None:
        assert "v2.1.49.3_" not in "v2.1.50_20260813_incremental"
