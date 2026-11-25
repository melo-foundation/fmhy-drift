"""Registry RDAP checks: domains re-registered after FMHY listed them, drop-catch registrars, expired / held domains.

Queries only registry RDAP servers (from the IANA bootstrap), `data.iana.org`, and `rdap.org` (to discover servers for
TLDs missing from the bootstrap). Never contacts the listed site.
"""

from __future__ import annotations

import asyncio
import hashlib
import re
import time
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlsplit

import httpx

from fmhy_drift.domains import (
    is_shared_platform_host,
    registrable_domain,
    site_key,
    url_host,
)
from fmhy_drift.models import SignalKind, SiteSignal
from fmhy_drift.passive_http import (
    CircuitBreaker,
    RateLimiter,
    make_client,
    past_deadline,
    retry_after_seconds,
)
from fmhy_drift.signal_cache import SignalCache

BOOTSTRAP_URL = "https://data.iana.org/rdap/dns.json"
RDAP_ORG_URL = "https://rdap.org/domain/"
ICANN_LOOKUP_URL = "https://lookup.icann.org/en/lookup?name="

CACHE_NS = "rdap"
META_NS = "rdap_meta"
CACHE_SCHEMA = 1

TTL_BASE = timedelta(days=28)
TTL_JITTER_HOURS = (
    7 * 24
)  # spreads refreshes of a first-run batch over a week of weekly runs
TTL_NOTFOUND = timedelta(days=7)
TTL_BOOTSTRAP = timedelta(days=7)
TTL_DISCOVERY = timedelta(days=30)

# Registration within this window after the listing date is treated as noise (timezones, registry date rounding,
# a site announcing a fresh domain on the day FMHY added it).
REREG_GRACE = timedelta(days=7)
# An expiry date with no hold / redemption / pending-delete status is only reported once it is this far in the past
# when fetched: registrars renew inside their 30-45 day grace window and some registries show the old date meanwhile.
# Measured 2026-11-25: mkvtoolnix.download, fing.com, organism.earth and canaldoensino.com.br showed expiry 1-13 days
# past with no status and all still resolved; every domain with a hold / redemption status returned NXDOMAIN.
EXPIRY_GRACE = timedelta(days=45)
# A drop-catch registration this recent is reported as a warning regardless of listing dates.
DROPCATCH_RECENT = timedelta(days=365)

# Registry servers that serve TLDs absent from the IANA bootstrap (verified 2026-11 by querying nic.<tld>).
EXTRA_SERVERS: dict[str, str] = {
    tld: "https://rdap.identitydigital.services/rdap/"
    for tld in ("io", "me", "sh", "ac", "ag", "bz", "lc", "mn", "sc", "vc", "gi", "pr")
}

# TLDs whose RDAP `registration` event is not the original creation date. Tonic (.to) reports 1337x.to as
# registered 2022-11-01 and lucida.to as 2025-02-01 although both resolved and served the same site before that.
UNRELIABLE_REGISTRATION_TLDS = frozenset({"to"})

# Second-level zones sold by the TLD registry itself (so its RDAP 404 means "unregistered").
REGISTRY_SOLD_SECOND_LEVEL = frozenset(
    {
        "co.uk",
        "org.uk",
        "me.uk",
        "net.uk",
        "ltd.uk",
        "plc.uk",
        "com.au",
        "net.au",
        "org.au",
        "com.br",
        "net.br",
    }
)

DEFAULT_SERVER_CONCURRENCY = 3
SERVER_CONCURRENCY: dict[str, int] = {
    "rdap.verisign.com": 8,
    "tld-rdap.verisign.com": 4,
    "rdap.identitydigital.services": 4,
    "pubapi.registry.google": 2,
}
# Minimum seconds between request starts. Measured 2026-11-25 from one IP: PIR answered 429 with Retry-After ~2900 s
# after ~300 lookups in ~40 min; Identity Digital answered 429 with Retry-After ~85700 s (a daily quota) after ~200
# lookups; Google and SIDN answered short 429s (no Retry-After) at ~1-2 req/s. Verisign took 7+ req/s with no 429.
SERVER_INTERVAL: dict[str, float] = {
    "rdap.publicinterestregistry.org": 6.0,
    "rdap.identitydigital.services": 3.0,
    "pubapi.registry.google": 1.0,
    "rdap.sidn.nl": 3.0,
}
# Lookups per run, kept under the measured quota so the rest of the quota window is not burned on 429s.
SERVER_RUN_CAP: dict[str, int] = {
    "rdap.publicinterestregistry.org": 300,
    "rdap.identitydigital.services": 150,
}
# A Retry-After longer than this means a quota window, not a burst limit: stop using the server and remember until when.
QUOTA_RETRY_AFTER = 300.0
GLOBAL_CONCURRENCY = 32
BREAKER_THRESHOLD = 6
MAX_ATTEMPTS = 3
RETRY_AFTER_CAP = 60.0
REQUEST_TIMEOUT = 20.0

