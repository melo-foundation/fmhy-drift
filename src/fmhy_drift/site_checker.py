"""Async proxy-enforced site liveness checker and homepage GitHub link discoverer."""

from __future__ import annotations

import asyncio
import html
import re
import ssl
import time
from collections import defaultdict
from urllib.parse import urljoin, urlsplit

import httpx

from fmhy_drift.models import GitHubRef, SiteCheckResult, SiteStatus, WikiEntry
from fmhy_drift.parser import extract_github_repo
from fmhy_drift.proxy_guard import validate_proxy_url

MAX_HTML_SNIPPET_BYTES = 65_536

BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

# Common documentation generator / template / icon repos that appear in site footers
BOILERPLATE_GITHUB_REPOS = frozenset(
    {
        "vuejs/vitepress",
        "vuejs/vuepress",
        "squidfunk/mkdocs-material",
        "mkdocs/mkdocs",
        "facebook/docusaurus",
        "withastro/astro",
        "withastro/starlight",
        "vercel/next.js",
        "shuding/nextra",
        "gohugoio/hugo",
        "jekyll/jekyll",
        "getzola/zola",
        "11ty/eleventy",
        "hexojs/hexo",
        "tailwindlabs/tailwindcss",
        "twbs/bootstrap",
        "fortawesome/font-awesome",
        "lucide-icons/lucide",
        "simple-icons/simple-icons",
        "feathericons/feather",
        "plausible/analytics",
        "umami-software/umami",
        "github/docs",
    }
)

PARKED_DOMAIN_SIGNATURES = (
    "sedoparking.com",
    "parkingcrew.net",
    "hugedomains.com",
    "bodis.com",
    "afternic.com",
    "above.com/marketplace",
    "domain is for sale",
    "buy this domain",
    "this domain may be for sale",
    "parked free, courtesy of godaddy",
)

WAF_CHALLENGE_SIGNATURES = (
    "just a moment...",
    "attention required! | cloudflare",
    "cf-browser-verification",
    "cf-chl-bypass",
    "ddos-guard",
    "checking your browser before accessing",
    "enable javascript and cookies to continue",
)

CLOUDFLARE_ORIGIN_DOWN_CODES = frozenset({520, 521, 522, 523, 524, 525, 526, 530})

HREF_RE = re.compile(r"""<a\b[^>]*?\bhref\s*=\s*["']([^"'<>]+)["']""", re.IGNORECASE)


def _tokenize_identifier(text: str) -> set[str]:
    cleaned = re.sub(r"[^a-z0-9]+", " ", text.lower())
    return {tok for tok in cleaned.split() if len(tok) >= 3}


