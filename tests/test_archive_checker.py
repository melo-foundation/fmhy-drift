"""Tests for Wayback CDX / Common Crawl archive-consensus signals (`check_archives`)."""

from __future__ import annotations

import asyncio
import json
import time
from datetime import UTC, datetime, timedelta

import httpx

from fmhy_drift.archive_checker import ARCHIVE_NAMESPACE, check_archives
from fmhy_drift.models import SignalKind
from fmhy_drift.signal_cache import SignalCache

NOW = datetime(2026, 11, 25, tzinfo=UTC)
ALLOWED = {"web.archive.org", "index.commoncrawl.org"}
CC_API = "https://index.commoncrawl.org/CC-MAIN-2026-47-index"

CDX: dict[str, list[list[str]]] = {
    # 200s, then two days of 5xx/404 (http 301 hops interleaved) -> ARCHIVE_DOWN from Wayback alone.
    "down.example": [
        ["20250101000000", "200", "https://down.example/"],
        ["20260301000000", "200", "https://down.example/"],
        ["20260901000000", "301", "http://down.example/"],
        ["20260902000000", "522", "https://down.example/"],
        ["20261020000000", "404", "https://down.example/"],
    ],
    # One error capture only; Common Crawl agrees -> ARCHIVE_DOWN (wayback+commoncrawl).
    "onedown.example": [
        ["20260101000000", "200", "https://onedown.example/"],
        ["20261101000000", "503", "https://onedown.example/"],
    ],
    # One error capture; Common Crawl disagrees -> nothing.
    "flaky.example": [
        ["20260101000000", "200", "https://flaky.example/"],
        ["20261101000000", "503", "https://flaky.example/"],
    ],
    # 403 = crawler blocked, not dead.
    "waf.example": [
        ["20260101000000", "200", "https://waf.example/"],
        ["20261001000000", "403", "https://waf.example/"],
        ["20261101000000", "403", "https://waf.example/"],
    ],
    # Errors only older than 18 months -> nothing.
    "ancient.example": [
        ["20200101000000", "200", "https://ancient.example/"],
        ["20230101000000", "404", "https://ancient.example/"],
        ["20230201000000", "404", "https://ancient.example/"],
    ],
    # Two redirect days to another registrable domain -> ARCHIVE_MOVED.
    "moved.example": [
        ["20250601000000", "200", "https://moved.example/"],
        ["20261001000000", "301", "https://moved.example/"],
        ["20261101000000", "301", "https://moved.example/"],
    ],
    # http -> https on the same domain -> nothing.
    "https.example": [
        ["20261001000000", "200", "https://https.example/"],
        ["20261101000000", "301", "http://https.example/"],
        ["20261102000000", "301", "http://https.example/"],
    ],
    "healthy.example": [["20261101000000", "200", "https://healthy.example/"]],
}


def _cdx_response(rows: list[list[str]]) -> httpx.Response:
    return httpx.Response(200, json=[["timestamp", "statuscode", "original"], *rows])


def test_archive_down_moved_and_conservative_cases() -> None:
    contacted: set[str] = set()
    cdx_calls: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        contacted.add(request.url.host)
        url = str(request.url)
        if request.url.host == "web.archive.org" and request.url.path == "/cdx/search/cdx":
            params = dict(request.url.params)
            cdx_calls.append(params)
            host = params["url"].rstrip("/")
            return _cdx_response(CDX.get(host, []))
        if request.url.host == "web.archive.org" and "/web/" in request.url.path:
            if "moved.example" in url and "https.example" not in url:
                return httpx.Response(
                    301, headers={"Location": "https://web.archive.org/web/20261101000000id_/https://new-home.org/"}
                )
            if "http://https.example" in url:
                return httpx.Response(
                    301, headers={"Location": "https://web.archive.org/web/20261101000000id_/https://https.example/"}
                )
            return httpx.Response(200, text="<html>")
        if url == "https://index.commoncrawl.org/collinfo.json":
            return httpx.Response(200, json=[{"id": "CC-MAIN-2026-47", "cdx-api": CC_API}])
        if url.startswith(CC_API):
            target = request.url.params.get("url", "")
            if target == "onedown.example/":
                line = {"timestamp": "20261110000000", "url": "https://onedown.example/", "status": "502"}
                return httpx.Response(200, text=json.dumps(line) + "\n")
            if target == "flaky.example/":
                line = {"timestamp": "20261110000000", "url": "https://flaky.example/", "status": "200"}
                return httpx.Response(200, text=json.dumps(line) + "\n")
            return httpx.Response(404, json={"message": "No Captures found"})
        raise AssertionError(f"unexpected request {url}")

    urls = [f"https://{h}/page" for h in CDX] + ["https://github.com/a/b", "https://www.down.example/"]
    cache = SignalCache(None, now=NOW)
    out = asyncio.run(
        check_archives(
            urls,
            cache=cache,
            now=NOW,
            transport=httpx.MockTransport(handler),
            cdx_interval=0,
            cc_interval=0,
            playback_interval=0,
        )
    )
    assert contacted <= ALLOWED
    assert set(out) == {
        "https://down.example/page",
        "https://www.down.example/",
        "https://onedown.example/page",
        "https://moved.example/page",
    }
    down = out["https://down.example/page"][0]
    assert down.kind == SignalKind.ARCHIVE_DOWN and down.severity == "warning"
    assert down.detail == "last 200 2026-03-01, latest 404 2026-10-20, 2 non-200 capture days since"
    assert down.evidence_url == "https://web.archive.org/web/*/down.example/"
    assert out["https://onedown.example/page"][0].source == "wayback+commoncrawl"
    moved = out["https://moved.example/page"][0]
    assert moved.kind == SignalKind.ARCHIVE_MOVED and moved.severity == "notice"
    assert "new-home.org" in moved.detail
    assert not any(c["url"].startswith("github.com") for c in cdx_calls)
    assert cache.get(ARCHIVE_NAMESPACE, "healthy.example", ttl=timedelta(days=28)) == {}


def test_archive_priority_budget_and_stale_fallback() -> None:
    cache = SignalCache(None, now=NOW - timedelta(days=60))
    cache.put(
        ARCHIVE_NAMESPACE,
        "old.example",
        {
            "kind": "archive_down",
            "severity": "warning",
            "source": "wayback",
            "detail": "last 200 2026-01-01, latest 404 2026-09-01, 3 non-200 capture days since",
            "observed_at": "2026-09-01T00:00:00+00:00",
            "evidence_url": "https://web.archive.org/web/*/old.example/",
        },
    )
    cache.now = NOW
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.params.get("url", ""))
        return _cdx_response([])

    out = asyncio.run(
        check_archives(
            ["https://a.example/", "https://old.example/", "https://prio.example/"],
            cache=cache,
            now=NOW,
            deadline=time.monotonic() - 1,
            transport=httpx.MockTransport(handler),
            priority_urls=["https://prio.example/"],
            cdx_interval=0,
        )
    )
    assert seen == []
    assert out["https://old.example/"][0].kind == SignalKind.ARCHIVE_DOWN


def test_archive_stops_after_repeated_429() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(429, headers={"Retry-After": "0"})

    out = asyncio.run(
        check_archives(
            [f"https://s{i}.example/" for i in range(20)],
            cache=SignalCache(None, now=NOW),
            now=NOW,
            transport=httpx.MockTransport(handler),
            cdx_interval=0,
        )
    )
    assert out == {}
    assert calls == 2
