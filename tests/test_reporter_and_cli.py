"""Unit tests for Markdown/JSON reporting, Discord webhook sending, and CLI proxy skip behavior."""

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
    render_json_report,
    render_markdown_report,
    send_discord_webhook,
)


def _sample_report() -> DriftReport:
    entry = WikiEntry(
        entry_id="tools.md:5",
        title="AbandonTool",
        category="Tools",
        subcategory="Utilities",
        source_file="tools.md",
        line_number=5,
        starred=True,
        site_urls=["https://abandontool.example.com/"],
        github_refs=[],
    )
    discovered_ref = GitHubRef(
        owner="dev",
        repo="abandontool",
        source="discovered_on_site",
        discovered_from_url="https://abandontool.example.com/",
    )
    entry_res = EntryDriftResult(
        entry=entry,
        site_results=[
            SiteCheckResult(
                url="https://abandontool.example.com/",
                status=SiteStatus.DEAD,
                http_status=404,
                reason="Not found / removed (HTTP 404)",
            )
        ],
        all_github_refs=[discovered_ref],
        repo_results=[
            RepoActivityResult(
                owner="dev",
                repo="abandontool",
                tier=StalenessTier.TIER_2_WARNING,
                last_activity_at=datetime(2026, 4, 1, tzinfo=UTC),
                inactive_days=237,
                reason="No push/commit/release for 237 days",
            )
        ],
    )
    return DriftReport(
        generated_at=datetime(2026, 11, 24, 12, 0, 0, tzinfo=UTC),
        index_source="tools.md",
        thresholds=TierThresholds(90, 180, 365),
        proxy_verification=ProxyVerification(
            redacted_proxy="socks5h://***:***@203.0.x.x:1080",
            egress_ip_sha256_prefix="a1b2c3d4e5f6",
            verified_distinct_from_host=True,
        ),
        entries=[entry_res],
        unique_sites_checked=1,
        unique_repos_checked=1,
    )


def test_cli_refuses_or_skips_when_no_proxy_provided(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.delenv("PROXY_URL", raising=False)
    summary_file = tmp_path / "step_summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary_file))

    # Without --skip-without-proxy -> exit code 2
    assert main(["--proxy", ""]) == 2

    # With --skip-without-proxy -> clean exit 0 and writes step summary
    assert main(["--proxy", "", "--skip-without-proxy"]) == 0
    assert "Skipping FMHY drift check" in summary_file.read_text(encoding="utf-8")


def test_markdown_and_json_reports_and_discord_webhook(tmp_path: Path) -> None:
    report = _sample_report()

    md = render_markdown_report(report)
    assert "AbandonTool" in md
    assert "ALL MIRRORS DOWN" in md
    assert "dev/abandontool" in md
    assert "237d" in md
    assert "Newly Discovered GitHub Repositories" in md

    parsed_json = json.loads(render_json_report(report))
    assert parsed_json["summary"]["sites_dead"] == 1
    assert parsed_json["summary"]["repos_tier2_warning"] == 1
    assert parsed_json["summary"]["newly_discovered_github_links"] == 1

    md_path = tmp_path / "fmhy-drift-report.md"
    md_path.write_text(md, encoding="utf-8")

    captured_requests: list[httpx.Request] = []

    def webhook_handler(request: httpx.Request) -> httpx.Response:
        captured_requests.append(request)
        return httpx.Response(204)

    asyncio.run(
        send_discord_webhook(
            "https://discord.com/api/webhooks/123/token",
            report,
            md_path,
            transport=httpx.MockTransport(webhook_handler),
        )
    )

    assert len(captured_requests) == 1
    req = captured_requests[0]
    assert "multipart/form-data" in req.headers["content-type"]
    body_text = req.read().decode("utf-8", errors="replace")
    assert "FMHY Index Drift & GitHub Staleness Check" in body_text
    assert "fmhy-drift-report.md" in body_text
