"""Unit tests for proxy-free DNS-over-HTTPS (DoH) dead and parked domain detection."""

from __future__ import annotations

import asyncio

import httpx

from fmhy_drift.dns_checker import DnsDeadChecker
from fmhy_drift.models import SiteStatus


def test_dns_checker_detects_nxdomain_sinkhole_parked_and_skips_alive_or_onion() -> None:
    contacted_hosts: set[str] = set()

    def handler(request: httpx.Request) -> httpx.Response:
        contacted_hosts.add(request.url.host)
        name = request.url.params.get("name", "")
        qtype = request.url.params.get("type", "A")

        if name == "alive.example.org":
            return httpx.Response(
                200,
                json={"Status": 0, "Answer": [{"name": name, "type": 1, "data": "93.184.216.34"}]},
            )

        if name == "ipv6only.example.org":
            if qtype == "A":
                return httpx.Response(200, json={"Status": 0})
            return httpx.Response(
                200,
                json={"Status": 0, "Answer": [{"name": name, "type": 28, "data": "2606:2800:220:1::248"}]},
            )

        if name == "expired.example.org":
            return httpx.Response(200, json={"Status": 3})

        if name == "flaky-nx.example.org":
            # Cloudflare says NXDOMAIN, Google says NOERROR -> must NOT false-positive!
            if request.url.host == "cloudflare-dns.com":
                return httpx.Response(200, json={"Status": 3})
            return httpx.Response(
                200,
                json={"Status": 0, "Answer": [{"name": name, "type": 1, "data": "198.51.100.5"}]},
            )

        if name == "sinkholed.example.org":
            if qtype == "A":
                return httpx.Response(
                    200,
                    json={"Status": 0, "Answer": [{"name": name, "type": 1, "data": "0.0.0.0"}]},
                )
            return httpx.Response(200, json={"Status": 0})

        if name == "forsale.example.org":
            return httpx.Response(
                200,
                json={
                    "Status": 0,
                    "Answer": [
                        {"name": name, "type": 5, "data": "parked.sedoparking.com."},
                        {"name": "parked.sedoparking.com.", "type": 1, "data": "91.195.240.12"},
                    ],
                },
            )

        return httpx.Response(200, json={"Status": 0, "Answer": [{"type": 1, "data": "1.2.3.4"}]})

    checker = DnsDeadChecker(transport=httpx.MockTransport(handler))
    urls = [
        "https://alive.example.org/app",
        "https://ipv6only.example.org/",
        "https://expired.example.org/tool",
        "https://flaky-nx.example.org/",
        "https://sinkholed.example.org/",
        "https://forsale.example.org/",
        "http://brave4u7jddbv7cyviptqjc7jusxh72uik7zt6adtckl5f4nwy2v72qd.onion/",
    ]
    results = asyncio.run(checker.check_urls(urls))

    # Strictly NEVER contacted any target website—only public DoH + passive archive/CT endpoints!
    assert contacted_hosts <= {"cloudflare-dns.com", "dns.google", "web.archive.org", "crt.sh"}
    assert not any("example.org" in h for h in contacted_hosts)

    assert "https://alive.example.org/app" not in results
    assert "https://ipv6only.example.org/" not in results
    assert "https://flaky-nx.example.org/" not in results
    assert "http://brave4u7jddbv7cyviptqjc7jusxh72uik7zt6adtckl5f4nwy2v72qd.onion/" not in results

    assert results["https://expired.example.org/tool"].status == SiteStatus.DEAD
    assert "NXDOMAIN" in results["https://expired.example.org/tool"].reason

    assert results["https://sinkholed.example.org/"].status == SiteStatus.DEAD
    assert "0.0.0.0" in results["https://sinkholed.example.org/"].reason

    assert results["https://forsale.example.org/"].status == SiteStatus.PARKED
    assert "sedoparking.com" in results["https://forsale.example.org/"].reason
