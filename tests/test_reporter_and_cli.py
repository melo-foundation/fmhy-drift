"""Unit tests for Dead vs Archived reporting, Website/Pages links, Whitelist, and Triage UI output."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path

import httpx

from fmhy_drift.cli import main
from fmhy_drift.models import (
    DriftReport,
    EntryDriftResult,
    GitHubRef,
    ProxyVerification,
    RepoActivityResult,
    SiteCheckResult,
    SiteStatus,
    StalenessTier,
    TierThresholds,
    WikiEntry,
)
from fmhy_drift.reporter import (
    build_discord_payload,
    format_duration_human,
    render_json_report,
    render_markdown_report,
    send_discord_webhook,
    summarize_report,
)
from fmhy_drift.whitelist import load_whitelist


def _sample_report(*, github_only: bool = False) -> DriftReport:
    dead_entry = WikiEntry(
        entry_id="tools.md:5",
        title="GoneTool",
        category="Tools",
        subcategory="Utilities",
        source_file="tools.md",
        line_number=5,
        starred=True,
        site_urls=["https://gonetool.github.io/app/"],
        github_refs=[GitHubRef("dev", "gonetool", "github_pages")],
    )
    arch_entry = WikiEntry(
        entry_id="tools.md:6",
        title="MovedTool",
        category="Tools",
        subcategory="Utilities",
        source_file="tools.md",
        line_number=6,
        starred=False,
        site_urls=["https://movedtool.example.org/"],
        github_refs=[GitHubRef("olddev", "movedtool", "explicit_markdown")],
    )

    entries = [
        EntryDriftResult(
            entry=dead_entry,
            site_results=(
                []
                if github_only
                else [
                    SiteCheckResult(
                        url="https://gonetool.github.io/app/",
                        status=SiteStatus.DEAD,
                        http_status=404,
                        reason="Not found / removed (HTTP 404)",
                    )
                ]
            ),
            all_github_refs=list(dead_entry.github_refs),
            repo_results=[
                RepoActivityResult(
                    owner="dev",
                    repo="gonetool",
                    tier=StalenessTier.MISSING,
                    reason="Repository not found or private (HTTP 404)",
                )
            ],
        ),
        EntryDriftResult(
            entry=arch_entry,
            site_results=[],
            all_github_refs=list(arch_entry.github_refs),
            repo_results=[
                RepoActivityResult(
                    owner="olddev",
                    repo="movedtool",
                    tier=StalenessTier.ARCHIVED,
                    inactive_days=300,
                    is_archived=True,
                    homepage_url="https://docs.movedtool.example.org/",
                    replacement_url="https://github.com/newdev/movedtool-ng",
                    replacement_status="active, last push 2d ago",
                    reason="Repository is archived (300d since last push)",
                )
            ],
        ),
    ]

    return DriftReport(
        generated_at=datetime(2026, 11, 24, 12, 0, 0, tzinfo=UTC),
        index_source="tools.md",
        thresholds=TierThresholds(90, 180, 365),
        proxy_verification=(
            None
            if github_only
            else ProxyVerification(
                redacted_proxy="socks5h://***:***@203.0.x.x:1080",
                egress_ip_sha256_prefix="a1b2c3d4e5f6",
                verified_distinct_from_host=True,
            )
        ),
        entries=entries,
        unique_sites_checked=0 if github_only else 1,
        unique_repos_checked=2,
        whitelisted_repos_skipped=3,
    )


def test_whitelist_loader_and_cli_triage_generation(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.delenv("PROXY_URL", raising=False)
    assert main(["--proxy", ""]) == 2

    wl_file = tmp_path / "whitelist.json"
    wl_file.write_text(
        json.dumps({"repos": ["https://github.com/KeepOrg/StableTool", "other/repo"], "urls": []}),
        encoding="utf-8",
    )
    wl = load_whitelist(wl_file)
    assert wl.is_repo_whitelisted("keeporg", "stabletool")
    assert wl.is_repo_whitelisted("Other", "Repo")

    md_in = tmp_path / "sample.md"
    md_in.write_text(
        "# Tools\n* [StableTool](https://github.com/KeepOrg/StableTool)\n",
        encoding="utf-8",
    )
    md_out = tmp_path / "reports" / "out.md"
    json_out = tmp_path / "reports" / "out.json"
    html_out = tmp_path / "reports" / "index.html"

    assert (
        main(
            [
                "--github-only",
                "--whitelist",
                str(wl_file),
                "--input",
                str(md_in),
                "--markdown-out",
                str(md_out),
                "--json-out",
                str(json_out),
                "--html-out",
                str(html_out),
            ]
        )
        == 0
    )
    assert html_out.is_file()
    assert "Discord Report Builder" in html_out.read_text(encoding="utf-8")
    parsed = json.loads(json_out.read_text(encoding="utf-8"))
    assert parsed["summary"]["whitelisted_repos_skipped"] == 1
    assert parsed["summary"]["unique_repos_checked"] == 0


def test_dead_vs_archived_separation_and_attached_links(tmp_path: Path) -> None:
    report = _sample_report(github_only=True)
    md = render_markdown_report(
        report, pages_url="https://melo-foundation.github.io/fmhy-drift/"
    )

    # Dead and Archived are distinct sections
    assert "## 1. 💀 Dead GitHub Repositories" in md
    assert "## 2. 📦 Archived GitHub Repositories" in md
    assert "### 2a. 🔀 Archived With Replacement / New Repo Link" in md

    # GitHub Pages, Website, Homepage, and Replacement links are attached inline!
    assert "Pages: <https://gonetool.github.io/app/>" in md
    assert "Site: <https://movedtool.example.org/>" in md
    assert "Site: <https://docs.movedtool.example.org/>" in md
    assert "https://github.com/newdev/movedtool-ng" in md
    assert "active, last push 2d ago" in md

    data = json.loads(render_json_report(report))
    assert data["summary"]["repos_missing"] == 1
    assert data["summary"]["repos_archived"] == 1
    assert data["summary"]["repos_archived_with_replacement"] == 1

    discord = build_discord_payload(report)
    assert discord["embeds"][0]["title"] == "FMHY GitHub Activity & Staleness Check"

    md_path = tmp_path / "fmhy-drift-report.md"
    md_path.write_text(md, encoding="utf-8")
    captured: list[httpx.Request] = []
    asyncio.run(
        send_discord_webhook(
            "https://discord.com/api/webhooks/123/token",
            report,
            md_path,
            transport=httpx.MockTransport(lambda r: (captured.append(r), httpx.Response(204))[1]),
        )
    )
    assert len(captured) == 1


def test_duration_364d_wayback_dead_label_and_json_fields() -> None:
    # 364d must NEVER format as "12m 4d (364d)"!
    assert format_duration_human(364) == "11m 34d (364d)"
    assert format_duration_human(360) == "11m 30d (360d)"
    assert format_duration_human(365) == "1y (365d)"
    assert format_duration_human(729) == "1y 11m (729d)"

    wb_entry = WikiEntry(
        entry_id="tools.md:10",
        title="WaybackDead",
        category="Tools",
        subcategory="",
        source_file="tools.md",
        line_number=10,
        starred=False,
        site_urls=[],
        github_refs=[GitHubRef("gone", "oldrepo", "explicit_markdown")],
        raw_line="* [WaybackDead](https://github.com/gone/oldrepo)",
    )
    renamed_entry = WikiEntry(
        entry_id="tools.md:11",
        title="RenamedPages",
        category="Tools",
        subcategory="",
        source_file="tools.md",
        line_number=11,
        starred=False,
        site_urls=["https://alice.github.io/old-slug/"],
        github_refs=[GitHubRef("alice", "old-slug", "github_pages")],
        raw_line="* [RenamedPages](https://alice.github.io/old-slug/)",
    )
    report = DriftReport(
        generated_at=datetime(2026, 11, 24, 12, 0, 0, tzinfo=UTC),
        index_source="tools.md",
        thresholds=TierThresholds(90, 180, 365),
        proxy_verification=None,
        entries=[
            EntryDriftResult(
                entry=wb_entry,
                all_github_refs=list(wb_entry.github_refs),
                repo_results=[
                    RepoActivityResult(
                        owner="gone",
                        repo="oldrepo",
                        tier=StalenessTier.MISSING,
                        last_activity_at=datetime(2023, 5, 1, 0, 0, 0, tzinfo=UTC),
                        inactive_days=1303,
                        reason="Repository not found or private (HTTP 404) — last archived alive 2023-05-01",
                    )
                ],
            ),
            EntryDriftResult(
                entry=renamed_entry,
                all_github_refs=list(renamed_entry.github_refs),
                repo_results=[
                    RepoActivityResult(
                        owner="alice",
                        repo="old-slug",
                        canonical_full_name="alice/real-repo",
                        tier=StalenessTier.TIER_3_CRITICAL,
                        inactive_days=500,
                        reason="No push/commit/release for 500 days (resolved -> alice/real-repo)",
                    )
                ],
            ),
        ],
        unique_sites_checked=0,
        unique_repos_checked=2,
    )

    md = render_markdown_report(report)
    assert "[Listed in FMHY]" in md
    assert "[Guessed: github.io (not in FMHY)]" in md
    assert "last seen alive" in md
    assert "without updates, last push `2023-05-01`" not in md

    parsed = json.loads(render_json_report(report))
    assert parsed["entries"][0]["raw_line"] == "* [WaybackDead](https://github.com/gone/oldrepo)"
    assert parsed["entries"][1]["raw_line"] == "* [RenamedPages](https://alice.github.io/old-slug/)"
    repo_json = parsed["entries"][1]["github_repos"][0]
    assert repo_json["full_name"] == "alice/real-repo"
    assert repo_json["requested_full_name"] == "alice/old-slug"

    # Non-JSON 429 response in send_discord_webhook must not crash on resp.json()
    attempts = 0

    def rate_limit_handler(req: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return httpx.Response(429, headers={"Retry-After": "0.01"}, text="<html>Rate limited</html>")
        return httpx.Response(204)

    asyncio.run(
        send_discord_webhook(
            "https://discord.com/api/webhooks/123/token",
            report,
            transport=httpx.MockTransport(rate_limit_handler),
        )
    )
    assert attempts == 2


def test_cli_3dash_deduplication_and_canonical_whitelist_roundtrip(
    monkeypatch,
    tmp_path: Path,
) -> None:
    wl_file = tmp_path / "whitelist.json"
    wl_file.write_text(
        json.dumps(
            {
                "repos": ["CanonOrg/WhitelistedTarget"],
                "urls": ["HTTPS://Example.COM/Some/Path/#frag"],
            }
        ),
        encoding="utf-8",
    )
    wl = load_whitelist(wl_file)
    assert wl.is_url_whitelisted("https://example.com/Some/Path")
    assert wl.is_url_whitelisted("https://EXAMPLE.com/Some/Path/#other")

    md_in = tmp_path / "gaming.md"
    md_in.write_text(
        "# Gaming\n"
        "* [3Dash](https://deluge-team.github.io/3Dash/) / [Editor](https://deluge-team.github.io/3Dash-Editor/)\n"
        "* [MixedSource](https://mixed.github.io/demo/) / [GitHub](https://github.com/mixed/real-demo)\n"
        "* [RenamedWL](https://oldalias.github.io/app/)\n",
        encoding="utf-8",
    )

    async def fake_check_repos(self, refs: list[GitHubRef]) -> dict[str, RepoActivityResult]:
        return {
            "deluge-team/3dash": RepoActivityResult(
                owner="deluge-team",
                repo="3Dash",
                canonical_full_name="deluge-team/deluge-team.github.io",
                tier=StalenessTier.TIER_2_WARNING,
                inactive_days=200,
                reason="No push/commit/release for 200 days (resolved -> deluge-team/deluge-team.github.io)",
            ),
            "deluge-team/3dash-editor": RepoActivityResult(
                owner="deluge-team",
                repo="3Dash-Editor",
                canonical_full_name="deluge-team/deluge-team.github.io",
                tier=StalenessTier.TIER_2_WARNING,
                inactive_days=200,
                reason="No push/commit/release for 200 days (resolved -> deluge-team/deluge-team.github.io)",
            ),
            "mixed/demo": RepoActivityResult(
                owner="mixed",
                repo="demo",
                canonical_full_name="mixed/real-demo",
                tier=StalenessTier.TIER_1_NOTICE,
                inactive_days=100,
                reason="No push/commit/release for 100 days (resolved -> mixed/real-demo)",
            ),
            "mixed/real-demo": RepoActivityResult(
                owner="mixed",
                repo="real-demo",
                canonical_full_name="mixed/real-demo",
                tier=StalenessTier.TIER_1_NOTICE,
                inactive_days=100,
                reason="No push/commit/release for 100 days",
            ),
            "oldalias/app": RepoActivityResult(
                owner="oldalias",
                repo="app",
                canonical_full_name="CanonOrg/WhitelistedTarget",
                tier=StalenessTier.ARCHIVED,
                is_archived=True,
                inactive_days=800,
                reason="Repository archived (resolved -> CanonOrg/WhitelistedTarget)",
            ),
        }

    monkeypatch.setattr("fmhy_drift.cli.GitHubActivityChecker.check_repos", fake_check_repos)

    md_out = tmp_path / "out.md"
    json_out = tmp_path / "out.json"
    html_out = tmp_path / "index.html"

    rc = main(
        [
            "--github-only",
            "--whitelist",
            str(wl_file),
            "--input",
            str(md_in),
            "--markdown-out",
            str(md_out),
            "--json-out",
            str(json_out),
            "--html-out",
            str(html_out),
        ]
    )
    assert rc == 0

    data = json.loads(json_out.read_text(encoding="utf-8"))
    # Canonical whitelist matched `CanonOrg/WhitelistedTarget`, skipping `RenamedWL` and counting it!
    assert data["summary"]["whitelisted_repos_skipped"] == 1
    titles = [e["title"] for e in data["entries"]]
    assert "RenamedWL" not in titles

    # 3Dash has only ONE deduplicated repo entry despite two `.github.io` subpaths resolving to one repo!
    dash_entry = next(e for e in data["entries"] if e["title"] == "3Dash")
    assert len(dash_entry["github_repos"]) == 1
    assert dash_entry["github_repos"][0]["full_name"] == "deluge-team/deluge-team.github.io"
    assert dash_entry["github_repos"][0]["requested_full_name"] == "deluge-team/3Dash"

    # MixedSource deduplicated `mixed/demo` (github_pages) + `mixed/real-demo` (explicit_markdown) -> kept explicit_markdown!
    mixed_entry = next(e for e in data["entries"] if e["title"] == "MixedSource")
    assert len(mixed_entry["github_repos"]) == 1
    assert mixed_entry["github_repos"][0]["source"] == "explicit_markdown"
    assert mixed_entry["github_repos"][0]["requested_full_name"] == "mixed/real-demo"

    # summarize_report also deduplicates by canonical full_name.lower()
    assert data["summary"]["repos_tier2_warning"] == 1
    assert data["summary"]["repos_tier1_notice"] == 1


def test_alive_siblings_mark_dead_link_on_live_line_as_cleanup() -> None:
    from fmhy_drift.cli import _attach_alive_siblings
    from fmhy_drift.models import SignalKind, SiteCheckResult, SiteSignal, SiteStatus, WikiEntry

    def entry(eid: str, urls: list[str]) -> WikiEntry:
        return WikiEntry(
            entry_id=eid, title=eid, category="c", subcategory="s", source_file="f",
            line_number=1, starred=False, site_urls=urls,
        )

    web, ff, chrome = "https://filmot.com/", "https://addons.mozilla.org/x/", "https://chromewebstore.google.com/detail/x/abc"
    lone = "https://lonely.example/"
    evil, evil_mirror = "https://evil.example/", "https://ok-mirror.example/"
    entries = [
        entry("single-page:1#1", [web]),
        entry("single-page:1#2", [ff]),
        entry("single-page:1#3", [chrome]),
        entry("single-page:2", [lone]),
        entry("single-page:3", [evil, evil_mirror]),
    ]
    removed = SiteSignal(kind=SignalKind.STORE_REMOVED, severity="critical", source="chrome", detail="listing gone")
    stale = SiteSignal(kind=SignalKind.STORE_STALE, severity="notice", source="amo", detail="last updated 2024-01-01")
    rereg = SiteSignal(kind=SignalKind.REREGISTERED, severity="critical", source="rdap", detail="registered 2026-01-01")
    results = {
        chrome: SiteCheckResult(url=chrome, status=SiteStatus.DEAD, signals=[removed]),
        ff: SiteCheckResult(url=ff, status=SiteStatus.FLAGGED, signals=[stale]),
        lone: SiteCheckResult(url=lone, status=SiteStatus.DEAD),
        evil: SiteCheckResult(url=evil, status=SiteStatus.DEAD, signals=[rereg]),
    }
    out = _attach_alive_siblings(entries, results)

    assert out[chrome].alive_siblings == [web, ff]
    assert out[chrome].is_link_cleanup
    assert out[ff].alive_siblings == [] and not out[ff].is_link_cleanup
    assert not out[lone].is_link_cleanup
    assert out[evil].alive_siblings == [evil_mirror]
    assert not out[evil].is_link_cleanup
