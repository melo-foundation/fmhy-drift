"""Archive-consensus site signals from the Wayback Machine CDX API and the Common Crawl index.

Never contacts listed sites: only `web.archive.org` and `index.commoncrawl.org` are queried.

Per site origin (host), one Wayback CDX query returns the latest per-day captures of the origin root. A trailing
run of 404/410/5xx captures after earlier 200s gives `ARCHIVE_DOWN`; a trailing run of 3xx captures whose
archived `Location` leaves the registrable domain gives `ARCHIVE_MOVED`. A single-capture run is only reported
when the newest Common Crawl captures agree.

Rate limits (observed / documented, 2026-11):
- Wayback CDX: hard limit ~30 req/min per IP (the `wayback` Python client sets 80% of it, 0.4 req/s, at the
  Internet Archive's request); 429s escalate to IP blocks. We start at most one CDX request every 4 s (15/min).
- Wayback playback (`/web/<ts>id_/`): ~600 req/min generic limit; used only to read redirect targets, 1 req/s.
- Common Crawl index: strict, unpublished limit, frequent 502/503/504 under load. Used only to confirm
  candidates, one request every 2 s, circuit-broken after repeated failures.
"""

from __future__ import annotations

import json
import re
import time
from datetime import UTC, datetime, timedelta
from urllib.parse import quote

import httpx

from fmhy_drift.dns_checker import KNOWN_LIVE_PLATFORM_HOSTS
from fmhy_drift.domains import is_public_dns_host, registrable_domain, url_host
from fmhy_drift.models import SignalKind, SiteSignal
from fmhy_drift.passive_http import CircuitBreaker, RateLimiter, make_client, past_deadline, retry_after_seconds
from fmhy_drift.signal_cache import SignalCache

WAYBACK_CDX_URL = "https://web.archive.org/cdx/search/cdx"
WAYBACK_PLAYBACK_TMPL = "https://web.archive.org/web/{ts}id_/{url}"
CC_COLLINFO_URL = "https://index.commoncrawl.org/collinfo.json"

ARCHIVE_NAMESPACE = "archive"
ARCHIVE_TTL = timedelta(days=28)
CC_COLLINFO_TTL = timedelta(days=3)

CDX_INTERVAL_S = 4.0
PLAYBACK_INTERVAL_S = 1.0
CC_INTERVAL_S = 2.0
CDX_ROWS = 12
RECENT_WINDOW = timedelta(days=548)  # ~18 months
CC_COLLECTIONS = 2

# Hosts whose root says nothing about the listed page (big platforms with many listed paths).
PLATFORM_URL_COUNT = 10
EXTRA_PLATFORM_HOSTS = frozenset(
    {
        "github.com",
        "gist.github.com",
        "reddit.com",
        "old.reddit.com",
        "youtube.com",
        "t.me",
        "discord.gg",
        "discord.com",
        "twitter.com",
        "x.com",
        "chrome.google.com",
        "f-droid.org",
        "apkmirror.com",
        "rentry.co",
        "rentry.org",
        "telegra.ph",
    }
)

# Statuses that mean "the archive crawler was refused", not "the site is gone": never counted either way.
BLOCKED_STATUSES = frozenset({401, 403, 407, 418, 429, 451, 999})
_PLAYBACK_LOC_RE = re.compile(r"/web/\d{1,14}(?:[a-z]{2}_)?/(.+)$")


def _parse_ts(ts: str) -> datetime | None:
    try:
        return datetime.strptime(ts[:14].ljust(14, "0"), "%Y%m%d%H%M%S").replace(tzinfo=UTC)
    except ValueError:
        return None


def _status(raw: object) -> int | None:
    try:
        return int(str(raw))
    except ValueError:
        return None


def _is_error(code: int) -> bool:
    return code in {404, 410} or 500 <= code <= 599


