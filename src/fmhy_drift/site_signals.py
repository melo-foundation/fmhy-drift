"""Orchestrates the passive site-signal checkers under one time budget and merges their signals into site results.

Phases (all third-party APIs only; listed sites never see runner traffic):

A. DNS parking (DoH), storefront listings, URL reputation feeds -- concurrently (independent services).
B. FMHY listing dates (GitHub), then registry RDAP (needs the listing dates for re-registration checks).
C. Archive consensus (Wayback / Common Crawl) on a prioritised suspect list, then CT renewal cadence on the top
   suspects, scanner confirmation (urlscan.io / Cloudflare) on the top ~40, and the rotating archive remainder.

Each phase gets a share of `budget_seconds`; time a phase does not use flows to the archive remainder, which is the
only checker that can always use more time (~4.6 s per origin at the Wayback CDX limit).
"""

from __future__ import annotations

import asyncio
import time
from collections import Counter
from collections.abc import Awaitable
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlsplit

import httpx

from fmhy_drift.archive_checker import ARCHIVE_NAMESPACE, check_archives
from fmhy_drift.ct_checker import CT_NAMESPACE, check_cert_cadence
from fmhy_drift.dns_checker import check_dns_parking
from fmhy_drift.domains import url_host
from fmhy_drift.listing_dates import fetch_listing_dates
from fmhy_drift.models import (
    DEAD_SIGNAL_KINDS,
    SignalKind,
    SiteCheckResult,
    SiteSignal,
    SiteStatus,
    TierThresholds,
)
from fmhy_drift.rdap_checker import check_rdap
from fmhy_drift.reputation_checker import check_reputation
from fmhy_drift.scan_confirm import CACHE_NS as SCAN_NAMESPACE
from fmhy_drift.scan_confirm import confirm_with_scanners
from fmhy_drift.signal_cache import SignalCache
from fmhy_drift.storefront_checker import (
    STOREFRONT_HOSTS,
    check_storefronts,
    is_storefront_url,
)

# Budget shares (fractions of `budget_seconds`). Phase A is dominated by the cold DoH parking sweep (~3 min for
# 15k hosts); RDAP is rate limited per registry; CT is capped by query count rather than time.
SHARE_PHASE_A = 0.30
SHARE_PHASE_B = 0.25
SHARE_ARCHIVE_SUSPECTS = 0.15
SHARE_CT = 0.10

SCAN_MAX = 40
CT_MAX_QUERIES_AUTH = 90
# Unauthenticated CertSpotter allows 100 issuance queries/hour per IP, shared with
# `enrich_unhealthy_sites_last_active` (dead-DNS last-active dates) earlier in the same run.
CT_MAX_QUERIES_UNAUTH = 50
CT_MAX_SUSPECTS = 200

SEVERITY_RANK = {"info": 0, "notice": 1, "warning": 2, "critical": 3}

# Suspect weights for ordering archive / CT / scanner work (higher first).
_SUSPECT_WEIGHTS: dict[SignalKind, int] = {
    SignalKind.REREGISTERED: 40,
    SignalKind.DROPCATCH_REGISTRAR: 35,
    SignalKind.DOMAIN_EXPIRED: 35,
    SignalKind.DOMAIN_HELD: 35,
    SignalKind.PARKED_DNS: 30,
    SignalKind.STORE_REMOVED: 30,
    SignalKind.SCAN_DOWN: 25,
    SignalKind.ARCHIVE_DOWN: 25,
    SignalKind.ARCHIVE_MOVED: 25,
    SignalKind.CERT_LAPSED: 20,
}
_DNS_DEAD_WEIGHT = 50
_SUSPECT_SIGNAL_KINDS = frozenset({SignalKind.ARCHIVE_DOWN, SignalKind.ARCHIVE_MOVED, SignalKind.CERT_LAPSED})
_KEPT_STATUSES = frozenset({SiteStatus.DEAD, SiteStatus.PARKED, SiteStatus.TLS_ERROR})
_SIGNAL_KIND_VALUES = frozenset(k.value for k in SignalKind)


def is_platform_store_url(url: str) -> bool:
    """Store / registry URLs (listings and bare store hosts): passive domain checks would describe the platform."""
    return is_storefront_url(url) or url_host(url) in STOREFRONT_HOSTS


def _is_dns_result(sr: SiteCheckResult | None) -> bool:
    return bool(sr and sr.is_unhealthy and "DNS" in sr.reason)


def _is_nxdomain(sr: SiteCheckResult | None) -> bool:
    return bool(sr and sr.status == SiteStatus.DEAD and "NXDOMAIN" in sr.reason)


