"""Tests for multi-forge extraction/checking, dead/archived author repo discovery, dead-DNS last-active dates, multi-forge OSV vulns, and incremental reuse."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest

from fmhy_drift.alt_forge_checker import AltForgeActivityChecker
from fmhy_drift.cli import main
from fmhy_drift.dns_checker import DnsDeadChecker
from fmhy_drift.github_checker import GitHubActivityChecker
from fmhy_drift.models import GitHubRef, RepoActivityResult, StalenessTier, TierThresholds
from fmhy_drift.parser import (
    extract_alt_forge_pages_repo,
    extract_alt_forge_repo,
    parse_markdown_content,
)
from fmhy_drift.vuln_checker import RepoVulnChecker

FIXED_NOW = datetime(2026, 11, 25, 0, 0, 0, tzinfo=UTC)


def test_extract_alt_forge_repos_and_pages() -> None:
    # GitLab subgroup + `/-/tree/...`
    gl = extract_alt_forge_repo(
        "https://gitlab.torproject.org/tpo/applications/tor-browser/-/tree/main"
    )
    assert gl is not None
    assert gl.forge == "gitlab"
    assert gl.forge_host == "gitlab.torproject.org"
    assert gl.owner == "tpo/applications"
    assert gl.repo == "tor-browser"
    assert gl.full_name == "gitlab.torproject.org/tpo/applications/tor-browser"

    # GitLab wiki citation skipped
    assert (
        extract_alt_forge_repo("https://gitlab.torproject.org/legacy/trac/-/wikis/doc/GoodBadISPs")
        is None
    )

    # Codeberg repo + raw subpath kept, wiki skipped
    cb = extract_alt_forge_repo(
        "https://codeberg.org/stdout12/BatUtil/src/branch/master/OfficeScrubber"
    )
    assert cb is not None
    assert cb.forge == "forgejo"
    assert cb.full_name == "codeberg.org/stdout12/BatUtil"
    assert extract_alt_forge_repo("https://codeberg.org/shitter/shitter/wiki/Instances") is None

    # SourceHut, Bitbucket, Pagure, SourceForge
    sr = extract_alt_forge_repo("https://git.sr.ht/~thestr4ng3r/chiaki")
    assert sr is not None and sr.forge == "srht" and sr.full_name == "git.sr.ht/~thestr4ng3r/chiaki"

    bb = extract_alt_forge_repo("https://bitbucket.org/wvd-vegt/ghostbuster/src/master/")
    assert bb is not None and bb.forge == "bitbucket" and bb.full_name == "bitbucket.org/wvd-vegt/ghostbuster"

    pg = extract_alt_forge_repo("https://pagure.io/JShelter/webextension")
    assert pg is not None and pg.forge == "pagure" and pg.full_name == "pagure.io/JShelter/webextension"

    sf = extract_alt_forge_repo("https://sourceforge.net/projects/opencamera/")
    assert sf is not None and sf.forge == "sourceforge" and sf.full_name == "sourceforge.net/projects/opencamera"

    # Forge Pages
    gl_pg = extract_alt_forge_pages_repo("https://bztsrc.gitlab.io/usbimager/", "USBImager")
    assert gl_pg is not None and gl_pg.source == "forge_pages" and gl_pg.full_name == "gitlab.com/bztsrc/usbimager"

    cb_pg = extract_alt_forge_pages_repo("https://rimgo.codeberg.page/", "rimgo")
    assert cb_pg is not None and cb_pg.full_name == "codeberg.org/rimgo/pages"

    sf_pg = extract_alt_forge_pages_repo("https://deadbeef.sourceforge.io/", "DeaDBeeF")
    assert sf_pg is not None and sf_pg.full_name == "sourceforge.net/projects/deadbeef"

    # Markdown bullet attaches `/ [GitLab](...)` and `/ [Codeberg](...)` directly onto parent entry
    md = "* ⭐ **[Tor Browser](https://www.torproject.org/)** - Onion Browser / [GitLab](https://gitlab.torproject.org/tpo/applications/tor-browser)\n"
    entries = parse_markdown_content(md)
    assert len(entries) == 1
    assert entries[0].title == "Tor Browser"
    assert entries[0].site_urls == ["https://www.torproject.org/"]
    assert [r.full_name for r in entries[0].github_refs] == [
        "gitlab.torproject.org/tpo/applications/tor-browser"
    ]


@pytest.mark.anyio
async def test_alt_forge_checker_gitlab_codeberg_srht_and_sourceforge() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        u = str(request.url)
        if "gitlab.com/api/v4/projects/auroraoss%2faurorastore" in u.lower() and "/repository/" not in u and "/releases" not in u:
            return httpx.Response(
                200,
                json={
                    "id": 42,
                    "path_with_namespace": "AuroraOSS/AuroraStore",
                    "web_url": "https://gitlab.com/AuroraOSS/AuroraStore",
                    "archived": False,
                    "empty_repo": False,
                },
            )
        if "gitlab.com/api/v4/projects/42/repository/commits" in u:
            return httpx.Response(
                200,
                json=[{"id": "1111111111111111", "committed_date": "2026-11-10T10:00:00Z"}],
            )
        if "gitlab.com/api/v4/projects/42/releases" in u:
            return httpx.Response(200, json=[])

        if "codeberg.org/api/v1/repos/olddev/legacytool" in u.lower() and "/commits" not in u and "/releases" not in u and "/raw/" not in u:
            return httpx.Response(
                200,
                json={
                    "full_name": "olddev/legacytool",
                    "html_url": "https://codeberg.org/olddev/legacytool",
                    "archived": True,
                    "archived_at": "2025-11-25T00:00:00Z",
                    "empty": False,
                    "description": "Moved to https://codeberg.org/newdev/moderntool",
                },
            )
        if "codeberg.org/api/v1/repos/olddev/legacytool/commits" in u.lower():
            return httpx.Response(
                200,
                json=[{"sha": "2222222222222222", "commit": {"committer": {"date": "2025-05-01T00:00:00Z"}}}],
            )
        if "codeberg.org/api/v1/repos/olddev/legacytool/releases" in u.lower():
            return httpx.Response(200, json=[])
        if "codeberg.org/api/v1/repos/olddev/legacytool/raw/" in u.lower():
            return httpx.Response(404)

        if "git.sr.ht/~thestr4ng3r/chiaki/log/rss.xml" in u:
            xml_body = """<?xml version="1.0" encoding="utf-8"?>
            <rss version="2.0"><channel><description>Chiaki PS4 Remote Play</description>
            <item><guid>https://git.sr.ht/~thestr4ng3r/chiaki/commit/abcdef1234567890</guid>
            <pubDate>Sun, 01 Jun 2025 12:00:00 +0000</pubDate></item></channel></rss>"""
            return httpx.Response(200, text=xml_body)

        if "sourceforge.net/rest/p/oldsfproj" in u and "/activity" not in u:
            return httpx.Response(
                200,
                json={
                    "status": "moved",
                    "moved_to_url": "https://github.com/neworg/oldsfproj",
                    "last_updated": "2024-01-15",
                },
            )
        if "sourceforge.net/projects/oldsfproj/best_release.json" in u:
            return httpx.Response(200, json={})
        if "sourceforge.net/rest/p/oldsfproj/activity" in u:
            return httpx.Response(200, json={"timeline": []})

        return httpx.Response(404)

    checker = AltForgeActivityChecker(
        thresholds=TierThresholds(90, 180, 365),
        now=FIXED_NOW,
        transport=httpx.MockTransport(handler),
    )
    refs = [
        GitHubRef("AuroraOSS", "AuroraStore", "explicit_markdown", forge="gitlab", forge_host="gitlab.com"),
        GitHubRef("olddev", "legacytool", "explicit_markdown", forge="forgejo", forge_host="codeberg.org"),
        GitHubRef("~thestr4ng3r", "chiaki", "explicit_markdown", forge="srht", forge_host="git.sr.ht"),
        GitHubRef("projects", "oldsfproj", "explicit_markdown", forge="sourceforge", forge_host="sourceforge.net"),
    ]
    res = await checker.check_repos(refs)

    gl_res = res["gitlab.com/auroraoss/aurorastore"]
    assert gl_res.tier == StalenessTier.ACTIVE
    assert gl_res.inactive_days == 14
    assert gl_res.head_commit_oid == "1111111111111111"

    cb_res = res["codeberg.org/olddev/legacytool"]
    assert cb_res.tier == StalenessTier.ARCHIVED
    assert cb_res.archived_days == 365
    assert cb_res.replacement_url == "https://codeberg.org/newdev/moderntool"

    sr_res = res["git.sr.ht/~thestr4ng3r/chiaki"]
    assert sr_res.tier == StalenessTier.TIER_3_CRITICAL
    assert sr_res.head_commit_oid == "abcdef1234567890"

    sf_res = res["sourceforge.net/projects/oldsfproj"]
    assert sf_res.tier == StalenessTier.ARCHIVED
    assert sf_res.replacement_url == "https://github.com/neworg/oldsfproj"


@pytest.mark.anyio
async def test_dead_and_archived_github_author_similar_repo_discovery() -> None:
    def gh_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/graphql":
            body = json.loads(request.content.decode())
            q = body.get("query", "")
            if "repositoryOwner" in q:
                return httpx.Response(
                    200,
                    json={
                        "data": {
                            "o0": {
                                "repositories": {
                                    "nodes": [
                                        {
                                            "nameWithOwner": "alice/dotfiles",
                                            "name": "dotfiles",
                                            "isArchived": False,
                                            "isDisabled": False,
                                            "isEmpty": False,
                                            "pushedAt": "2026-11-20T00:00:00Z",
                                        },
                                        {
                                            "nameWithOwner": "alice/coolstream-v2",
                                            "name": "coolstream-v2",
                                            "isArchived": False,
                                            "isDisabled": False,
                                            "isEmpty": False,
                                            "pushedAt": "2026-11-15T00:00:00Z",
                                        },
                                    ]
                                },
                                "repositoriesContributedTo": {"nodes": []},
                            },
                            "o1": {
                                "repositories": {"nodes": []},
                                "repositoriesContributedTo": {
                                    "nodes": [
                                        {
                                            "nameWithOwner": "bobarr-org/bobarr",
                                            "name": "bobarr",
                                            "isArchived": False,
                                            "isDisabled": False,
                                            "isEmpty": False,
                                            "pushedAt": "2026-11-01T00:00:00Z",
                                        }
                                    ]
                                },
                            },
                        }
                    },
                )
        return httpx.Response(404)

    checker = GitHubActivityChecker(
        thresholds=TierThresholds(90, 180, 365),
        github_token="ghp_fake",
        now=FIXED_NOW,
        transport=httpx.MockTransport(gh_handler),
    )
    initial = {
        "alice/coolstream": RepoActivityResult(
            owner="alice",
            repo="coolstream",
            tier=StalenessTier.MISSING,
            reason="Repository not found or private (HTTP 404)",
        ),
        "bob/bobarr": RepoActivityResult(
            owner="bob",
            repo="bobarr",
            tier=StalenessTier.ARCHIVED,
            is_archived=True,
            last_activity_at=datetime(2024, 1, 1, tzinfo=UTC),
            inactive_days=1059,
            reason="Repository archived",
        ),
    }
    enriched = await checker.enrich_dead_and_archived_author_repos(initial)
    assert enriched["alice/coolstream"].replacement_url == "https://github.com/alice/coolstream-v2"
    assert "author's similar repo" in (enriched["alice/coolstream"].replacement_status or "")

    assert enriched["bob/bobarr"].replacement_url == "https://github.com/bobarr-org/bobarr"
    assert "similar repo @bob works in" in (enriched["bob/bobarr"].replacement_status or "")


@pytest.mark.anyio
async def test_dead_dns_enriches_last_active_from_wayback_and_crtsh_and_catches_bogon_ips() -> None:
    def dns_and_archive_handler(request: httpx.Request) -> httpx.Response:
        u = str(request.url)
        host = request.url.params.get("name", "")
        qtype = request.url.params.get("type", "")

        if "cloudflare-dns.com" in u or "dns.google" in u:
            if host == "bogon-dead.example.org":
                if qtype == "A":
                    return httpx.Response(200, json={"Status": 0, "Answer": [{"type": 1, "data": "192.168.1.1"}]})
                return httpx.Response(200, json={"Status": 0})
            if host == "expired-nx.example.org":
                return httpx.Response(200, json={"Status": 3})

        if "web.archive.org/cdx/search/cdx" in u:
            target = request.url.params.get("url", "")
            if "expired-nx.example.org" in target:
                return httpx.Response(
                    200,
                    json=[["timestamp", "statuscode"], ["20240210120000", "200"]],
                )
            return httpx.Response(200, json=[])

        if "api.certspotter.com" in u:
            q = request.url.params.get("domain", "")
            if q == "expired-nx.example.org":
                return httpx.Response(
                    200,
                    json=[
                        {
                            "not_before": "2024-09-01T00:00:00Z",
                            "not_after": "2024-11-30T00:00:00Z",
                            "revoked": False,
                        }
                    ],
                )
            return httpx.Response(200, json=[])

        if "rdap.org/domain/" in u:
            return httpx.Response(
                200,
                json={
                    "status": ["redemptionPeriod"],
                    "events": [{"eventAction": "expiration", "eventDate": "2024-10-01T00:00:00Z"}],
                },
            )

        return httpx.Response(404)

    checker = DnsDeadChecker(
        now=FIXED_NOW,
        transport=httpx.MockTransport(dns_and_archive_handler),
    )
    out = await checker.check_urls(
        ["https://bogon-dead.example.org/", "https://expired-nx.example.org/"]
    )
    assert "https://bogon-dead.example.org/" in out
    assert "192.168.1.1" in out["https://bogon-dead.example.org/"].reason

    nx = out["https://expired-nx.example.org/"]
    # RDAP expiration 2024-10-01 is newer than CT 2024-09-01 & Wayback 2024-02-10!
    assert nx.last_seen_at == datetime(2024, 10, 1, tzinfo=UTC)
    assert nx.inactive_days == 785
    assert "last active 2024-10-01" in nx.reason
    assert "last TLS cert expired 2024-11-30" in nx.reason
    assert "redemptionPeriod" in nx.reason


@pytest.mark.anyio
async def test_vuln_checker_scans_gitlab_and_codeberg_manifests() -> None:
    def forge_handler(request: httpx.Request) -> httpx.Response:
        u = str(request.url)
        if "gitlab.com/api/v4/projects/oldorg%2foldgl/repository/tree" in u.lower():
            return httpx.Response(200, json=[{"name": "requirements.txt", "type": "blob"}])
        if "gitlab.com/api/v4/projects/oldorg%2foldgl/repository/files/requirements.txt/raw" in u.lower():
            return httpx.Response(200, text="urllib3==1.25.8\n")
        if "codeberg.org/api/v1/repos/cborg/oldcb/contents" in u.lower():
            return httpx.Response(200, json=[{"name": "Cargo.toml", "type": "file", "size": 120}])
        if "codeberg.org/api/v1/repos/cborg/oldcb/raw/cargo.toml" in u.lower():
            return httpx.Response(200, text='[dependencies]\nopenssl = "=0.10.20"\n')
        return httpx.Response(404)

    def osv_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/querybatch":
            qs = json.loads(request.content.decode())["queries"]
            res = []
            for q in qs:
                pkg = (q.get("package") or {}).get("name")
                if pkg == "urllib3":
                    res.append({"vulns": [{"id": "GHSA-gl-urllib3"}]})
                elif pkg == "openssl":
                    res.append({"vulns": [{"id": "RUSTSEC-cb-openssl"}]})
                else:
                    res.append({})
            return httpx.Response(200, json={"results": res})
        if request.url.path == "/v1/vulns/GHSA-gl-urllib3":
            return httpx.Response(
                200,
                json={
                    "id": "GHSA-gl-urllib3",
                    "aliases": ["CVE-2024-99991"],
                    "summary": "High severity flaw in urllib3",
                    "database_specific": {"severity": "HIGH"},
                },
            )
        if request.url.path == "/v1/vulns/RUSTSEC-cb-openssl":
            return httpx.Response(
                200,
                json={
                    "id": "RUSTSEC-cb-openssl",
                    "aliases": ["CVE-2024-99992"],
                    "summary": "Critical flaw in openssl crate",
                    "severity": [{"type": "CVSS_V3", "score": "9.8"}],
                },
            )
        return httpx.Response(404)

    vc = RepoVulnChecker(
        github_token=None,
        min_inactive_days=90,
        gh_transport=httpx.MockTransport(forge_handler),
        osv_transport=httpx.MockTransport(osv_handler),
    )
    repos = {
        "gitlab.com/oldorg/oldgl": RepoActivityResult(
            owner="oldorg",
            repo="oldgl",
            tier=StalenessTier.TIER_3_CRITICAL,
            inactive_days=500,
            last_activity_at=datetime(2025, 1, 1, tzinfo=UTC),
            forge="gitlab",
            forge_host="gitlab.com",
        ),
        "codeberg.org/cborg/oldcb": RepoActivityResult(
            owner="cborg",
            repo="oldcb",
            tier=StalenessTier.ARCHIVED,
            is_archived=True,
            inactive_days=600,
            last_activity_at=datetime(2024, 9, 1, tzinfo=UTC),
            forge="forgejo",
            forge_host="codeberg.org",
        ),
    }
    enriched = await vc.enrich_repo_vulns(repos)
    assert enriched["gitlab.com/oldorg/oldgl"].vulns[0].vuln_id == "CVE-2024-99991"
    assert enriched["codeberg.org/cborg/oldcb"].vulns[0].vuln_id == "CVE-2024-99992"
    assert enriched["codeberg.org/cborg/oldcb"].vulns[0].severity == "CRITICAL"


def test_cli_incremental_reuse_report_preserves_github_and_sites(tmp_path: Path) -> None:
    md_file = tmp_path / "sample.md"
    md_file.write_text(
        "# Tools\n* ⭐ **[CachedGH](https://example.org/)** / [GitHub](https://github.com/alice/cached-tool)\n",
        encoding="utf-8",
    )
    prev_json = tmp_path / "prev.json"
    prev_json.write_text(
        json.dumps(
            {
                "generated_at": "2026-11-25T00:00:00+00:00",
                "mode": "github_and_dns",
                "summary": {"unique_sites_checked": 42, "unique_repos_checked": 100},
                "entries": [
                    {
                        "entry_id": "sample.md:2",
                        "sites": [
                            {
                                "url": "https://example.org/",
                                "status": "dead",
                                "last_seen_at": "2025-01-01T00:00:00+00:00",
                                "inactive_days": 693,
                                "reason": "DNS NXDOMAIN — last active 2025-01-01",
                            }
                        ],
                        "github_repos": [
                            {
                                "full_name": "alice/cached-tool",
                                "requested_full_name": "alice/cached-tool",
                                "forge": "github",
                                "forge_host": "github.com",
                                "html_url": "https://github.com/alice/cached-tool",
                                "tier": "tier3_critical",
                                "inactive_days": 500,
                                "last_activity_at": "2025-07-13T00:00:00+00:00",
                                "vulns": [
                                    {
                                        "vuln_id": "CVE-2025-0001",
                                        "severity": "CRITICAL",
                                        "summary": "Cached RCE",
                                        "package_name": "npm:foo@1.0.0",
                                        "advisory_url": "https://github.com/advisories/GHSA-1",
                                    }
                                ],
                                "reason": "No push/commit/release for 500 days",
                            }
                        ],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    out_json = tmp_path / "out" / "report.json"
    out_md = tmp_path / "out" / "report.md"
    out_html = tmp_path / "out" / "index.html"

    rc = main(
        [
            "--skip-without-proxy",
            "--no-vuln-check",
            "--input",
            str(md_file),
            "--reuse-report",
            str(prev_json),
            "--only-alt-forges",
            "--json-out",
            str(out_json),
            "--markdown-out",
            str(out_md),
            "--html-out",
            str(out_html),
        ]
    )
    assert rc == 0
    result = json.loads(out_json.read_text(encoding="utf-8"))
    assert result["summary"]["unique_sites_checked"] == 42
    assert result["entries"][0]["sites"][0]["inactive_days"] == 693
    assert result["entries"][0]["github_repos"][0]["vulns"][0]["vuln_id"] == "CVE-2025-0001"


@pytest.mark.anyio
async def test_gitlab_subgroup_gitea_cross_fallback_and_pagure_html_guard() -> None:
    # 1. Cwtch (`git.openprivacy.ca`) is now extracted directly as `forgejo`!
    cwtch_ref = extract_alt_forge_repo("https://git.openprivacy.ca/cwtch.im/cwtch")
    assert cwtch_ref is not None and cwtch_ref.forge == "forgejo"

    def handler(request: httpx.Request) -> httpx.Response:
        u = str(request.url)
        # GitLab GraphQL returns null project for subgroup `videostreaming/plugins`
        if "gitlab.futo.org/api/graphql" in u:
            return httpx.Response(200, json={"data": {"p0": None}})
        if "gitlab.futo.org/api/v4/projects/videostreaming%2fplugins" in u.lower():
            return httpx.Response(404, json={"message": "404 Project Not Found"})
        if "gitlab.futo.org/api/v4/groups/videostreaming%2fplugins/projects" in u.lower():
            return httpx.Response(
                200,
                json=[
                    {
                        "id": 316,
                        "path_with_namespace": "videostreaming/plugins/crunchyroll",
                        "archived": False,
                        "last_activity_at": "2026-11-20T00:00:00Z",
                    }
                ],
            )

        # Self-hosted server misclassified as GitLab returns HTML 404 on `/api/v4/`, JSON 200 on `/api/v1/`
        if "misclassified.example.org/api/graphql" in u:
            return httpx.Response(404, text="<html>Not Found</html>", headers={"content-type": "text/html"})
        if "misclassified.example.org/api/v4/projects/" in u:
            return httpx.Response(404, text="<html>Not Found</html>", headers={"content-type": "text/html"})
        if "misclassified.example.org/api/v1/repos/cwtch.im/cwtch" in u and "/commits" not in u and "/releases" not in u:
            return httpx.Response(
                200,
                json={
                    "full_name": "cwtch.im/cwtch",
                    "html_url": "https://misclassified.example.org/cwtch.im/cwtch",
                    "archived": False,
                    "empty": False,
                    "updated_at": "2026-11-18T00:00:00Z",
                },
            )
        if "misclassified.example.org/api/v1/repos/cwtch.im/cwtch/" in u:
            return httpx.Response(200, json=[])

        # Pagure `/api/0/` disabled (HTML 404) while web repo page is 200 OK
        if "pagure.io/api/0/JShelter/webextension" in u:
            return httpx.Response(404, text="<!DOCTYPE HTML><html>Not Found</html>", headers={"content-type": "text/html"})
        if u == "https://pagure.io/JShelter/webextension/":
            return httpx.Response(200, text="<html>Updated 2026-10-15</html>", headers={"content-type": "text/html"})

        return httpx.Response(404, json={"message": "Not Found"})

    checker = AltForgeActivityChecker(
        thresholds=TierThresholds(90, 180, 365),
        now=FIXED_NOW,
        transport=httpx.MockTransport(handler),
    )
    res = await checker.check_repos(
        [
            GitHubRef("videostreaming", "plugins", "explicit_markdown", forge="gitlab", forge_host="gitlab.futo.org"),
            GitHubRef("cwtch.im", "cwtch", "explicit_markdown", forge="gitlab", forge_host="misclassified.example.org"),
            GitHubRef("JShelter", "webextension", "explicit_markdown", forge="pagure", forge_host="pagure.io"),
        ]
    )
    assert res["gitlab.futo.org/videostreaming/plugins"].tier == StalenessTier.ACTIVE
    assert res["gitlab.futo.org/videostreaming/plugins"].inactive_days == 5
    assert res["misclassified.example.org/cwtch.im/cwtch"].tier == StalenessTier.ACTIVE
    assert res["pagure.io/jshelter/webextension"].tier == StalenessTier.ACTIVE
