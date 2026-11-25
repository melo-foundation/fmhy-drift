"""Unit tests for urlscan.io / Cloudflare URL Scanner confirmation, MockTransport only."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from fmhy_drift import scan_confirm as sc
from fmhy_drift.models import SignalKind
from fmhy_drift.signal_cache import SignalCache

NOW = datetime(2026, 11, 25, 12, 0, tzinfo=UTC)
ALLOWED_HOSTS = {"urlscan.io", "api.cloudflare.com"}
CF_ACCOUNT = "acc123"


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    async def _fast(_seconds: float) -> None:
        return None

    monkeypatch.setattr(sc, "_sleep", _fast)


def _quota(unlisted_day: int = 1000, unlisted_hour: int = 100) -> dict:
    def w(limit: int, remaining: int) -> dict:
        return {"limit": limit, "used": limit - remaining, "remaining": remaining, "percent": 0}

    return {
        "scope": "user",
        "limits": {
            "unlisted": {"minute": w(60, 60), "hour": w(100, unlisted_hour), "day": w(1000, unlisted_day)},
            "retrieve": {"minute": w(120, 120), "hour": w(5000, 5000), "day": w(10000, 10000)},
            "public": {"minute": w(60, 60), "hour": w(500, 500), "day": w(5000, 5000)},
            "private": {"minute": w(5, 5), "hour": w(10, 10), "day": w(50, 50)},
            "search": {"minute": w(60, 60), "hour": w(500, 500), "day": w(1000, 1000)},
        },
    }


def _result(task_url: str, *, status: str | None, title: str = "", final: str | None = None,
            error: str | None = None, server: str = "nginx") -> dict:
    page: dict = {}
    if status is not None:
        page = {"url": final or task_url, "domain": httpx.URL(final or task_url).host, "status": status,
                "title": title, "server": server, "apexDomain": "", "ip": "203.0.113.5"}
    requests = []
    if error:
        requests = [{"request": {"documentURL": task_url}, "response": {"failed": {"errorText": f"net::{error}"}}}]
    elif status is not None:
        requests = [{"request": {"documentURL": task_url}, "response": {"response": {"status": int(status)}}}]
    return {
        "task": {"url": task_url, "uuid": "x", "time": "2026-11-25T11:00:00.000Z", "visibility": "unlisted",
                 "method": "api"},
        "page": page,
        "data": {"requests": requests},
        "lists": {},
        "verdicts": {"urlscan": {"score": 0}},
    }


# Scenarios keyed by target URL: what urlscan sees.
URLSCAN_RESULTS = {
    "https://dead404.example/": _result("https://dead404.example/", status="404", title="404 Not Found"),
    "https://cf522.example/": _result("https://cf522.example/", status="522", title="cf522.example | 522: Connection timed out"),
    "https://refused.example/": _result("https://refused.example/", status=None, error="ERR_CONNECTION_REFUSED"),
    "https://tls.example/": _result("https://tls.example/", status=None, error="ERR_CERT_DATE_INVALID"),
    "https://alive.example/": _result("https://alive.example/", status="200", title="Alive Tool"),
    "https://moved.example/": _result("https://moved.example/", status="200", title="Welcome",
                                      final="https://totally-other.test/landing"),
    "https://parked.example/": _result("https://parked.example/", status="200", title="parked.example is for sale!"),
    "https://waf.example/": _result("https://waf.example/", status="403", title="Just a moment...", server="cloudflare"),
    "https://ratelimited.example/": _result("https://ratelimited.example/", status="429", title=""),
    "https://redirwww.example/": _result("https://redirwww.example/", status="200", title="ok",
                                         final="https://www.redirwww.example/home"),
    "https://flip.example/": _result("https://flip.example/", status="404", title="Not Found"),
}


class FakeScanners:
    def __init__(self, *, urlscan_quota: dict | None = None, cf_results: dict | None = None,
                 urlscan_dns_fail: set[str] | None = None, pending_polls: int = 2):
        self.requests: list[httpx.Request] = []
        self.quota = urlscan_quota or _quota()
        self.cf_results = cf_results or {}
        self.dns_fail = urlscan_dns_fail or set()
        self.pending_polls = pending_polls
        self.polls: dict[str, int] = {}
        self.uuid_to_url: dict[str, str] = {}
        self.submitted: dict[str, list[str]] = {"urlscan": [], "cloudflare": []}

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        host, path = request.url.host, request.url.path
        if host == "urlscan.io":
            assert request.headers.get("API-Key") == "us-key"
            if path == "/api/v1/quotas":
                return httpx.Response(200, json=self.quota)
            if path == "/api/v1/scan/" and request.method == "POST":
                body = json.loads(request.content)
                assert body["visibility"] == "unlisted"
                url = body["url"]
                self.submitted["urlscan"].append(url)
                if url in self.dns_fail:
                    return httpx.Response(400, json={
                        "message": "DNS Error - Could not resolve domain",
                        "description": "The domain could not be resolved to a valid IPv4/IPv6 address.",
                        "status": 400,
                        "errors": [{"title": "DNS Error - Could not resolve domain", "detail": "x", "status": 400}],
                    })
                uuid = f"us-{len(self.uuid_to_url)}"
                self.uuid_to_url[uuid] = url
                return httpx.Response(200, json={
                    "message": "Submission successful", "uuid": uuid,
                    "result": f"https://urlscan.io/result/{uuid}/",
                    "api": f"https://urlscan.io/api/v1/result/{uuid}/",
                    "visibility": "unlisted", "url": url,
                }, headers={"X-Rate-Limit-Scope": "user", "X-Rate-Limit-Action": "unlisted",
                            "X-Rate-Limit-Window": "minute", "X-Rate-Limit-Remaining": "50"})
            if path.startswith("/api/v1/result/"):
                uuid = path.split("/")[4]
                n = self.polls.get(uuid, 0)
                self.polls[uuid] = n + 1
                if n < self.pending_polls:
                    return httpx.Response(404, json={"message": "Scan is not finished yet", "status": 404})
                return httpx.Response(200, json=URLSCAN_RESULTS[self.uuid_to_url[uuid]])
        if host == "api.cloudflare.com":
            assert request.headers.get("Authorization") == "Bearer cf-token"
            base = f"/client/v4/accounts/{CF_ACCOUNT}/urlscanner/v2"
            if path == f"{base}/scan" and request.method == "POST":
                body = json.loads(request.content)
                url = body["url"]
                self.submitted["cloudflare"].append(url)
                uuid = f"cf-{len(self.uuid_to_url)}"
                self.uuid_to_url[uuid] = url
                return httpx.Response(200, json={
                    "api": f"https://api.cloudflare.com{base}/result/{uuid}",
                    "message": "Submission successful", "result": f"https://radar.cloudflare.com/scan/{uuid}/summary",
                    "url": url, "uuid": uuid, "visibility": body.get("visibility", "Public").lower(),
                })
            if path.startswith(f"{base}/result/"):
                uuid = path.rsplit("/", 1)[-1]
                n = self.polls.get(uuid, 0)
                self.polls[uuid] = n + 1
                if n < self.pending_polls:
                    return httpx.Response(404, json={"message": "Scan is not finished yet.", "status": 404,
                                                     "task": {"status": "InProgress", "uuid": uuid}})
                url = self.uuid_to_url[uuid]
                res = dict(self.cf_results[url])
                res["task"] = {**res["task"], "success": True, "status": "Finished"}
                return httpx.Response(200, json=res)
        raise AssertionError(f"unexpected request to {request.url}")


def _run(urls, fake, cache=None, **kw):
    cache = cache or SignalCache(None, now=NOW)
    out = asyncio.run(sc.confirm_with_scanners(urls, cache=cache, now=NOW, transport=httpx.MockTransport(fake), **kw))
    assert {r.url.host for r in fake.requests} <= ALLOWED_HOSTS
    return out, cache


def test_urlscan_classifies_down_up_and_inconclusive() -> None:
    urls = list(URLSCAN_RESULTS) + ["https://nxdomain.example/"]
    urls.remove("https://flip.example/")
    fake = FakeScanners(urlscan_dns_fail={"https://nxdomain.example/"})
    out, _ = _run(urls, fake, urlscan_api_key="us-key")

    def one(url):
        (sig,) = out[url]
        return sig

    for url in ["https://dead404.example/", "https://cf522.example/", "https://refused.example/",
                "https://tls.example/", "https://nxdomain.example/"]:
        s = one(url)
        assert s.kind == SignalKind.SCAN_DOWN and s.severity == "critical", url
    assert "HTTP 404" in one("https://dead404.example/").detail
    assert "404 Not Found" in one("https://dead404.example/").detail
    assert "522" in one("https://cf522.example/").detail
    assert "connection refused" in one("https://refused.example/").detail
    assert "TLS failure" in one("https://tls.example/").detail
    assert "DNS failure" in one("https://nxdomain.example/").detail
    assert one("https://dead404.example/").evidence_url.startswith("https://urlscan.io/result/us-")
    assert one("https://nxdomain.example/").evidence_url == "https://dns.google/query?name=nxdomain.example"

    alive = one("https://alive.example/")
    assert alive.kind == SignalKind.SCAN_UP and alive.severity == "info"
    assert "HTTP 200" in alive.detail and "Alive Tool" in alive.detail and "2026-11-25" in alive.detail
    moved = one("https://moved.example/")
    assert moved.kind == SignalKind.SCAN_UP and moved.severity == "warning"
    assert "totally-other.test" in moved.detail and "final https://totally-other.test/landing" in moved.detail
    parked = one("https://parked.example/")
    assert parked.severity == "warning" and "parking" in parked.detail
    assert one("https://redirwww.example/").severity == "info"  # same registrable domain

    assert "https://waf.example/" not in out
    assert "https://ratelimited.example/" not in out


def test_cache_prevents_rescan_and_inconclusive_expires_sooner() -> None:
    fake = FakeScanners()
    urls = ["https://dead404.example/", "https://waf.example/"]
    out, cache = _run(urls, fake, urlscan_api_key="us-key")
    assert set(out) == {"https://dead404.example/"}
    assert cache.get(sc.CACHE_NS, "https://dead404.example/", ttl=sc.CACHE_TTL)["outcome"] == "down"

    fake2 = FakeScanners()
    cache.now = NOW + timedelta(days=2)
    out2 = asyncio.run(sc.confirm_with_scanners(urls, cache=cache, now=NOW + timedelta(days=2),
                                                transport=httpx.MockTransport(fake2), urlscan_api_key="us-key"))
    # dead404 still fresh (7 d); waf inconclusive (1 d TTL) gets rescanned
    assert fake2.submitted["urlscan"] == ["https://waf.example/"]
    assert set(out2) == {"https://dead404.example/"}


def test_max_scans_and_quota_cap_submissions() -> None:
    urls = [f"https://site{i}.example/" for i in range(10)]
    for u in urls:
        URLSCAN_RESULTS.setdefault(u, _result(u, status="200", title="t"))
    fake = FakeScanners()
    _run(urls, fake, urlscan_api_key="us-key", max_scans=4)
    assert fake.submitted["urlscan"] == urls[:4]  # priority order preserved

    fake = FakeScanners(urlscan_quota=_quota(unlisted_day=5))  # 5 - reserve 3 = 2
    _run(urls, fake, urlscan_api_key="us-key", max_scans=10)
    assert len(fake.submitted["urlscan"]) == 2


def test_urlscan_429_day_window_stops_submissions() -> None:
    urls = ["https://a.example/", "https://b.example/", "https://c.example/"]
    calls = {"n": 0}
    inner = FakeScanners()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v1/scan/":
            calls["n"] += 1
            inner.requests.append(request)
            return httpx.Response(429, json={"message": "Rate limit exceeded", "status": 429},
                                  headers={"X-Rate-Limit-Window": "day", "X-Rate-Limit-Reset-After": "36000"})
        return inner(request)

    handler.requests = inner.requests  # type: ignore[attr-defined]
    out = asyncio.run(sc.confirm_with_scanners(urls, cache=SignalCache(None, now=NOW), now=NOW,
                                               transport=httpx.MockTransport(handler), urlscan_api_key="us-key"))
    assert out == {}
    assert calls["n"] == 1


def test_cloudflare_fallback_and_second_opinion() -> None:
    cf_results = {
        "https://waf.example/": _result("https://waf.example/", status="200", title="Real Content"),
        "https://flip.example/": _result("https://flip.example/", status="200", title="Back Online"),
        "https://dead404.example/": _result("https://dead404.example/", status="404", title="Not Found"),
    }
    fake = FakeScanners(cf_results=cf_results)
    urls = ["https://waf.example/", "https://flip.example/", "https://dead404.example/", "https://alive.example/"]
    out, _ = _run(urls, fake, urlscan_api_key="us-key", cloudflare_api_token="cf-token",
                  cloudflare_account_id=CF_ACCOUNT)
    # urlscan inconclusive -> Cloudflare decides
    (waf,) = out["https://waf.example/"]
    assert waf.kind == SignalKind.SCAN_UP and waf.source == "cloudflare_url_scanner"
    assert waf.evidence_url.startswith("https://radar.cloudflare.com/scan/cf-")
    # urlscan down, Cloudflare up -> up, flagged as disagreement
    (flip,) = out["https://flip.example/"]
    assert flip.kind == SignalKind.SCAN_UP and "urlscan disagreed" in flip.detail and flip.severity == "notice"
    # both down -> urlscan's down stands
    (dead,) = out["https://dead404.example/"]
    assert dead.kind == SignalKind.SCAN_DOWN and dead.source == "urlscan"
    # urlscan up -> no Cloudflare scan
    assert "https://alive.example/" not in fake.submitted["cloudflare"]
    assert out["https://alive.example/"][0].source == "urlscan"


def test_cloudflare_only_and_missing_keys(capsys) -> None:
    cf_results = {"https://dead404.example/": _result("https://dead404.example/", status="410", title="Gone")}
    fake = FakeScanners(cf_results=cf_results)
    out, _ = _run(["https://dead404.example/"], fake, cloudflare_api_token="cf-token",
                  cloudflare_account_id=CF_ACCOUNT)
    (sig,) = out["https://dead404.example/"]
    assert sig.kind == SignalKind.SCAN_DOWN and "HTTP 410" in sig.detail
    body = json.loads(next(r for r in fake.requests if r.method == "POST").content)
    assert body == {"url": "https://dead404.example/", "visibility": "Public"}
    capsys.readouterr()

    fake = FakeScanners()
    out, _ = _run(["https://dead404.example/"], fake)
    assert out == {} and fake.requests == []
    line = capsys.readouterr().out
    assert "URLSCAN_API_KEY" in line and "CLOUDFLARE_API_TOKEN" in line


def test_stale_cache_used_when_over_budget() -> None:
    cache = SignalCache(None, now=NOW - timedelta(days=30))
    cache.put(sc.CACHE_NS, "https://old.example/", {"outcome": "down", "signals": [
        {"kind": "scan_down", "severity": "critical", "source": "urlscan", "detail": "HTTP 404",
         "observed_at": "2026-10-26T00:00:00+00:00", "evidence_url": "https://urlscan.io/result/old/"}]})
    cache.now = NOW
    fake = FakeScanners()
    out, _ = _run(["https://alive.example/", "https://old.example/"], fake, cache=cache,
                  urlscan_api_key="us-key", max_scans=1)
    assert fake.submitted["urlscan"] == ["https://alive.example/"]
    assert out["https://old.example/"][0].evidence_url == "https://urlscan.io/result/old/"


def test_poll_timeout_is_not_a_signal() -> None:
    fake = FakeScanners(pending_polls=10_000)
    out, _ = _run(["https://alive.example/"], fake, urlscan_api_key="us-key")
    assert out == {}
