"""Proxy-free DNS-over-HTTPS (DoH) preliminary dead/parked domain checker.

Queries ONLY Cloudflare (`1.1.1.1`) and Google (`8.8.8.8`) DoH JSON endpoints—never
sending any traffic directly to FMHY target websites from the runner/host IP.
"""

from __future__ import annotations

import asyncio
import ipaddress
import time
from urllib.parse import urlsplit

import httpx

from fmhy_drift.models import SiteCheckResult, SiteStatus

CLOUDFLARE_DOH_URL = "https://cloudflare-dns.com/dns-query"
GOOGLE_DOH_URL = "https://dns.google/resolve"

PARKING_CNAME_SUFFIXES = (
    "sedoparking.com",
    "parkingcrew.net",
    "bodis.com",
    "above.com",
    "hugedomains.com",
    "afternic.com",
    "dan.com",
    "undeveloped.com",
    "eftydns.com",
    "parklogic.com",
    "ztomy.com",
)

# Well-known major platforms whose root hostnames never need NXDOMAIN checks
KNOWN_LIVE_PLATFORM_HOSTS = frozenset(
    {
        "google.com",
        "docs.google.com",
        "drive.google.com",
        "play.google.com",
        "chromewebstore.google.com",
        "mozilla.org",
        "addons.mozilla.org",
        "microsoft.com",
        "apps.microsoft.com",
        "apple.com",
        "apps.apple.com",
        "store.steampowered.com",
        "steamcommunity.com",
        "archive.org",
        "web.archive.org",
        "wikipedia.org",
        "en.wikipedia.org",
        "gitlab.com",
        "codeberg.org",
        "bitbucket.org",
        "sourceforge.net",
        "greasyfork.org",
        "sleazyfork.org",
        "openuserjs.org",
        "crates.io",
        "pypi.org",
        "npmjs.com",
        "hub.docker.com",
        "huggingface.co",
        "mega.nz",
        "mediafire.com",
        "itch.io",
    }
)

NON_PUBLIC_DNS_TLDS = (".onion", ".i2p", ".bit", ".eth", ".localhost", ".local")


def _is_sinkhole_ip(raw_ip: str) -> bool:
    try:
        addr = ipaddress.ip_address(raw_ip.strip())
    except ValueError:
        return False
    return addr.is_unspecified or addr.is_loopback


def _should_skip_dns_host(host: str) -> bool:
    if not host or "." not in host:
        return True
    if host in KNOWN_LIVE_PLATFORM_HOSTS:
        return True
    if host.endswith(NON_PUBLIC_DNS_TLDS):
        return True
    # Raw IPv4 / IPv6 literals are not DNS names
    try:
        ipaddress.ip_address(host.strip("[]"))
        return True
    except ValueError:
        return False


def _match_parking_cname(answers: list[dict]) -> str | None:
    for ans in answers:
        if ans.get("type") == 5:  # CNAME
            target = str(ans.get("data") or "").strip().rstrip(".").lower()
            for suffix in PARKING_CNAME_SUFFIXES:
                if target == suffix or target.endswith("." + suffix):
                    return target
    return None


def _extract_valid_ips(answers: list[dict], record_type: int) -> tuple[list[str], list[str]]:
    """Return `(valid_public_ips, sinkhole_ips)` for `record_type` (`1` = A, `28` = AAAA)."""
    valid: list[str] = []
    sinkholed: list[str] = []
    for ans in answers:
        if ans.get("type") != record_type:
            continue
        data = str(ans.get("data") or "").strip()
        if not data:
            continue
        if _is_sinkhole_ip(data):
            sinkholed.append(data)
        else:
            valid.append(data)
    return valid, sinkholed


