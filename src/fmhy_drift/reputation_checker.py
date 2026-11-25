"""Passive URL-reputation checker: Google Safe Browsing, abuse.ch URLhaus / ThreatFox, OpenPhish.

Only third-party threat-intel APIs and feeds are contacted; listed sites never see traffic from the runner.

Sources (each optional, each cached separately):
- Google Safe Browsing v4 Lookup API `threatMatches:find` (env GOOGLE_SAFE_BROWSING_API_KEY), up to 500
  entries per request; both the exact URL and the bare origin are looked up.
- URLhaus bulk export (env ABUSECH_AUTH_KEY; since 2026-10-21 every abuse.ch file export needs the key in the URL,
  the legacy `urlhaus.abuse.ch/downloads/*` paths answer 410 Gone).
- ThreatFox bulk export (same ABUSECH_AUTH_KEY), domain + URL IOCs.
- OpenPhish community feed (no key; served from GitHub raw since 2026, updated every 12 h, non-commercial use only).

Feed entries for big shared platforms (Google Drive, GitHub, MEGA, MediaFire, Pastebin, cloud storage, ...) are
almost always individual uploaded files, so they only flag an FMHY URL when the feed URL lives under the FMHY URL's
own path. Tenant hosts under PSL private suffixes (`user.github.io`, `x.pages.dev`) need the same path relation,
except that a feed entry for the tenant root flags the whole tenant.
"""

from __future__ import annotations

import asyncio
import csv
import hashlib
import io
import time
import zipfile
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from urllib.parse import quote, urlsplit

import httpx

from fmhy_drift.domains import is_public_dns_host, is_shared_platform_host, registrable_domain, url_host
from fmhy_drift.models import SignalKind, SiteSignal
from fmhy_drift.signal_cache import SignalCache

USER_AGENT = "fmhy-drift-checker/0.1"

GSB_ENDPOINT = "https://safebrowsing.googleapis.com/v4/threatMatches:find"
GSB_BATCH = 500  # documented max threatEntries per threatMatches:find request
GSB_THREAT_TYPES = ["MALWARE", "SOCIAL_ENGINEERING", "UNWANTED_SOFTWARE", "POTENTIALLY_HARMFUL_APPLICATION"]
GSB_CONCURRENCY = 2

URLHAUS_EXPORT_BASE = "https://urlhaus-api.abuse.ch/v2/files/exports/{key}/{name}"
# `full.csv.zip` = online or added within 90 days; `recent.csv` (documented example) = added within 30 days.
URLHAUS_EXPORT_NAMES = ("full.csv.zip", "recent.csv")
THREATFOX_EXPORT_BASE = "https://threatfox-api.abuse.ch/v2/files/exports/{key}/{name}"
THREATFOX_EXPORT_NAMES = ("full.csv.zip", "recent.csv")
OPENPHISH_FEED = "https://openphish.com/feed.txt"

URLHAUS_RECENT_DAYS = 14  # offline URLhaus entries still count when added this recently
THREATFOX_MIN_CONFIDENCE = 50
THREATFOX_RECENT_DAYS = 90
MAX_FEED_BYTES = 256 * 1024 * 1024

CACHE_NS = "reputation"  # key: url -> {source: [signal json, ...]}
RUNS_NS = "reputation_runs"  # key: source -> {"urls": sha1 of the checked url set}
CACHE_TTL = timedelta(days=1)

SOURCE_GSB = "google_safe_browsing"
SOURCE_URLHAUS = "urlhaus"
SOURCE_THREATFOX = "threatfox"
SOURCE_OPENPHISH = "openphish"

MAX_RETRY_AFTER = 30.0
MAX_ATTEMPTS = 3
BREAKER_FAILURES = 3