def _day(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%d")


def _parse_cdx_rows(body: str) -> list[tuple[datetime, int, str]]:
    """Return `[(captured_at, status, original_url)]` sorted oldest -> newest, skipping revisits / junk."""
    if not body.strip():
        return []
    data = json.loads(body)
    if not isinstance(data, list) or len(data) < 2:
        return []
    header = data[0]
    try:
        i_ts, i_st, i_orig = header.index("timestamp"), header.index("statuscode"), header.index("original")
    except (ValueError, AttributeError):
        return []
    rows: list[tuple[datetime, int, str]] = []
    for row in data[1:]:
        if not isinstance(row, list) or len(row) < len(header):
            continue
        dt = _parse_ts(str(row[i_ts]))
        code = _status(row[i_st])
        if dt is None or code is None:
            continue
        rows.append((dt, code, str(row[i_orig])))
    rows.sort(key=lambda r: r[0])
    return rows


def _trailing_run(
    rows: list[tuple[datetime, int, str]], *, want, now: datetime
) -> tuple[list[tuple[datetime, int, str]], datetime | None]:
    """Newest-first run of recent captures matching `want`, ignoring blocked and (for errors) 3xx captures.

    Returns `(run, newest_ok)` where `newest_ok` is the newest 2xx capture date anywhere in `rows`.
    """
    newest_ok = max((r[0] for r in rows if 200 <= r[1] < 300), default=None)
    run: list[tuple[datetime, int, str]] = []
    for dt, code, orig in reversed(rows):
        if now - dt > RECENT_WINDOW:
            break
        if code in BLOCKED_STATUSES:
            continue
        if want(code):
            run.append((dt, code, orig))
            continue
        if want is _is_error and 300 <= code < 400:
            continue  # http->https hops recorded alongside the failing https capture
        break
    if newest_ok is not None:
        run = [r for r in run if r[0] > newest_ok]
    return run, newest_ok


def _distinct_days(run: list[tuple[datetime, int, str]]) -> int:
    return len({_day(r[0]) for r in run})


def _playback_target(location: str) -> str | None:
    m = _PLAYBACK_LOC_RE.search(location) if "web.archive.org/web/" in location else None
    if m:
        return m.group(1)
    if location.startswith(("http://", "https://")) and "web.archive.org" not in location:
        return location
    return None


async def check_archives(
    urls: list[str],
    *,
    cache: SignalCache,
    now: datetime,
    deadline: float | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
    priority_urls: list[str] | None = None,
    cdx_interval: float = CDX_INTERVAL_S,
    cc_interval: float = CC_INTERVAL_S,
    playback_interval: float = PLAYBACK_INTERVAL_S,
) -> dict[str, list[SiteSignal]]:
    """Emit `ARCHIVE_DOWN` / `ARCHIVE_MOVED` from archived captures of each URL's origin (rotating budget)."""
    unique = list(dict.fromkeys(urls))
    host_urls: dict[str, list[str]] = {}
    for u in unique:
        h = url_host(u)
        if is_public_dns_host(h):
            host_urls.setdefault(h, []).append(u)
    hosts = [
        h
        for h, us in host_urls.items()
        if h not in KNOWN_LIVE_PLATFORM_HOSTS
        and h not in EXTRA_PLATFORM_HOSTS
        and len(us) < PLATFORM_URL_COUNT
    ]
    host_set = set(hosts)
    prio: list[str] = []
    for u in priority_urls or []:
        h = url_host(u)
        if h in host_set and h not in prio:
            prio.append(h)
    prio_set = set(prio)
    order = prio + cache.stale_first(ARCHIVE_NAMESPACE, [h for h in hosts if h not in prio_set])

    cdx_rl = RateLimiter(cdx_interval)
    pb_rl = RateLimiter(playback_interval)
    cc_rl = RateLimiter(cc_interval)
    cdx_br = CircuitBreaker(5)
    cc_br = CircuitBreaker(3)
    stats = {"queried": 0, "cached": 0, "cdx_requests": 0, "cc_requests": 0, "playback": 0, "rate_limited": 0}
    verdicts: dict[str, dict] = {}
    budget_hit = False

    async def _get(client: httpx.AsyncClient, url: str, params: dict | None, rl: RateLimiter, kind: str):
        await rl.wait()
        stats[kind] += 1
        return await client.get(url, params=params)

    async def _cdx(client: httpx.AsyncClient, params: dict) -> list[tuple[datetime, int, str]] | None:
        for _attempt in range(2):
            if cdx_br.open:
                return None
            try:
                resp = await _get(client, WAYBACK_CDX_URL, params, cdx_rl, "cdx_requests")
            except Exception:
                cdx_br.failure()
                continue
            if resp.status_code == 429:
                stats["rate_limited"] += 1
                wait = retry_after_seconds(resp, default=60.0, cap=120.0)
                cdx_rl.penalize(wait)
                if stats["rate_limited"] >= 2:
                    cdx_br.trip()  # a second 429 risks an IP block: stop for this run
                    return None
                continue
            if resp.status_code >= 500:
                cdx_br.failure()
                cdx_rl.penalize(retry_after_seconds(resp, default=10.0, cap=60.0))
                continue
            if resp.status_code != 200:
                return None
            try:
                rows = _parse_cdx_rows(resp.text)
            except ValueError:
                return None
            cdx_br.success()
            return rows
        return None

    async def _redirect_target(client: httpx.AsyncClient, ts: datetime, original: str) -> str | None:
        """Follow archived redirects (up to 3 hops) while they stay on the same registrable domain."""
        url = original
        stamp = ts.strftime("%Y%m%d%H%M%S")
        start_reg = registrable_domain(url_host(original))
        for _hop in range(3):
            if past_deadline(deadline):
                return None
            try:
                resp = await _get(
                    client, WAYBACK_PLAYBACK_TMPL.format(ts=stamp, url=url), None, pb_rl, "playback"
                )
            except Exception:
                return None
            if resp.status_code == 429:
                pb_rl.penalize(retry_after_seconds(resp, default=30.0, cap=120.0))
                return None
            if not (300 <= resp.status_code < 400):
                return None
            target = _playback_target(resp.headers.get("Location", ""))
            if not target:
                return None
            reg = registrable_domain(url_host(target))
            if reg and reg != start_reg:
                return target
            url = target
        return None

    cc_colls: list[str] | None = None

    async def _cc_collections(client: httpx.AsyncClient) -> list[str]:
        nonlocal cc_colls
        if cc_colls is not None:
            return cc_colls
        cached = cache.get(ARCHIVE_NAMESPACE, "cc:collinfo", ttl=CC_COLLINFO_TTL)
        if isinstance(cached, list) and cached:
            cc_colls = [str(c) for c in cached]
            return cc_colls
        cc_colls = []
        if cc_br.open:
            return cc_colls
        try:
            resp = await _get(client, CC_COLLINFO_URL, None, cc_rl, "cc_requests")
            if resp.status_code == 200:
                rows = resp.json()
                apis = [r.get("cdx-api") for r in rows if isinstance(r, dict) and r.get("cdx-api")]
                cc_colls = [str(a) for a in apis[:CC_COLLECTIONS]]
                if cc_colls:
                    cache.put(ARCHIVE_NAMESPACE, "cc:collinfo", cc_colls)
            else:
                cc_br.failure()
        except Exception:
            cc_br.failure()
        return cc_colls

    async def _cc_latest(client: httpx.AsyncClient, host: str) -> list[dict]:
        """Captures of the origin root in the newest Common Crawl collection that has any, newest first."""
        for api in await _cc_collections(client):
            if cc_br.open or past_deadline(deadline):
                return []
            try:
                resp = await _get(
                    client, api, {"url": f"{host}/", "output": "json", "limit": "20"}, cc_rl, "cc_requests"
                )
            except Exception:
                cc_br.failure()
                continue
            if resp.status_code == 404:
                cc_br.success()
                continue  # "No Captures found"
            if resp.status_code != 200:
                cc_br.failure()
                cc_rl.penalize(retry_after_seconds(resp, default=5.0, cap=30.0))
                continue
            cc_br.success()
            out: list[dict] = []
            for line in resp.text.splitlines():
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if isinstance(row, dict) and _status(row.get("status")) is not None:
                    out.append(row)
            if out:
                out.sort(key=lambda r: str(r.get("timestamp")), reverse=True)
                return out
        return []

    def _cc_agrees(
        rows: list[dict], *, want, after: datetime, foreign_reg: str | None = None, origin_reg: str | None = None
    ) -> str | None:
        """Return `"commoncrawl <status> <date>"` when the newest CC capture (not older than `after`) agrees."""
        considered = [r for r in rows if _status(r.get("status")) not in BLOCKED_STATUSES]
        if not considered:
            return None
        newest = considered[0]
        code = _status(newest.get("status")) or 0
        dt = _parse_ts(str(newest.get("timestamp") or ""))
        if dt is None or now - dt > RECENT_WINDOW or dt < after:
            return None
        tag = f"commoncrawl {code} {_day(dt)}"
        if want == "error":
            # No 2xx anywhere in the newest collection, newest capture is an error.
            ok = _is_error(code) and not any(200 <= (_status(r.get("status")) or 0) < 300 for r in considered)
            return tag if ok else None
        if want == "moved" and 300 <= code < 400:
            target = str(newest.get("redirect") or "")
            reg = registrable_domain(url_host(target)) if target else None
            ok = bool(reg and reg != origin_reg and (foreign_reg is None or reg == foreign_reg))
            return tag if ok else None
        return None

    async def _check_host(client: httpx.AsyncClient, host: str) -> dict | None:
        rows = await _cdx(
            client,
            {
                "url": f"{host}/",
                "output": "json",
                "fl": "timestamp,statuscode,original",
                "filter": "!statuscode:-",
                "collapse": "timestamp:8",
                "limit": f"-{CDX_ROWS}",
            },
        )
        if rows is None:
            return None
        evidence = f"https://web.archive.org/web/*/{quote(host)}/"
        origin_reg = registrable_domain(host)

        # --- ARCHIVE_DOWN ---
        err_run, newest_ok = _trailing_run(rows, want=_is_error, now=now)
        if err_run:
            last_ok = newest_ok
            if last_ok is None:
                more = await _cdx(
                    client,
                    {
                        "url": f"{host}/",
                        "output": "json",
                        "fl": "timestamp,statuscode,original",
                        "filter": "statuscode:200",
                        "limit": "-1",
                    },
                )
                if more:
                    last_ok = more[-1][0]
            if last_ok is not None:
                n_days = _distinct_days(err_run)
                sources = "wayback"
                ok = n_days >= 2
                cc_tag = None
                if not ok:
                    cc = await _cc_latest(client, host)
                    cc_tag = _cc_agrees(cc, want="error", after=last_ok)
                    ok = cc_tag is not None
                    sources = "wayback+commoncrawl"
                if ok:
                    latest = err_run[0]
                    return {
                        "kind": SignalKind.ARCHIVE_DOWN.value,
                        "severity": "warning",
                        "source": sources,
                        "detail": (
                            f"last 200 {_day(last_ok)}, latest {latest[1]} {_day(latest[0])}, "
                            f"{n_days} non-200 capture days since"
                            + (f", {cc_tag}" if cc_tag else "")
                        ),
                        "observed_at": latest[0].isoformat(),
                        "evidence_url": evidence,
                    }
            return {}

        # --- ARCHIVE_MOVED ---
        redir_run, _ = _trailing_run(rows, want=lambda c: 300 <= c < 400, now=now)
        if redir_run:
            target = await _redirect_target(client, redir_run[0][0], redir_run[0][2])
            if target:
                t_reg = registrable_domain(url_host(target))
                ok = False
                sources = "wayback"
                if len(redir_run) >= 2 and _distinct_days(redir_run) >= 2:
                    second = await _redirect_target(client, redir_run[1][0], redir_run[1][2])
                    ok = bool(second and registrable_domain(url_host(second)) == t_reg)
                if not ok:
                    cc = await _cc_latest(client, host)
                    ok = (
                        _cc_agrees(
                            cc, want="moved", after=redir_run[-1][0] - RECENT_WINDOW, foreign_reg=t_reg, origin_reg=origin_reg
                        )
                        is not None
                    )
                    sources = "wayback+commoncrawl"
                if ok:
                    latest = redir_run[0]
                    return {
                        "kind": SignalKind.ARCHIVE_MOVED.value,
                        "severity": "notice",
                        "source": sources,
                        "detail": (
                            f"redirects to {t_reg} ({target[:120]}), latest {latest[1]} {_day(latest[0])}"
                        ),
                        "observed_at": latest[0].isoformat(),
                        "evidence_url": evidence,
                    }
        return {}

    started = time.monotonic()
    try:
        async with make_client(transport, timeout=30.0, follow_redirects=False) as client:
            for host in order:
                cached = cache.get(ARCHIVE_NAMESPACE, host, ttl=ARCHIVE_TTL)
                if isinstance(cached, dict):
                    verdicts[host] = cached
                    stats["cached"] += 1
                    continue
                if past_deadline(deadline):
                    budget_hit = True
                    break
                if cdx_br.open:
                    break
                try:
                    res = await _check_host(client, host)
                except Exception:
                    res = None
                if res is None:
                    continue
                stats["queried"] += 1
                verdicts[host] = res
                cache.put(ARCHIVE_NAMESPACE, host, res)
    except Exception as exc:  # never raise out of a checker
        print(f"archive: aborted ({type(exc).__name__}: {exc})", flush=True)

    stale = 0
    for host in hosts:
        if host not in verdicts:
            old = cache.get_any_age(ARCHIVE_NAMESPACE, host)
            if old and isinstance(old[0], dict):
                verdicts[host] = old[0]
                stale += 1

    out: dict[str, list[SiteSignal]] = {}
    hits = 0
    for host, v in verdicts.items():
        if not v.get("kind"):
            continue
        sig = SiteSignal.from_json(v)
        if sig is None:
            continue
        hits += 1
        for u in host_urls.get(host, []):
            out.setdefault(u, []).append(sig)
    remaining = sum(1 for h in hosts if cache.get(ARCHIVE_NAMESPACE, h, ttl=ARCHIVE_TTL) is None)
    print(
        f"archive: {len(hosts)} origins, {stats['queried']} checked, {stats['cached']} cached, {stale} stale-reused, "
        f"{hits} signals, {stats['cdx_requests']} CDX / {stats['playback']} playback / {stats['cc_requests']} CC requests"
        + (f", {stats['rate_limited']} wayback 429s" if stats["rate_limited"] else "")
        + (", wayback circuit open" if cdx_br.open else "")
        + (", commoncrawl circuit open" if cc_br.open else "")
        + (f", deadline reached ({remaining} origins due)" if budget_hit else "")
        + f", {time.monotonic() - started:.0f}s",
        flush=True,
    )
    return out