def _domain_tokens(url: str) -> set[str]:
    host = (urlsplit(url).hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    labels = host.split(".")
    if len(labels) >= 2:
        labels = labels[:-1]
    return _tokenize_identifier(" ".join(labels))


def discover_github_repos_in_html(
    html_snippet: str,
    page_url: str,
    *,
    max_candidates: int = 5,
) -> list[GitHubRef]:
    """Extract candidate GitHub repositories linked from `<a href=...>` on a live page."""
    dom_tokens = _domain_tokens(page_url)
    scored: list[tuple[int, int, GitHubRef]] = []
    seen_keys: set[str] = set()

    for order_idx, raw_href in enumerate(HREF_RE.findall(html_snippet)):
        unescaped = html.unescape(raw_href.strip())
        if "github.com" not in unescaped.lower():
            continue
        resolved = urljoin(page_url, unescaped)
        extracted = extract_github_repo(resolved)
        if extracted is None:
            continue

        owner, repo = extracted
        ref = GitHubRef(
            owner=owner,
            repo=repo,
            source="discovered_on_site",
            discovered_from_url=page_url,
        )
        if ref.key in seen_keys or ref.key in BOILERPLATE_GITHUB_REPOS:
            continue
        seen_keys.add(ref.key)

        repo_tokens = _tokenize_identifier(f"{owner} {repo}")
        overlap = len(dom_tokens & repo_tokens)
        # Exact substring match between domain stem and repo/owner name gets highest priority
        exact_bonus = 0
        for dtok in dom_tokens:
            if dtok in owner.lower() or dtok in repo.lower():
                exact_bonus = 10
                break

        score = exact_bonus + (overlap * 3)
        scored.append((score, -order_idx, ref))

    if not scored:
        return []

    scored.sort(reverse=True, key=lambda item: (item[0], item[1]))
    # If the page links 1-2 GitHub repos total, keep them (typical navbar/footer source link).
    # If the page links many repos (e.g. directory/blog), only keep ones matching the domain.
    if len(scored) <= 2:
        return [ref for _, _, ref in scored]

    matched = [ref for score, _, ref in scored if score > 0]
    return matched[:max_candidates]


def filter_discovered_for_entry(
    entry: WikiEntry,
    candidates: list[GitHubRef],
) -> list[GitHubRef]:
    """Filter homepage-discovered GitHub repos using entry title + domain context."""
    if not candidates:
        return []
    if len(candidates) == 1:
        return candidates

    title_tokens = _tokenize_identifier(entry.title)
    site_tokens: set[str] = set()
    for u in entry.site_urls:
        site_tokens |= _domain_tokens(u)
    context_tokens = title_tokens | site_tokens

    matched: list[GitHubRef] = []
    for ref in candidates:
        ref_haystack = f"{ref.owner.lower()} {ref.repo.lower()}"
        ref_tokens = _tokenize_identifier(ref_haystack)
        if (context_tokens & ref_tokens) or any(t in ref_haystack for t in context_tokens):
            matched.append(ref)

    return matched if matched else candidates[:1]


def _is_parked_page(final_url: str, body_lower: str) -> bool:
    final_lower = final_url.lower()
    if any(sig in final_lower for sig in PARKED_DOMAIN_SIGNATURES):
        return True
    return any(sig in body_lower for sig in PARKED_DOMAIN_SIGNATURES)


def _is_waf_or_rate_limit(status_code: int, headers: httpx.Headers, body_lower: str) -> bool:
    if status_code == 429:
        return True
    if status_code not in {401, 403, 503}:
        return False

    server = headers.get("server", "").lower()
    has_waf_header = (
        "cloudflare" in server
        or "ddos-guard" in server
        or "sucuri" in server
        or "akamai" in server
        or "cf-ray" in headers
        or "x-sucuri-id" in headers
    )
    if any(sig in body_lower for sig in WAF_CHALLENGE_SIGNATURES):
        return True
    if status_code == 403 and has_waf_header:
        return True
    return False


def _is_tls_exception(exc: BaseException) -> bool:
    cur: BaseException | None = exc
    while cur is not None:
        if isinstance(cur, ssl.SSLError):
            return True
        msg = str(cur).lower()
        if any(
            token in msg
            for token in (
                "certificate_verify_failed",
                "ssl:",
                "tls",
                "hostname mismatch",
                "cert_authority_invalid",
            )
        ):
            return True
        cur = cur.__cause__ or cur.__context__
    return False


class SiteChecker:
    """Checks FMHY site URLs strictly via an explicit HTTP/SOCKS proxy."""

    def __init__(
        self,
        proxy_url: str,
        *,
        concurrency: int = 25,
        per_host_concurrency: int = 2,
        timeout: float = 20.0,
        retries: int = 1,
        discover_github: bool = True,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        # Defense-in-depth: refuse construction without a valid proxy URL
        self.proxy_url = validate_proxy_url(proxy_url)
        self.concurrency = max(1, concurrency)
        self.per_host_concurrency = max(1, per_host_concurrency)
        self.timeout = timeout
        self.retries = max(0, retries)
        self.discover_github = discover_github
        self._transport = transport

    async def check_urls(self, urls: list[str]) -> dict[str, SiteCheckResult]:
        """Deduplicate and check all URLs concurrently through the configured proxy."""
        unique_urls = list(dict.fromkeys(urls))
        if not unique_urls:
            return {}

        global_sem = asyncio.Semaphore(self.concurrency)
        host_sems: dict[str, asyncio.Semaphore] = defaultdict(
            lambda: asyncio.Semaphore(self.per_host_concurrency)
        )

        client_kwargs: dict = {
            "timeout": httpx.Timeout(self.timeout, connect=min(10.0, self.timeout)),
            "follow_redirects": True,
            "max_redirects": 8,
            "trust_env": False,
            "headers": BROWSER_HEADERS,
        }
        if self._transport is not None:
            client_kwargs["transport"] = self._transport
        else:
            client_kwargs["proxy"] = self.proxy_url

        results: dict[str, SiteCheckResult] = {}

        async with httpx.AsyncClient(**client_kwargs) as client:

            async def _worker(target_url: str) -> None:
                host = (urlsplit(target_url).hostname or "").lower()
                async with global_sem, host_sems[host]:
                    results[target_url] = await self._check_single_with_retry(client, target_url)

            await asyncio.gather(*(_worker(u) for u in unique_urls))

        return results

    async def _check_single_with_retry(
        self,
        client: httpx.AsyncClient,
        url: str,
    ) -> SiteCheckResult:
        last_result: SiteCheckResult | None = None
        for attempt in range(self.retries + 1):
            result = await self._check_once(client, url)
            last_result = result
            if result.status in {
                SiteStatus.ALIVE,
                SiteStatus.PARKED,
                SiteStatus.WAF_OR_RATE_LIMITED,
                SiteStatus.TLS_ERROR,
            }:
                return result
            # Don't retry permanent client 404/410/451 responses
            if result.http_status in {404, 410, 451}:
                return result
            if attempt < self.retries:
                await asyncio.sleep(0.6 * (attempt + 1))

        assert last_result is not None
        return last_result

    async def _check_once(
        self,
        client: httpx.AsyncClient,
        url: str,
    ) -> SiteCheckResult:
        started = time.perf_counter()
        try:
            async with client.stream("GET", url) as resp:
                status_code = resp.status_code
                final_url = str(resp.url)
                content_type = resp.headers.get("content-type", "").lower()

                chunks: list[bytes] = []
                read_bytes = 0
                if "html" in content_type or status_code >= 400 or not content_type:
                    async for chunk in resp.aiter_bytes():
                        chunks.append(chunk)
                        read_bytes += len(chunk)
                        if read_bytes >= MAX_HTML_SNIPPET_BYTES:
                            break

                elapsed_ms = int((time.perf_counter() - started) * 1000)
                snippet = b"".join(chunks)[:MAX_HTML_SNIPPET_BYTES].decode(
                    resp.encoding or "utf-8", errors="replace"
                )
                snippet_lower = snippet.lower()

                if status_code in CLOUDFLARE_ORIGIN_DOWN_CODES:
                    return SiteCheckResult(
                        url=url,
                        status=SiteStatus.DEAD,
                        http_status=status_code,
                        final_url=final_url,
                        elapsed_ms=elapsed_ms,
                        reason=f"Cloudflare origin unreachable (HTTP {status_code})",
                    )

                if 200 <= status_code < 400:
                    if _is_parked_page(final_url, snippet_lower):
                        return SiteCheckResult(
                            url=url,
                            status=SiteStatus.PARKED,
                            http_status=status_code,
                            final_url=final_url,
                            elapsed_ms=elapsed_ms,
                            reason="Parked or for-sale domain detected",
                        )

                    discovered: list[GitHubRef] = []
                    if self.discover_github and snippet:
                        discovered = discover_github_repos_in_html(snippet, final_url)

                    return SiteCheckResult(
                        url=url,
                        status=SiteStatus.ALIVE,
                        http_status=status_code,
                        final_url=final_url,
                        elapsed_ms=elapsed_ms,
                        discovered_githubs=discovered,
                    )

                if _is_waf_or_rate_limit(status_code, resp.headers, snippet_lower):
                    return SiteCheckResult(
                        url=url,
                        status=SiteStatus.WAF_OR_RATE_LIMITED,
                        http_status=status_code,
                        final_url=final_url,
                        elapsed_ms=elapsed_ms,
                        reason=f"WAF challenge or rate limit (HTTP {status_code})",
                    )

                if status_code == 451:
                    reason = "Unavailable for legal reasons / DMCA (HTTP 451)"
                elif status_code in {404, 410}:
                    reason = f"Not found / removed (HTTP {status_code})"
                else:
                    reason = f"HTTP {status_code}"

                return SiteCheckResult(
                    url=url,
                    status=SiteStatus.DEAD,
                    http_status=status_code,
                    final_url=final_url,
                    elapsed_ms=elapsed_ms,
                    reason=reason,
                )

        except httpx.TimeoutException:
            elapsed_ms = int((time.perf_counter() - started) * 1000)
            return SiteCheckResult(
                url=url,
                status=SiteStatus.DEAD,
                elapsed_ms=elapsed_ms,
                reason=f"Timeout after {self.timeout:.0f}s",
            )
        except httpx.TooManyRedirects:
            elapsed_ms = int((time.perf_counter() - started) * 1000)
            return SiteCheckResult(
                url=url,
                status=SiteStatus.DEAD,
                elapsed_ms=elapsed_ms,
                reason="Redirect loop (too many redirects)",
            )
        except httpx.HTTPError as exc:
            elapsed_ms = int((time.perf_counter() - started) * 1000)
            if _is_tls_exception(exc):
                return SiteCheckResult(
                    url=url,
                    status=SiteStatus.TLS_ERROR,
                    elapsed_ms=elapsed_ms,
                    reason="TLS/SSL certificate error",
                )
            msg = str(exc).strip() or exc.__class__.__name__
            # Strip any accidental proxy string just in case
            if self.proxy_url in msg:
                msg = msg.replace(self.proxy_url, "<proxy>")
            return SiteCheckResult(
                url=url,
                status=SiteStatus.DEAD,
                elapsed_ms=elapsed_ms,
                reason=f"Connection failed: {msg[:140]}",
            )
