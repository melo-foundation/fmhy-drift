"""Confirm suspect sites with third-party browser scanners (urlscan.io, Cloudflare URL Scanner).

The scanners fetch the page from their own infrastructure; the runner only talks to their APIs.

- urlscan.io: `POST https://urlscan.io/api/v1/scan/` (header `API-Key`, visibility "unlisted"), poll
  `GET /api/v1/result/{uuid}/` (404 until finished, 410 if deleted); quotas from `GET /api/v1/quotas`.
- Cloudflare URL Scanner v2: `POST /client/v4/accounts/{id}/urlscanner/v2/scan` (Bearer token, permission
  Account > URL Scanner > Edit), poll `GET .../urlscanner/v2/result/{uuid}` (404 while queued / in progress).
  The Free plan has no Unlisted quota and allows 1 scan per 10 s, so scans default to Public there.

urlscan is preferred; Cloudflare runs when urlscan is unavailable / out of quota, when urlscan is inconclusive,
and as a second opinion on urlscan "down" verdicts.
"""

from __future__ import annotations

import asyncio
import re
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from urllib.parse import quote

import httpx

from fmhy_drift.domains import is_shared_platform_host, registrable_domain, site_key, url_host
from fmhy_drift.models import SignalKind, SiteSignal
from fmhy_drift.signal_cache import SignalCache

USER_AGENT = "fmhy-drift-checker/0.1"

URLSCAN_BASE = "https://urlscan.io"
CLOUDFLARE_BASE = "https://api.cloudflare.com/client/v4"

CACHE_NS = "scan_confirm"
CACHE_TTL = timedelta(days=7)
INCONCLUSIVE_TTL = timedelta(days=1)
DOWN_TTL = CACHE_TTL

URLSCAN_MIN_INTERVAL = 2.0  # seconds between urlscan submissions (raised from the per-minute quota)
CLOUDFLARE_MIN_INTERVAL = 10.0  # Free / self-serve plans: 1 scan per 10 seconds
POLL_INITIAL_WAIT = 12.0  # urlscan: "wait at least 10 seconds before starting to poll"
POLL_INTERVALS = (3.0, 3.0, 5.0, 5.0, 8.0, 10.0, 15.0, 15.0, 20.0, 30.0, 30.0, 30.0)
QUOTA_RESERVE = 3
MAX_RETRY_AFTER = 60.0
CONCURRENCY = 4
BREAKER_FAILURES = 3

# Test hook: replaced with a no-op coroutine in unit tests.
_sleep = asyncio.sleep

CHALLENGE_MARKERS = (
    "just a moment",
    "attention required",
    "checking your browser",
    "ddos-guard",
    "ddos protection",
    "verify you are human",
    "are you a robot",
    "captcha",
    "access denied",
    "security check",
    "please wait",
    "one more step",
    "bot verification",
    "sucuri website firewall",
    "request blocked",
)
PARKING_TITLE_RE = re.compile(
    r"(domain (name )?(is |may be )?for sale|\b(is|may be) for sale\b|buy this domain|this domain (is|may be) (for sale|available)"
    r"|domain is available|parked (free|domain|by)|this domain has been registered|hugedomains|\bsedo\b"
    r"|afternic|dan\.com|domain parking|is available for purchase|related searches|future home of)",
    re.IGNORECASE,
)
PARKING_HOST_DOMAINS = frozenset(
    {"sedo.com", "sedoparking.com", "dan.com", "afternic.com", "hugedomains.com", "bodis.com",
     "parkingcrew.net", "above.com", "undeveloped.com", "godaddy.com", "namecheap.com", "porkbun.com",
     "spaceship.com", "atom.com", "squadhelp.com", "parklogic.com"}
)

DOWN_ERRORS = {
    "ERR_NAME_NOT_RESOLVED": "DNS failure",
    "ERR_NAME_RESOLUTION_FAILED": "DNS failure",
    "ERR_CONNECTION_REFUSED": "connection refused",
    "ERR_CONNECTION_RESET": "connection reset",
    "ERR_CONNECTION_CLOSED": "connection closed",
    "ERR_CONNECTION_TIMED_OUT": "connection timed out",
    "ERR_ADDRESS_UNREACHABLE": "address unreachable",
    "ERR_EMPTY_RESPONSE": "empty response",
}
DOWN_HTTP = {404, 410, 520, 521, 522, 523, 524, 525, 526, 530}
INCONCLUSIVE_HTTP = {401, 403, 407, 429, 503}


