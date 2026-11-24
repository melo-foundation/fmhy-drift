"""Proxy-free DNS-over-HTTPS (DoH) preliminary dead/parked domain checker.

Queries ONLY Cloudflare (`1.1.1.1`) and Google (`8.8.8.8`) DoH JSON endpoints—never
sending any traffic directly to FMHY target websites from the runner/host IP.
"""

from __future__ import annotations

import asyncio
import ipaddress
import time
from dataclasses import replace
from datetime import UTC, datetime
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