HOLD_STATUSES = {
    "redemptionperiod": "redemption period",
    "pendingdelete": "pending delete",
    "pendingrestore": "pending restore",
    "serverhold": "server hold",
    "clienthold": "client hold",
}
# Statuses that only occur after expiry (gTLD lifecycle: renew grace -> redemption -> pending delete).
EXPIRY_LIFECYCLE_STATUSES = frozenset({"redemptionperiod", "pendingdelete", "pendingrestore"})

# Drop-catch / backorder / aftermarket shell registrars, as IANA registrar ID ranges. Derived 2026-11 from
# https://www.iana.org/assignments/registrar-ids/registrar-ids-1.csv by grouping accredited registrars on their RDAP
# base URL: each family runs hundreds of shell accreditations so it gets more parallel connections at drop time.
# The retail parent of a family (Dynadot Inc 472, Gname.com 1923, Sav.com 609, Register.com 9) is excluded.
_DROPCATCH_ID_RANGES: dict[str, tuple[tuple[int, int], ...]] = {
    "DropCatch/NameBright": (
        (1441, 1441),
        (1529, 1529),
        (1535, 1535),
        (1537, 1541),
        (1566, 1580),
        (1610, 1618),
        (1677, 1695),
        (1756, 1856),
        (1926, 2225),
        (2505, 2804),
        (3255, 3754),
        (4590, 4889),
    ),
    "SnapNames/NameJet": (
        (66, 66),
        (144, 144),
        (270, 270),
        (413, 413),
        (448, 448),
        (606, 606),
        (619, 619),
        (635, 635),
        (646, 647),
        (650, 650),
        (652, 656),
        (658, 659),
        (661, 663),
        (666, 666),
        (672, 672),
        (682, 682),
        (698, 708),
        (719, 719),
        (734, 734),
        (743, 743),
        (748, 748),
        (750, 755),
        (762, 763),
        (771, 771),
        (780, 782),
        (785, 785),
        (792, 792),
        (845, 848),
        (850, 851),
        (853, 856),
        (858, 859),
        (861, 864),
        (866, 879),
        (881, 886),
        (902, 903),
        (910, 910),
        (912, 912),
        (914, 915),
        (917, 923),
        (931, 934),
        (944, 947),
        (967, 968),
        (970, 971),
        (980, 981),
        (1008, 1010),
        (1012, 1014),
        (1022, 1029),
        (1031, 1031),
        (1033, 1038),
        (1042, 1051),
        (1053, 1067),
        (1069, 1072),
        (1075, 1075),
        (1077, 1078),
        (1157, 1173),
        (1175, 1235),
        (1263, 1289),
        (1292, 1297),
        (1299, 1314),
        (1341, 1344),
        (1346, 1360),
        (1593, 1597),
        (1633, 1633),
        (1642, 1644),
        (1668, 1672),
        (1715, 1715),
        (1737, 1737),
        (1751, 1751),
        (2226, 2250),
        (2252, 2279),
        (2329, 2353),
        (2806, 2840),
        (2842, 2896),
        (2898, 2905),
        (3796, 3800),
        (3807, 3811),
    ),
    "Pheenix": ((888, 888), (2375, 2474)),
    "Gname shells": (
        (3941, 3950),
        (3980, 4019),
        (4043, 4142),
        (4164, 4312),
        (4343, 4542),
    ),
    "Dynadot shells": (
        (1559, 1559),
        (1637, 1638),
        (1651, 1653),
        (1718, 1720),
        (1864, 1867),
        (2282, 2286),
        (4031, 4035),
    ),
    "Sav.com shells": ((3891, 3940),),
    "XZ.com": (
        (817, 817),
        (913, 913),
        (1019, 1019),
        (1411, 1411),
        (1863, 1863),
        (1870, 1873),
        (1899, 1900),
        (1902, 1905),
        (1907, 1907),
        (1919, 1922),
    ),
    "17 Domain shells": ((3771, 3771), (3872, 3872), (3876, 3879)),
    "SecureBackorder": ((3958, 3958),),
}