@dataclass
class _Outcome:
    verdict: str  # "down" | "up" | "inconclusive" | "error"
    signal: SiteSignal | None = None
    note: str = ""


def _day(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%d")


def _parse_dt(raw: object) -> datetime | None:
    if not isinstance(raw, str) or not raw:
        return None
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def _retry_after(resp: httpx.Response, default: float = 10.0) -> float:
    for header in ("Retry-After", "X-Rate-Limit-Reset-After"):
        val = resp.headers.get(header)
        if val:
            try:
                return min(max(float(val), 0.0), MAX_RETRY_AFTER)
            except ValueError:
                continue
    return default


def _primary_error(result: dict) -> str | None:
    """Chrome net error of the primary (first document) request, e.g. `net::ERR_NAME_NOT_RESOLVED`."""
    reqs = ((result.get("data") or {}).get("requests")) or []
    for req in reqs[:3]:
        if not isinstance(req, dict):
            continue
        resp = req.get("response") or {}
        failed = resp.get("failed") if isinstance(resp, dict) else None
        if isinstance(failed, dict) and failed.get("errorText"):
            return str(failed["errorText"])
        if isinstance(resp, dict) and resp.get("response"):
            return None  # primary request got an HTTP response
    return None


def _is_challenge(title: str, server: str) -> bool:
    t = title.lower()
    return any(m in t for m in CHALLENGE_MARKERS) or (
        not t and server.lower() in {"ddos-guard", "sucuri/cloudproxy"}
    )


def _same_site(a: str, b: str) -> bool:
    if not a or not b:
        return True
    if a == b:
        return True
    if is_shared_platform_host(a) or is_shared_platform_host(b):
        return site_key(a) == site_key(b)
    ra, rb = registrable_domain(a), registrable_domain(b)
    return ra is None or rb is None or ra == rb


def classify_result(
    url: str, result: dict, *, source: str, evidence_url: str, now: datetime
) -> _Outcome:
    """Map a urlscan / Cloudflare v2 result JSON (same `page` / `task` / `data` layout) to an outcome."""
    page = result.get("page") if isinstance(result.get("page"), dict) else {}
    task = result.get("task") if isinstance(result.get("task"), dict) else {}
    observed = _parse_dt(task.get("time")) or now
    title = str(page.get("title") or "").strip()
    final_url = str(page.get("url") or "")
    server = str(page.get("server") or "")
    try:
        status = int(str(page.get("status") or "").strip() or 0)
    except ValueError:
        status = 0
    shown_title = f'"{title[:80]}"' if title else "no title"

    def _sig(kind: SignalKind, severity: str, fact: str) -> SiteSignal:
        final = f"; final {final_url}" if final_url and final_url.rstrip("/") != url.rstrip("/") else ""
        return SiteSignal(
            kind=kind,
            severity=severity,  # type: ignore[arg-type]
            source=source,
            detail=f"{fact}{final}; {shown_title}; scanned {_day(observed)}",
            observed_at=observed,
            evidence_url=evidence_url,
        )

    if not status:
        err = _primary_error(result)
        if err:
            code = err.removeprefix("net::")
            if code in DOWN_ERRORS:
                return _Outcome("down", _sig(SignalKind.SCAN_DOWN, "critical", f"{DOWN_ERRORS[code]} ({code})"))
            if code.startswith(("ERR_SSL", "ERR_CERT", "ERR_BAD_SSL")):
                return _Outcome("down", _sig(SignalKind.SCAN_DOWN, "critical", f"TLS failure ({code})"))
            return _Outcome("inconclusive", note=code)
        return _Outcome("inconclusive", note="no page status")

    if status in INCONCLUSIVE_HTTP or _is_challenge(title, server):
        return _Outcome("inconclusive", note=f"HTTP {status} waf/challenge")
    if status in DOWN_HTTP:
        label = "Cloudflare origin error" if 520 <= status <= 530 else "gone"
        return _Outcome("down", _sig(SignalKind.SCAN_DOWN, "critical", f"HTTP {status} {label}"))
    if 200 <= status < 300:
        orig_host = url_host(url)
        final_host = url_host(final_url) if final_url else orig_host
        final_reg = registrable_domain(final_host) or final_host
        if PARKING_TITLE_RE.search(title) or final_reg in PARKING_HOST_DOMAINS:
            return _Outcome("up", _sig(SignalKind.SCAN_UP, "warning", f"HTTP {status} parking/for-sale page"))
        if not _same_site(orig_host, final_host):
            return _Outcome(
                "up", _sig(SignalKind.SCAN_UP, "warning", f"HTTP {status} redirects to {final_reg}")
            )
        return _Outcome("up", _sig(SignalKind.SCAN_UP, "info", f"HTTP {status}"))
    return _Outcome("inconclusive", note=f"HTTP {status}")


class _Scanner:
    name = ""

    def __init__(self, client: httpx.AsyncClient, *, budget: int, deadline: float | None, now: datetime) -> None:
        self.client = client
        self.budget = budget
        self.deadline = deadline
        self.now = now
        self.submitted = 0
        self.exhausted = False
        self.failures = 0
        self._lock = asyncio.Lock()
        self._last_submit = 0.0
        self.interval = 0.0

    def available(self) -> bool:
        return not self.exhausted and self.failures < BREAKER_FAILURES and self.submitted < self.budget and not (
            self.deadline is not None and time.monotonic() >= self.deadline
        )

    def _past_deadline(self, extra: float = 0.0) -> bool:
        return self.deadline is not None and time.monotonic() + extra >= self.deadline

    async def _pace(self) -> None:
        wait = self._last_submit + self.interval - time.monotonic()
        if wait > 0:
            await _sleep(wait)
        self._last_submit = time.monotonic()

    async def submit(self, url: str) -> tuple[str, dict] | _Outcome:
        raise NotImplementedError

    async def fetch_result(self, ref: dict) -> httpx.Response | None:
        raise NotImplementedError

    def evidence(self, ref: dict) -> str:
        raise NotImplementedError

    async def scan(self, url: str) -> _Outcome:
        async with self._lock:
            if not self.available():
                return _Outcome("error", note="unavailable")
            await self._pace()
            self.submitted += 1
            sub = await self.submit(url)
        if isinstance(sub, _Outcome):
            return sub
        _uuid, ref = sub
        await _sleep(POLL_INITIAL_WAIT)
        for wait in (0.0, *POLL_INTERVALS):
            if wait:
                await _sleep(wait)
            if self._past_deadline():
                return _Outcome("error", note="deadline while polling")
            resp = await self.fetch_result(ref)
            if resp is None:
                continue
            if resp.status_code == 200:
                try:
                    body = resp.json()
                except ValueError:
                    return _Outcome("error", note="bad result json")
                body = body.get("result", body) if isinstance(body, dict) and "page" not in body else body
                if not isinstance(body, dict):
                    return _Outcome("error", note="bad result json")
                return classify_result(url, body, source=self.name, evidence_url=self.evidence(ref), now=self.now)
            if resp.status_code == 410:
                return _Outcome("error", note="result deleted")
            if resp.status_code == 429:
                await _sleep(_retry_after(resp))
                continue
            if resp.status_code != 404:
                return _Outcome("error", note=f"result HTTP {resp.status_code}")
        return _Outcome("error", note="poll timeout")


def _dns_down(url: str, source: str, message: str, now: datetime) -> _Outcome:
    host = url_host(url)
    return _Outcome(
        "down",
        SiteSignal(
            kind=SignalKind.SCAN_DOWN,
            severity="critical",
            source=source,
            detail=f"DNS failure (scanner refused: {message[:80]}); scanned {_day(now)}",
            observed_at=now,
            evidence_url=f"https://dns.google/query?name={quote(host)}",
        ),
    )


def _error_message(resp: httpx.Response) -> str:
    try:
        body = resp.json()
    except ValueError:
        return resp.text[:120]
    if isinstance(body, dict):
        parts = [str(body.get("message") or "")]
        for err in body.get("errors") or []:
            if isinstance(err, dict):
                parts.append(str(err.get("title") or err.get("message") or ""))
                parts.append(str(err.get("detail") or ""))
        return " ".join(p for p in parts if p).strip()
    return ""


class _UrlscanScanner(_Scanner):
    name = "urlscan"

    def __init__(self, client: httpx.AsyncClient, api_key: str, **kw) -> None:
        super().__init__(client, **kw)
        self.headers = {"API-Key": api_key}
        self.interval = URLSCAN_MIN_INTERVAL

    async def read_quota(self) -> str:
        """Clamp budget to the remaining unlisted-scan and result-retrieval quota."""
        try:
            resp = await self.client.get(f"{URLSCAN_BASE}/api/v1/quotas", headers=self.headers)
        except (httpx.HTTPError, OSError):
            return "quota unknown (network)"
        if resp.status_code in (401, 403):
            self.exhausted = True
            return f"quota HTTP {resp.status_code} (bad URLSCAN_API_KEY?)"
        if resp.status_code != 200:
            return f"quota HTTP {resp.status_code}"
        try:
            limits = (resp.json() or {}).get("limits") or {}
        except (ValueError, AttributeError):
            return "quota unparsable"
        unlisted = limits.get("unlisted") or {}
        retrieve = limits.get("retrieve") or {}
        remaining: list[int] = []
        for window in ("minute", "hour", "day"):
            w = unlisted.get(window) or {}
            if isinstance(w.get("remaining"), int):
                rem = w["remaining"] if window == "minute" else w["remaining"] - QUOTA_RESERVE
                if window != "minute":
                    remaining.append(rem)
            if window == "minute" and isinstance(w.get("limit"), int) and w["limit"] > 0:
                self.interval = max(URLSCAN_MIN_INTERVAL, 60.0 / w["limit"])
        for window in ("hour", "day"):
            w = retrieve.get(window) or {}
            if isinstance(w.get("remaining"), int):
                remaining.append((w["remaining"] - QUOTA_RESERVE) // (len(POLL_INTERVALS) + 1))
        if remaining:
            self.budget = max(0, min(self.budget, *remaining))
        if self.budget == 0:
            self.exhausted = True
        return f"quota ok, budget {self.budget}"

    async def submit(self, url: str) -> tuple[str, dict] | _Outcome:
        body = {"url": url, "visibility": "unlisted", "tags": ["fmhy-drift"]}
        for _attempt in range(2):
            try:
                resp = await self.client.post(f"{URLSCAN_BASE}/api/v1/scan/", headers=self.headers, json=body)
            except (httpx.HTTPError, OSError):
                self.failures += 1
                return _Outcome("error", note="submit network error")
            if resp.status_code == 429:
                wait = _retry_after(resp)
                window = resp.headers.get("X-Rate-Limit-Window", "minute")
                if window != "minute" or self._past_deadline(wait):
                    self.exhausted = True
                    return _Outcome("error", note=f"urlscan {window} quota exhausted")
                await _sleep(wait)
                continue
            break
        else:
            self.exhausted = True
            return _Outcome("error", note="urlscan rate limited")
        if resp.headers.get("X-Rate-Limit-Remaining") == "0" and resp.headers.get("X-Rate-Limit-Window") in (
            "hour",
            "day",
        ):
            self.exhausted = True
        if resp.status_code in (401, 403):
            self.exhausted = True
            return _Outcome("error", note=f"submit HTTP {resp.status_code}")
        if resp.status_code == 400:
            msg = _error_message(resp)
            if "dns error" in msg.lower() or "could not resolve" in msg.lower():
                return _dns_down(url, self.name, msg, self.now)
            return _Outcome("inconclusive", note=f"submit refused: {msg[:80]}")
        if resp.status_code != 200:
            self.failures += 1
            return _Outcome("error", note=f"submit HTTP {resp.status_code}")
        self.failures = 0
        try:
            data = resp.json()
        except ValueError:
            return _Outcome("error", note="submit bad json")
        uuid = str(data.get("uuid") or "")
        if not uuid:
            return _Outcome("error", note="submit without uuid")
        return uuid, {
            "uuid": uuid,
            "api": data.get("api") or f"{URLSCAN_BASE}/api/v1/result/{uuid}/",
            "result": data.get("result") or f"{URLSCAN_BASE}/result/{uuid}/",
        }

    async def fetch_result(self, ref: dict) -> httpx.Response | None:
        try:
            return await self.client.get(ref["api"], headers=self.headers)
        except (httpx.HTTPError, OSError):
            return None

    def evidence(self, ref: dict) -> str:
        return ref["result"]


class _CloudflareScanner(_Scanner):
    name = "cloudflare_url_scanner"

    def __init__(
        self, client: httpx.AsyncClient, token: str, account_id: str, *, visibility: str, **kw
    ) -> None:
        super().__init__(client, **kw)
        self.headers = {"Authorization": f"Bearer {token}"}
        self.base = f"{CLOUDFLARE_BASE}/accounts/{quote(account_id.strip(), safe='')}/urlscanner/v2"
        self.visibility = visibility
        self.interval = CLOUDFLARE_MIN_INTERVAL

    async def submit(self, url: str) -> tuple[str, dict] | _Outcome:
        body = {"url": url, "visibility": self.visibility}
        for _attempt in range(2):
            try:
                resp = await self.client.post(f"{self.base}/scan", headers=self.headers, json=body)
            except (httpx.HTTPError, OSError):
                self.failures += 1
                return _Outcome("error", note="submit network error")
            if resp.status_code == 429:
                wait = _retry_after(resp, default=CLOUDFLARE_MIN_INTERVAL)
                if self._past_deadline(wait):
                    self.exhausted = True
                    return _Outcome("error", note="cloudflare rate limited")
                await _sleep(wait)
                continue
            break
        else:
            self.exhausted = True
            return _Outcome("error", note="cloudflare rate limited")
        if resp.status_code in (401, 403):
            self.exhausted = True
            return _Outcome("error", note=f"submit HTTP {resp.status_code}")
        if resp.status_code == 400:
            msg = _error_message(resp)
            if "dns" in msg.lower() and ("resolv" in msg.lower() or "error" in msg.lower()):
                return _dns_down(url, self.name, msg, self.now)
            if "quota" in msg.lower() or "limit" in msg.lower() or "visibility" in msg.lower():
                self.exhausted = True
            return _Outcome("inconclusive", note=f"submit refused: {msg[:80]}")
        if resp.status_code == 409:
            return _Outcome("inconclusive", note="submit conflict")
        if resp.status_code != 200:
            self.failures += 1
            return _Outcome("error", note=f"submit HTTP {resp.status_code}")
        self.failures = 0
        try:
            data = resp.json()
        except ValueError:
            return _Outcome("error", note="submit bad json")
        uuid = str(data.get("uuid") or "")
        if not uuid:
            return _Outcome("error", note="submit without uuid")
        return uuid, {
            "uuid": uuid,
            "api": f"{self.base}/result/{uuid}",
            "result": data.get("result") or f"https://radar.cloudflare.com/scan/{uuid}/summary",
        }

    async def fetch_result(self, ref: dict) -> httpx.Response | None:
        try:
            return await self.client.get(ref["api"], headers=self.headers)
        except (httpx.HTTPError, OSError):
            return None

    def evidence(self, ref: dict) -> str:
        return ref["result"]


def _cached(cache: SignalCache, url: str, now: datetime) -> list[SiteSignal] | None:
    """Fresh cached signals for `url` (possibly empty), or None when it needs a new scan."""
    hit = cache.get_any_age(CACHE_NS, url)
    if not hit or not isinstance(hit[0], dict):
        return None
    data, checked = hit
    outcome = data.get("outcome")
    ttl = INCONCLUSIVE_TTL if outcome == "inconclusive" else DOWN_TTL if outcome == "down" else CACHE_TTL
    if now - checked > ttl:
        return None
    return _signals_of(data)


def _signals_of(data: object) -> list[SiteSignal]:
    if not isinstance(data, dict):
        return []
    raw = data.get("signals") or []
    return [s for s in (SiteSignal.from_json(r) for r in raw if isinstance(r, dict)) if s is not None]


async def confirm_with_scanners(
    urls: list[str],
    *,
    cache: SignalCache,
    now: datetime,
    deadline: float | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
    urlscan_api_key: str | None = None,
    cloudflare_api_token: str | None = None,
    cloudflare_account_id: str | None = None,
    max_scans: int = 40,
    cloudflare_visibility: str = "Public",
) -> dict[str, list[SiteSignal]]:
    unique = list(dict.fromkeys(u.strip() for u in urls if u and u.strip()))
    out: dict[str, list[SiteSignal]] = {}
    to_scan: list[str] = []
    for url in unique:
        sigs = _cached(cache, url, now)
        if sigs is None:
            to_scan.append(url)
        elif sigs:
            out[url] = sigs
    cached_count = len(unique) - len(to_scan)

    have_urlscan = bool(urlscan_api_key)
    have_cf = bool(cloudflare_api_token and cloudflare_account_id)
    log: list[str] = []
    if not have_urlscan:
        log.append("urlscan skipped (set URLSCAN_API_KEY)")
    if not have_cf:
        log.append("cloudflare skipped (set CLOUDFLARE_API_TOKEN + CLOUDFLARE_ACCOUNT_ID)")

    def _stale_fallback(rest: list[str]) -> None:
        for url in rest:
            hit = cache.get_any_age(CACHE_NS, url)
            if hit:
                sigs = _signals_of(hit[0])
                if sigs:
                    out[url] = sigs

    if not to_scan or not (have_urlscan or have_cf) or max_scans <= 0:
        _stale_fallback(to_scan)
        print(
            f"scan_confirm: {len(unique)} urls, {cached_count} cached, 0 scanned, {len(out)} with signals; "
            + "; ".join(log),
            flush=True,
        )
        return out

    batch = to_scan[:max_scans]
    leftover = to_scan[max_scans:]
    counts = {"down": 0, "up": 0, "inconclusive": 0, "error": 0}

    async with httpx.AsyncClient(
        trust_env=False,
        follow_redirects=True,
        timeout=httpx.Timeout(30.0, connect=10.0),
        headers={"User-Agent": USER_AGENT},
        transport=transport,
    ) as client:
        urlscan = (
            _UrlscanScanner(client, urlscan_api_key, budget=max_scans, deadline=deadline, now=now)  # type: ignore[arg-type]
            if have_urlscan
            else None
        )
        cf = (
            _CloudflareScanner(
                client,
                cloudflare_api_token,  # type: ignore[arg-type]
                cloudflare_account_id,  # type: ignore[arg-type]
                visibility=cloudflare_visibility,
                budget=max_scans,
                deadline=deadline,
                now=now,
            )
            if have_cf
            else None
        )
        if urlscan is not None:
            log.append("urlscan " + await urlscan.read_quota())

        sem = asyncio.Semaphore(CONCURRENCY)
        unscanned: list[str] = []

        async def _one(url: str) -> None:
            async with sem:
                primary: _Outcome | None = None
                if urlscan is not None and urlscan.available():
                    primary = await urlscan.scan(url)
                final = primary
                need_cf = primary is None or primary.verdict in {"inconclusive", "error", "down"}
                if need_cf and cf is not None and cf.available():
                    second = await cf.scan(url)
                    if primary is not None and primary.verdict == "down":
                        if second.verdict == "up" and second.signal is not None:
                            s = second.signal
                            final = _Outcome(
                                "up",
                                SiteSignal(
                                    kind=s.kind,
                                    severity="notice" if s.severity == "info" else s.severity,
                                    source=s.source,
                                    detail=f"{s.detail}; urlscan disagreed",
                                    observed_at=s.observed_at,
                                    evidence_url=s.evidence_url,
                                ),
                            )
                    elif second.verdict != "error" or final is None:
                        final = second
                if final is None or final.verdict == "error":
                    counts["error"] += 1
                    unscanned.append(url)
                    return
                counts[final.verdict] += 1
                sigs = [final.signal] if final.signal is not None else []
                cache.put(CACHE_NS, url, {"outcome": final.verdict, "signals": [s.to_json() for s in sigs]})
                if sigs:
                    out[url] = sigs

        try:
            await asyncio.gather(*(_one(u) for u in batch))
        except Exception as exc:  # noqa: BLE001 - checker must never raise
            log.append(f"error {type(exc).__name__}")
        if urlscan is not None:
            log.append(f"urlscan submitted {urlscan.submitted}" + (" (quota stop)" if urlscan.exhausted else ""))
        if cf is not None:
            log.append(f"cloudflare submitted {cf.submitted}" + (" (quota stop)" if cf.exhausted else ""))

    _stale_fallback(unscanned + leftover)
    print(
        f"scan_confirm: {len(unique)} urls, {cached_count} cached, {len(batch)} attempted "
        f"(down {counts['down']}, up {counts['up']}, inconclusive {counts['inconclusive']}, "
        f"failed {counts['error']}), {len(leftover)} over max_scans, {len(out)} with signals; " + "; ".join(log),
        flush=True,
    )
    return out
