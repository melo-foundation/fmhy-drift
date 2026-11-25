"""Proxy-free DNS-over-HTTPS (DoH) preliminary dead/parked domain checker.

Queries ONLY Cloudflare (`1.1.1.1`) and Google (`8.8.8.8`) DoH JSON endpoints—never
sending any traffic directly to FMHY target websites from the runner/host IP.
"""

from __future__ import annotations

import asyncio
import ipaddress
import time
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from urllib.parse import urlsplit

import httpx

from fmhy_drift.domains import is_public_dns_host, is_shared_platform_host, registrable_domain
from fmhy_drift.models import SignalKind, SiteCheckResult, SiteSignal, SiteStatus
from fmhy_drift.passive_http import CircuitBreaker, make_client, past_deadline, retry_after_seconds
from fmhy_drift.signal_cache import SignalCache

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
    # Catch 0.0.0.0, 127.0.0.0/8, RFC1918 private, CGNAT, link-local, TEST-NET, benchmark/registrar sinkhole IPs
    return not addr.is_global


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
        enrich_last_active: bool = True,
        now: datetime | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.concurrency = max(1, concurrency)
        self.timeout = timeout
        self.enrich_last_active = enrich_last_active
        self.now = now or datetime.now(UTC)
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

        if results and self.enrich_last_active:
            results = await enrich_unhealthy_sites_last_active(
                results,
                now=self.now,
                transport=self._transport,
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


async def resolving_urls(
    urls: list[str],
    *,
    transport: httpx.AsyncBaseTransport | None = None,
    timeout: float = 8.0,
) -> set[str]:
    """URLs whose host now resolves to a routable, non-parking address on BOTH Cloudflare and Google DoH.

    Used to clear DNS-dead results reused from an earlier report. A failed or ambiguous lookup never
    counts as recovered, so a flaky resolver cannot drop a dead site.
    """
    host_to_urls: dict[str, list[str]] = {}
    for u in dict.fromkeys(urls):
        host = (urlsplit(u).hostname or "").lower().rstrip(".")
        if host:
            host_to_urls.setdefault(host, []).append(u)
    if not host_to_urls:
        return set()
    kwargs: dict = {
        "timeout": httpx.Timeout(timeout),
        "follow_redirects": True,
        "trust_env": False,
        "headers": {"Accept": "application/dns-json", "User-Agent": "fmhy-drift-checker/0.1"},
    }
    if transport is not None:
        kwargs["transport"] = transport
    sem = asyncio.Semaphore(20)
    alive: set[str] = set()

    async def _resolves(client: httpx.AsyncClient, endpoint: str, host: str) -> bool:
        for qtype, rtype in (("A", 1), ("AAAA", 28)):
            try:
                resp = await client.get(endpoint, params={"name": host, "type": qtype})
                data = resp.json() if resp.status_code == 200 else None
            except Exception:
                return False
            if not isinstance(data, dict) or data.get("Status") != 0:
                return False
            answers = data.get("Answer") if isinstance(data.get("Answer"), list) else []
            if _match_parking_cname(answers):
                return False
            valid, _ = _extract_valid_ips(answers, rtype)
            if valid:
                return True
        return False

    async with httpx.AsyncClient(**kwargs) as client:

        async def _worker(host: str) -> None:
            async with sem:
                if await _resolves(client, CLOUDFLARE_DOH_URL, host) and await _resolves(
                    client, GOOGLE_DOH_URL, host
                ):
                    alive.update(host_to_urls[host])

        await asyncio.gather(*(_worker(h) for h in host_to_urls))
    return alive


def _parse_ct_iso(raw: str | None) -> datetime | None:
    if not raw or not isinstance(raw, str):
        return None
    s = raw.strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
        return dt.astimezone(UTC) if dt.tzinfo else dt.replace(tzinfo=UTC)
    except ValueError:
        return None


async def enrich_unhealthy_sites_last_active(
    results: dict[str, SiteCheckResult],
    *,
    now: datetime | None = None,
    force_refresh: bool = False,
    transport: httpx.AsyncBaseTransport | None = None,
) -> dict[str, SiteCheckResult]:
    """Populate `last_seen_at` and `inactive_days` on dead/parked/TLS-broken sites via Wayback CDX, CertSpotter CT, & RDAP.

    Never sends any network traffic to target websites; queries only `web.archive.org`, `api.certspotter.com`, and `rdap.org`.
    """
    ref_now = now or datetime.now(UTC)
    target_urls = [
        u
        for u, sr in results.items()
        if sr.is_unhealthy and (force_refresh or sr.last_seen_at is None)
    ]
    if not target_urls:
        return results

    host_to_urls: dict[str, list[str]] = {}
    reg_domain_for_host: dict[str, str] = {}
    for u in target_urls:
        h = (urlsplit(u).hostname or "").lower().rstrip(".")
        if h.startswith("www."):
            h = h[4:]
        if h:
            host_to_urls.setdefault(h, []).append(u)
            parts = h.split(".")
            reg_domain_for_host[h] = ".".join(parts[-2:]) if len(parts) >= 2 else h

    kwargs: dict = {
        "timeout": httpx.Timeout(7.0),
        "follow_redirects": True,
        "trust_env": False,
        "headers": {"User-Agent": "fmhy-drift-checker/0.1"},
    }
    if transport is not None:
        kwargs["transport"] = transport

    wb_sem = asyncio.Semaphore(6)
    ct_sem = asyncio.Semaphore(5)
    rdap_sem = asyncio.Semaphore(5)
    wb_dates: dict[str, datetime] = {}
    ct_info: dict[str, tuple[datetime | None, datetime | None]] = {}
    rdap_expiry: dict[str, tuple[datetime | None, list[str]]] = {}
    wb_failures = 0
    ct_failures = 0
    rdap_failures = 0

    async with httpx.AsyncClient(**kwargs) as client:

        async def _query_wayback(u: str) -> None:
            nonlocal wb_failures
            if wb_failures >= 5:
                return
            async with wb_sem:
                if wb_failures >= 5:
                    return
                p = urlsplit(u)
                candidates_u = [u]
                if p.path and p.path not in {"", "/"} and p.scheme and p.netloc:
                    candidates_u.append(f"{p.scheme}://{p.netloc}/")
                for cand_u in candidates_u:
                    try:
                        resp = await client.get(
                            "https://web.archive.org/cdx/search/cdx",
                            params={
                                "url": cand_u,
                                "output": "json",
                                "fl": "timestamp,statuscode",
                                "filter": "statuscode:200",
                                "limit": "-1",
                            },
                        )
                        if resp.status_code in {429, 503}:
                            wb_failures = 99
                            return
                        if resp.status_code != 200:
                            wb_failures += 1
                            continue
                        wb_failures = 0
                        rows = resp.json()
                        if isinstance(rows, list) and len(rows) >= 2:
                            ts = str(rows[-1][0])
                            if len(ts) >= 8:
                                wb_dates[u] = datetime.strptime(ts[:8], "%Y%m%d").replace(tzinfo=UTC)
                                return
                    except Exception:
                        wb_failures += 1

        async def _query_certspotter(host: str) -> None:
            nonlocal ct_failures
            if ct_failures >= 4 or host.endswith(NON_PUBLIC_DNS_TLDS):
                return
            async with ct_sem:
                if ct_failures >= 4:
                    return
                try:
                    resp = await client.get(
                        "https://api.certspotter.com/v1/issuances",
                        params={"domain": host},
                    )
                    if resp.status_code in {429, 502, 503, 504}:
                        ct_failures += 1
                        return
                    if resp.status_code != 200:
                        return
                    ct_failures = 0
                    entries = resp.json()
                    if not isinstance(entries, list) or not entries:
                        return
                    latest_issued: datetime | None = None
                    latest_expiry: datetime | None = None
                    for row in entries[-40:]:
                        if not isinstance(row, dict) or row.get("revoked"):
                            continue
                        nb = _parse_ct_iso(row.get("not_before"))
                        na = _parse_ct_iso(row.get("not_after"))
                        if nb and nb <= ref_now and (latest_issued is None or nb > latest_issued):
                            latest_issued = nb
                        if na and (latest_expiry is None or na > latest_expiry):
                            latest_expiry = na
                    if latest_issued or latest_expiry:
                        ct_info[host] = (latest_issued, latest_expiry)
                except Exception:
                    ct_failures += 1

        async def _query_rdap(reg_dom: str) -> None:
            nonlocal rdap_failures
            if rdap_failures >= 5 or reg_dom.endswith(NON_PUBLIC_DNS_TLDS):
                return
            async with rdap_sem:
                if rdap_failures >= 5:
                    return
                try:
                    resp = await client.get(f"https://rdap.org/domain/{reg_dom}")
                    if resp.status_code in {429, 502, 503, 504}:
                        rdap_failures += 1
                        return
                    if resp.status_code != 200:
                        return
                    rdap_failures = 0
                    body = resp.json()
                    if not isinstance(body, dict):
                        return
                    statuses = [str(s) for s in (body.get("status") or []) if isinstance(s, str)]
                    exp_dt: datetime | None = None
                    for ev in body.get("events") or []:
                        if isinstance(ev, dict) and str(ev.get("eventAction") or "").lower() == "expiration":
                            exp_dt = _parse_ct_iso(ev.get("eventDate"))
                    if exp_dt is not None or statuses:
                        rdap_expiry[reg_dom] = (exp_dt, statuses)
                except Exception:
                    rdap_failures += 1

        unique_reg_domains = sorted(set(reg_domain_for_host.values()))
        try:
            await asyncio.wait_for(
                asyncio.gather(
                    *(_query_wayback(u) for u in target_urls),
                    *(_query_certspotter(h) for h in host_to_urls),
                    *(_query_rdap(rd) for rd in unique_reg_domains),
                ),
                timeout=30.0,
            )
        except TimeoutError:
            pass

    updated = dict(results)
    for u in target_urls:
        sr = updated[u]
        h = (urlsplit(u).hostname or "").lower().rstrip(".")
        if h.startswith("www."):
            h = h[4:]
        reg_dom = reg_domain_for_host.get(h, h)
        wb_dt = wb_dates.get(u)
        ct_issued, ct_expiry = ct_info.get(h, (None, None))
        reg_exp_dt, reg_statuses = rdap_expiry.get(reg_dom, (None, []))

        candidates = [d for d in (wb_dt, ct_issued) if d is not None]
        if reg_exp_dt is not None and reg_exp_dt <= ref_now:
            candidates.append(reg_exp_dt)

        if not candidates and ct_expiry is None and reg_exp_dt is None:
            continue

        best_dt = max(candidates) if candidates else None
        days = (
            max(0, int((ref_now - best_dt).total_seconds() // 86400))
            if best_dt is not None
            else None
        )

        notes: list[str] = []
        if best_dt is not None:
            if reg_exp_dt is not None and best_dt == reg_exp_dt:
                src_tag = "RDAP domain expiration"
            elif wb_dt is not None and best_dt == wb_dt:
                src_tag = "Wayback 200 OK"
            else:
                src_tag = "CT TLS cert issued"
            notes.append(f"last active {best_dt.strftime('%Y-%m-%d')} ({src_tag})")

        if ct_expiry is not None and ct_expiry < ref_now:
            notes.append(f"last TLS cert expired {ct_expiry.strftime('%Y-%m-%d')}")

        hold_flags = [
            s
            for s in reg_statuses
            if any(k in s.lower() for k in ("redemption", "pending delete", "hold"))
        ]
        if hold_flags:
            notes.append(f"RDAP: {', '.join(hold_flags[:2])}")
        elif reg_exp_dt is not None and reg_exp_dt < ref_now and best_dt != reg_exp_dt:
            notes.append(f"domain expired {reg_exp_dt.strftime('%Y-%m-%d')}")

        base_reason = sr.reason.split(" — last active ", 1)[0]
        new_reason = f"{base_reason} — {'; '.join(notes)}" if notes else base_reason

        updated[u] = replace(
            sr,
            last_seen_at=best_dt,
            inactive_days=days,
            reason=new_reason,
        )

    return updated


# ---------------------------------------------------------------------------------------------
# Passive parking detection via NS / A / CNAME records (DoH only, all hosts).
# ---------------------------------------------------------------------------------------------

DNS_PARKING_NAMESPACE = "dns_parking"
DNS_PARKING_TTL = timedelta(days=6)
DNS_PARKING_POSITIVE_TTL = timedelta(days=6)

# Nameserver host suffixes that serve ONLY parking / for-sale landers. Each entry was resolved via DoH on
# 2026-11-25 and appears in passive-DNS history (dns.coffee) on parked domains. Deliberately excluded:
# registrar / host default DNS that also serves real zones (Namecheap `registrar-servers.com`, GoDaddy
# `domaincontrol.com`, Dynadot `dyna-ns.net`, Porkbun `porkbun.com`, Hostinger `dns-parking.com` -- despite its
# name it is Hostinger's default NS for hosted sites, 163 listed FMHY domains use it) -- such domains may be live.
PARKING_NS_SUFFIXES: dict[str, str] = {
    "sedoparking.com": "Sedo",  # ns1/ns2.sedoparking.com (91.195.240.0/23, SEDO-NET)
    "bodis.com": "Bodis",  # ns1/ns2.bodis.com
    "parkingcrew.net": "ParkingCrew",  # ns1/ns2.parkingcrew.net (Team Internet)
    "above.com": "Above.com",  # ns1/ns2.above.com, nsNN.above.com (Trellian)
    "abovedomains.com": "Above.com",  # ns1/ns2.abovedomains.com (seen 2026-11-25 with A in 103.224.212.0/24)
    "afternic.com": "Afternic",  # ns1..ns6.afternic.com (Afternic fast-transfer for-sale listing)
    "dan.com": "Dan.com",  # ns1/ns2.dan.com (for-sale lander, now GoDaddy-operated)
    "undeveloped.com": "Undeveloped",  # ns1/ns2.undeveloped.com (same GoDaddy for-sale cluster as dan.com)
    "hugedomains.com": "HugeDomains",  # ns1/ns2.hugedomains.com
    "parklogic.com": "ParkLogic",  # ns1/ns2.parklogic.com and per-account nsN.<id>.parklogic.com
    "eftydns.com": "Efty",  # ns1/ns2.eftydns.com (for-sale lander)
}

# CNAME targets that are parking-only, in addition to `PARKING_CNAME_SUFFIXES` (kept separate so the
# `DnsDeadChecker` behaviour does not change). `pixie.porkbun.com` is NOT here: it also serves URL forwarding.
EXTRA_PARKING_CNAME_SUFFIXES = (
    "parkingpage.namecheap.com",  # Namecheap default "www" parking record
    "pixie-parking.porkbun.com",  # Porkbun parking-only lander (kb.porkbun.com article 183)
)

# A-record networks operated by parking companies. Ownership verified via RDAP (rdap.org/ip/...) on
# 2026-11-25; only blocks whose registered owner IS the parking company. GoDaddy / Namecheap parking IPs are
# excluded: they sit on shared cloud or shared-hosting ranges (e.g. 203.161.32.251 PTR nc-ph-*.web-hosting.com).
PARKING_IP_NETWORKS: tuple[tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, str], ...] = (
    (ipaddress.ip_network("64.190.62.0/23"), "Sedo"),  # SEDO-NET2 (sedoparking.com -> 64.190.63.136)
    (ipaddress.ip_network("91.195.240.0/23"), "Sedo"),  # SEDO-NET
    (ipaddress.ip_network("199.59.240.0/22"), "Bodis"),  # BODIS-COM, Bodis LLC (bodis.com -> 199.59.243.50)
    (ipaddress.ip_network("185.53.177.0/24"), "ParkingCrew"),  # Team Internet (parkingcrew.net -> 185.53.177.29)
    (ipaddress.ip_network("185.53.178.0/24"), "ParkingCrew"),  # Team Internet
    (ipaddress.ip_network("185.53.179.0/24"), "ParkingCrew"),  # Team Internet
    (ipaddress.ip_network("103.224.182.0/24"), "Above.com"),  # TRELLIAN-AU (park.above.com, PTR lb-182-*.above.com)
    (ipaddress.ip_network("103.224.212.0/24"), "Above.com"),  # TRELLIAN-AU (ns1.above.com, PTR lb-212-*.above.com)
    (ipaddress.ip_network("162.4.84.0/24"), "ParkLogic"),  # PARKLOGICPTYLTD-AU (ns1.parklogic.com -> 162.4.84.45)
)


def _ns_suffix_match(name: str) -> tuple[str, str] | None:
    n = name.strip().rstrip(".").lower()
    for suffix, label in PARKING_NS_SUFFIXES.items():
        if n.endswith("." + suffix):
            return n, label
    return None


def _cname_parking_match(answers: list[dict]) -> str | None:
    hit = _match_parking_cname(answers)
    if hit:
        return hit
    for ans in answers:
        if ans.get("type") == 5:
            target = str(ans.get("data") or "").strip().rstrip(".").lower()
            for suffix in EXTRA_PARKING_CNAME_SUFFIXES:
                if target == suffix or target.endswith("." + suffix):
                    return target
    return None


def _parking_ip_label(raw_ip: str) -> str | None:
    try:
        addr = ipaddress.ip_address(raw_ip.strip())
    except ValueError:
        return None
    for net, label in PARKING_IP_NETWORKS:
        if addr.version == net.version and addr in net:
            return label
    return None


def _eval_ns(body: dict) -> tuple[list[str], str | None]:
    """Return `(ns_names, parking_detail)`; parking only when EVERY NS record is a parking nameserver."""
    answers = body.get("Answer") if isinstance(body.get("Answer"), list) else []
    names = sorted(
        {str(a.get("data") or "").strip().rstrip(".").lower() for a in answers if a.get("type") == 2} - {""}
    )
    if not names:
        return names, None
    matches = [_ns_suffix_match(n) for n in names]
    if all(matches):
        first = matches[0]
        assert first is not None
        return names, f"NS {first[0]} ({first[1]})"
    return names, None


def _eval_a(body: dict) -> tuple[list[str], str | None]:
    """Return `(a_ips, parking_detail)` from an A answer (CNAME chain match, or ALL A records in parking nets)."""
    answers = body.get("Answer") if isinstance(body.get("Answer"), list) else []
    cname = _cname_parking_match(answers)
    ips = [str(a.get("data") or "").strip() for a in answers if a.get("type") == 1]
    ips = [i for i in ips if i]
    if cname:
        return ips, f"CNAME {cname}"
    if ips:
        labels = [_parking_ip_label(i) for i in ips]
        if all(labels):
            return ips, f"A {ips[0]} ({labels[0]})"
    return ips, None


def _parking_signal(detail: str, evidence_name: str, rr: str, now: datetime) -> SiteSignal:
    return SiteSignal(
        kind=SignalKind.PARKED_DNS,
        severity="warning",
        source="doh",
        detail=f"{detail}, checked {now.strftime('%Y-%m-%d')}",
        observed_at=now,
        evidence_url=f"https://dns.google/query?name={evidence_name}&rr_type={rr}",
    )


async def check_dns_parking(
    urls: list[str],
    *,
    cache: SignalCache,
    now: datetime,
    deadline: float | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
    concurrency: int = 50,
    timeout: float = 8.0,
) -> dict[str, list[SiteSignal]]:
    """Emit `PARKED_DNS` for URLs whose zone NS, host CNAME, or host A records point at a parking provider.

    Queries only Cloudflare / Google DoH. A positive answer from one resolver is confirmed on the other before
    it is reported or cached. Cache keys in `dns_parking`: `ns:<registrable domain>` and `a:<host>`.
    """
    host_to_urls: dict[str, list[str]] = {}
    for u in dict.fromkeys(urls):
        host = (urlsplit(u).hostname or "").lower().rstrip(".")  # query the listed name itself (keeps `www.`)
        if not is_public_dns_host(host) or is_shared_platform_host(host):
            continue
        reg = registrable_domain(host)
        if reg is None or host in KNOWN_LIVE_PLATFORM_HOSTS or reg in KNOWN_LIVE_PLATFORM_HOSTS:
            continue
        host_to_urls.setdefault(host, []).append(u)
    if not host_to_urls:
        print("dns_parking: 0 hosts to check", flush=True)
        return {}

    reg_of = {h: registrable_domain(h) or h for h in host_to_urls}
    regs = sorted(set(reg_of.values()))
    sem = asyncio.Semaphore(max(1, concurrency))
    breakers = {CLOUDFLARE_DOH_URL: CircuitBreaker(40), GOOGLE_DOH_URL: CircuitBreaker(40)}
    stats = {"queries": 0, "cached": 0, "stale": 0, "unknown": 0, "skipped_deadline": 0}
    ns_hit: dict[str, str | None] = {}
    a_hit: dict[str, str | None] = {}

    async def _doh(client: httpx.AsyncClient, endpoint: str, name: str, qtype: str) -> dict | None:
        br = breakers[endpoint]
        if br.open:
            return None
        for attempt in range(2):
            stats["queries"] += 1
            try:
                resp = await client.get(endpoint, params={"name": name, "type": qtype})
            except Exception:
                br.failure()
                continue
            if resp.status_code in {429, 503}:
                br.failure()
                await asyncio.sleep(retry_after_seconds(resp, default=1.0, cap=10.0))
                continue
            if resp.status_code != 200:
                br.failure()
                return None
            try:
                data = resp.json()
            except ValueError:
                br.failure()
                return None
            if not isinstance(data, dict) or data.get("Status") not in {0, 3}:
                # SERVFAIL / REFUSED: no verdict, not a resolver failure.
                br.success()
                return None
            br.success()
            return data
        return None

    async def _confirmed(
        client: httpx.AsyncClient, name: str, qtype: str, evaluate
    ) -> tuple[bool, list[str], str | None]:
        """Return `(decided, records, detail)`; positives must be reproduced on the second resolver."""
        order = [CLOUDFLARE_DOH_URL, GOOGLE_DOH_URL]
        if breakers[CLOUDFLARE_DOH_URL].open:
            order.reverse()
        first = await _doh(client, order[0], name, qtype)
        used = order[0]
        if first is None:
            first = await _doh(client, order[1], name, qtype)
            used = order[1]
            if first is None:
                return False, [], None
        records, detail = evaluate(first)
        if detail is None:
            return True, records, None
        other = order[1] if used == order[0] else order[0]
        second = await _doh(client, other, name, qtype)
        if second is None:
            return False, records, None
        _, detail2 = evaluate(second)
        return True, records, detail if detail2 is not None else None

    def _from_cache(key: str) -> tuple[bool, str | None]:
        row = cache.get(
            DNS_PARKING_NAMESPACE,
            key,
            ttl=DNS_PARKING_TTL,
            positive_ttl=DNS_PARKING_POSITIVE_TTL,
            is_positive=lambda d: isinstance(d, dict) and bool(d.get("parked")),
        )
        if isinstance(row, dict):
            stats["cached"] += 1
            return True, row.get("parked")
        return False, None

    async def _ns_task(client: httpx.AsyncClient, reg: str) -> None:
        key = f"ns:{reg}"
        found, parked = _from_cache(key)
        if found:
            ns_hit[reg] = parked
            return
        async with sem:
            if past_deadline(deadline):
                stats["skipped_deadline"] += 1
                return
            decided, records, detail = await _confirmed(client, reg, "NS", _eval_ns)
        if not decided:
            stats["unknown"] += 1
            return
        ns_hit[reg] = detail
        cache.put(DNS_PARKING_NAMESPACE, key, {"ns": records[:8], "parked": detail})

    async def _a_task(client: httpx.AsyncClient, host: str) -> None:
        key = f"a:{host}"
        found, parked = _from_cache(key)
        if found:
            a_hit[host] = parked
            return
        async with sem:
            if past_deadline(deadline):
                stats["skipped_deadline"] += 1
                return
            decided, records, detail = await _confirmed(client, host, "A", _eval_a)
        if not decided:
            stats["unknown"] += 1
            return
        a_hit[host] = detail
        cache.put(DNS_PARKING_NAMESPACE, key, {"a": records[:8], "parked": detail})

    started = time.monotonic()
    try:
        async with make_client(
            transport,
            timeout=timeout,
            headers={"Accept": "application/dns-json"},
            max_connections=max(1, concurrency),
        ) as client:
            await asyncio.gather(
                *(_ns_task(client, r) for r in regs),
                *(_a_task(client, h) for h in host_to_urls),
            )
    except Exception as exc:  # never raise out of a checker
        print(f"dns_parking: aborted ({type(exc).__name__}: {exc})", flush=True)

    # Budget / failure fallback: reuse any-age cached verdicts for keys not refreshed this run.
    for reg in regs:
        if reg not in ns_hit:
            old = cache.get_any_age(DNS_PARKING_NAMESPACE, f"ns:{reg}")
            if old and isinstance(old[0], dict):
                ns_hit[reg] = old[0].get("parked")
                stats["stale"] += 1
    for host in host_to_urls:
        if host not in a_hit:
            old = cache.get_any_age(DNS_PARKING_NAMESPACE, f"a:{host}")
            if old and isinstance(old[0], dict):
                a_hit[host] = old[0].get("parked")
                stats["stale"] += 1

    out: dict[str, list[SiteSignal]] = {}
    hits = 0
    for host, host_urls in host_to_urls.items():
        reg = reg_of[host]
        parts = [str(x) for x in (ns_hit.get(reg), a_hit.get(host)) if x]
        if parts:
            hits += 1
            sig = (
                _parking_signal("; ".join(parts), reg, "NS", now)
                if ns_hit.get(reg)
                else _parking_signal(parts[0], host, "A", now)
            )
            for u in host_urls:
                out[u] = [sig]
    broken = [n for n, b in (("cloudflare", breakers[CLOUDFLARE_DOH_URL]), ("google", breakers[GOOGLE_DOH_URL])) if b.open]
    print(
        f"dns_parking: {len(host_to_urls)} hosts / {len(regs)} domains, {hits} parked hosts, "
        f"{stats['queries']} DoH queries, {stats['cached']} cached, {stats['stale']} stale-reused, "
        f"{stats['unknown']} undecided, {stats['skipped_deadline']} skipped (deadline)"
        + (f", circuit open: {','.join(broken)}" if broken else "")
        + f", {time.monotonic() - started:.0f}s",
        flush=True,
    )
    return out
