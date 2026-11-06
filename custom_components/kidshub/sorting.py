"""Sorting of browse results (protocol section 3.3)."""

from __future__ import annotations


def _stars(item: dict) -> int:
    return item.get("stars") or 0


def _strip(items: list[dict]) -> list[dict]:
    return [{k: v for k, v in i.items() if k != "_published"} for i in items]


def _in_progress(item: dict) -> bool:
    progress = item.get("progress") or {}
    return bool(progress.get("position_ms", 0) > 0 and not progress.get("finished"))


def sort_episodes(items: list[dict], sort: str) -> list[dict]:
    """Sort podcast episodes and drop the internal ``_published`` key."""
    newest = sorted(items, key=lambda i: i.get("_published", 0.0), reverse=True)
    if sort == "stars":
        result = sorted(newest, key=_stars, reverse=True)
    else:
        result = [i for i in newest if _in_progress(i)] + [
            i for i in newest if not _in_progress(i)
        ]
    return _strip(result)


def sort_tracks(items: list[dict], sort: str) -> list[dict]:
    """Sort tracks; ``stars`` puts rated tracks first, otherwise unchanged."""
    if sort == "stars":
        return sorted(items, key=_stars, reverse=True)
    return list(items)