# Retail brands that share a drop-catch family's back end (Web.com brands on the SnapNames platform).
_DROPCATCH_EXCLUDE_IDS = frozenset({9, 448, 606, 886, 1593})

# Name patterns, for registries that report no IANA ID (ccTLDs) and for shells accredited after the table above.
_DROPCATCH_NAME_RE = re.compile(
    r"dropcatch|snapnames|namejet|pheenix|backorder|namebright|turncommerce"
    r"|^dynadot\s*\d+\b|^gname\s+\d+\b|^sav\.com,?\s+llc\s*-\s*\d+|^17 domain \d",
    re.IGNORECASE,
)

# Last run diagnostics (per-server request counts and timing) for the CLI / manual verification.
LAST_RUN_STATS: dict[str, Any] = {}


def dropcatch_family(
    name: str | None, iana_id: str | None, *, gtld: bool
) -> str | None:
    """Drop-catch family label for a registrar, or None. IANA IDs are trusted only on gTLDs (ccTLDs number freely)."""
    if gtld and iana_id and iana_id.isdigit():
        rid = int(iana_id)
        if rid in _DROPCATCH_EXCLUDE_IDS:
            return None
        for fam, ranges in _DROPCATCH_ID_RANGES.items():
            if any(lo <= rid <= hi for lo, hi in ranges):
                return fam
    if name and _DROPCATCH_NAME_RE.search(name.strip()):
        return name.strip()
    return None


def _parse_dt(raw: Any) -> datetime | None:
    if not isinstance(raw, str) or not raw.strip():
        return None
    s = raw.strip().replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        m = re.match(r"(\d{4}-\d{2}-\d{2})", s)
        if not m:
            return None
        dt = datetime.fromisoformat(m.group(1))
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def _norm_status(raw: Any) -> str:
    return re.sub(r"[^a-z]", "", str(raw).lower())


def _vcard_fn(entity: dict) -> str | None:
    vc = entity.get("vcardArray")
    if isinstance(vc, list) and len(vc) > 1 and isinstance(vc[1], list):
        for prop in vc[1]:
            if (
                isinstance(prop, list)
                and len(prop) >= 4
                and prop[0] == "fn"
                and isinstance(prop[3], str)
            ):
                return prop[3].strip() or None
    return None


def parse_rdap_domain(body: dict) -> dict[str, Any]:
    """Compact, cacheable view of an RDAP domain object (RFC 9083), tolerant of registry quirks."""
    events: dict[str, str] = {}
    for ev in body.get("events") or []:
        if not isinstance(ev, dict):
            continue
        action = str(ev.get("eventAction") or "").strip().lower()
        dt = _parse_dt(ev.get("eventDate"))
        if action and dt and action not in events:
            events[action] = dt.isoformat()
    registrar_name: str | None = None
    registrar_id: str | None = None
    for ent in body.get("entities") or []:
        if not isinstance(ent, dict):
            continue
        roles = [str(r).lower() for r in ent.get("roles") or []]
        if "registrar" not in roles:
            continue
        registrar_name = _vcard_fn(ent) or registrar_name
        for pid in ent.get("publicIds") or []:
            if isinstance(pid, dict) and "iana" in str(pid.get("type", "")).lower():
                registrar_id = str(pid.get("identifier") or "").strip() or None
        if (
            registrar_name is None
            and isinstance(ent.get("handle"), str)
            and not ent["handle"].isdigit()
        ):
            registrar_name = ent["handle"]
        break
    statuses = sorted({_norm_status(s) for s in body.get("status") or [] if s})
    return {
        "schema": CACHE_SCHEMA,
        "state": "ok",
        "registration": events.get("registration"),
        "expiration": events.get("expiration"),
        "transfer": events.get("transfer"),
        "last_changed": events.get("last changed"),
        "statuses": statuses,
        "registrar": registrar_name,
        "registrar_id": registrar_id,
    }


TTL_FLAGGED = timedelta(hours=20)


def _ttl_for(domain: str, data: Any) -> timedelta:
    if isinstance(data, dict) and data.get("state") == "notfound":
        return min(TTL_NOTFOUND, TTL_FLAGGED)
    if isinstance(data, dict) and data.get("state") == "ok":
        # Expired / held domains get renewed or released: recheck them every run instead of monthly.
        statuses = set(data.get("statuses") or [])
        exp = _parse_dt(data.get("expiration"))
        if statuses & set(HOLD_STATUSES) or (exp is not None and exp < datetime.now(UTC)):
            return TTL_FLAGGED
    jitter = int(hashlib.sha1(domain.encode()).hexdigest()[:8], 16) % TTL_JITTER_HOURS
    return TTL_BASE + timedelta(hours=jitter)


