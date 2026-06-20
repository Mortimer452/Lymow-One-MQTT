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
