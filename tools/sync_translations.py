"""Mirror strings.json -> translations/en.json.

For custom HA integrations there's no auto-translation pipeline like the
one HA core integrations get via Lokalise. Both `strings.json` and
`translations/en.json` need to be present and in sync:
- `strings.json` is the source-of-truth, read by HA's `hassfest` validation
  and IDE tooling
- `translations/en.json` is what HA loads at runtime for English UI strings

Without this script (or some equivalent enforcement), the two files drift
the moment a new entity translation gets added to one but not the other,
and runtime entity friendly names silently fall back to default
(typically just the device name).

Run before committing translation changes, OR add to a pre-commit hook.
The drift-detection test (`tests/test_translations.py`) will fail the
test suite if the two files diverge.

Usage: python tools/sync_translations.py
"""
from __future__ import annotations

import shutil
import sys
from pathlib import Path

_REPO = Path(__file__).parent.parent
_STRINGS = _REPO / "custom_components" / "lymow_mqtt" / "strings.json"
_EN = _REPO / "custom_components" / "lymow_mqtt" / "translations" / "en.json"


def main() -> int:
    if not _STRINGS.exists():
        print(f"ERROR: {_STRINGS} not found", file=sys.stderr)
        return 1
    _EN.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy(_STRINGS, _EN)
    print(f"Mirrored {_STRINGS.relative_to(_REPO)} -> {_EN.relative_to(_REPO)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
