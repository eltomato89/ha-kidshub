"""Sunrise colour curve shared with the app (protocol section 4)."""

from __future__ import annotations

STOPS: tuple[tuple[float, tuple[int, int, int]], ...] = (
    (0.00, (90, 10, 5)),
    (0.35, (200, 45, 15)),
    (0.65, (255, 120, 30)),
    (0.85, (255, 180, 80)),
    (1.00, (255, 225, 160)),
)


def _round_half_up(x: float) -> int:
    return int(x + 0.5)


def sunrise_color(p: float) -> tuple[int, int, int]:
    """Return the RGB colour for progress ``p`` (clamped to 0..1)."""
    p = min(1.0, max(0.0, p))
    for (p0, c0), (p1, c1) in zip(STOPS, STOPS[1:]):
        if p <= p1:
            t = (p - p0) / (p1 - p0)
            r, g, b = (_round_half_up(a + (z - a) * t) for a, z in zip(c0, c1))
            return (r, g, b)
    return STOPS[-1][1]


def lamp_brightness(p: float) -> int:
    """Return the lamp brightness 0..255 for progress ``p``."""
    if p <= 0.02:
        return 0
    return _round_half_up(min(1.0, p) * 255)
