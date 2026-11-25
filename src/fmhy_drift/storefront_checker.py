"""Storefront / package-registry listing checker (removed or stale listings).

Queries ONLY the storefront platforms themselves (official JSON APIs where they exist, the public listing
page otherwise). A listed item on a store is a platform-hosted page, so no traffic reaches the item's own
website. Live response shapes these parsers rely on were verified 2026-11-25:

- AMO `GET /api/v5/addons/addon/<slug>/` -> `last_updated`, `status`, `is_disabled`; unknown slug -> 404.
- GreasyFork `GET api.greasyfork.org/scripts/<id>.json` (greasyfork.org/*.json 308-redirects there) ->
  `code_updated_at`, `deleted`; deleted id -> 404 with empty body. SleazyFork ids live on api.sleazyfork.org
  only (a sleazy id 404s on the GreasyFork API), so a 404 is confirmed on the sibling site before "removed".
- iTunes `lookup?id=a,b&country=cc` -> `currentVersionReleaseDate`; apps can be absent from one storefront
  (DeepSeek: present in us/cn, absent in gb), so "removed" requires absence from every country in APPLE_COUNTRIES.
- Google Play `/store/apps/details?id=&hl=en&gl=US` -> 200 with `Updated on</div><div ...>Nov 18, 2026`;
  removed app -> 404 (~1.6 KB body). Region-locked apps still return 200.
- Chrome Web Store `/detail/<id>?hl=en` -> 301 to `/detail/<slug>/<id>`; removed / unknown id ->
  301 to `/detail/empty-title/<id>` (200, no item data). Date: `Updated</div><div>November 24, 2026`.
- Edge `GET /addons/getproductdetailsbycrxid/<id>` -> JSON `lastUpdateDate` (epoch seconds); unknown -> 404.
- F-Droid package page -> `Added on Nov 23, 2026` per version (API has no dates); unknown -> 404.
- IzzyOnDroid package page -> `LastAppUpdate:</b></td><td>2025-03-06`; API `/api/v1/packages/<id>` 404s if gone.
- OpenUserJS script page -> `<time class="script-updated" datetime="...">`; unknown -> 404.
- Flathub `/api/v2/appstream/<id>` -> `releases[].timestamp`, `is_eol`; unknown -> 404.
- Steam `api/appdetails?appids=` -> `{"<id>": {"success": false}}` when delisted.
- VS Marketplace `extensionquery` (filterType 7) -> `lastUpdated`; unknown -> empty `extensions`.
- Open VSX `/api/<ns>/<name>` -> `timestamp`; unknown -> 404. PyPI / npm / crates.io / Docker Hub / Snap /
  AUR RPC v5 (`LastModified`, `OutOfDate`) / Obsidian `community-plugins.json`.
"""

from __future__ import annotations

import asyncio
import re
import time
from collections import Counter
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
from typing import Any
from urllib.parse import parse_qs, quote, unquote, urlsplit

import httpx

from fmhy_drift.domains import url_host
from fmhy_drift.models import SignalKind, SiteSignal, StalenessTier, TierThresholds
from fmhy_drift.signal_cache import SignalCache

CACHE_NS = "storefront"
CACHE_TTL = timedelta(days=6)
POSITIVE_TTL = timedelta(hours=20)  # removed / stale listings are rechecked every daily-or-slower run

API_UA = "fmhy-drift-checker/0.1"
# Google Play / Chrome Web Store / F-Droid serve reduced or bot pages to unknown agents.
BROWSER_UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36"

BREAKER_THRESHOLD = 4  # consecutive transient failures before a platform is skipped for this run
RETRY_AFTER_CAP = 20.0
_sleep = asyncio.sleep  # patched in tests

# Storefronts where some country can lack an app that still exists elsewhere.
APPLE_COUNTRIES = ("us", "gb", "de", "fr", "jp", "cn", "in", "br", "ru", "kr", "tr", "au", "ca", "mx", "hk")
APPLE_BATCH = 100

PLATFORM_LABELS: dict[str, str] = {
    "play": "Google Play",
    "apple": "App Store",
    "greasyfork": "GreasyFork",
    "amo": "AMO",
    "cws": "Chrome Web Store",
    "fdroid": "F-Droid",
    "openuserjs": "OpenUserJS",
    "flathub": "Flathub",
    "steam": "Steam",
    "izzy": "IzzyOnDroid",
    "pypi": "PyPI",
    "vsmarket": "VS Marketplace",
    "edge": "Edge Add-ons",
    "openvsx": "Open VSX",
    "obsidian": "Obsidian plugins",
    "crates": "crates.io",
    "npm": "npm",
    "snap": "Snapcraft",
    "docker": "Docker Hub",
    "aur": "AUR",
}

SOURCE_NAMES: dict[str, str] = {
    "play": "google_play",
    "apple": "apple_app_store",
    "greasyfork": "greasyfork",
    "amo": "amo",
    "cws": "chrome_web_store",
    "fdroid": "f_droid",
    "openuserjs": "openuserjs",
    "flathub": "flathub",
    "steam": "steam",
    "izzy": "izzyondroid",
    "pypi": "pypi",
    "vsmarket": "vs_marketplace",
    "edge": "edge_addons",
    "openvsx": "open_vsx",
    "obsidian": "obsidian",
    "crates": "crates_io",
    "npm": "npm",
    "snap": "snapcraft",
    "docker": "docker_hub",
    "aur": "aur",
}

PLATFORM_CONCURRENCY: dict[str, int] = {"play": 2, "cws": 2, "fdroid": 2, "izzy": 2, "steam": 2}
DEFAULT_CONCURRENCY = 3

