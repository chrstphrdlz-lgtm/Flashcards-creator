"""Reads flashcards.toml: defaults and your standing selection criteria.

Standing criteria are preferences that apply to every chapter ("skip archaic
spellings", "no medieval legal vocabulary") so you do not have to retype them
each time. The selection stage applies them alongside whatever you say in the
moment.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover
    try:
        import tomli as tomllib
    except ImportError:
        tomllib = None

from . import paths

DEFAULTS: dict[str, Any] = {
    "book": "",
    "epub": "",
    "min_level": "B2",
    "model": "claude-sonnet-5",
    "standing_criteria": [],
}


def load(path: Path | None = None) -> dict[str, Any]:
    """Merge flashcards.toml over the defaults. Missing file is fine."""
    cfg = dict(DEFAULTS)
    path = path or paths.CONFIG_FILE
    if not path.exists() or tomllib is None:
        return cfg

    try:
        with path.open("rb") as fh:
            data = tomllib.load(fh)
    except (OSError, ValueError) as exc:
        print(f"warning: ignoring {path} ({exc})", file=sys.stderr)
        return cfg

    for key in ("book", "epub", "min_level", "model"):
        if key in data.get("book_settings", {}):
            cfg[key] = data["book_settings"][key]
        elif key in data:
            cfg[key] = data[key]

    selection = data.get("selection", {})
    criteria = selection.get("standing_criteria")
    if isinstance(criteria, list):
        cfg["standing_criteria"] = [str(c) for c in criteria]
    return cfg


def standing_criteria(cfg: dict[str, Any] | None = None) -> list[str]:
    return list((cfg or load()).get("standing_criteria") or [])
