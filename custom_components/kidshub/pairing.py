"""Pending device pairings (6-digit code, single use, 10 minutes)."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
import hashlib
import secrets
import time
import uuid

PAIRING_TTL = 600


@dataclass
class PendingPairing:
    """A pairing waiting for the app to enter its code."""

    code: str
    child_name: str
    device_id: str
    expires_at: float
    done: asyncio.Event = field(default_factory=asyncio.Event)
    token_hash: str | None = None
    app_version: str | None = None


class PairingRegistry:
    """Open pairings keyed by code."""

    def __init__(self) -> None:
        self._pending: dict[str, PendingPairing] = {}

    def _purge(self) -> None:
        now = time.time()
        for code in [c for c, p in self._pending.items() if p.expires_at <= now]:
            del self._pending[code]

    def start(self, child_name: str) -> PendingPairing:
        """Open a new pairing with a code unique among the open ones."""
        self._purge()
        while True:
            code = f"{secrets.randbelow(1_000_000):06d}"
            if code not in self._pending:
                break
        pending = PendingPairing(
            code=code,
            child_name=child_name,
            device_id=uuid.uuid4().hex,
            expires_at=time.time() + PAIRING_TTL,
        )
        self._pending[code] = pending
        return pending

    async def complete(
        self, code: str, app_version: str
    ) -> tuple[PendingPairing, str] | None:
        """Consume a code, generate the token (returned only here) and signal."""
        self._purge()
        pending = self._pending.pop(code, None)
        if pending is None:
            return None
        token = secrets.token_urlsafe(32)
        pending.token_hash = hashlib.sha256(token.encode()).hexdigest()
        pending.app_version = app_version
        pending.done.set()
        return pending, token

    def cancel(self, code: str) -> None:
        """Drop an open pairing."""
        self._pending.pop(code, None)