STOREFRONT_HOSTS = frozenset(
    {
        "play.google.com",
        "apps.apple.com",
        "itunes.apple.com",
        "greasyfork.org",
        "sleazyfork.org",
        "addons.mozilla.org",
        "chromewebstore.google.com",
        "chrome.google.com",
        "f-droid.org",
        "openuserjs.org",
        "flathub.org",
        "store.steampowered.com",
        "apt.izzysoft.de",
        "pypi.org",
        "marketplace.visualstudio.com",
        "microsoftedge.microsoft.com",
        "open-vsx.org",
        "obsidian.md",
        "crates.io",
        "npmjs.com",
        "snapcraft.io",
        "hub.docker.com",
        "aur.archlinux.org",
    }
)


@dataclass(frozen=True)
class StoreItem:
    platform: str
    ident: str
    country: str | None = None  # Apple storefront country from the URL
    site: str | None = None  # "greasyfork" / "sleazyfork" for userscript hosts

    @property
    def key(self) -> str:
        return f"{self.platform}:{self.ident}"


# ---------------------------------------------------------------------------------------------- URL parsing

_CWS_ID = re.compile(r"(?<![a-p])([a-p]{32})(?![a-p])")
_LOCALE = re.compile(r"^[a-z]{2}(?:[-_][A-Za-z]{2,4})?$")
_PKG_ID = re.compile(r"^[A-Za-z][\w]*(?:\.[\w]+)+$")
_GF_SITE_NAMES = frozenset(["greasyfork.org", "sleazyfork.org"])


def _segments(path: str) -> list[str]:
    return [unquote(s) for s in path.split("/") if s]


def _drop_locale(segs: list[str]) -> list[str]:
    return segs[1:] if segs and _LOCALE.match(segs[0]) and segs[0] not in {"app", "apps"} else segs


def parse_storefront_url(url: str) -> StoreItem | None:
    """Map a store URL to a single listed item; `None` for search / developer / collection / home pages."""
    try:
        parts = urlsplit(url.strip())
    except ValueError:
        return None
    host = url_host(url)
    if host not in STOREFRONT_HOSTS:
        return None
    segs = _segments(parts.path)
    qs = parse_qs(parts.query)

    if host == "play.google.com":
        if segs[:3] == ["store", "apps", "details"]:
            pkg = (qs.get("id") or [""])[0].strip()
            if _PKG_ID.match(pkg):
                return StoreItem("play", pkg)
        return None

    if host in {"apps.apple.com", "itunes.apple.com"}:
        country = segs[0].lower() if segs and re.fullmatch(r"[a-zA-Z]{2}", segs[0]) else None
        rest = segs[1:] if country else segs
        if rest and rest[0] == "app":
            m = re.search(r"\bid(\d{5,12})$", rest[-1]) if rest else None
            if m:
                return StoreItem("apple", m.group(1), country=country)
        return None

    if host in _GF_SITE_NAMES:
        segs = _drop_locale(segs)
        if len(segs) >= 2 and segs[0] == "scripts":
            m = re.match(r"^(\d+)(?:-|$)", segs[1])
            if m:
                return StoreItem("greasyfork", m.group(1), site=host.split(".")[0])
        return None

    if host == "addons.mozilla.org":
        segs = _drop_locale(segs)
        if segs and segs[0] in {"firefox", "android", "thunderbird", "seamonkey"}:
            segs = segs[1:]
        if len(segs) >= 2 and segs[0] == "addon":
            return StoreItem("amo", segs[1])
        return None

    if host in {"chromewebstore.google.com", "chrome.google.com"}:
        if host == "chrome.google.com" and segs[:1] != ["webstore"]:
            return None
        if "detail" in segs:
            m = _CWS_ID.search(parts.path)
            if m:
                return StoreItem("cws", m.group(1))
        return None

    if host == "f-droid.org":
        segs = _drop_locale(segs)
        if len(segs) >= 2 and segs[0] == "packages" and _PKG_ID.match(segs[1]):
            return StoreItem("fdroid", segs[1])
        return None

    if host == "apt.izzysoft.de":
        # /fdroid/index/apk/<package>
        if len(segs) >= 4 and segs[:3] == ["fdroid", "index", "apk"] and _PKG_ID.match(segs[3]):
            return StoreItem("izzy", segs[3])
        return None

    if host == "openuserjs.org":
        if len(segs) >= 3 and segs[0] == "scripts":
            return StoreItem("openuserjs", f"{segs[1]}/{segs[2]}")
        return None

    if host == "flathub.org":
        segs = _drop_locale(segs)
        if len(segs) >= 2 and segs[0] == "apps" and _PKG_ID.match(segs[1]):
            return StoreItem("flathub", segs[1])
        return None

    if host == "store.steampowered.com":
        if len(segs) >= 2 and segs[0] == "app" and segs[1].isdigit():
            return StoreItem("steam", segs[1])
        return None

    if host == "pypi.org":
        if len(segs) >= 2 and segs[0] == "project":
            return StoreItem("pypi", segs[1].lower())
        return None

    if host == "marketplace.visualstudio.com":
        if segs[:1] == ["items"]:
            name = (qs.get("itemName") or [""])[0].strip()
            if re.fullmatch(r"[\w-]+\.[\w.-]+", name):
                return StoreItem("vsmarket", name.lower())
        return None

    if host == "microsoftedge.microsoft.com":
        if segs[:2] == ["addons", "detail"]:
            m = _CWS_ID.search(parts.path)
            if m:
                return StoreItem("edge", m.group(1))
        return None

    if host == "open-vsx.org":
        if len(segs) >= 3 and segs[0] == "extension":
            return StoreItem("openvsx", f"{segs[1]}/{segs[2]}")
        return None

    if host == "obsidian.md":
        if segs[:1] == ["plugins"]:
            pid = (qs.get("id") or [""])[0].strip()
            if pid:
                return StoreItem("obsidian", pid)
        return None

    if host == "crates.io":
        if len(segs) >= 2 and segs[0] == "crates":
            return StoreItem("crates", segs[1])
        return None

    if host == "npmjs.com":
        if len(segs) >= 2 and segs[0] == "package":
            if segs[1].startswith("@") and len(segs) >= 3:
                return StoreItem("npm", f"{segs[1]}/{segs[2]}")
            if not segs[1].startswith("@"):
                return StoreItem("npm", segs[1])
        return None

    if host == "snapcraft.io":
        reserved = {"store", "search", "docs", "about", "publisher", "snaps", "blog", "first-snap", "build"}
        if len(segs) == 1 and segs[0] not in reserved and re.fullmatch(r"[a-z0-9-]+", segs[0]):
            return StoreItem("snap", segs[0])
        return None

    if host == "hub.docker.com":
        if len(segs) >= 2 and segs[0] == "_":
            return StoreItem("docker", f"library/{segs[1]}")
        if len(segs) >= 3 and segs[0] == "r":
            return StoreItem("docker", f"{segs[1]}/{segs[2]}")
        return None

    if host == "aur.archlinux.org":
        if len(segs) >= 2 and segs[0] == "packages":
            return StoreItem("aur", segs[1])
        return None

    return None