# Registrable domains whose hosts serve files / pages for countless unrelated users.
BIG_SHARED_DOMAINS = frozenset(
    {
        "google.com",
        "googleapis.com",
        "googleusercontent.com",
        "goo.gl",
        "github.com",
        "githubusercontent.com",
        "gitlab.com",
        "bitbucket.org",
        "codeberg.org",
        "sourceforge.net",
        "mega.nz",
        "mega.io",
        "mediafire.com",
        "pastebin.com",
        "paste.ee",
        "dropbox.com",
        "dropboxusercontent.com",
        "live.com",
        "1drv.ms",
        "sharepoint.com",
        "box.com",
        "amazonaws.com",
        "cloudfront.net",
        "windows.net",
        "azureedge.net",
        "digitaloceanspaces.com",
        "backblazeb2.com",
        "r2.dev",
        "discord.com",
        "discordapp.com",
        "discordapp.net",
        "discord.gg",
        "t.me",
        "telegram.org",
        "telegra.ph",
        "archive.org",
        "gofile.io",
        "pixeldrain.com",
        "catbox.moe",
        "transfer.sh",
        "wetransfer.com",
        "yandex.ru",
        "yandex.com",
        "disk.yandex.ru",
        "jsdelivr.net",
        "unpkg.com",
        "huggingface.co",
        "reddit.com",
        "youtube.com",
        "x.com",
        "twitter.com",
        "facebook.com",
        "linktr.ee",
        "bit.ly",
        "tinyurl.com",
        "notion.site",
        "gitbook.io",
        "weebly.com",
        "wixsite.com",
        "blogspot.com",
        "wordpress.com",
        "medium.com",
        "4shared.com",
        "zippyshare.com",
        "anonfiles.com",
        "krakenfiles.com",
        "workupload.com",
        "sendspace.com",
        "uploadhaven.com",
        "qiwi.gg",
        "buzzheavier.com",
    }
)


@dataclass(frozen=True)
class _NormUrl:
    host: str
    segments: tuple[str, ...]
    query: str

    @property
    def is_root(self) -> bool:
        return not self.segments


@dataclass(frozen=True)
class _FeedEntry:
    url: _NormUrl
    raw: str
    detail: str
    observed_at: datetime | None
    evidence_url: str | None


def _norm(url: str) -> _NormUrl | None:
    raw = url.strip()
    if "://" not in raw:
        raw = "http://" + raw
    try:
        parts = urlsplit(raw)
    except ValueError:
        return None
    if parts.scheme.lower() not in {"http", "https"}:
        return None
    host = url_host(raw)
    if not host:
        return None
    segments = tuple(s for s in parts.path.split("/") if s)
    return _NormUrl(host=host, segments=segments, query=parts.query)


def is_big_shared_host(host: str) -> bool:
    """True for file-hosting / code-hosting / cloud-storage platforms shared by unrelated users."""
    reg = registrable_domain(host)
    if reg in BIG_SHARED_DOMAINS or host in BIG_SHARED_DOMAINS:
        return True
    return any(host.endswith("." + d) for d in BIG_SHARED_DOMAINS)


def feed_entry_matches(fmhy_url: str, feed_url: str) -> bool:
    """Whether a threat-feed URL is evidence against the FMHY-listed URL (false-positive guarded)."""
    site = _norm(fmhy_url)
    feed = _norm(feed_url)
    if site is None or feed is None:
        return False
    return _matches(site, feed)


def _segment_prefix(prefix: tuple[str, ...], full: tuple[str, ...]) -> bool:
    return len(prefix) <= len(full) and full[: len(prefix)] == prefix


def _matches(site: _NormUrl, feed: _NormUrl) -> bool:
    if site.host != feed.host:
        return False
    if site.segments == feed.segments and site.query == feed.query:
        return True
    if is_big_shared_host(site.host):
        # Shared platform: the feed URL must sit under the listed path, which must name something specific.
        if site.is_root or not _segment_prefix(site.segments, feed.segments):
            return False
        return not site.query or (site.segments != feed.segments) or site.query == feed.query
    if is_shared_platform_host(site.host):
        # PSL-private tenant (`user.github.io`): tenant root flags everything, otherwise path-prefix only.
        return feed.is_root or _segment_prefix(site.segments, feed.segments)
    return True


def _parse_dt(raw: str | None) -> datetime | None:
    if not raw:
        return None
    text = raw.strip().replace("Z", "+00:00")
    for candidate in (text, text.replace(" ", "T", 1)):
        try:
            dt = datetime.fromisoformat(candidate)
        except ValueError:
            continue
        return dt if dt.tzinfo else dt.replace(tzinfo=UTC)
    return None


def _day(dt: datetime | None) -> str:
    return dt.strftime("%Y-%m-%d") if dt else "unknown date"


def _url_set_hash(urls: list[str]) -> str:
    return hashlib.sha1("\n".join(sorted(urls)).encode("utf-8")).hexdigest()


def _retry_after(resp: httpx.Response) -> float:
    try:
        return min(max(float(resp.headers.get("Retry-After", "5")), 0.0), MAX_RETRY_AFTER)
    except ValueError:
        return 5.0


