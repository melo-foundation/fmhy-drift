"""Certificate-Transparency renewal-cadence check (`CERT_LAPSED`) for a small, prioritized set of hosts.

Never contacts listed sites: queries only `api.certspotter.com` and `crt.sh`.

1. CertSpotter `/v1/issuances` (returns UNEXPIRED certificates only, `match_wildcards=true`): any unexpired
   cert covering the host -> not lapsed.
2. Only when CertSpotter answered "none": crt.sh JSON (full history, slow / flaky): if the host had a short-lived (<=100 d) renewal
   cadence, the newest cert has expired, and nothing was issued for (max renewal gap + 45 d) -> `CERT_LAPSED`.

Limits: CertSpotter unauthenticated = 100 issuance queries/hour (`x-ratelimit-limit: 100`, observed
2026-11-25; SSLMate docs give no number). Set `CERTSPOTTER_API_KEY` for subscription limits. crt.sh has no
published limit and blocks heavy automated use: one request at a time, 3 s apart, 40 s timeout.
"""

from __future__ import annotations

import os
import statistics
import time
from datetime import UTC, datetime, timedelta
from urllib.parse import urlsplit

import httpx

from fmhy_drift.dns_checker import KNOWN_LIVE_PLATFORM_HOSTS
from fmhy_drift.domains import is_public_dns_host, is_shared_platform_host, registrable_domain
from fmhy_drift.models import SignalKind, SiteSignal
from fmhy_drift.passive_http import CircuitBreaker, RateLimiter, make_client, past_deadline, retry_after_seconds
from fmhy_drift.signal_cache import SignalCache

CERTSPOTTER_URL = "https://api.certspotter.com/v1/issuances"
CRTSH_URL = "https://crt.sh/"

CT_NAMESPACE = "ct"
CT_TTL = timedelta(days=14)
CT_POSITIVE_TTL = timedelta(days=7)

SHORT_LIVED_MAX_DAYS = 100  # LE / ZeroSSL / GTS 90-day style (and shorter)
MIN_SHORT_ISSUANCES = 3
GRACE_DAYS = 45
SAME_RENEWAL_DAYS = 7  # issuances closer than this are one renewal (duplicates, re-issues, RSA+ECDSA pairs)
CADENCE_WINDOW = 8  # renewals considered for the cadence
CRTSH_INTERVAL_S = 3.0
CRTSH_TIMEOUT_S = 40.0  # observed 3-38 s per query on 2026-11-25


def _parse_dt(raw: object) -> datetime | None:
    if not isinstance(raw, str) or not raw:
        return None
    s = raw.strip().replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    return dt.astimezone(UTC) if dt.tzinfo else dt.replace(tzinfo=UTC)


def covers(name: str, host: str) -> bool:
    """True when certificate SAN `name` matches `host` exactly or as a single-label wildcard."""
    n = name.strip().rstrip(".").lower()
    if n == host:
        return True
    if n.startswith("*."):
        parent = n[2:]
        return host.endswith("." + parent) and "." not in host[: -len(parent) - 1]
    return False


def _host_variants(host: str) -> list[str]:
    """Names whose certificates count for `host` (the listed hostname itself; wildcards via `covers`)."""
    return [host]


def evaluate_cadence(
    certs: list[tuple[datetime, datetime]], *, now: datetime
) -> tuple[bool, str | None, datetime | None]:
    """Return `(lapsed, detail, last_issued)` from `(not_before, not_after)` pairs covering one host."""
    if not certs:
        return False, None, None
    newest_expiry = max(na for _, na in certs)
    if newest_expiry >= now:
        return False, None, None
    short = sorted(nb for nb, na in certs if (na - nb).days <= SHORT_LIVED_MAX_DAYS and nb <= now)
    renewals: list[datetime] = []
    for nb in short:
        if not renewals or (nb - renewals[-1]).days >= SAME_RENEWAL_DAYS:
            renewals.append(nb)
    renewals = renewals[-CADENCE_WINDOW:]
    if len(renewals) < MIN_SHORT_ISSUANCES:
        return False, None, None
    gaps = [(b - a).days for a, b in zip(renewals, renewals[1:])]
    if statistics.median(gaps) > SHORT_LIVED_MAX_DAYS:
        return False, None, None
    last_issued = max(nb for nb, _ in certs if nb <= now)
    silent = (now - last_issued).days
    if silent <= max(gaps) + GRACE_DAYS:
        return False, None, None
    detail = (
        f"last cert issued {last_issued:%Y-%m-%d}, expired {newest_expiry:%Y-%m-%d}, "
        f"prior cadence ~{round(statistics.median(gaps))}d"
    )
    return True, detail, last_issued