def _ascii_domain(domain: str) -> str:
    try:
        return domain.encode("idna").decode("ascii").lower()
    except UnicodeError:
        return domain.lower()


def _is_gtld(tld: str) -> bool:
    # IDN TLDs are treated like ccTLDs (name matching only): xn-- does not tell gTLD from ccTLD apart.
    return len(tld) > 2 and not tld.startswith("xn--")


def _registry_sells(domain: str) -> bool:
    labels = domain.split(".")
    if len(labels) == 2:
        return True
    return ".".join(labels[1:]) in REGISTRY_SOLD_SECOND_LEVEL


def _evidence_url(domain: str, rdap_url: str | None, tld: str) -> str:
    if _is_gtld(tld) or not rdap_url:
        return ICANN_LOOKUP_URL + domain
    return rdap_url


def build_signals(
    domain: str,
    data: dict[str, Any],
    *,
    checked_at: datetime,
    listed_since: datetime | None,
) -> list[SiteSignal]:
    tld = domain.rsplit(".", 1)[-1]
    rdap_url = data.get("url")
    evidence = _evidence_url(domain, rdap_url, tld)
    out: list[SiteSignal] = []
    checked_s = checked_at.date().isoformat()

    if data.get("state") == "notfound":
        # A registry's server answers 404 for second-level zones it does not run (ac.uk, gov.uk): only trust 404
        # for names registered directly under the TLD or under a zone the same registry sells.
        if data.get("authoritative") and _registry_sells(domain):
            last_exp = _parse_dt(data.get("last_expiration"))
            last_reg_seen = _parse_dt(data.get("last_registered_seen"))
            first_nf = _parse_dt(data.get("first_notfound_seen")) or checked_at
            parts = []
            if last_exp is not None:
                parts.append(f"expired {last_exp.date().isoformat()}")
            if last_reg_seen is not None:
                parts.append(f"deleted between {last_reg_seen.date().isoformat()} and {first_nf.date().isoformat()}")
            else:
                parts.append(f"deleted by {first_nf.date().isoformat()}")
            out.append(
                SiteSignal(
                    kind=SignalKind.DOMAIN_EXPIRED,
                    severity="warning",
                    source="rdap",
                    detail="not registered (registry RDAP 404); " + "; ".join(parts),
                    observed_at=last_exp or first_nf,
                    evidence_url=evidence,
                )
            )
        return out
    if data.get("state") != "ok":
        return out

    reg = _parse_dt(data.get("registration"))
    rereg_from = _parse_dt(data.get("rereg_from"))
    unreg_seen = _parse_dt(data.get("unregistered_seen"))
    rereg = False
    if tld in UNRELIABLE_REGISTRATION_TLDS:
        reg_for_rereg = None
    else:
        reg_for_rereg = reg
    if reg_for_rereg and listed_since and reg_for_rereg > listed_since + REREG_GRACE:
        rereg = True
        out.append(
            SiteSignal(
                kind=SignalKind.REREGISTERED,
                severity="critical",
                source="rdap",
                detail=f"registered {reg.date().isoformat()}, on FMHY since {listed_since.date().isoformat()}",
                observed_at=reg,
                evidence_url=evidence,
            )
        )
    elif reg_for_rereg and rereg_from and reg > rereg_from + timedelta(days=1):
        rereg = True
        out.append(
            SiteSignal(
                kind=SignalKind.REREGISTERED,
                severity="critical",
                source="rdap",
                detail=f"registered {reg.date().isoformat()}, previously registered {rereg_from.date().isoformat()}",
                observed_at=reg,
                evidence_url=evidence,
            )
        )
    elif reg_for_rereg and unreg_seen and reg > unreg_seen - timedelta(days=1):
        rereg = True
        out.append(
            SiteSignal(
                kind=SignalKind.REREGISTERED,
                severity="critical",
                source="rdap",
                detail=f"registered {reg.date().isoformat()}, unregistered on {unreg_seen.date().isoformat()}",
                observed_at=reg,
                evidence_url=evidence,
            )
        )

    fam = dropcatch_family(
        data.get("registrar"), data.get("registrar_id"), gtld=_is_gtld(tld)
    )
    if fam:
        reg_s = f", registered {reg.date().isoformat()}" if reg else ""
        rid = f" (IANA {data['registrar_id']})" if data.get("registrar_id") else ""
        if rereg:
            sev = "critical"
        elif (
            reg
            and listed_since
            and reg <= listed_since + REREG_GRACE
            and reg < checked_at - DROPCATCH_RECENT
        ):
            # Caught long before FMHY's current listing: the current owner bought it from the drop.
            # A recent catch stays a warning even when FMHY (re-)listed it afterwards: apkcombo.com was caught by
            # DropCatch on 2026-11-14 and re-added to FMHY on 2026-11-24.
            sev = "notice"
        else:
            sev = "warning"
        out.append(
            SiteSignal(
                kind=SignalKind.DROPCATCH_REGISTRAR,
                severity=sev,  # type: ignore[arg-type]
                source="rdap",
                detail=f"registrar {data.get('registrar') or fam}{rid}{reg_s}",
                observed_at=reg,
                evidence_url=evidence,
            )
        )

    statuses = data.get("statuses") or []
    holds = [HOLD_STATUSES[s] for s in statuses if s in HOLD_STATUSES]
    lifecycle = [HOLD_STATUSES[s] for s in statuses if s in EXPIRY_LIFECYCLE_STATUSES]
    exp = _parse_dt(data.get("expiration"))
    last_changed = _parse_dt(data.get("last_changed"))
    expired = exp is not None and exp < checked_at
    if lifecycle or (expired and exp is not None and exp < checked_at - EXPIRY_GRACE):
        parts = []
        if exp is not None:
            parts.append(f"expired {exp.date().isoformat()}")
        if holds:
            parts.append("status " + ", ".join(holds))
        out.append(
            SiteSignal(
                kind=SignalKind.DOMAIN_EXPIRED,
                severity="warning",
                source="rdap",
                detail="; ".join(parts) + f", checked {checked_s}",
                observed_at=exp if exp is not None else checked_at,
                evidence_url=evidence,
            )
        )
    elif holds:
        # A hold on an unexpired domain is a suspension (abuse / DMCA / non-payment dispute), not an expiry.
        # RDAP "last changed" is the best available date for when the hold was applied.
        since = f" since {last_changed.date().isoformat()}" if last_changed else ""
        until = f"; paid until {exp.date().isoformat()}" if exp else ""
        raw_holds = {s for s in statuses if s in HOLD_STATUSES}
        if raw_holds == {"clienthold"}:
            who = "suspended by registrar"
        elif raw_holds == {"serverhold"}:
            who = "suspended by registry"
        else:
            who = "suspended by registrar and registry"
        out.append(
            SiteSignal(
                kind=SignalKind.DOMAIN_HELD,
                severity="warning",
                source="rdap",
                detail=f"{who}, likely DMCA / abuse takedown; status {', '.join(holds)}{since}{until}",
                observed_at=last_changed or checked_at,
                evidence_url=evidence,
            )
        )
    return out


