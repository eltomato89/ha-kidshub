"""Pending device pairings (6-digit code, single use, 10 minutes)."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
import hashlib
import hmac
import secrets
import time
import uuid

PAIRING_TTL = 600
MAX_FAILED_ATTEMPTS = 10


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
    aborted: bool = False


@dataclass
class _FreshPairing:
    """A completed pairing whose device entry is not loaded yet."""

    token_hash: str
    expires_at: float
    loaded: asyncio.Event = field(default_factory=asyncio.Event)


class PairingRegistry:
    """Open pairings keyed by code, completed ones by device id.

    A completed pairing keeps its token valid until the device entry is
    loaded (``mark_loaded``) or ``PAIRING_TTL`` passed: the app connects right
    after ``POST /api/kidshub/pair``, before the config flow created the entry.
    """

    def __init__(self) -> None:
        self._pending: dict[str, PendingPairing] = {}
        self._fresh: dict[str, _FreshPairing] = {}
        self._failures = 0

    def _purge(self) -> None:
        now = time.time()
        for code in [c for c, p in self._pending.items() if p.expires_at <= now]:
            del self._pending[code]
        for dev in [d for d, f in self._fresh.items() if f.expires_at <= now]:
            del self._fresh[dev]

    def start(self, child_name: str) -> PendingPairing:
        """Open a new pairing with a code unique among the open ones."""
        self._purge()
        self._failures = 0
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
            self.register_failure()
            return None
        token = secrets.token_urlsafe(32)
        pending.token_hash = hashlib.sha256(token.encode()).hexdigest()
        pending.app_version = app_version
        self._fresh[pending.device_id] = _FreshPairing(
            token_hash=pending.token_hash, expires_at=time.time() + PAIRING_TTL
        )
        pending.done.set()
        return pending, token

    def fresh_token_valid(self, device_id: str, token: str) -> bool:
        """Return whether ``token`` was just issued for the not yet loaded ``device_id``."""
        self._purge()
        fresh = self._fresh.get(device_id)
        if fresh is None:
            return False
        digest = hashlib.sha256(token.encode()).hexdigest()
        return hmac.compare_digest(digest, fresh.token_hash)

    async def wait_loaded(self, device_id: str, timeout: float) -> bool:
        """Wait up to ``timeout`` seconds for the entry of ``device_id`` to load."""
        fresh = self._fresh.get(device_id)
        if fresh is None:
            return False
        try:
            async with asyncio.timeout(timeout):
                await fresh.loaded.wait()
        except TimeoutError:
            return False
        return True

    def mark_loaded(self, device_id: str) -> None:
        """The device entry is loaded: its token is checked against the entry now."""
        fresh = self._fresh.pop(device_id, None)
        if fresh is not None:
            fresh.loaded.set()

    def register_failure(self) -> None:
        """Count a failed attempt; too many invalidate all open pairings."""
        self._purge()
        if not self._pending:
            return
        self._failures += 1
        if self._failures >= MAX_FAILED_ATTEMPTS:
            for pending in self._pending.values():
                pending.aborted = True
                pending.done.set()
            self._pending.clear()

    def cancel(self, code: str) -> None:
        """Drop an open pairing."""
        self._pending.pop(code, None)
