"""Pytest configuration for Lymow MQTT integration tests.

These tests run outside HA — pure Python, no homeassistant imports.
"""
from __future__ import annotations

import sys
from pathlib import Path

# Allow tests to import the integration as a package.
_REPO_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(_REPO_ROOT / "custom_components"))


def load_fixture(name: str) -> bytes:
    """Load a binary fixture file from tests/fixtures/."""
    path = Path(__file__).parent / "fixtures" / name
    return path.read_bytes()


def load_fixture_text(name: str) -> str:
    """Load a text fixture file from tests/fixtures/."""
    path = Path(__file__).parent / "fixtures" / name
    return path.read_text(encoding="utf-8")