class _Server:
    """Per-host limits; Verisign serves .com and .net from separate bases on one host, so they share a budget."""

    def __init__(self, host: str) -> None:
        self.host = host
        self.sem = asyncio.Semaphore(
            SERVER_CONCURRENCY.get(self.host, DEFAULT_SERVER_CONCURRENCY)
        )
        self.breaker = CircuitBreaker(BREAKER_THRESHOLD)
        self.limiter = RateLimiter(SERVER_INTERVAL.get(host, 0.0))
        self.run_cap = SERVER_RUN_CAP.get(host)
        self.blocked_for: float | None = None  # seconds, from a quota-sized Retry-After
        self.pause_until = 0.0
        self.requests = 0
        self.ok = 0
        self.notfound = 0
        self.failed = 0
        self.rate_limited = 0
        self.first = 0.0
        self.last = 0.0


def _bootstrap_map(raw: Any) -> dict[str, str]:
    out: dict[str, str] = {}
    if not isinstance(raw, dict):
        return out
    for svc in raw.get("services") or []:
        if not (isinstance(svc, list) and len(svc) >= 2):
            continue
        urls = [u for u in svc[1] if isinstance(u, str)]
        url = next(
            (u for u in urls if u.startswith("https://")), urls[0] if urls else None
        )
        if not url:
            continue
        if not url.endswith("/"):
            url += "/"
        for tld in svc[0]:
            out[str(tld).lower()] = url
    return out


