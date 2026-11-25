"""Small shared helpers for passive third-party API checkers (rate limiting, Retry-After, circuit breaking)."""

from __future__ import annotations

import asyncio
import time
from email.utils import parsedate_to_datetime
from datetime import UTC, datetime

import httpx

USER_AGENT = "fmhy-drift-checker/0.1"


def make_client(
    transport: httpx.AsyncBaseTransport | None,
    *,
    timeout: float,
    follow_redirects: bool = True,
    headers: dict[str, str] | None = None,
    max_connections: int | None = None,
) -> httpx.AsyncClient:
    hdrs = {"User-Agent": USER_AGENT}
    if headers:
        hdrs.update(headers)
    kwargs: dict = {
        "timeout": httpx.Timeout(timeout),
        "follow_redirects": follow_redirects,
        "trust_env": False,
        "headers": hdrs,
    }
    if max_connections is not None:
        kwargs["limits"] = httpx.Limits(max_connections=max_connections, max_keepalive_connections=max_connections)
    if transport is not None:
        kwargs["transport"] = transport
    return httpx.AsyncClient(**kwargs)


def retry_after_seconds(resp: httpx.Response, *, default: float, cap: float) -> float:
    """Seconds to wait from a `Retry-After` header (delta-seconds or HTTP date), clamped to `[0, cap]`."""
    raw = (resp.headers.get("Retry-After") or "").strip()
    wait = default
    if raw:
        try:
            wait = float(raw)
        except ValueError:
            try:
                dt = parsedate_to_datetime(raw)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=UTC)
                wait = (dt - datetime.now(UTC)).total_seconds()
            except (TypeError, ValueError):
                wait = default
    return max(0.0, min(cap, wait))


class RateLimiter:
    """Serializes request starts so that consecutive starts are at least `interval` seconds apart."""

    def __init__(self, interval: float) -> None:
        self.interval = max(0.0, interval)
        self._lock = asyncio.Lock()
        self._next = 0.0

    async def wait(self) -> None:
        if self.interval <= 0:
            return
        async with self._lock:
            now = time.monotonic()
            if now < self._next:
                await asyncio.sleep(self._next - now)
                now = time.monotonic()
            self._next = now + self.interval

    def penalize(self, seconds: float) -> None:
        """Push the next allowed start out by `seconds` (after a 429 / 503)."""
        self._next = max(self._next, time.monotonic() + max(0.0, seconds))


class CircuitBreaker:
    """Opens after `threshold` consecutive failures; any success resets the count."""

    def __init__(self, threshold: int) -> None:
        self.threshold = max(1, threshold)
        self.failures = 0
        self.tripped = False

    @property
    def open(self) -> bool:
        return self.tripped

    def success(self) -> None:
        if not self.tripped:
            self.failures = 0

    def failure(self) -> None:
        self.failures += 1
        if self.failures >= self.threshold:
            self.tripped = True

    def trip(self) -> None:
        self.tripped = True


def past_deadline(deadline: float | None) -> bool:
    return deadline is not None and time.monotonic() >= deadline