async def check_cert_cadence(
    urls: list[str],
    *,
    cache: SignalCache,
    now: datetime,
    deadline: float | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
    certspotter_api_key: str | None = None,
    max_queries: int = 90,
    crtsh_interval: float = CRTSH_INTERVAL_S,
) -> dict[str, list[SiteSignal]]:
    """Emit `CERT_LAPSED` for hosts whose short-cadence CT renewals stopped (hosts in the given priority order)."""
    api_key = certspotter_api_key if certspotter_api_key is not None else os.environ.get("CERTSPOTTER_API_KEY")
    host_urls: dict[str, list[str]] = {}
    skipped_shared = 0
    for u in dict.fromkeys(urls):
        h = (urlsplit(u).hostname or "").lower().rstrip(".")  # the listed name, `www.` kept: certs are per name
        if not is_public_dns_host(h) or h in KNOWN_LIVE_PLATFORM_HOSTS:
            continue
        if is_shared_platform_host(h):
            skipped_shared += 1  # *.github.io etc.: the platform's wildcard cert says nothing about the site
            continue
        host_urls.setdefault(h, []).append(u)

    cs_rl = RateLimiter(0.2)
    crt_rl = RateLimiter(crtsh_interval)
    cs_br = CircuitBreaker(3)
    crt_br = CircuitBreaker(3)
    stats = {"certspotter": 0, "crtsh": 0, "cached": 0, "undecided": 0}
    verdicts: dict[str, dict] = {}
    budget_note = ""

    async def _certspotter(client: httpx.AsyncClient, host: str) -> bool | None:
        """True = unexpired cert covers host, False = none, None = unavailable."""
        if cs_br.open or stats["certspotter"] >= max_queries:
            return None
        await cs_rl.wait()
        stats["certspotter"] += 1
        headers = {"Authorization": f"Bearer {api_key}"} if api_key else None
        try:
            resp = await client.get(
                CERTSPOTTER_URL,
                params=[
                    ("domain", host),
                    ("include_subdomains", "false"),
                    ("match_wildcards", "true"),
                    ("expand", "dns_names"),
                ],
                headers=headers,
            )
        except Exception:
            cs_br.failure()
            return None
        if resp.status_code == 429:
            cs_br.trip()  # hourly quota gone; Retry-After is typically tens of minutes
            return None
        if resp.status_code != 200:
            cs_br.failure()
            return None
        try:
            rows = resp.json()
        except ValueError:
            cs_br.failure()
            return None
        cs_br.success()
        if not isinstance(rows, list):
            return None
        for row in rows:
            if not isinstance(row, dict) or row.get("revoked"):
                continue
            na = _parse_dt(row.get("not_after"))
            names = row.get("dns_names") or []
            if na and na >= now and any(covers(str(n), v) for n in names for v in _host_variants(host)):
                return True
        return False

    async def _crtsh(client: httpx.AsyncClient, query: str) -> list[dict] | None:
        if crt_br.open or stats["crtsh"] >= max_queries or past_deadline(deadline):
            return None
        await crt_rl.wait()
        stats["crtsh"] += 1
        try:
            resp = await client.get(
                CRTSH_URL,
                params={"q": query, "output": "json", "deduplicate": "Y"},
                timeout=CRTSH_TIMEOUT_S,
            )
        except Exception:
            crt_br.failure()
            return None
        if resp.status_code in {429, 503}:
            crt_br.failure()
            crt_rl.penalize(retry_after_seconds(resp, default=30.0, cap=60.0))
            return None
        if resp.status_code != 200:
            crt_br.failure()
            return None
        try:
            rows = resp.json()
        except ValueError:
            crt_br.failure()
            return None
        crt_br.success()
        return [r for r in rows if isinstance(r, dict)] if isinstance(rows, list) else None

    def _covering(rows: list[dict], host: str) -> list[tuple[datetime, datetime]]:
        out: list[tuple[datetime, datetime]] = []
        variants = _host_variants(host)
        for r in rows:
            names = str(r.get("name_value") or "").split("\n") + [str(r.get("common_name") or "")]
            if not any(covers(n, v) for n in names if n for v in variants):
                continue
            nb, na = _parse_dt(r.get("not_before")), _parse_dt(r.get("not_after"))
            if nb and na:
                out.append((nb, na))
        return out

    async def _check(client: httpx.AsyncClient, host: str) -> dict | None:
        live = await _certspotter(client, host)
        if live:
            return {"lapsed": False}
        if live is None:
            # crt.sh alone is not trusted to prove absence: it lags / skips some newer (static-API) CT logs,
            # e.g. a 2026-10-24 Let's Encrypt cert for musicdex.org was in CertSpotter but not crt.sh.
            return None
        rows = await _crtsh(client, host)
        if rows is None:
            return None
        certs = _covering(rows, host)
        parent = host.split(".", 1)[1] if "." in host else ""
        if host != registrable_domain(host) and parent and registrable_domain(parent):
            lapsed, _, _ = evaluate_cadence(certs, now=now)
            if certs and not lapsed and max(na for _, na in certs) >= now:
                return {"lapsed": False}
            wild = await _crtsh(client, f"*.{parent}")
            if wild is None:
                return None
            certs += _covering(wild, host)
        if not certs:
            return {"lapsed": False}
        lapsed, detail, last_issued = evaluate_cadence(certs, now=now)
        if not lapsed:
            return {"lapsed": False}
        return {
            "lapsed": True,
            "detail": detail,
            "observed_at": last_issued.isoformat() if last_issued else None,
        }

    started = time.monotonic()
    try:
        async with make_client(transport, timeout=15.0) as client:
            for host in host_urls:
                cached = cache.get(
                    CT_NAMESPACE,
                    host,
                    ttl=CT_TTL,
                    positive_ttl=CT_POSITIVE_TTL,
                    is_positive=lambda d: isinstance(d, dict) and bool(d.get("lapsed")),
                )
                if isinstance(cached, dict):
                    verdicts[host] = cached
                    stats["cached"] += 1
                    continue
                if past_deadline(deadline):
                    budget_note = ", deadline reached"
                    break
                if cs_br.open or stats["certspotter"] >= max_queries:
                    budget_note = ", query budget exhausted"
                    break
                try:
                    res = await _check(client, host)
                except Exception:
                    res = None
                if res is None:
                    stats["undecided"] += 1
                    continue
                verdicts[host] = res
                cache.put(CT_NAMESPACE, host, res)
    except Exception as exc:  # never raise out of a checker
        print(f"ct: aborted ({type(exc).__name__}: {exc})", flush=True)

    stale = 0
    for host in host_urls:
        if host not in verdicts:
            old = cache.get_any_age(CT_NAMESPACE, host)
            if old and isinstance(old[0], dict):
                verdicts[host] = old[0]
                stale += 1

    out: dict[str, list[SiteSignal]] = {}
    hits = 0
    for host, v in verdicts.items():
        if not v.get("lapsed"):
            continue
        obs = _parse_dt(v.get("observed_at"))
        sig = SiteSignal(
            kind=SignalKind.CERT_LAPSED,
            severity="warning",
            source="crt.sh",
            detail=str(v.get("detail") or ""),
            observed_at=obs,
            evidence_url=f"https://crt.sh/?q={host}",
        )
        hits += 1
        for u in host_urls[host]:
            out[u] = [sig]
    print(
        f"ct: {len(host_urls)} hosts ({skipped_shared} shared-platform skipped), {hits} lapsed, "
        f"{stats['certspotter']} CertSpotter / {stats['crtsh']} crt.sh queries, {stats['cached']} cached, "
        f"{stale} stale-reused, {stats['undecided']} undecided"
        + ("" if api_key else ", CertSpotter unauthenticated (set CERTSPOTTER_API_KEY for higher limits)")
        + (", CertSpotter circuit open" if cs_br.open else "")
        + (", crt.sh circuit open" if crt_br.open else "")
        + budget_note
        + f", {time.monotonic() - started:.0f}s",
        flush=True,
    )
    return out