async def _load_bootstrap(
    client: httpx.AsyncClient, cache: SignalCache, stats: dict, deadline: float | None
) -> dict[str, str]:
    cached = cache.get(META_NS, "bootstrap", ttl=TTL_BOOTSTRAP)
    if isinstance(cached, dict) and cached:
        return cached
    stale = cache.get_any_age(META_NS, "bootstrap")
    if past_deadline(deadline) and stale and isinstance(stale[0], dict):
        return stale[0]
    try:
        resp = await client.get(BOOTSTRAP_URL)
        if resp.status_code == 200:
            mapping = _bootstrap_map(resp.json())
            if mapping:
                cache.put(META_NS, "bootstrap", mapping)
                return mapping
    except (httpx.HTTPError, ValueError):
        pass
    stats["bootstrap_failed"] = True
    return stale[0] if stale and isinstance(stale[0], dict) else {}


async def _discover_via_rdap_org(
    client: httpx.AsyncClient,
    cache: SignalCache,
    tld: str,
    sample: str,
    deadline: float | None,
) -> str | None:
    """Learn the RDAP base for a TLD missing from the bootstrap from rdap.org's redirect (one probe per TLD)."""
    key = f"tld:{tld}"
    hit = cache.get(META_NS, key, ttl=TTL_DISCOVERY)
    if isinstance(hit, dict):
        return hit.get("base")
    if past_deadline(deadline):
        stale = cache.get_any_age(META_NS, key)
        return stale[0].get("base") if stale and isinstance(stale[0], dict) else None
    try:
        resp = await client.get(RDAP_ORG_URL + sample, follow_redirects=False)
    except httpx.HTTPError:
        return None
    base: str | None = None
    if resp.status_code in (301, 302, 303, 307, 308):
        loc = resp.headers.get("location") or ""
        marker = "domain/" + sample
        if loc.startswith("https://") and loc.lower().endswith(marker):
            base = loc[: -len(marker)]
    elif resp.status_code not in (404,):
        return None  # rate-limited or broken: decide next run
    cache.put(META_NS, key, {"base": base})
    return base