async def _request(
    client: httpx.AsyncClient,
    method: str,
    url: str,
    *,
    deadline: float | None,
    **kwargs,
) -> httpx.Response | None:
    """One request with capped Retry-After handling for 429 / 503. Returns None on network error / deadline."""
    for attempt in range(MAX_ATTEMPTS):
        if deadline is not None and time.monotonic() >= deadline:
            return None
        try:
            resp = await client.request(method, url, **kwargs)
        except (httpx.HTTPError, OSError):
            if attempt + 1 < MAX_ATTEMPTS:
                await asyncio.sleep(1.0 * (attempt + 1))
                continue
            return None
        if resp.status_code in (429, 503) and attempt + 1 < MAX_ATTEMPTS:
            wait = _retry_after(resp)
            if deadline is not None and time.monotonic() + wait >= deadline:
                return resp
            await asyncio.sleep(wait)
            continue
        return resp
    return None


def _unpack_feed(resp: httpx.Response) -> str:
    body = resp.content
    if len(body) > MAX_FEED_BYTES:
        raise ValueError("feed too large")
    if body[:2] == b"PK":
        with zipfile.ZipFile(io.BytesIO(body)) as zf:
            names = [n for n in zf.namelist() if not n.endswith("/")]
            if not names:
                raise ValueError("empty zip")
            name = next((n for n in names if n.lower().endswith((".csv", ".txt", ".json"))), names[0])
            info = zf.getinfo(name)
            if info.file_size > MAX_FEED_BYTES:
                raise ValueError("feed too large")
            body = zf.read(name)
    return body.decode("utf-8", errors="replace")


def _abusech_csv_rows(text: str, default_header: list[str]) -> list[dict[str, str]]:
    """Parse abuse.ch CSV exports: `#`-comment preamble whose last comment line is the column header."""
    header = default_header
    data_lines: list[str] = []
    for line in text.splitlines():
        if line.startswith("#"):
            candidate = line.lstrip("#").strip()
            if "," in candidate and ("dateadded" in candidate or "ioc_value" in candidate):
                header = [c.strip().strip('"') for c in next(csv.reader([candidate], skipinitialspace=True))]
            continue
        if line.strip():
            data_lines.append(line)
    rows: list[dict[str, str]] = []
    for rec in csv.reader(data_lines, skipinitialspace=True):
        rows.append({header[i]: v.strip() for i, v in enumerate(rec) if i < len(header)})
    return rows


URLHAUS_COLUMNS = ["id", "dateadded", "url", "url_status", "last_online", "threat", "tags", "urlhaus_link", "reporter"]
THREATFOX_COLUMNS = [
    "first_seen_utc",
    "ioc_id",
    "ioc_value",
    "ioc_type",
    "threat_type",
    "fk_malware",
    "malware_alias",
    "malware_printable",
    "last_seen_utc",
    "confidence_level",
    "reference",
    "tags",
    "anonymous",
    "reporter",
]


def parse_urlhaus_csv(text: str, *, now: datetime, hosts: set[str] | None = None) -> list[_FeedEntry]:
    """Current URLhaus entries: `online`, or added within URLHAUS_RECENT_DAYS."""
    out: list[_FeedEntry] = []
    cutoff = now - timedelta(days=URLHAUS_RECENT_DAYS)
    for row in _abusech_csv_rows(text, URLHAUS_COLUMNS):
        raw_url = row.get("url", "")
        norm = _norm(raw_url)
        if norm is None or (hosts is not None and norm.host not in hosts):
            continue
        status = row.get("url_status", "").lower()
        added = _parse_dt(row.get("dateadded"))
        if status != "online" and not (added and added >= cutoff):
            continue
        threat = row.get("threat") or "malware"
        out.append(
            _FeedEntry(
                url=norm,
                raw=raw_url,
                detail=f"{threat}; {status or 'unknown'}; added {_day(added)}",
                observed_at=added,
                evidence_url=row.get("urlhaus_link") or None,
            )
        )
    return out