def is_storefront_url(url: str) -> bool:
    """True when `url` is a single store / registry listing this checker reports on."""
    return parse_storefront_url(url) is not None


# ---------------------------------------------------------------------------------------------- helpers


class _Transient(Exception):
    """Network error, 429, 5xx, captcha or consent page: no evidence either way."""


def _iso_dt(raw: Any) -> datetime | None:
    if raw is None or raw == "":
        return None
    if isinstance(raw, (int, float)):
        try:
            return datetime.fromtimestamp(float(raw), tz=UTC)
        except (OverflowError, OSError, ValueError):
            return None
    s = str(raw).strip()
    if s.isdigit():
        return _iso_dt(int(s))
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    return dt.astimezone(UTC) if dt.tzinfo else dt.replace(tzinfo=UTC)


def _text_date(raw: str) -> datetime | None:
    raw = " ".join(raw.replace(",", ", ").split()).replace(" ,", ",")
    for fmt in ("%b %d, %Y", "%B %d, %Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(raw, fmt).replace(tzinfo=UTC)
        except ValueError:
            continue
    return None


def _ok(updated: datetime | None, evidence: str, note: str | None = None) -> dict[str, Any]:
    return {
        "status": "ok",
        "updated": updated.isoformat() if updated else None,
        "note": note,
        "evidence": evidence,
    }


def _removed(reason: str, evidence: str) -> dict[str, Any]:
    return {"status": "removed", "reason": reason, "evidence": evidence}


def _retry_after(resp: httpx.Response) -> float:
    raw = resp.headers.get("Retry-After")
    if not raw:
        return 3.0
    try:
        return max(0.0, min(float(raw), RETRY_AFTER_CAP))
    except ValueError:
        try:
            delta = (parsedate_to_datetime(raw) - datetime.now(UTC)).total_seconds()
            return max(0.0, min(delta, RETRY_AFTER_CAP))
        except (TypeError, ValueError):
            return 3.0


def _evidence(item: StoreItem) -> str:
    p, i = item.platform, item.ident
    return {
        "play": f"https://play.google.com/store/apps/details?id={i}",
        "apple": f"https://apps.apple.com/{item.country or 'us'}/app/id{i}",
        "greasyfork": f"https://{item.site or 'greasyfork'}.org/scripts/{i}",
        "amo": f"https://addons.mozilla.org/firefox/addon/{i}/",
        "cws": f"https://chromewebstore.google.com/detail/{i}",
        "fdroid": f"https://f-droid.org/packages/{i}/",
        "izzy": f"https://apt.izzysoft.de/fdroid/index/apk/{i}",
        "openuserjs": f"https://openuserjs.org/scripts/{i}",
        "flathub": f"https://flathub.org/apps/{i}",
        "steam": f"https://store.steampowered.com/app/{i}/",
        "pypi": f"https://pypi.org/project/{i}/",
        "vsmarket": f"https://marketplace.visualstudio.com/items?itemName={i}",
        "edge": f"https://microsoftedge.microsoft.com/addons/detail/{i}",
        "openvsx": f"https://open-vsx.org/extension/{i}",
        "obsidian": f"https://obsidian.md/plugins?id={i}",
        "crates": f"https://crates.io/crates/{i}",
        "npm": f"https://www.npmjs.com/package/{i}",
        "snap": f"https://snapcraft.io/{i}",
        "docker": f"https://hub.docker.com/r/{i}",
        "aur": f"https://aur.archlinux.org/packages/{i}",
    }.get(p, "")


# ---------------------------------------------------------------------------------------------- signals

_SEVERITY_RANK = {"info": 0, "notice": 1, "warning": 2, "critical": 3}
_TIER_SEVERITY = {
    StalenessTier.TIER_1_NOTICE: "notice",
    StalenessTier.TIER_2_WARNING: "warning",
    StalenessTier.TIER_3_CRITICAL: "critical",
}


def signals_from_data(
    item: StoreItem, data: dict[str, Any], *, now: datetime, thresholds: TierThresholds
) -> list[SiteSignal]:
    """Turn a cached per-item result into signals (computed at read time so tiers age with `now`)."""
    label = PLATFORM_LABELS.get(item.platform, item.platform)
    source = SOURCE_NAMES.get(item.platform, item.platform)
    evidence = data.get("evidence") or _evidence(item)
    if data.get("status") == "removed":
        return [
            SiteSignal(
                kind=SignalKind.STORE_REMOVED,
                severity="critical",
                source=source,
                detail=f"{data.get('reason') or 'listing gone'} ({label})",
                observed_at=_iso_dt(data.get("first_seen")),
                evidence_url=evidence,
            )
        ]
    if data.get("status") != "ok":
        return []
    updated = _iso_dt(data.get("updated"))
    note = data.get("note")
    severity: str | None = None
    parts: list[str] = []
    if updated is not None:
        days = max(0, int((now - updated).total_seconds() // 86400))
        tier = thresholds.classify_days(days)
        if tier in _TIER_SEVERITY:
            severity = _TIER_SEVERITY[tier]
            parts.append(f"last updated {updated:%Y-%m-%d}")
    if note:
        parts.append(str(note))
        if severity is None or _SEVERITY_RANK[severity] < _SEVERITY_RANK["warning"]:
            severity = "warning"
    if severity is None:
        return []
    return [
        SiteSignal(
            kind=SignalKind.STORE_STALE,
            severity=severity,  # type: ignore[arg-type]
            source=source,
            detail=f"{'; '.join(parts)} ({label})",
            observed_at=updated,
            evidence_url=evidence,
        )
    ]


# ---------------------------------------------------------------------------------------------- checker


@dataclass
class _Platform:
    sem: asyncio.Semaphore
    fails: int = 0
    open: bool = False
    stats: Counter = field(default_factory=Counter)


class _Run:
    def __init__(self, client: httpx.AsyncClient, deadline: float | None) -> None:
        self.client = client
        self.deadline = deadline
        self.platforms: dict[str, _Platform] = {}

    def plat(self, name: str) -> _Platform:
        if name not in self.platforms:
            self.platforms[name] = _Platform(
                sem=asyncio.Semaphore(PLATFORM_CONCURRENCY.get(name, DEFAULT_CONCURRENCY))
            )
        return self.platforms[name]

    def out_of_time(self) -> bool:
        return self.deadline is not None and time.monotonic() >= self.deadline

    async def request(
        self,
        method: str,
        url: str,
        *,
        params: dict | None = None,
        headers: dict | None = None,
        json_body: Any = None,
        follow: bool = False,
    ) -> httpx.Response:
        """One request with a single Retry-After-honouring retry; raises `_Transient` on no-evidence outcomes."""
        for attempt in range(2):
            try:
                resp = await self.client.request(
                    method, url, params=params, headers=headers, json=json_body, follow_redirects=follow
                )
            except (httpx.HTTPError, OSError) as exc:
                if attempt == 0:
                    await _sleep(1.0)
                    continue
                raise _Transient(type(exc).__name__) from exc
            if resp.status_code in {429, 503} or resp.status_code >= 500:
                if attempt == 0:
                    await _sleep(_retry_after(resp))
                    continue
                raise _Transient(f"http {resp.status_code}")
            return resp
        raise _Transient("retry exhausted")

    async def get_json(self, url: str, **kw: Any) -> tuple[int, Any]:
        resp = await self.request("GET", url, **kw)
        if resp.status_code != 200:
            return resp.status_code, None
        try:
            return 200, resp.json()
        except ValueError as exc:
            raise _Transient("bad json") from exc


# --- per-item fetchers: return cache data, or raise _Transient -------------------------------------------


async def _fetch_amo(run: _Run, item: StoreItem) -> dict:
    ev = _evidence(item)
    status, body = await run.get_json(
        f"https://addons.mozilla.org/api/v5/addons/addon/{quote(item.ident, safe='')}/"
    )
    if status == 404:
        return _removed("API 404", ev)
    if status in {401, 403}:
        return _removed(f"not public (API {status})", ev)
    if status != 200 or not isinstance(body, dict):
        raise _Transient(f"http {status}")
    ev = body.get("url") or ev
    if body.get("is_disabled") or body.get("status") not in {"public", None}:
        return _removed(f"status {body.get('status')}" + (", disabled" if body.get("is_disabled") else ""), ev)
    return _ok(_iso_dt(body.get("last_updated")), ev)


async def _fetch_greasyfork(run: _Run, item: StoreItem) -> dict:
    sites = ["greasyfork", "sleazyfork"]
    if item.site == "sleazyfork":
        sites.reverse()
    ev = _evidence(item)
    for site in sites:
        resp = await run.request("GET", f"https://api.{site}.org/scripts/{item.ident}.json")
        if resp.status_code in {301, 302, 307, 308}:
            # Merged scripts redirect to their replacement (`/en/scripts/<new id>`): follow once.
            m = re.search(r"/scripts/(\d+)", resp.headers.get("Location", ""))
            if not m or m.group(1) == item.ident:
                raise _Transient(f"redirect {resp.status_code}")
            resp = await run.request("GET", f"https://api.{site}.org/scripts/{m.group(1)}.json")
        status = resp.status_code
        body = None
        if status == 200:
            try:
                body = resp.json()
            except ValueError as exc:
                raise _Transient("bad json") from exc
        if status == 200 and isinstance(body, dict):
            ev = body.get("url") or f"https://{site}.org/scripts/{item.ident}"
            if body.get("deleted"):
                return _removed("script deleted", ev)
            return _ok(_iso_dt(body.get("code_updated_at")), ev)
        if status not in {404, 410}:
            raise _Transient(f"http {status}")
    return _removed("script 404 on GreasyFork and SleazyFork", ev)


async def _fetch_openuserjs(run: _Run, item: StoreItem) -> dict:
    ev = _evidence(item)
    resp = await run.request("GET", ev)
    if resp.status_code == 404:
        return _removed("script page 404", ev)
    if resp.status_code != 200:
        raise _Transient(f"http {resp.status_code}")
    m = re.search(r'class="script-updated"[^>]*datetime="([^"]+)"', resp.text) or re.search(
        r'datetime="([^"]+)"[^>]*class="script-updated"', resp.text
    )
    return _ok(_iso_dt(m.group(1)) if m else None, ev)


_PLAY_UPDATED = re.compile(r"Updated on</div><div[^>]*>([A-Z][a-z]{2,8}\.? \d{1,2}, \d{4})<")


async def _fetch_play(run: _Run, item: StoreItem) -> dict:
    ev = _evidence(item)
    url = "https://play.google.com/store/apps/details"
    headers = {"User-Agent": BROWSER_UA, "Accept-Language": "en-US,en;q=0.9"}
    resp = await run.request("GET", url, params={"id": item.ident, "hl": "en", "gl": "US"}, headers=headers)
    if resp.status_code == 404:
        # Confirm once with Google's own geo default before calling it removed.
        again = await run.request("GET", url, params={"id": item.ident, "hl": "en"}, headers=headers)
        if again.status_code == 404:
            return _removed("listing 404", ev)
        if again.status_code != 200:
            raise _Transient(f"http {again.status_code}")
        resp = again
    if resp.status_code != 200:
        raise _Transient(f"http {resp.status_code}")  # 302 to consent / sorry pages land here
    if item.ident not in resp.text:
        raise _Transient("unexpected page")
    m = _PLAY_UPDATED.search(resp.text)
    return _ok(_text_date(m.group(1).replace(".", "")) if m else None, ev)


_CWS_UPDATED = re.compile(r">Updated</div><div[^>]*>([A-Z][a-z]+ \d{1,2}, \d{4})<")


async def _fetch_cws(run: _Run, item: StoreItem) -> dict:
    ev = _evidence(item)
    headers = {"User-Agent": BROWSER_UA, "Accept-Language": "en-US,en;q=0.9"}
    resp = await run.request(
        "GET", f"https://chromewebstore.google.com/detail/{item.ident}", params={"hl": "en"},
        headers=headers, follow=True,
    )
    final_path = urlsplit(str(resp.url)).path
    if resp.status_code == 404:
        return _removed("listing 404", ev)
    if resp.status_code != 200:
        raise _Transient(f"http {resp.status_code}")
    if urlsplit(str(resp.url)).hostname != "chromewebstore.google.com":
        raise _Transient("redirected off store")
    m = _CWS_UPDATED.search(resp.text)
    if f"/detail/empty-title/{item.ident}" in final_path and not m:
        return _removed("listing gone (empty-title)", ev)
    if f"/{item.ident}" not in final_path:
        raise _Transient("unexpected redirect")
    return _ok(_text_date(m.group(1)) if m else None, f"https://chromewebstore.google.com{final_path}")


async def _fetch_edge(run: _Run, item: StoreItem) -> dict:
    ev = _evidence(item)
    status, body = await run.get_json(
        f"https://microsoftedge.microsoft.com/addons/getproductdetailsbycrxid/{item.ident}",
        params={"hl": "en-US", "gl": "US"},
    )
    if status == 404:
        return _removed("API 404", ev)
    if status != 200 or not isinstance(body, dict) or not body.get("crxId"):
        raise _Transient(f"http {status}")
    return _ok(_iso_dt(body.get("lastUpdateDate")), ev)


_FDROID_ADDED = re.compile(r"Added on ([A-Z][a-z]{2} \d{1,2}, \d{4})")


async def _fetch_fdroid(run: _Run, item: StoreItem) -> dict:
    ev = _evidence(item)
    resp = await run.request("GET", f"https://f-droid.org/en/packages/{item.ident}/", headers={"User-Agent": BROWSER_UA})
    if resp.status_code == 404:
        status, _ = await run.get_json(f"https://f-droid.org/api/v1/packages/{item.ident}")
        if status == 404:
            return _removed("package 404 (page + API)", ev)
        if status != 200:
            raise _Transient(f"api http {status}")
        return _ok(None, ev)
    if resp.status_code != 200:
        raise _Transient(f"http {resp.status_code}")
    dates = [d for d in (_text_date(x) for x in _FDROID_ADDED.findall(resp.text)) if d]
    return _ok(max(dates) if dates else None, ev)


async def _fetch_izzy(run: _Run, item: StoreItem) -> dict:
    ev = _evidence(item)
    status, _ = await run.get_json(f"https://apt.izzysoft.de/fdroid/api/v1/packages/{item.ident}")
    if status == 404:
        return _removed("package 404 (API)", ev)
    if status != 200:
        raise _Transient(f"http {status}")
    resp = await run.request("GET", ev)
    if resp.status_code != 200:
        return _ok(None, ev)
    m = re.search(r"LastAppUpdate:</b></td><td>(\d{4}-\d{2}-\d{2})<", resp.text) or re.search(
        r"LastRepoUpdate:</b></td><td>(\d{4}-\d{2}-\d{2})<", resp.text
    )
    note = None
    if re.search(r"<code>SourceArchived</code>", resp.text):
        note = "source archived (IzzyOnDroid anti-feature)"
    return _ok(_text_date(m.group(1)) if m else None, ev, note)


async def _fetch_flathub(run: _Run, item: StoreItem) -> dict:
    ev = _evidence(item)
    status, body = await run.get_json(f"https://flathub.org/api/v2/appstream/{item.ident}")
    if status == 404:
        return _removed("appstream 404", ev)
    if status != 200 or not isinstance(body, dict):
        raise _Transient(f"http {status}")
    stamps = [_iso_dt(r.get("timestamp")) for r in body.get("releases") or [] if isinstance(r, dict)]
    stamps = [s for s in stamps if s]
    return _ok(max(stamps) if stamps else None, ev, "marked end-of-life" if body.get("is_eol") else None)


async def _fetch_steam(run: _Run, item: StoreItem) -> dict:
    ev = _evidence(item)
    # Region-locked apps report success:false for some country codes, so require it across several.
    for cc in ("us", None, "de"):
        params = {"appids": item.ident, "filters": "basic", "l": "en"}
        if cc:
            params["cc"] = cc
        status, body = await run.get_json("https://store.steampowered.com/api/appdetails", params=params)
        if status != 200 or not isinstance(body, dict):
            raise _Transient(f"http {status}")
        row = body.get(item.ident)
        if not isinstance(row, dict):
            raise _Transient("unexpected json")
        if row.get("success"):
            return _ok(None, ev)  # Steam exposes no reliable last-update date; removal only.
    return _removed("appdetails success:false (us, default, de)", ev)


async def _fetch_pypi(run: _Run, item: StoreItem) -> dict:
    ev = _evidence(item)
    status, body = await run.get_json(f"https://pypi.org/pypi/{quote(item.ident)}/json")
    if status == 404:
        return _removed("project 404", ev)
    if status != 200 or not isinstance(body, dict):
        raise _Transient(f"http {status}")
    times = [
        _iso_dt(f.get("upload_time_iso_8601"))
        for files in (body.get("releases") or {}).values()
        for f in files or []
        if isinstance(f, dict) and not f.get("yanked")
    ]
    times = [t for t in times if t]
    return _ok(max(times) if times else None, ev)


async def _fetch_npm(run: _Run, item: StoreItem) -> dict:
    ev = _evidence(item)
    status, body = await run.get_json(f"https://registry.npmjs.org/{item.ident.replace('/', '%2F')}")
    if status == 404:
        return _removed("package 404", ev)
    if status != 200 or not isinstance(body, dict):
        raise _Transient(f"http {status}")
    t = body.get("time") or {}
    if t.get("unpublished"):
        return _removed("unpublished", ev)
    latest = (body.get("dist-tags") or {}).get("latest")
    updated = _iso_dt(t.get(latest)) if latest else None
    deprecated = ((body.get("versions") or {}).get(latest) or {}).get("deprecated") if latest else None
    return _ok(updated, ev, "latest version deprecated" if deprecated else None)


async def _fetch_crates(run: _Run, item: StoreItem) -> dict:
    ev = _evidence(item)
    status, body = await run.get_json(f"https://crates.io/api/v1/crates/{quote(item.ident)}")
    if status == 404:
        return _removed("crate 404", ev)
    if status != 200 or not isinstance(body, dict):
        raise _Transient(f"http {status}")
    times = [_iso_dt(v.get("created_at")) for v in body.get("versions") or [] if isinstance(v, dict)]
    times = [t for t in times if t]
    updated = max(times) if times else _iso_dt((body.get("crate") or {}).get("updated_at"))
    return _ok(updated, ev)


async def _fetch_docker(run: _Run, item: StoreItem) -> dict:
    ev = _evidence(item) if not item.ident.startswith("library/") else f"https://hub.docker.com/_/{item.ident[8:]}"
    status, body = await run.get_json(f"https://hub.docker.com/v2/repositories/{item.ident}/")
    if status == 404:
        return _removed("repository 404", ev)
    if status != 200 or not isinstance(body, dict):
        raise _Transient(f"http {status}")
    return _ok(_iso_dt(body.get("last_updated")), ev)


async def _fetch_snap(run: _Run, item: StoreItem) -> dict:
    ev = _evidence(item)
    status, body = await run.get_json(
        f"https://api.snapcraft.io/v2/snaps/info/{item.ident}",
        params={"fields": "version,created-at"},
        headers={"Snap-Device-Series": "16"},
    )
    if status == 404:
        return _removed("snap 404", ev)
    if status != 200 or not isinstance(body, dict):
        raise _Transient(f"http {status}")
    stamps = [
        _iso_dt(c.get("created-at"))
        for c in body.get("channel-map") or []
        if isinstance(c, dict) and (c.get("channel") or {}).get("risk") == "stable"
    ]
    stamps = [s for s in stamps if s]
    return _ok(max(stamps) if stamps else None, ev)


async def _fetch_openvsx(run: _Run, item: StoreItem) -> dict:
    ev = _evidence(item)
    status, body = await run.get_json(f"https://open-vsx.org/api/{item.ident}")
    if status == 404:
        return _removed("extension 404", ev)
    if status != 200 or not isinstance(body, dict):
        raise _Transient(f"http {status}")
    return _ok(_iso_dt(body.get("timestamp")), ev)


async def _fetch_vsmarket(run: _Run, item: StoreItem) -> dict:
    ev = _evidence(item)
    resp = await run.request(
        "POST",
        "https://marketplace.visualstudio.com/_apis/public/gallery/extensionquery",
        headers={"Accept": "application/json;api-version=3.0-preview.1", "Content-Type": "application/json"},
        json_body={"filters": [{"criteria": [{"filterType": 7, "value": item.ident}]}], "flags": 914},
    )
    if resp.status_code != 200:
        raise _Transient(f"http {resp.status_code}")
    try:
        results = resp.json()["results"][0]
        exts = results["extensions"]
    except (ValueError, KeyError, IndexError, TypeError) as exc:
        raise _Transient("bad json") from exc
    if not exts:
        return _removed("extensionquery 0 results", ev)
    ext = exts[0]
    flags = str(ext.get("flags") or "")
    if "unpublished" in flags.lower():
        return _removed("unpublished", ev)
    return _ok(_iso_dt(ext.get("lastUpdated")), ev)


FETCHERS: dict[str, Callable[[_Run, StoreItem], Awaitable[dict]]] = {
    "amo": _fetch_amo,
    "greasyfork": _fetch_greasyfork,
    "openuserjs": _fetch_openuserjs,
    "play": _fetch_play,
    "cws": _fetch_cws,
    "edge": _fetch_edge,
    "fdroid": _fetch_fdroid,
    "izzy": _fetch_izzy,
    "flathub": _fetch_flathub,
    "steam": _fetch_steam,
    "pypi": _fetch_pypi,
    "npm": _fetch_npm,
    "crates": _fetch_crates,
    "docker": _fetch_docker,
    "snap": _fetch_snap,
    "openvsx": _fetch_openvsx,
    "vsmarket": _fetch_vsmarket,
}


# --- batched platforms: return {key: data}; keys absent = transient --------------------------------------


async def _batch_apple(run: _Run, items: list[StoreItem]) -> dict[str, dict]:
    out: dict[str, dict] = {}
    pending = {i.ident: i for i in items}
    unverified: set[str] = set()  # idents whose lookup failed in at least one storefront
    countries: list[str] = []
    for c in [i.country for i in items if i.country] + list(APPLE_COUNTRIES):
        if c not in countries:
            countries.append(c)
    plat = run.plat("apple")
    for cc in countries:
        if not pending or plat.open or run.out_of_time():
            break
        ids = list(pending)
        for start in range(0, len(ids), APPLE_BATCH):
            chunk = ids[start : start + APPLE_BATCH]
            try:
                async with plat.sem:
                    status, body = await run.get_json(
                        "https://itunes.apple.com/lookup", params={"id": ",".join(chunk), "country": cc}
                    )
                if status != 200 or not isinstance(body, dict):
                    raise _Transient(f"http {status}")
                plat.fails = 0
            except _Transient:
                unverified.update(chunk)
                plat.fails += 1
                plat.stats["errors"] += 1
                if plat.fails >= BREAKER_THRESHOLD:
                    plat.open = True
                    break
                continue
            for r in body.get("results") or []:
                tid = str(r.get("trackId") or "")
                if tid in pending:
                    it = pending.pop(tid)
                    ev = (r.get("trackViewUrl") or _evidence(it)).split("?")[0]
                    out[it.key] = _ok(_iso_dt(r.get("currentVersionReleaseDate")), ev)
    if not plat.open and not run.out_of_time():
        # Only items that were actually looked up in every country count as removed.
        for ident, it in pending.items():
            if ident in unverified:
                continue
            out[it.key] = _removed(f"iTunes lookup 0 results in {len(countries)} storefronts", _evidence(it))
    return out


async def _batch_aur(run: _Run, items: list[StoreItem]) -> dict[str, dict]:
    out: dict[str, dict] = {}
    plat = run.plat("aur")
    names = [i.ident for i in items]
    for start in range(0, len(names), 100):
        chunk = names[start : start + 100]
        try:
            async with plat.sem:
                status, body = await run.get_json(
                    "https://aur.archlinux.org/rpc/v5/info", params=[("arg[]", n) for n in chunk]  # type: ignore[arg-type]
                )
            if status != 200 or not isinstance(body, dict) or body.get("type") == "error":
                raise _Transient(f"http {status}")
        except _Transient:
            plat.stats["errors"] += 1
            continue
        found = {r.get("Name"): r for r in body.get("results") or [] if isinstance(r, dict)}
        for n in chunk:
            it = StoreItem("aur", n)
            r = found.get(n)
            if r is None:
                out[it.key] = _removed("RPC info 0 results", _evidence(it))
                continue
            ood = _iso_dt(r.get("OutOfDate")) if r.get("OutOfDate") else None
            note = f"flagged out-of-date {ood:%Y-%m-%d}" if ood else None
            out[it.key] = _ok(_iso_dt(r.get("LastModified")), _evidence(it), note)
    return out


OBSIDIAN_PLUGINS_URL = "https://raw.githubusercontent.com/obsidianmd/obsidian-releases/master/community-plugins.json"


async def _batch_obsidian(run: _Run, items: list[StoreItem]) -> dict[str, dict]:
    plat = run.plat("obsidian")
    try:
        status, body = await run.get_json(OBSIDIAN_PLUGINS_URL)
        if status != 200 or not isinstance(body, list):
            raise _Transient(f"http {status}")
    except _Transient:
        plat.stats["errors"] += len(items)
        return {}
    ids = {str(p.get("id")) for p in body if isinstance(p, dict)}
    if len(ids) < 100:  # truncated / wrong file: no evidence
        return {}
    return {
        it.key: (_ok(None, _evidence(it)) if it.ident in ids else _removed("not in community-plugins.json", _evidence(it)))
        for it in items
    }


BATCHERS: dict[str, Callable[[_Run, list[StoreItem]], Awaitable[dict[str, dict]]]] = {
    "apple": _batch_apple,
    "aur": _batch_aur,
    "obsidian": _batch_obsidian,
}

PLATFORM_ORDER = (
    "play", "apple", "greasyfork", "amo", "cws", "fdroid", "openuserjs", "flathub", "steam", "izzy", "pypi",
    "vsmarket", "edge", "openvsx", "obsidian", "crates", "npm", "snap", "docker", "aur",
)


async def check_storefronts(
    urls: list[str],
    *,
    cache: SignalCache,
    now: datetime,
    thresholds: TierThresholds,
    deadline: float | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
    stats: dict[str, Any] | None = None,
) -> dict[str, list[SiteSignal]]:
    """Report STORE_REMOVED / STORE_STALE for store and registry listings in `urls`.

    `stats`, if given, is filled with per-platform counters (checked, cached, removed, stale tiers, errors...).
    """
    item_urls: dict[str, list[str]] = {}
    items: dict[str, StoreItem] = {}
    skipped = Counter()
    for u in dict.fromkeys(urls):
        host = url_host(u)
        if host not in STOREFRONT_HOSTS:
            continue
        it = parse_storefront_url(u)
        if it is None:
            skipped[host] += 1
            continue
        items.setdefault(it.key, it)
        item_urls.setdefault(it.key, []).append(u)

    results: dict[str, dict] = {}
    by_platform: dict[str, list[StoreItem]] = {}
    plat_stats: dict[str, Counter] = {}

    def pstat(p: str) -> Counter:
        return plat_stats.setdefault(p, Counter())

    for key, it in items.items():
        pstat(it.platform)["items"] += 1
        hit = cache.get(
            CACHE_NS,
            key,
            ttl=CACHE_TTL,
            positive_ttl=POSITIVE_TTL,
            is_positive=lambda d, _it=it: isinstance(d, dict)
            and bool(signals_from_data(_it, d, now=now, thresholds=thresholds)),
        )
        if isinstance(hit, dict):
            results[key] = hit
            pstat(it.platform)["cached"] += 1
        else:
            by_platform.setdefault(it.platform, []).append(it)

    client = httpx.AsyncClient(
        trust_env=False,
        follow_redirects=False,
        timeout=httpx.Timeout(20.0),
        headers={"User-Agent": API_UA, "Accept-Language": "en-US,en;q=0.9"},
        transport=transport,
    )
    run = _Run(client, deadline)
    deadline_hit = False

    def store(it: StoreItem, data: dict) -> None:
        if data.get("status") == "removed":
            prev = cache.get_any_age(CACHE_NS, it.key)
            first = None
            if prev and isinstance(prev[0], dict) and prev[0].get("status") == "removed":
                first = prev[0].get("first_seen")
            data["first_seen"] = first or now.isoformat()
        cache.put(CACHE_NS, it.key, data)
        results[it.key] = data
        pstat(it.platform)["checked"] += 1

    async def one(it: StoreItem) -> None:
        nonlocal deadline_hit
        plat = run.plat(it.platform)
        if plat.open:
            return
        if run.out_of_time():
            deadline_hit = True
            return
        async with plat.sem:
            if plat.open or run.out_of_time():
                deadline_hit = deadline_hit or run.out_of_time()
                return
            try:
                data = await FETCHERS[it.platform](run, it)
            except _Transient:
                plat.fails += 1
                pstat(it.platform)["errors"] += 1
                if plat.fails >= BREAKER_THRESHOLD:
                    plat.open = True
                return
            except Exception:  # noqa: BLE001 - parser bug on an odd page must not fail the run
                pstat(it.platform)["errors"] += 1
                return
            plat.fails = 0
            store(it, data)

    async def platform_task(p: str, its: list[StoreItem]) -> None:
        if p in BATCHERS:
            if run.out_of_time():
                return
            try:
                got = await BATCHERS[p](run, its)
            except Exception:  # noqa: BLE001 - same: never fail the run
                got = {}
            for it in its:
                if it.key in got:
                    store(it, got[it.key])
            pstat(p)["errors"] += run.plat(p).stats["errors"]
            return
        await asyncio.gather(*(one(it) for it in its))

    try:
        ordered = sorted(by_platform.items(), key=lambda kv: PLATFORM_ORDER.index(kv[0]))
        await asyncio.gather(*(platform_task(p, its) for p, its in ordered))
    finally:
        await client.aclose()

    # Keys we could not refresh this run: fall back to any-age cache so known signals persist.
    fallback = 0
    for key, it in items.items():
        if key not in results:
            hit = cache.get_any_age(CACHE_NS, key)
            if hit and isinstance(hit[0], dict):
                results[key] = hit[0]
                fallback += 1
                pstat(it.platform)["stale_cache"] += 1

    out: dict[str, list[SiteSignal]] = {}
    for key, data in results.items():
        it = items[key]
        sigs = signals_from_data(it, data, now=now, thresholds=thresholds)
        st = pstat(it.platform)
        if data.get("status") == "ok" and not data.get("updated") and it.platform != "steam" and it.platform != "obsidian":
            st["no_date"] += 1
        for s in sigs:
            if s.kind == SignalKind.STORE_REMOVED:
                st["removed"] += 1
            else:
                st[f"stale_{s.severity}"] += 1
        if sigs:
            for u in item_urls[key]:
                out.setdefault(u, []).extend(sigs)

    opened = sorted(p for p, pl in run.platforms.items() if pl.open)
    total = Counter()
    for c in plat_stats.values():
        total.update(c)
    print(
        f"storefront: {len(items)} items from {sum(len(v) for v in item_urls.values())} URLs, "
        f"{sum(skipped.values())} non-item store URLs skipped; fetched={total['checked']} cached={total['cached']} "
        f"stale_cache={fallback} errors={total['errors']} removed={total['removed']} "
        f"stale(n/w/c)={total['stale_notice']}/{total['stale_warning']}/{total['stale_critical']}"
        + (f" breaker_open={','.join(opened)}" if opened else "")
        + (" deadline_hit" if deadline_hit else ""),
        flush=True,
    )
    if stats is not None:
        stats["platforms"] = {p: dict(c) for p, c in plat_stats.items()}
        stats["skipped_by_host"] = dict(skipped)
        stats["breaker_open"] = opened
        stats["deadline_hit"] = deadline_hit
    return out