async def check_rdap(
    urls: list[str],
    *,
    cache: SignalCache,
    now: datetime,
    listed_since: dict[str, datetime],
    deadline: float | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
) -> dict[str, list[SiteSignal]]:
    """RDAP signals per URL: REREGISTERED, DROPCATCH_REGISTRAR, DOMAIN_EXPIRED. Only URLs with signals are returned."""
    started = time.monotonic()
    stats: dict[str, Any] = {
        "domains": 0,
        "skipped_platform": 0,
        "skipped_nonpublic": 0,
        "no_rdap": 0,
        "fresh": 0,
        "due": 0,
        "queried": 0,
        "deadline_left": 0,
        "breaker_left": 0,
        "stale_used": 0,
        "cap_left": 0,
        "quota_blocked": [],
    }
    dom_urls: dict[str, list[str]] = {}
    for u in dict.fromkeys(urls):
        host = url_host(u)
        if is_shared_platform_host(host):
            stats["skipped_platform"] += 1
            continue
        dom = registrable_domain(host)
        if not dom:
            stats["skipped_nonpublic"] += 1
            continue
        dom_urls.setdefault(_ascii_domain(dom), []).append(u)
    stats["domains"] = len(dom_urls)
    if not dom_urls:
        print("rdap: no registrable domains", flush=True)
        return {}

    servers: dict[str, _Server] = {}
    authoritative_bases: set[str] = set()

    try:
        async with make_client(
            transport,
            timeout=REQUEST_TIMEOUT,
            follow_redirects=True,
            headers={"Accept": "application/rdap+json, application/json"},
            max_connections=GLOBAL_CONCURRENCY * 2,
        ) as client:
            bootstrap = await _load_bootstrap(client, cache, stats, deadline)
            authoritative_bases = set(bootstrap.values())

            due: list[str] = []
            for dom in dom_urls:
                hit = cache.get_any_age(CACHE_NS, dom)
                if (
                    hit
                    and isinstance(hit[0], dict)
                    and hit[0].get("schema") == CACHE_SCHEMA
                    and now - hit[1] <= _ttl_for(dom, hit[0])
                ):
                    stats["fresh"] += 1
                    continue
                due.append(dom)

            # Resolve a server for every due domain's TLD.
            tld_base: dict[str, str | None] = {}
            unknown: dict[str, str] = {}
            for dom in due:
                tld = dom.rsplit(".", 1)[-1]
                if tld in tld_base or tld in unknown:
                    continue
                base = bootstrap.get(tld) or EXTRA_SERVERS.get(tld)
                if base:
                    tld_base[tld] = base
                else:
                    unknown[tld] = dom
            disc_sem = asyncio.Semaphore(2)

            async def _disc(tld: str, sample: str) -> None:
                async with disc_sem:
                    tld_base[tld] = await _discover_via_rdap_org(
                        client, cache, tld, sample, deadline
                    )

            await asyncio.gather(*(_disc(t, s) for t, s in unknown.items()))

            work: list[tuple[str, _Server]] = []
            for dom in cache.stale_first(CACHE_NS, due):
                base = tld_base.get(dom.rsplit(".", 1)[-1])
                if not base:
                    stats["no_rdap"] += 1
                    continue
                host = urlsplit(base).hostname or base
                srv = servers.get(host)
                if srv is None:
                    srv = servers[host] = _Server(host)
                work.append((dom, base, srv))
            stats["due"] = len(work)

            for srv in servers.values():
                blk = cache.get_any_age(META_NS, f"block:{srv.host}")
                until = (
                    _parse_dt(blk[0].get("until"))
                    if blk and isinstance(blk[0], dict)
                    else None
                )
                if until and until > now:
                    srv.breaker.trip()
                    stats["quota_blocked"].append(srv.host)

            global_sem = asyncio.Semaphore(GLOBAL_CONCURRENCY)

            async def _one(dom: str, base: str, srv: _Server) -> None:
                async with srv.sem:
                    if past_deadline(deadline):
                        stats["deadline_left"] += 1
                        return
                    if srv.breaker.open:
                        stats["breaker_left"] += 1
                        return
                    if srv.run_cap is not None and srv.requests >= srv.run_cap:
                        stats["cap_left"] += 1
                        return
                    async with global_sem:
                        await _query(
                            client,
                            cache,
                            dom,
                            base,
                            srv,
                            base in authoritative_bases,
                            deadline,
                            stats,
                        )

            await asyncio.gather(*(_one(d, b, s) for d, b, s in work))
            for srv in servers.values():
                if srv.blocked_for:
                    until = now + timedelta(seconds=srv.blocked_for)
                    cache.put(
                        META_NS, f"block:{srv.host}", {"until": until.isoformat()}
                    )
    except Exception as exc:  # never fail the run; fall through to cached results
        print(
            f"rdap: aborted ({type(exc).__name__}: {exc}); using cached results",
            flush=True,
        )

    results: dict[str, list[SiteSignal]] = {}
    sig_counts: dict[str, int] = {}
    for dom, dom_url_list in dom_urls.items():
        hit = cache.get_any_age(CACHE_NS, dom)
        if (
            not hit
            or not isinstance(hit[0], dict)
            or hit[0].get("schema") != CACHE_SCHEMA
        ):
            continue
        data, checked_at = hit
        if now - checked_at > _ttl_for(dom, data):
            stats["stale_used"] += 1
        sigs = build_signals(
            dom,
            data,
            checked_at=checked_at,
            listed_since=listed_since.get(site_key(dom)),
        )
        if not sigs:
            continue
        for s in sigs:
            sig_counts[s.kind.value] = sig_counts.get(s.kind.value, 0) + 1
        for u in dom_url_list:
            results[u] = list(sigs)

    srv_stats = {
        s.host: {
            "requests": s.requests,
            "ok": s.ok,
            "notfound": s.notfound,
            "failed": s.failed,
            "rate_limited": s.rate_limited,
            "seconds": round(max(0.0, s.last - s.first), 1),
            "breaker_open": s.breaker.open,
        }
        for s in servers.values()
    }
    LAST_RUN_STATS.clear()
    LAST_RUN_STATS.update(
        {"stats": dict(stats), "servers": srv_stats, "signals": dict(sig_counts)}
    )
    open_breakers = [h for h, v in srv_stats.items() if v["breaker_open"]]
    print(
        f"rdap: {stats['domains']} domains, {stats['fresh']} fresh, {stats['queried']} queried, "
        f"{stats['no_rdap']} no-rdap TLD, {stats['deadline_left']} left at deadline, "
        f"{stats['breaker_left']} skipped by {len(open_breakers)} open breakers {open_breakers[:3]}, "
        f"{stats['cap_left']} over per-run cap, quota-blocked {stats['quota_blocked']}, "
        f"{stats['stale_used']} stale; signals {sig_counts}; {time.monotonic() - started:.0f}s",
        flush=True,
    )
    return results