class DnsDeadChecker:
    """Checks FMHY site hostnames via DoH (`1.1.1.1` + `8.8.8.8`) without contacting target sites."""

    def __init__(
        self,
        *,
        concurrency: int = 75,
        timeout: float = 8.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.concurrency = max(1, concurrency)
        self.timeout = timeout
        self._transport = transport

    async def check_urls(self, urls: list[str]) -> dict[str, SiteCheckResult]:
        """Return `SiteCheckResult` entries ONLY for URLs whose domain is provably dead/parked in DNS."""
        unique_urls = list(dict.fromkeys(urls))
        if not unique_urls:
            return {}

        host_to_urls: dict[str, list[str]] = {}
        for u in unique_urls:
            host = (urlsplit(u).hostname or "").lower().rstrip(".")
            if _should_skip_dns_host(host):
                continue
            host_to_urls.setdefault(host, []).append(u)

        if not host_to_urls:
            return {}

        sem = asyncio.Semaphore(self.concurrency)
        client_kwargs: dict = {
            "timeout": httpx.Timeout(self.timeout),
            "follow_redirects": True,
            "trust_env": False,
            "headers": {
                "Accept": "application/dns-json",
                "User-Agent": "fmhy-drift-checker/0.1",
            },
        }
        if self._transport is not None:
            client_kwargs["transport"] = self._transport

        host_outcomes: dict[str, tuple[SiteStatus, str, int]] = {}

        async with httpx.AsyncClient(**client_kwargs) as client:

            async def _worker(host: str) -> None:
                async with sem:
                    outcome = await self._check_host(client, host)
                    if outcome is not None:
                        host_outcomes[host] = outcome

            await asyncio.gather(*(_worker(h) for h in host_to_urls))

        results: dict[str, SiteCheckResult] = {}
        for host, (status, reason, elapsed_ms) in host_outcomes.items():
            for u in host_to_urls[host]:
                results[u] = SiteCheckResult(
                    url=u,
                    status=status,
                    http_status=None,
                    final_url=None,
                    elapsed_ms=elapsed_ms,
                    reason=reason,
                )
        return results

    async def _query_doh(
        self,
        client: httpx.AsyncClient,
        endpoint: str,
        host: str,
        qtype: str,
    ) -> dict | None:
        try:
            resp = await client.get(endpoint, params={"name": host, "type": qtype})
            if resp.status_code != 200:
                return None
            data = resp.json()
            return data if isinstance(data, dict) else None
        except Exception:
            return None

    async def _check_host(
        self,
        client: httpx.AsyncClient,
        host: str,
    ) -> tuple[SiteStatus, str, int] | None:
        """Return `(status, reason, elapsed_ms)` ONLY when `host` is provably dead or parked."""
        started = time.perf_counter()
        cf_a = await self._query_doh(client, CLOUDFLARE_DOH_URL, host, "A")
        if cf_a is None:
            return None

        rcode = cf_a.get("Status")
        answers_a = cf_a.get("Answer") if isinstance(cf_a.get("Answer"), list) else []

        # 1. Parked CNAME detection (e.g. sedoparking.com, bodis.com, hugedomains.com)
        parked_cname = _match_parking_cname(answers_a)
        if parked_cname:
            elapsed = int((time.perf_counter() - started) * 1000)
            return (
                SiteStatus.PARKED,
                f"DNS CNAME points to domain parking ({parked_cname})",
                elapsed,
            )

        # 2. Valid public IPv4 -> domain resolves cleanly
        valid_v4, sink_v4 = _extract_valid_ips(answers_a, 1)
        if valid_v4:
            return None

        # 3. NXDOMAIN (Status == 3): double-confirm on independent Google DoH (8.8.8.8)
        if rcode == 3:
            gg_a = await self._query_doh(client, GOOGLE_DOH_URL, host, "A")
            if gg_a is not None and gg_a.get("Status") == 3:
                elapsed = int((time.perf_counter() - started) * 1000)
                return (
                    SiteStatus.DEAD,
                    "Domain does not exist (DNS NXDOMAIN confirmed on Cloudflare & Google)",
                    elapsed,
                )
            return None

        # 4. Sinkholed IPv4 (0.0.0.0 / 127.0.0.1) or NOERROR without A record -> check AAAA too
        if rcode == 0:
            cf_aaaa = await self._query_doh(client, CLOUDFLARE_DOH_URL, host, "AAAA")
            if cf_aaaa is None:
                return None
            answers_aaaa = (
                cf_aaaa.get("Answer") if isinstance(cf_aaaa.get("Answer"), list) else []
            )
            parked_v6 = _match_parking_cname(answers_aaaa)
            if parked_v6:
                elapsed = int((time.perf_counter() - started) * 1000)
                return (
                    SiteStatus.PARKED,
                    f"DNS CNAME points to domain parking ({parked_v6})",
                    elapsed,
                )
            valid_v6, sink_v6 = _extract_valid_ips(answers_aaaa, 28)
            if valid_v6:
                return None

            if sink_v4 or sink_v6:
                gg_a = await self._query_doh(client, GOOGLE_DOH_URL, host, "A")
                if gg_a is not None:
                    gg_ans = gg_a.get("Answer") if isinstance(gg_a.get("Answer"), list) else []
                    gg_valid_v4, gg_sink_v4 = _extract_valid_ips(gg_ans, 1)
                    if not gg_valid_v4 and (gg_sink_v4 or gg_a.get("Status") in {0, 3}):
                        elapsed = int((time.perf_counter() - started) * 1000)
                        sink_ip = (sink_v4 or sink_v6)[0]
                        return (
                            SiteStatus.DEAD,
                            f"DNS sinkholed to {sink_ip} (no routable A/AAAA record)",
                            elapsed,
                        )
                return None

            # Empty zone (NODATA: NOERROR with 0 A, 0 AAAA, 0 CNAME) confirmed on Google DNS
            has_any_cname = any(a.get("type") == 5 for a in answers_a + answers_aaaa)
            if not has_any_cname and cf_aaaa.get("Status") == 0:
                gg_a = await self._query_doh(client, GOOGLE_DOH_URL, host, "A")
                gg_aaaa = await self._query_doh(client, GOOGLE_DOH_URL, host, "AAAA")
                if (
                    gg_a is not None
                    and gg_aaaa is not None
                    and gg_a.get("Status") == 0
                    and gg_aaaa.get("Status") == 0
                    and not gg_a.get("Answer")
                    and not gg_aaaa.get("Answer")
                ):
                    elapsed = int((time.perf_counter() - started) * 1000)
                    return (
                        SiteStatus.DEAD,
                        "Empty DNS zone — no A, AAAA, or CNAME records (confirmed Cloudflare & Google)",
                        elapsed,
                    )

        # 5. Persistent SERVFAIL (Status == 2) on BOTH Cloudflare & Google after brief retry
        if rcode == 2:
            await asyncio.sleep(0.4)
            gg_a = await self._query_doh(client, GOOGLE_DOH_URL, host, "A")
            cf_retry = await self._query_doh(client, CLOUDFLARE_DOH_URL, host, "A")
            if (
                gg_a is not None
                and cf_retry is not None
                and gg_a.get("Status") == 2
                and cf_retry.get("Status") == 2
            ):
                elapsed = int((time.perf_counter() - started) * 1000)
                return (
                    SiteStatus.DEAD,
                    "Broken/expired nameservers (DNS SERVFAIL confirmed on Cloudflare & Google)",
                    elapsed,
                )

        return None