def parse_threatfox_csv(text: str, *, now: datetime, hosts: set[str] | None = None) -> list[_FeedEntry]:
    """ThreatFox domain / URL IOCs with confidence >= THREATFOX_MIN_CONFIDENCE seen within THREATFOX_RECENT_DAYS."""
    out: list[_FeedEntry] = []
    cutoff = now - timedelta(days=THREATFOX_RECENT_DAYS)
    for row in _abusech_csv_rows(text, THREATFOX_COLUMNS):
        ioc_type = row.get("ioc_type", "").lower()
        if ioc_type not in {"domain", "url"}:
            continue
        try:
            confidence = int(row.get("confidence_level") or 0)
        except ValueError:
            confidence = 0
        if confidence < THREATFOX_MIN_CONFIDENCE:
            continue
        first = _parse_dt(row.get("first_seen_utc"))
        last = _parse_dt(row.get("last_seen_utc")) or first
        if not last or last < cutoff:
            continue
        value = row.get("ioc_value", "")
        norm = _norm(value)
        if norm is None or (hosts is not None and norm.host not in hosts):
            continue
        malware = row.get("malware_printable") or row.get("fk_malware") or "unknown"
        threat_type = row.get("threat_type") or "ioc"
        ioc_id = row.get("ioc_id", "")
        out.append(
            _FeedEntry(
                url=norm,
                raw=value,
                detail=f"{threat_type} ({malware}); {ioc_type} IOC; first seen {_day(first)}",
                observed_at=first,
                evidence_url=f"https://threatfox.abuse.ch/ioc/{ioc_id}/" if ioc_id else None,
            )
        )
    return out


def parse_openphish_feed(text: str, *, now: datetime, hosts: set[str] | None = None) -> list[_FeedEntry]:
    out: list[_FeedEntry] = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        norm = _norm(line)
        if norm is None or (hosts is not None and norm.host not in hosts):
            continue
        out.append(
            _FeedEntry(
                url=norm,
                raw=line,
                detail=f"phishing; in community feed {_day(now)}",
                observed_at=now,
                evidence_url=OPENPHISH_FEED,
            )
        )
    return out


def _match_feed(urls: list[str], entries: list[_FeedEntry], source: str) -> dict[str, list[SiteSignal]]:
    by_host: dict[str, list[_FeedEntry]] = {}
    for e in entries:
        by_host.setdefault(e.url.host, []).append(e)
    out: dict[str, list[SiteSignal]] = {}
    for url in urls:
        site = _norm(url)
        if site is None:
            continue
        hits = [e for e in by_host.get(site.host, ()) if _matches(site, e.url)]
        if not hits:
            continue
        hits.sort(key=lambda e: e.observed_at or datetime.min.replace(tzinfo=UTC), reverse=True)
        best = hits[0]
        extra = f"; {len(hits)} feed entries" if len(hits) > 1 else ""
        out[url] = [
            SiteSignal(
                kind=SignalKind.MALICIOUS,
                severity="critical",
                source=source,
                detail=f"{best.detail}; {best.raw}{extra}",
                observed_at=best.observed_at,
                evidence_url=best.evidence_url,
            )
        ]
    return out


def _gsb_origin(url: str) -> str | None:
    try:
        parts = urlsplit(url.strip())
    except ValueError:
        return None
    if parts.scheme.lower() not in {"http", "https"} or not parts.hostname:
        return None
    return f"{parts.scheme.lower()}://{parts.netloc.lower()}/"


def _gsb_valid(url: str) -> bool:
    try:
        parts = urlsplit(url.strip())
    except ValueError:
        return False
    return parts.scheme.lower() in {"http", "https"} and bool(parts.hostname) and " " not in url.strip()