def _signal_derived(sr: SiteCheckResult) -> bool:
    """True when `sr` was built from signals by an earlier run (reused report): rebuild it from this run's signals."""
    if sr.status == SiteStatus.FLAGGED:
        return True
    head = sr.reason.split(":", 1)[0].strip()
    return head in _SIGNAL_KIND_VALUES


def _dedupe(signals: list[SiteSignal]) -> list[SiteSignal]:
    seen: set[tuple] = set()
    out: list[SiteSignal] = []
    for s in signals:
        key = (s.kind, s.source, s.detail)
        if key in seen:
            continue
        seen.add(key)
        out.append(s)
    out.sort(key=lambda s: (-SEVERITY_RANK.get(s.severity, 0), s.kind.value))
    return out


def _ts(dt: datetime | None) -> float:
    if dt is None:
        return float("-inf")
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.timestamp()


def status_for_signals(existing: SiteStatus | None, signals: list[SiteSignal]) -> SiteStatus:
    """Site status after merging `signals` (existing DEAD / PARKED / TLS_ERROR always wins)."""
    if existing in _KEPT_STATUSES:
        return existing  # type: ignore[return-value]
    kinds = {s.kind for s in signals}
    scan_up = [s for s in signals if s.kind == SignalKind.SCAN_UP]
    if scan_up:
        newest_up = max(_ts(s.observed_at) for s in scan_up)
        suspects = [s for s in signals if s.kind in _SUSPECT_SIGNAL_KINDS]
        if all(newest_up >= _ts(s.observed_at) for s in suspects):
            return SiteStatus.FLAGGED
    if kinds & DEAD_SIGNAL_KINDS:
        return SiteStatus.DEAD
    if SignalKind.PARKED_DNS in kinds:
        return SiteStatus.PARKED
    return SiteStatus.FLAGGED


def _summary_reason(signals: list[SiteSignal]) -> str:
    top = signals[0]
    more = f" (+{len(signals) - 1} more)" if len(signals) > 1 else ""
    return f"{top.kind.value}: {top.detail}{more}"


def _with_last_active(sig: SiteSignal, last_seen: datetime) -> SiteSignal:
    """Date a deleted domain (registry 404 with no known expiry) by when the site was last seen working."""
    if (
        sig.kind != SignalKind.DOMAIN_EXPIRED
        or "not registered" not in sig.detail
        or "expired " in sig.detail
        or "last active" in sig.detail
    ):
        return sig
    return replace(sig, detail=f"{sig.detail}; site last active {last_seen.date().isoformat()}", observed_at=last_seen)


def merge_site_signals(
    existing: dict[str, SiteCheckResult],
    signals_by_url: dict[str, list[SiteSignal]],
) -> dict[str, SiteCheckResult]:
    """Attach `signals_by_url` to `existing` results (creating FLAGGED / DEAD / PARKED results where needed)."""
    out: dict[str, SiteCheckResult] = {}
    for url, sr in existing.items():
        if _signal_derived(sr):
            continue  # rebuilt below from this run's signals (or dropped when no signal remains)
        out[url] = replace(sr, signals=[]) if sr.signals else sr

    for url, raw in signals_by_url.items():
        sigs = _dedupe(raw)
        if not sigs:
            continue
        base = out.get(url)
        if base is not None and base.last_seen_at is not None:
            sigs = [_with_last_active(s, base.last_seen_at) for s in sigs]
        if base is None and all(s.kind == SignalKind.SCAN_UP for s in sigs):
            continue  # a lone "scanner says up" is not worth a row
        status = status_for_signals(base.status if base else None, sigs)
        if base is None:
            out[url] = SiteCheckResult(url=url, status=status, reason=_summary_reason(sigs), signals=sigs)
        else:
            reason = base.reason if status == base.status and base.reason else _summary_reason(sigs)
            out[url] = replace(base, status=status, reason=reason, signals=sigs)
    return out


def _add(acc: dict[str, list[SiteSignal]], found: dict[str, list[SiteSignal]] | None) -> None:
    for u, sigs in (found or {}).items():
        if sigs:
            acc.setdefault(u, []).extend(sigs)


async def _guard(name: str, coro: Awaitable[Any], default: Any) -> Any:
    try:
        return await coro
    except Exception as exc:  # noqa: BLE001 -- a broken checker must never take the whole run down
        print(f"site-signals: {name} failed ({type(exc).__name__}: {exc})", flush=True)
        return default


