"""Tests for passive DoH parking detection (`check_dns_parking`)."""

from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime, timedelta

import httpx

from fmhy_drift.dns_checker import DNS_PARKING_NAMESPACE, check_dns_parking
from fmhy_drift.models import SignalKind
from fmhy_drift.signal_cache import SignalCache

NOW = datetime(2026, 11, 25, tzinfo=UTC)
DOH_HOSTS = {"cloudflare-dns.com", "dns.google"}


def _ans(name: str, rtype: int, data: str) -> dict:
    return {"name": name, "type": rtype, "data": data}


ZONES: dict[tuple[str, str], list[dict]] = {
    ("sedo-ns.com", "NS"): [_ans("sedo-ns.com", 2, "ns1.sedoparking.com."), _ans("sedo-ns.com", 2, "ns2.sedoparking.com.")],
    ("sedo-ns.com", "A"): [_ans("sedo-ns.com", 1, "91.195.240.94")],
    ("bodis-ip.net", "NS"): [_ans("bodis-ip.net", 2, "ns1.example-dns.net.")],
    ("bodis-ip.net", "A"): [_ans("bodis-ip.net", 1, "199.59.243.228")],
    ("mixed-ip.org", "NS"): [_ans("mixed-ip.org", 2, "ns1.example-dns.net.")],
    ("mixed-ip.org", "A"): [_ans("mixed-ip.org", 1, "199.59.243.228"), _ans("mixed-ip.org", 1, "93.184.216.34")],
    ("cname-park.io", "NS"): [_ans("cname-park.io", 2, "ns1.example-dns.net.")],
    ("www.cname-park.io", "A"): [
        _ans("www.cname-park.io", 5, "parkingpage.namecheap.com."),
        _ans("parkingpage.namecheap.com", 1, "198.51.100.7"),
    ],
    ("namecheap-default.com", "NS"): [
        _ans("namecheap-default.com", 2, "dns1.registrar-servers.com."),
        _ans("namecheap-default.com", 2, "dns2.registrar-servers.com."),
    ],
    ("namecheap-default.com", "A"): [_ans("namecheap-default.com", 1, "198.51.100.8")],
    ("hostinger.com.br", "NS"): [_ans("hostinger.com.br", 2, "ns1.dns-parking.com.")],
    ("hostinger.com.br", "A"): [_ans("hostinger.com.br", 1, "198.51.100.9")],
    ("disputed.com", "NS"): [_ans("disputed.com", 2, "ns1.bodis.com.")],
    ("disputed.com", "A"): [_ans("disputed.com", 1, "198.51.100.10")],
}


def test_dns_parking_ns_ip_cname_and_conservative_cases() -> None:
    contacted: set[str] = set()
    calls: list[tuple[str, str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        contacted.add(request.url.host)
        name = request.url.params.get("name", "")
        qtype = request.url.params.get("type", "A")
        calls.append((request.url.host, name, qtype))
        if name == "disputed.com" and qtype == "NS" and request.url.host == "dns.google":
            return httpx.Response(200, json={"Status": 0, "Answer": [_ans(name, 2, "ns1.example-dns.net.")]})
        answers = ZONES.get((name, qtype))
        if answers is None:
            return httpx.Response(200, json={"Status": 0})
        return httpx.Response(200, json={"Status": 0, "Answer": answers})

    urls = [
        "https://sedo-ns.com/",
        "https://sedo-ns.com/other",
        "https://bodis-ip.net/x",
        "https://mixed-ip.org/",
        "https://www.cname-park.io/",
        "https://namecheap-default.com/",
        "https://hostinger.com.br/",
        "https://disputed.com/",
        "https://someone.github.io/tool",
        "http://exampleonion.onion/",
        "https://drive.google.com/x",
    ]
    cache = SignalCache(None, now=NOW)
    out = asyncio.run(check_dns_parking(urls, cache=cache, now=NOW, transport=httpx.MockTransport(handler)))

    assert contacted <= DOH_HOSTS
    assert set(out) == {
        "https://sedo-ns.com/",
        "https://sedo-ns.com/other",
        "https://bodis-ip.net/x",
        "https://www.cname-park.io/",
    }
    sedo = out["https://sedo-ns.com/"]
    assert {s.kind for s in sedo} == {SignalKind.PARKED_DNS}
    assert any("ns1.sedoparking.com (Sedo)" in s.detail for s in sedo)
    assert any("A 91.195.240.94 (Sedo)" in s.detail for s in sedo)
    assert all(s.severity == "warning" for s in sedo)
    assert "Bodis" in out["https://bodis-ip.net/x"][0].detail
    assert "parkingpage.namecheap.com" in out["https://www.cname-park.io/"][0].detail
    # Shared platforms are never queried; onion is skipped.
    assert not any("github.io" in n for _, n, _ in calls)
    # Positive answers are confirmed on the second resolver.
    assert ("dns.google", "sedo-ns.com", "NS") in calls
    # A positive not reproduced on the second resolver is recorded as not parked.
    assert cache.get(DNS_PARKING_NAMESPACE, "ns:disputed.com", ttl=timedelta(days=6))["parked"] is None
    assert cache.get(DNS_PARKING_NAMESPACE, "ns:namecheap-default.com", ttl=timedelta(days=6)) == {
        "ns": ["dns1.registrar-servers.com", "dns2.registrar-servers.com"],
        "parked": None,
    }


def test_dns_parking_uses_cache_and_stale_fallback_after_deadline() -> None:
    cache = SignalCache(None, now=NOW - timedelta(days=30))
    cache.put(DNS_PARKING_NAMESPACE, "ns:old-parked.com", {"ns": ["ns1.bodis.com"], "parked": "NS ns1.bodis.com (Bodis)"})
    cache.put(DNS_PARKING_NAMESPACE, "a:old-parked.com", {"a": [], "parked": None})
    cache.now = NOW

    requests: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(str(request.url))
        return httpx.Response(200, json={"Status": 0})

    out = asyncio.run(
        check_dns_parking(
            ["https://old-parked.com/"],
            cache=cache,
            now=NOW,
            deadline=time.monotonic() - 1,
            transport=httpx.MockTransport(handler),
        )
    )
    assert requests == []
    assert "Bodis" in out["https://old-parked.com/"][0].detail


def test_dns_parking_survives_resolver_failures() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "cloudflare-dns.com":
            raise httpx.ConnectError("boom")
        return httpx.Response(500)

    out = asyncio.run(
        check_dns_parking(
            [f"https://site{i}.com/" for i in range(30)],
            cache=SignalCache(None, now=NOW),
            now=NOW,
            transport=httpx.MockTransport(handler),
        )
    )
    assert out == {}
