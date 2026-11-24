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
    render_json_report,
    render_markdown_report,
    send_discord_webhook,
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