def _cached_hit(cache: SignalCache, url: str) -> int:
    """Suspect weight from earlier runs' cached archive / CT / scanner verdicts (0 when none)."""
    weight = 0
    host = url_host(url)
    arch = cache.get_any_age(ARCHIVE_NAMESPACE, host)
    if arch and isinstance(arch[0], dict) and arch[0].get("kind"):
        weight = max(weight, _SUSPECT_WEIGHTS[SignalKind.ARCHIVE_DOWN])
    raw_host = (urlsplit(url).hostname or "").lower().rstrip(".")
    ct = cache.get_any_age(CT_NAMESPACE, raw_host)
    if ct and isinstance(ct[0], dict) and ct[0].get("lapsed"):
        weight = max(weight, _SUSPECT_WEIGHTS[SignalKind.CERT_LAPSED])
    scan = cache.get_any_age(SCAN_NAMESPACE, url)
    if scan and isinstance(scan[0], dict):
        for s in scan[0].get("signals") or []:
            if isinstance(s, dict) and s.get("kind") == SignalKind.SCAN_DOWN.value:
                weight = max(weight, _SUSPECT_WEIGHTS[SignalKind.SCAN_DOWN])
    return weight


def rank_suspects(
    urls: list[str],
    *,
    existing: dict[str, SiteCheckResult],
    signals: dict[str, list[SiteSignal]],
    cache: SignalCache,
) -> list[str]:
    """Suspect URLs ordered strongest first (DNS-dead, parked, hijack, store removed, earlier archive/CT/scan hits)."""
    scored: list[tuple[int, int, str]] = []
    for idx, u in enumerate(urls):
        score = 0
        sr = existing.get(u)
        if sr is not None and sr.status == SiteStatus.DEAD and not _signal_derived(sr):
            score += _DNS_DEAD_WEIGHT
        elif sr is not None and sr.status == SiteStatus.PARKED and not _signal_derived(sr):
            score += _SUSPECT_WEIGHTS[SignalKind.PARKED_DNS]
        for s in signals.get(u, []):
            score += _SUSPECT_WEIGHTS.get(s.kind, 0)
        score += _cached_hit(cache, u)
        if score > 0:
            scored.append((score, idx, u))
    scored.sort(key=lambda t: (-t[0], t[1]))
    return [u for _, _, u in scored]


