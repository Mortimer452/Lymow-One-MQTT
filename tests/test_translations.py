"""Verify strings.json and translations/en.json stay in sync.

For custom HA integrations there's no auto-generation pipeline — both
files have to be maintained manually. This test catches drift so it
becomes visible at PR/CI time instead of breaking entity friendly names
silently at runtime.

To fix a failure: `python tools/sync_translations.py` mirrors strings.json
to translations/en.json (strings.json is the source of truth).
"""
from __future__ import annotations

import json
from pathlib import Path

_REPO = Path(__file__).parent.parent
_STRINGS = _REPO / "custom_components" / "lymow_mqtt" / "strings.json"
_EN = _REPO / "custom_components" / "lymow_mqtt" / "translations" / "en.json"


def test_strings_json_matches_translations_en_json() -> None:
    """The two translation files must contain identical JSON.

    HA core integrations have CI tooling that regenerates translations/<lang>.json
    from strings.json. Custom integrations don't — runtime reads ONLY from
    translations/en.json, so if a new entity translation lands in strings.json
    without being mirrored to en.json, the runtime falls back to default
    friendly names (typically just the device name).
    """
    with _STRINGS.open(encoding="utf-8") as f:
        strings = json.load(f)
    with _EN.open(encoding="utf-8") as f:
        en = json.load(f)
    assert strings == en, (
        "strings.json and translations/en.json have drifted out of sync. "
        "Run `python tools/sync_translations.py` to fix "
        "(strings.json is the source of truth)."
    )