async def _query(
    client: httpx.AsyncClient,
    cache: SignalCache,
    dom: str,
    base: str,
    srv: _Server,
    authoritative: bool,
    deadline: float | None,
    stats: dict,
) -> None:
    url = f"{base}domain/{dom}"
    for attempt in range(MAX_ATTEMPTS):
        wait = srv.pause_until - time.monotonic()
        if wait > 0:
            await asyncio.sleep(wait)
        if attempt and past_deadline(deadline):
            return
        await srv.limiter.wait()
        if not srv.first:
            srv.first = time.monotonic()
        srv.requests += 1
        stats["queried"] += 1 if attempt == 0 else 0
        try:
            resp = await client.get(url)
        except httpx.HTTPError:
            resp = None
        srv.last = time.monotonic()
        if resp is not None and resp.status_code == 200:
            try:
                body = resp.json()
            except ValueError:
                body = None
            if isinstance(body, dict) and (
                body.get("objectClassName") == "domain" or "events" in body
            ):
                data = parse_rdap_domain(body)
                data["url"] = url
                _carry_rereg(cache, dom, data)
                cache.put(CACHE_NS, dom, data)
                srv.ok += 1
                srv.breaker.success()
                return
            srv.failed += 1
            srv.breaker.failure()
            return
        if resp is not None and resp.status_code == 404:
            nf: dict[str, Any] = {
                "schema": CACHE_SCHEMA,
                "state": "notfound",
                "authoritative": authoritative,
                "url": url,
            }
            _carry_last_registered(cache, dom, nf)
            cache.put(CACHE_NS, dom, nf)
            srv.notfound += 1
            srv.breaker.success()
            return
        if resp is not None and resp.status_code in (429, 503):
            srv.rate_limited += 1
            pause = retry_after_seconds(
                resp, default=5.0 * (attempt + 1), cap=10 * 86400.0
            )
            if pause > QUOTA_RETRY_AFTER:
                srv.blocked_for = pause
                srv.breaker.trip()
                return
            srv.pause_until = max(
                srv.pause_until, time.monotonic() + min(pause, RETRY_AFTER_CAP)
            )
            srv.limiter.penalize(min(pause, RETRY_AFTER_CAP))
            continue
        if (
            resp is not None
            and resp.status_code < 500
            and resp.status_code not in (408,)
        ):
            srv.failed += 1  # 400 / 403 / 422: this server will not answer for this name; don't retry
            srv.breaker.failure()
            return
        await asyncio.sleep(1.0 * (attempt + 1))
    srv.failed += 1
    srv.breaker.failure()


def _carry_last_registered(cache: SignalCache, dom: str, nf: dict[str, Any]) -> None:
    """Keep the last registered record's expiry / statuses on a registry 404, so a deletion keeps its dates."""
    prev = cache.get_any_age(CACHE_NS, dom)
    if not prev or not isinstance(prev[0], dict):
        return
    old, old_checked = prev
    if old.get("state") == "ok":
        nf["last_expiration"] = old.get("expiration")
        nf["last_statuses"] = old.get("statuses") or []
        nf["last_registered_seen"] = old_checked.isoformat()
    elif old.get("state") == "notfound":
        for k in ("last_expiration", "last_statuses", "last_registered_seen"):
            if old.get(k):
                nf[k] = old[k]
        nf["first_notfound_seen"] = old.get("first_notfound_seen") or old_checked.isoformat()


def _carry_rereg(cache: SignalCache, dom: str, data: dict[str, Any]) -> None:
    """Record a registration date that moved forward since the previous check (deleted and registered again)."""
    prev = cache.get_any_age(CACHE_NS, dom)
    if not prev or not isinstance(prev[0], dict):
        return
    old, old_checked = prev
    new_reg = _parse_dt(data.get("registration"))
    if old.get("registration") == data.get("registration"):
        for k in ("rereg_from", "unregistered_seen"):
            if old.get(k):
                data[k] = old[k]
        return
    if (
        old.get("state") == "notfound"
        and old.get("authoritative")
        and new_reg
        and new_reg > old_checked - timedelta(days=1)
    ):
        data["unregistered_seen"] = old_checked.isoformat()
        return
    old_reg = _parse_dt(old.get("registration"))
    if (
        old.get("state") == "ok"
        and old_reg
        and new_reg
        and new_reg > old_reg + timedelta(days=1)
    ):
        data["rereg_from"] = old_reg.isoformat()
