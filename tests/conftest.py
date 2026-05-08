"""Pytest configuration for Lymow MQTT integration tests.

These tests run outside HA — pure Python, no homeassistant imports.
"""
from __future__ import annotations

import sys
import types
from pathlib import Path

# Allow tests to import the integration as a package.
_REPO_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(_REPO_ROOT / "custom_components"))
# Also expose the package directory so `import lymow_extracted_pb2` works
# in tests (matching how the harness imports it).
sys.path.insert(0, str(_REPO_ROOT / "custom_components" / "lymow_mqtt"))

# The integration's __init__.py imports homeassistant and voluptuous, which
# aren't available in the unit-test environment. Inject a stub package object
# for `lymow_mqtt` so submodule imports (`lymow_mqtt.protocol`,
# `lymow_mqtt.lymow_extracted_pb2`, etc.) work without executing the real
# __init__.py. The integration loads normally inside Home Assistant.
if "lymow_mqtt" not in sys.modules:
    _PKG_PATH = _REPO_ROOT / "custom_components" / "lymow_mqtt"
    _stub = types.ModuleType("lymow_mqtt")
    _stub.__path__ = [str(_PKG_PATH)]
    sys.modules["lymow_mqtt"] = _stub


def load_fixture(name: str) -> bytes:
    """Load a binary fixture file from tests/fixtures/."""
    path = Path(__file__).parent / "fixtures" / name
    return path.read_bytes()


def load_fixture_text(name: str) -> str:
    """Load a text fixture file from tests/fixtures/."""
    path = Path(__file__).parent / "fixtures" / name
    return path.read_text(encoding="utf-8")
