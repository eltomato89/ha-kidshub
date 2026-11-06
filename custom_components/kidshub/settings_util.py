"""Helpers to read and patch nested settings paths."""

from __future__ import annotations

from typing import Any


def get_path(settings: dict[str, Any], path: tuple[str, ...]) -> Any:
    """Return the value at ``path`` or None."""
    value: Any = settings
    for part in path:
        if not isinstance(value, dict):
            return None
        value = value.get(part)
    return value


def patch_for(path: tuple[str, ...], value: Any) -> dict[str, Any]:
    """Return a nested patch dict setting ``path`` to ``value``."""
    patch: Any = value
    for part in reversed(path):
        patch = {part: patch}
    return patch
