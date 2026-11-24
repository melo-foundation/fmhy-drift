"""Unit tests for proxy-only site liveness checking and homepage GitHub discovery."""

from __future__ import annotations

import asyncio

import httpx
import pytest

from fmhy_drift.models import SiteStatus, WikiEntry
from fmhy_drift.proxy_guard import ProxyMissingError
from fmhy_drift.site_checker import SiteChecker, filter_discovered_for_entry


def test_site_checker_refuses_missing_proxy() -> None:
    with pytest.raises(ProxyMissingError):
        SiteChecker(proxy_url="")


def test_site_checker_classifies_statuses_and_discovers_github() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        host = request.url.host
        if host == "cobalt.tools":
            html = """
            <html><body>
              <a href="https://github.com/vuejs/vitepress">Built with VitePress</a>
              <a href="https://github.com/imputnet/cobalt">Source on GitHub</a>
            </body></html>
            """
            return httpx.Response(200, headers={"content-type": "text/html"}, text=html)
        if host == "parked.example.com":
            return httpx.Response(
                200,
                headers={"content-type": "text/html"},
                text="<html><body>This domain is for sale at SedoParking.com</body></html>",
            )
        if host == "waf.example.com":
            return httpx.Response(
                403,
                headers={"content-type": "text/html", "server": "cloudflare", "cf-ray": "abc"},
                text="<html><title>Just a moment...</title></html>",
            )
        if host == "origin-down.example.com":
            return httpx.Response(522, headers={"server": "cloudflare"}, text="Origin timed out")
        if host == "dmca.example.com":
            return httpx.Response(451, text="Unavailable for legal reasons")
        return httpx.Response(404, text="Not found")

    checker = SiteChecker(
        proxy_url="socks5h://127.0.0.1:1080",
        retries=0,
        transport=httpx.MockTransport(handler),
    )

    urls = [
        "https://cobalt.tools/",
        "https://parked.example.com/",
        "https://waf.example.com/",
        "https://origin-down.example.com/",
        "https://dmca.example.com/",
        "https://missing.example.com/",
    ]
    results = asyncio.run(checker.check_urls(urls))

    cobalt = results["https://cobalt.tools/"]
    assert cobalt.status == SiteStatus.ALIVE
    # Boilerplate `vuejs/vitepress` filtered out; `imputnet/cobalt` discovered!
    assert [r.full_name for r in cobalt.discovered_githubs] == ["imputnet/cobalt"]

    assert results["https://parked.example.com/"].status == SiteStatus.PARKED
    assert results["https://waf.example.com/"].status == SiteStatus.WAF_OR_RATE_LIMITED
    assert results["https://origin-down.example.com/"].status == SiteStatus.DEAD
    assert "522" in results["https://origin-down.example.com/"].reason
    assert results["https://dmca.example.com/"].status == SiteStatus.DEAD
    assert "451" in results["https://dmca.example.com/"].reason
    assert results["https://missing.example.com/"].status == SiteStatus.DEAD


def test_filter_discovered_for_entry_prefers_matching_repo() -> None:
    entry = WikiEntry(
        entry_id="tools.md:10",
        title="LibRedirect",
        category="Privacy",
        subcategory="Frontends",
        source_file="tools.md",
        line_number=10,
        starred=True,
        site_urls=["https://libredirect.github.io/"],
    )
    from fmhy_drift.models import GitHubRef

    candidates = [
        GitHubRef("random", "unrelated-blog", "discovered_on_site", "https://libredirect.github.io/"),
        GitHubRef("libredirect", "instances", "discovered_on_site", "https://libredirect.github.io/"),
    ]
    filtered = filter_discovered_for_entry(entry, candidates)
    assert [r.full_name for r in filtered] == ["libredirect/instances"]