async def _check_gsb(
    client: httpx.AsyncClient,
    urls: list[str],
    *,
    api_key: str,
    now: datetime,
    deadline: float | None,
) -> tuple[dict[str, list[SiteSignal]], set[str], str]:
    """Returns (signals, urls that could not be checked, summary)."""
    entry_owners: dict[str, set[str]] = {}
    for url in urls:
        if not _gsb_valid(url):
            continue
        entry_owners.setdefault(url.strip(), set()).add(url)
        host = url_host(url)
        origin = _gsb_origin(url)
        if origin and origin != url.strip() and not is_big_shared_host(host) and is_public_dns_host(host):
            entry_owners.setdefault(origin, set()).add(url)
    entries = list(entry_owners)
    batches = [entries[i : i + GSB_BATCH] for i in range(0, len(entries), GSB_BATCH)]
    sem = asyncio.Semaphore(GSB_CONCURRENCY)
    failures = 0
    failed_entries: set[str] = set()
    matches: list[dict] = []
    requests_made = 0

    async def _post(batch: list[str], depth: int = 0) -> None:
        nonlocal failures, requests_made
        if failures >= BREAKER_FAILURES:
            failed_entries.update(batch)
            return
        body = {
            "client": {"clientId": "fmhy-drift-checker", "clientVersion": "0.1"},
            "threatInfo": {
                "threatTypes": GSB_THREAT_TYPES,
                "platformTypes": ["ANY_PLATFORM"],
                "threatEntryTypes": ["URL"],
                "threatEntries": [{"url": u} for u in batch],
            },
        }
        async with sem:
            resp = await _request(client, "POST", GSB_ENDPOINT, params={"key": api_key}, json=body, deadline=deadline)
            requests_made += 1
        if resp is not None and resp.status_code == 400 and len(batch) > 1 and depth < 4:
            # One unparsable URL rejects the whole batch: bisect a few levels to isolate it.
            mid = len(batch) // 2
            await asyncio.gather(_post(batch[:mid], depth + 1), _post(batch[mid:], depth + 1))
            return
        if resp is None or resp.status_code != 200:
            failures += 1
            failed_entries.update(batch)
            return
        failures = 0
        try:
            payload = resp.json()
        except ValueError:
            failed_entries.update(batch)
            return
        if isinstance(payload, dict):
            matches.extend(m for m in payload.get("matches") or [] if isinstance(m, dict))

    await asyncio.gather(*(_post(b) for b in batches))

    per_url: dict[str, dict[str, tuple[str, str]]] = {}
    for m in matches:
        threat_url = str((m.get("threat") or {}).get("url") or "")
        threat_type = str(m.get("threatType") or "THREAT")
        for owner in entry_owners.get(threat_url, ()):
            scope = "exact URL" if threat_url == owner.strip() else "origin"
            per_url.setdefault(owner, {}).setdefault(threat_type, (threat_url, scope))
    out: dict[str, list[SiteSignal]] = {}
    for owner, types in per_url.items():
        parts = [f"{t} ({scope} {u})" for t, (u, scope) in sorted(types.items())]
        out[owner] = [
            SiteSignal(
                kind=SignalKind.MALICIOUS,
                severity="critical",
                source=SOURCE_GSB,
                detail=f"{'; '.join(parts)}; checked {_day(now)}",
                observed_at=now,
                evidence_url="https://transparencyreport.google.com/safe-browsing/search?url="
                + quote(owner.strip(), safe=""),
            )
        ]
    unchecked = {o for e in failed_entries for o in entry_owners.get(e, ())}
    summary = f"{len(entries)} entries/{requests_made} requests, {len(out)} hits"
    if unchecked:
        summary += f", {len(unchecked)} unchecked"
    return out, unchecked, summary


async def _download_first(
    client: httpx.AsyncClient, candidates: list[str], *, deadline: float | None
) -> tuple[str | None, str]:
    """GET the first candidate that answers 200; returns (text, note)."""
    note = "no response"
    for url in candidates:
        resp = await _request(client, "GET", url, deadline=deadline)
        if resp is None:
            note = "network error / deadline"
            break
        if resp.status_code == 200:
            try:
                return _unpack_feed(resp), url.rsplit("/", 1)[-1]
            except (ValueError, zipfile.BadZipFile) as exc:
                note = f"bad payload ({exc})"
                continue
        note = f"HTTP {resp.status_code}"
        if resp.status_code in (401, 403, 429):
            break  # bad key / quota: other file names will fail the same way
    return None, note