async def run_site_signals(
    urls: list[str],
    *,
    cache: SignalCache,
    now: datetime,
    thresholds: TierThresholds,
    store_thresholds: TierThresholds | None,
    budget_seconds: float,
    github_token: str | None,
    gsb_api_key: str | None,
    abusech_auth_key: str | None,
    urlscan_api_key: str | None,
    cloudflare_api_token: str | None,
    cloudflare_account_id: str | None,
    certspotter_api_key: str | None,
    existing: dict[str, SiteCheckResult],
    transport: httpx.AsyncBaseTransport | None = None,
) -> dict[str, SiteCheckResult]:
    """Run every passive checker within `budget_seconds` and return `existing` merged with the signals found."""
    t0 = time.monotonic()
    budget = max(1.0, float(budget_seconds))
    end = t0 + budget
    unique = list(dict.fromkeys(u for u in urls if u))
    store_urls = [u for u in unique if is_storefront_url(u)]
    domain_urls = [u for u in unique if not is_platform_store_url(u)]
    signals: dict[str, list[SiteSignal]] = {}

    def _deadline(share: float) -> float:
        return min(end, time.monotonic() + share * budget)

    def _count(found: dict[str, list[SiteSignal]]) -> int:
        return len(found)

    print(
        f"site-signals: {len(unique)} urls ({len(store_urls)} store listings, {len(domain_urls)} domains), "
        f"budget {budget / 60:.1f} min",
        flush=True,
    )

    # ---- Phase A: DNS parking + storefronts + reputation (independent services, run concurrently)
    pa = time.monotonic()
    dl_a = _deadline(SHARE_PHASE_A)
    parked, stores, reputation = await asyncio.gather(
        _guard(
            "dns_parking",
            check_dns_parking(domain_urls, cache=cache, now=now, deadline=dl_a, transport=transport),
            {},
        ),
        _guard(
            "storefront",
            check_storefronts(
                store_urls,
                cache=cache,
                now=now,
                thresholds=store_thresholds or thresholds,
                deadline=dl_a,
                transport=transport,
            ),
            {},
        ),
        _guard(
            "reputation",
            check_reputation(
                unique,
                cache=cache,
                now=now,
                deadline=dl_a,
                transport=transport,
                gsb_api_key=gsb_api_key or None,
                abusech_auth_key=abusech_auth_key or None,
            ),
            {},
        ),
    )
    for found in (parked, stores, reputation):
        _add(signals, found)
    print(
        f"site-signals: phase A {_count(parked)} parked, {_count(stores)} store, {_count(reputation)} reputation "
        f"urls, {time.monotonic() - pa:.0f}s",
        flush=True,
    )

    # ---- Phase B: listing dates, then RDAP
    pb = time.monotonic()
    dl_b = _deadline(SHARE_PHASE_B)
    listed: dict[str, datetime] = await _guard(
        "listing_dates",
        fetch_listing_dates(github_token=github_token or None, cache=cache, now=now, transport=transport),
        {},
    )
    rdap = await _guard(
        "rdap",
        check_rdap(domain_urls, cache=cache, now=now, listed_since=listed, deadline=dl_b, transport=transport),
        {},
    )
    _add(signals, rdap)
    print(
        f"site-signals: phase B {len(listed)} listing dates, {_count(rdap)} rdap urls, {time.monotonic() - pb:.0f}s",
        flush=True,
    )

    # ---- Phase C: archive on suspects, then CT + scanners + archive remainder concurrently
    pc = time.monotonic()
    suspects = rank_suspects(domain_urls, existing=existing, signals=signals, cache=cache)
    archive_found: dict[str, list[SiteSignal]] = {}
    if suspects:
        arch1 = await _guard(
            "archive",
            check_archives(
                suspects,
                cache=cache,
                now=now,
                deadline=_deadline(SHARE_ARCHIVE_SUSPECTS),
                transport=transport,
                priority_urls=suspects,
            ),
            {},
        )
        _add(archive_found, arch1)
    # Re-rank with this run's archive hits so CT / scanners see them.
    merged_now: dict[str, list[SiteSignal]] = {u: list(s) for u, s in signals.items()}
    _add(merged_now, archive_found)
    suspects = rank_suspects(domain_urls, existing=existing, signals=merged_now, cache=cache)

    ct_targets = [u for u in suspects if not _is_dns_result(existing.get(u))][:CT_MAX_SUSPECTS]
    scan_targets = [u for u in suspects if not _is_nxdomain(existing.get(u))][:SCAN_MAX]
    ct_max = CT_MAX_QUERIES_AUTH if certspotter_api_key else CT_MAX_QUERIES_UNAUTH

    async def _no_signals() -> dict[str, list[SiteSignal]]:
        return {}

    ct_found, scan_found, arch2 = await asyncio.gather(
        _guard(
            "ct",
            check_cert_cadence(
                ct_targets,
                cache=cache,
                now=now,
                deadline=_deadline(SHARE_CT),
                transport=transport,
                certspotter_api_key=certspotter_api_key or None,
                max_queries=ct_max,
            )
            if ct_targets
            else _no_signals(),
            {},
        ),
        _guard(
            "scan_confirm",
            confirm_with_scanners(
                scan_targets,
                cache=cache,
                now=now,
                deadline=end,
                transport=transport,
                urlscan_api_key=urlscan_api_key or None,
                cloudflare_api_token=cloudflare_api_token or None,
                cloudflare_account_id=cloudflare_account_id or None,
                max_scans=SCAN_MAX,
            )
            if scan_targets
            else _no_signals(),
            {},
        ),
        _guard(
            "archive",
            check_archives(
                domain_urls,
                cache=cache,
                now=now,
                deadline=end,
                transport=transport,
                priority_urls=suspects,
            ),
            {},
        ),
    )
    # Pass 2 re-reports pass-1 hosts from cache, so it supersedes pass 1 for the URLs it covers.
    archive_final = dict(archive_found)
    archive_final.update(arch2)
    for found in (archive_final, ct_found, scan_found):
        _add(signals, found)
    print(
        f"site-signals: phase C {len(suspects)} suspects, {_count(archive_final)} archive, {_count(ct_found)} ct "
        f"({len(ct_targets)} candidates), {_count(scan_found)} scan ({len(scan_targets)} candidates), "
        f"{time.monotonic() - pc:.0f}s",
        flush=True,
    )

    merged = merge_site_signals(existing, signals)
    kinds = Counter(k.value for sr in merged.values() for k in sr.signal_kinds)
    kinds_txt = ", ".join(f"{k} {n}" for k, n in sorted(kinds.items())) or "none"
    print(
        f"site-signals: {sum(1 for sr in merged.values() if sr.signals)} urls with signals ({kinds_txt}), "
        f"{time.monotonic() - t0:.0f}s total",
        flush=True,
    )
    return merged