async def check_reputation(
    urls: list[str],
    *,
    cache: SignalCache,
    now: datetime,
    deadline: float | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
    gsb_api_key: str | None = None,
    abusech_auth_key: str | None = None,
    use_openphish: bool = True,
    use_threatfox: bool = True,
) -> dict[str, list[SiteSignal]]:
    unique = list(dict.fromkeys(u.strip() for u in urls if u and u.strip()))
    if not unique:
        return {}
    url_hash = _url_set_hash(unique)
    hosts = {n.host for n in (_norm(u) for u in unique) if n is not None}
    log: list[str] = []
    results: dict[str, dict[str, list[SiteSignal]]] = {}  # source -> url -> signals

    def _cached_positive(source: str, url: str) -> list[SiteSignal]:
        hit = cache.get_any_age(CACHE_NS, url)
        if not hit or not isinstance(hit[0], dict):
            return []
        raw = hit[0].get(source) or []
        return [s for s in (SiteSignal.from_json(r) for r in raw if isinstance(r, dict)) if s is not None]

    def _from_cache(source: str, subset: list[str] | set[str]) -> dict[str, list[SiteSignal]]:
        out: dict[str, list[SiteSignal]] = {}
        for url in subset:
            sigs = _cached_positive(source, url)
            if sigs:
                out[url] = sigs
        return out

    def _store(source: str, found: dict[str, list[SiteSignal]], checked: list[str]) -> None:
        for url in checked:
            hit = cache.get_any_age(CACHE_NS, url)
            row = dict(hit[0]) if hit and isinstance(hit[0], dict) else {}
            sigs = found.get(url)
            if sigs:
                row[source] = [s.to_json() for s in sigs]
            elif row.get(source):
                row[source] = []
            else:
                continue
            cache.put(CACHE_NS, url, row)

    def _fresh(source: str) -> bool:
        marker = cache.get(RUNS_NS, source, ttl=CACHE_TTL)
        return isinstance(marker, dict) and marker.get("urls") == url_hash

    def _past_deadline() -> bool:
        return deadline is not None and time.monotonic() >= deadline

    async with httpx.AsyncClient(
        trust_env=False,
        follow_redirects=True,
        timeout=httpx.Timeout(60.0, connect=15.0),
        headers={"User-Agent": USER_AGENT},
        transport=transport,
    ) as client:
        # Google Safe Browsing
        if not gsb_api_key:
            log.append("gsb skipped (set GOOGLE_SAFE_BROWSING_API_KEY)")
        elif _fresh(SOURCE_GSB):
            results[SOURCE_GSB] = _from_cache(SOURCE_GSB, unique)
            log.append(f"gsb cached ({len(results[SOURCE_GSB])} hits)")
        elif _past_deadline():
            results[SOURCE_GSB] = _from_cache(SOURCE_GSB, unique)
            log.append("gsb deadline, stale cache")
        else:
            try:
                found, unchecked, summary = await _check_gsb(
                    client, unique, api_key=gsb_api_key, now=now, deadline=deadline
                )
            except Exception as exc:  # noqa: BLE001 - checker must never raise
                found, unchecked, summary = {}, set(unique), f"error {type(exc).__name__}"
            checked = [u for u in unique if u not in unchecked]
            _store(SOURCE_GSB, found, checked)
            if not unchecked:
                cache.put(RUNS_NS, SOURCE_GSB, {"urls": url_hash})
            found.update(_from_cache(SOURCE_GSB, unchecked))
            results[SOURCE_GSB] = found
            log.append(f"gsb {summary}")

        # Bulk feeds: one download per run, matched locally.
        feeds: list[tuple[str, list[str], object]] = []
        if abusech_auth_key:
            key = quote(abusech_auth_key.strip(), safe="")
            feeds.append(
                (SOURCE_URLHAUS, [URLHAUS_EXPORT_BASE.format(key=key, name=n) for n in URLHAUS_EXPORT_NAMES],
                 parse_urlhaus_csv)
            )
            if use_threatfox:
                feeds.append(
                    (SOURCE_THREATFOX,
                     [THREATFOX_EXPORT_BASE.format(key=key, name=n) for n in THREATFOX_EXPORT_NAMES],
                     parse_threatfox_csv)
                )
        else:
            log.append("urlhaus/threatfox skipped (set ABUSECH_AUTH_KEY)")
        if use_openphish:
            feeds.append((SOURCE_OPENPHISH, [OPENPHISH_FEED], parse_openphish_feed))

        for source, candidates, parser in feeds:
            if _fresh(source):
                results[source] = _from_cache(source, unique)
                log.append(f"{source} cached ({len(results[source])} hits)")
                continue
            if _past_deadline():
                results[source] = _from_cache(source, unique)
                log.append(f"{source} deadline, stale cache")
                continue
            try:
                text, note = await _download_first(client, candidates, deadline=deadline)
                if text is None:
                    raise RuntimeError(note)
                entries = parser(text, now=now, hosts=hosts)  # type: ignore[operator]
                found = _match_feed(unique, entries, source)
            except Exception as exc:  # noqa: BLE001 - checker must never raise
                results[source] = _from_cache(source, unique)
                log.append(f"{source} failed ({exc}), stale cache {len(results[source])}")
                continue
            _store(source, found, unique)
            cache.put(RUNS_NS, source, {"urls": url_hash})
            results[source] = found
            log.append(f"{source} {note}: {len(entries)} entries on listed hosts, {len(found)} hits")

    merged: dict[str, list[SiteSignal]] = {}
    for per_source in results.values():
        for url, sigs in per_source.items():
            merged.setdefault(url, []).extend(sigs)
    print(f"reputation: {len(unique)} urls, {len(merged)} flagged; " + "; ".join(log), flush=True)
    return merged
