"""Markdown, JSON, and Discord webhook report generators for FMHY drift runs."""

from __future__ import annotations

import asyncio
import json
import os
from dataclasses import dataclass
from pathlib import Path

import httpx

from fmhy_drift.models import (
    DriftReport,
    EntryDriftResult,
    RepoActivityResult,
    SiteCheckResult,
    SiteStatus,
    StalenessTier,
)


@dataclass(frozen=True)
class ReportSummary:
    """Aggregated counts across an FMHY drift run."""

    total_entries: int
    site_checks_enabled: bool
    unique_sites_checked: int
    sites_alive: int
    sites_dead: int
    sites_parked: int
    sites_tls_error: int
    sites_waf_or_rate_limited: int
    entries_all_sites_dead: int
    unique_repos_checked: int
    repos_active: int
    repos_tier1_notice: int
    repos_tier2_warning: int
    repos_tier3_critical: int
    repos_archived: int
    repos_missing: int
    repos_error: int
    newly_discovered_github_links: int


def summarize_report(report: DriftReport) -> ReportSummary:
    """Compute deduplicated site and repository health counts."""
    sites_by_url: dict[str, SiteCheckResult] = {}
    repos_by_key: dict[str, RepoActivityResult] = {}
    newly_discovered_count = 0
    entries_all_dead = 0

    for item in report.entries:
        if item.all_sites_unhealthy:
            entries_all_dead += 1
        newly_discovered_count += len(item.newly_discovered_refs)
        for sr in item.site_results:
            sites_by_url.setdefault(sr.url, sr)
        for rr in item.repo_results:
            repos_by_key.setdefault(f"{rr.owner.lower()}/{rr.repo.lower()}", rr)

    site_vals = list(sites_by_url.values())
    repo_vals = list(repos_by_key.values())

    return ReportSummary(
        total_entries=len(report.entries),
        site_checks_enabled=report.proxy_verification is not None,
        unique_sites_checked=report.unique_sites_checked,
        sites_alive=sum(1 for s in site_vals if s.status == SiteStatus.ALIVE),
        sites_dead=sum(1 for s in site_vals if s.status == SiteStatus.DEAD),
        sites_parked=sum(1 for s in site_vals if s.status == SiteStatus.PARKED),
        sites_tls_error=sum(1 for s in site_vals if s.status == SiteStatus.TLS_ERROR),
        sites_waf_or_rate_limited=sum(
            1 for s in site_vals if s.status == SiteStatus.WAF_OR_RATE_LIMITED
        ),
        entries_all_sites_dead=entries_all_dead,
        unique_repos_checked=report.unique_repos_checked,
        repos_active=sum(1 for r in repo_vals if r.tier == StalenessTier.ACTIVE),
        repos_tier1_notice=sum(1 for r in repo_vals if r.tier == StalenessTier.TIER_1_NOTICE),
        repos_tier2_warning=sum(1 for r in repo_vals if r.tier == StalenessTier.TIER_2_WARNING),
        repos_tier3_critical=sum(1 for r in repo_vals if r.tier == StalenessTier.TIER_3_CRITICAL),
        repos_archived=sum(1 for r in repo_vals if r.tier == StalenessTier.ARCHIVED),
        repos_missing=sum(1 for r in repo_vals if r.tier == StalenessTier.MISSING),
        repos_error=sum(1 for r in repo_vals if r.tier == StalenessTier.ERROR),
        newly_discovered_github_links=newly_discovered_count,
    )


def _entry_location(item: EntryDriftResult) -> str:
    star = "⭐ " if item.entry.starred else ""
    cat = item.entry.category
    if item.entry.subcategory:
        cat = f"{cat} > {item.entry.subcategory}"
    return f"{star}**{item.entry.title}** (`{cat}` — `{item.entry.entry_id}`)"


def render_markdown_report(report: DriftReport) -> str:
    """Render a comprehensive human-readable Markdown report."""
    summary = summarize_report(report)
    t = report.thresholds
    ts_str = report.generated_at.strftime("%Y-%m-%d %H:%M:%S UTC")
    pv = report.proxy_verification

    mode_line = (
        f"- **Proxy Verification:** `{pv.redacted_proxy}` "
        f"(egress fingerprint `{pv.egress_ip_sha256_prefix}`, "
        f"isolated from runner: `{pv.verified_distinct_from_host}`)"
        if pv is not None
        else "- **Mode:** `GitHub-Only` _(site liveness & homepage scraping skipped — no proxy used)_"
    )

    lines: list[str] = [
        "# FMHY Index Drift & GitHub Activity Report",
        "",
        f"- **Generated:** `{ts_str}`",
        f"- **Index Source:** `{report.index_source}`",
        mode_line,
        f"- **GitHub Inactivity Thresholds:** "
        f"🟡 Notice `>= {t.tier1_days}d` | "
        f"🟠 Warning `>= {t.tier2_days}d` | "
        f"🔴 Critical `>= {t.tier3_days}d`",
        "",
        "## Summary",
        "",
        "| Metric | Count |",
        "| :--- | ---: |",
        f"| Entries Evaluated | {summary.total_entries} |",
    ]

    if pv is not None:
        lines.extend(
            [
                f"| Unique Sites Checked (via Proxy) | {summary.unique_sites_checked} |",
                f"| 🟢 Sites Alive | {summary.sites_alive} |",
                f"| 🔴 Sites Dead / Unreachable | {summary.sites_dead} |",
                f"| 🟣 Sites Parked / For Sale | {summary.sites_parked} |",
                f"| 🔐 Sites TLS/SSL Error | {summary.sites_tls_error} |",
                f"| 🛡️ Sites Behind WAF / Rate-Limited | {summary.sites_waf_or_rate_limited} |",
                f"| ❌ Entries Where ALL Site Links Failed | {summary.entries_all_sites_dead} |",
            ]
        )

    lines.extend(
        [
            f"| Unique GitHub Repos Checked | {summary.unique_repos_checked} |",
            f"| 🟢 Repos Active (`< {t.tier1_days}d`) | {summary.repos_active} |",
            f"| 🟡 Tier 1 Notice (`{t.tier1_days}–{t.tier2_days - 1}d` inactive) | {summary.repos_tier1_notice} |",
            f"| 🟠 Tier 2 Warning (`{t.tier2_days}–{t.tier3_days - 1}d` inactive) | {summary.repos_tier2_warning} |",
            f"| 🔴 Tier 3 Critical (`>= {t.tier3_days}d` inactive) | {summary.repos_tier3_critical} |",
            f"| 📦 Repos Archived / Disabled | {summary.repos_archived} |",
            f"| 💀 Repos Missing / 404 / DMCA | {summary.repos_missing} |",
        ]
    )
    if pv is not None:
        lines.append(
            f"| 🔎 Newly Discovered GitHub Links on Sites | {summary.newly_discovered_github_links} |"
        )
    if summary.repos_error:
        lines.append(f"| ⚠️ GitHub API Errors / Rate-Limited | {summary.repos_error} |")

    # Section 1: Dead / Parked / TLS Error sites (only when site checks ran)
    lines.extend(["", "## 1. Unhealthy Sites (Dead, Parked, or TLS Error)", ""])
    if pv is None:
        lines.append("_Skipped in GitHub-only mode (configure `PROXY_URL` to enable proxy site liveness checks)._")
    else:
        unhealthy_entries = [e for e in report.entries if e.any_site_unhealthy]
        if not unhealthy_entries:
            lines.append("_No dead, parked, or TLS-broken sites detected._")
        else:
            for item in unhealthy_entries:
                badge = (
                    "🚨 **[ALL MIRRORS DOWN]** "
                    if item.all_sites_unhealthy
                    else "⚠️ **[PARTIAL/MIRROR DOWN]** "
                )
                lines.append(f"- {badge}{_entry_location(item)}")
                for sr in item.site_results:
                    if sr.is_unhealthy:
                        lines.append(f"  - `{sr.status.value}`: <{sr.url}> — {sr.reason}")

    # Section 2: Missing & Archived GitHub Repositories
    lines.extend(["", "## 2. Deleted, DMCA'd, or Archived GitHub Repositories", ""])
    missing_or_archived = _collect_entries_by_tiers(
        report.entries, {StalenessTier.MISSING, StalenessTier.ARCHIVED}
    )
    if not missing_or_archived:
        lines.append("_No missing or archived GitHub repositories detected._")
    else:
        for item, matched_repos in missing_or_archived:
            lines.append(f"- {_entry_location(item)}")
            for rr in matched_repos:
                icon = "💀" if rr.tier == StalenessTier.MISSING else "📦"
                lines.append(f"  - {icon} [{rr.full_name}]({rr.html_url}) — {rr.reason}")

    # Sections 3–5: Stale GitHub Repositories by Inactivity Tier
    tier_sections = [
        (
            StalenessTier.TIER_3_CRITICAL,
            f"## 3. 🔴 Critical Inactivity (`>= {t.tier3_days}` Days Without Activity)",
        ),
        (
            StalenessTier.TIER_2_WARNING,
            f"## 4. 🟠 Warning Inactivity (`{t.tier2_days}–{t.tier3_days - 1}` Days Without Activity)",
        ),
        (
            StalenessTier.TIER_1_NOTICE,
            f"## 5. 🟡 Notice Inactivity (`{t.tier1_days}–{t.tier2_days - 1}` Days Without Activity)",
        ),
    ]

    for tier_enum, header in tier_sections:
        lines.extend(["", header, ""])
        bucket = _collect_entries_by_tiers(report.entries, {tier_enum})
        if not bucket:
            lines.append("_None._")
            continue
        for item, matched_repos in bucket:
            lines.append(f"- {_entry_location(item)}")
            for rr in matched_repos:
                last_str = (
                    rr.last_activity_at.strftime("%Y-%m-%d")
                    if rr.last_activity_at
                    else "never"
                )
                src_map = {ref.key: ref.source for ref in item.all_github_refs}
                src = src_map.get(f"{rr.owner.lower()}/{rr.repo.lower()}", "explicit_markdown")
                via = " _(auto-discovered on site)_" if src == "discovered_on_site" else ""
                lines.append(
                    f"  - [{rr.full_name}]({rr.html_url}) — **{rr.inactive_days}d** inactive "
                    f"(last activity `{last_str}`){via}"
                )

    # Section 6: Newly Discovered GitHub Repos
    lines.extend(["", "## 6. 🔎 Newly Discovered GitHub Repositories on Live Sites", ""])
    if pv is None:
        lines.append("_Skipped in GitHub-only mode._")
    else:
        discovered_entries = [e for e in report.entries if e.newly_discovered_refs]
        if not discovered_entries:
            lines.append("_No unlinked GitHub repositories discovered on checked sites._")
        else:
            for item in discovered_entries:
                lines.append(f"- {_entry_location(item)}")
                repo_lookup = {
                    f"{r.owner.lower()}/{r.repo.lower()}": r for r in item.repo_results
                }
                for ref in item.newly_discovered_refs:
                    rr = repo_lookup.get(ref.key)
                    status_note = ""
                    if rr is not None:
                        if rr.inactive_days is not None:
                            status_note = (
                                f" — `{rr.tier.value}` ({rr.inactive_days}d since last activity)"
                            )
                        else:
                            status_note = f" — `{rr.tier.value}` ({rr.reason})"
                    lines.append(
                        f"  - [{ref.full_name}]({ref.html_url}) "
                        f"(found on <{ref.discovered_from_url}>){status_note}"
                    )

    lines.append("")
    return "\n".join(lines)


def _collect_entries_by_tiers(
    entries: list[EntryDriftResult],
    tiers: set[StalenessTier],
) -> list[tuple[EntryDriftResult, list[RepoActivityResult]]]:
    matched_list: list[tuple[EntryDriftResult, list[RepoActivityResult]]] = []
    for item in entries:
        matched_repos = [r for r in item.repo_results if r.tier in tiers]
        if matched_repos:
            matched_list.append((item, matched_repos))
    return matched_list


def render_json_report(report: DriftReport) -> str:
    """Serialize the full `DriftReport` and summary metrics to formatted JSON."""
    summary = summarize_report(report)
    pv = report.proxy_verification
    payload = {
        "generated_at": report.generated_at.isoformat(),
        "mode": "full" if pv is not None else "github_only",
        "index_source": report.index_source,
        "thresholds": {
            "tier1_days": report.thresholds.tier1_days,
            "tier2_days": report.thresholds.tier2_days,
            "tier3_days": report.thresholds.tier3_days,
        },
        "proxy_verification": (
            {
                "redacted_proxy": pv.redacted_proxy,
                "egress_ip_sha256_prefix": pv.egress_ip_sha256_prefix,
                "verified_distinct_from_host": pv.verified_distinct_from_host,
            }
            if pv is not None
            else None
        ),
        "summary": summary.__dict__,
        "entries": [
            {
                "entry_id": item.entry.entry_id,
                "title": item.entry.title,
                "category": item.entry.category,
                "subcategory": item.entry.subcategory,
                "starred": item.entry.starred,
                "all_sites_unhealthy": item.all_sites_unhealthy,
                "sites": [
                    {
                        "url": sr.url,
                        "status": sr.status.value,
                        "http_status": sr.http_status,
                        "final_url": sr.final_url,
                        "elapsed_ms": sr.elapsed_ms,
                        "reason": sr.reason,
                    }
                    for sr in item.site_results
                ],
                "github_repos": [
                    {
                        "full_name": rr.full_name,
                        "html_url": rr.html_url,
                        "tier": rr.tier.value,
                        "inactive_days": rr.inactive_days,
                        "last_activity_at": (
                            rr.last_activity_at.isoformat() if rr.last_activity_at else None
                        ),
                        "is_archived": rr.is_archived,
                        "is_disabled": rr.is_disabled,
                        "latest_release_tag": rr.latest_release_tag,
                        "reason": rr.reason,
                        "source": next(
                            (
                                ref.source
                                for ref in item.all_github_refs
                                if ref.key == f"{rr.owner.lower()}/{rr.repo.lower()}"
                            ),
                            "explicit_markdown",
                        ),
                    }
                    for rr in item.repo_results
                ],
            }
            for item in report.entries
        ],
    }
    return json.dumps(payload, indent=2, ensure_ascii=False)


def _github_run_url() -> str | None:
    server = os.environ.get("GITHUB_SERVER_URL")
    repo = os.environ.get("GITHUB_REPOSITORY")
    run_id = os.environ.get("GITHUB_RUN_ID")
    if server and repo and run_id:
        return f"{server.rstrip('/')}/{repo}/actions/runs/{run_id}"
    return None


def build_discord_payload(report: DriftReport) -> dict:
    """Build a rich Discord webhook JSON embed summarizing the drift run."""
    s = summarize_report(report)
    t = report.thresholds
    pv = report.proxy_verification

    critical_total = (
        s.entries_all_sites_dead + s.repos_missing + s.repos_archived + s.repos_tier3_critical
    )
    if critical_total > 0:
        color = 0xE74C3C  # Red
    elif s.sites_dead > 0 or s.repos_tier2_warning > 0:
        color = 0xE67E22  # Orange
    elif s.repos_tier1_notice > 0:
        color = 0xF1C40F  # Yellow
    else:
        color = 0x2ECC71  # Green

    highlights: list[str] = []
    for item in report.entries:
        if len(highlights) >= 8:
            break
        if item.all_sites_unhealthy and item.site_results:
            star = "⭐ " if item.entry.starred else ""
            highlights.append(
                f"• 🚨 {star}**{item.entry.title}** — `{item.site_results[0].reason}`"
            )
    for item in report.entries:
        if len(highlights) >= 12:
            break
        for rr in item.repo_results:
            if rr.tier in {
                StalenessTier.MISSING,
                StalenessTier.ARCHIVED,
                StalenessTier.TIER_3_CRITICAL,
            }:
                icon = (
                    "💀"
                    if rr.tier == StalenessTier.MISSING
                    else ("📦" if rr.tier == StalenessTier.ARCHIVED else "🔴")
                )
                detail = (
                    f"{rr.inactive_days}d inactive"
                    if rr.tier == StalenessTier.TIER_3_CRITICAL
                    else rr.tier.value
                )
                highlights.append(f"• {icon} **{item.entry.title}** (`{rr.full_name}`) — {detail}")
                break

    if pv is not None:
        intro = (
            f"Checked **{s.total_entries}** entries (`{s.unique_sites_checked}` unique sites via proxy, "
            f"`{s.unique_repos_checked}` unique GitHub repos)."
        )
    else:
        intro = (
            f"Checked **{s.unique_repos_checked}** unique GitHub repositories across "
            f"**{s.total_entries}** FMHY entries _(GitHub-only mode; site liveness skipped)_."
        )

    description_lines = [intro]
    run_url = _github_run_url()
    if run_url:
        description_lines.append(f"[View GitHub Actions Run & Artifacts]({run_url})")
    if highlights:
        description_lines.append("\n**Top Actionable Flags:**\n" + "\n".join(highlights[:10]))

    description = "\n".join(description_lines)
    if len(description) > 3900:
        description = description[:3897] + "..."

    fields: list[dict] = []
    if pv is not None:
        fields.append(
            {
                "name": "🌐 Site Liveness (Proxy)",
                "value": (
                    f"🟢 Alive: **{s.sites_alive}**\n"
                    f"🔴 Dead: **{s.sites_dead}** _(all-down entries: {s.entries_all_sites_dead})_\n"
                    f"🟣 Parked: **{s.sites_parked}** | 🔐 TLS: **{s.sites_tls_error}**\n"
                    f"🛡️ WAF/429: **{s.sites_waf_or_rate_limited}**"
                ),
                "inline": True,
            }
        )
    else:
        fields.append(
            {
                "name": "🌐 Site Liveness",
                "value": "⏭️ Skipped _(GitHub-only / no proxy)_",
                "inline": True,
            }
        )

    fields.append(
        {
            "name": "🐙 GitHub Activity",
            "value": (
                f"🟢 Active (`<{t.tier1_days}d`): **{s.repos_active}**\n"
                f"🟡 Notice (`>={t.tier1_days}d`): **{s.repos_tier1_notice}**\n"
                f"🟠 Warning (`>={t.tier2_days}d`): **{s.repos_tier2_warning}**\n"
                f"🔴 Critical (`>={t.tier3_days}d`): **{s.repos_tier3_critical}**\n"
                f"📦 Archived: **{s.repos_archived}** | 💀 404/DMCA: **{s.repos_missing}**"
            ),
            "inline": True,
        }
    )

    if pv is not None:
        fields.append(
            {
                "name": "🔎 Linked Repo Discovery",
                "value": f"**{s.newly_discovered_github_links}** unlinked GitHub repo(s) found on live sites",
                "inline": False,
            }
        )

    footer_prefix = (
        f"Proxy fingerprint: {pv.egress_ip_sha256_prefix}"
        if pv is not None
        else "Mode: GitHub-only"
    )

    embed = {
        "title": (
            "FMHY Index Drift & GitHub Staleness Check"
            if pv is not None
            else "FMHY GitHub Activity & Staleness Check"
        ),
        "description": description,
        "color": color,
        "timestamp": report.generated_at.isoformat(),
        "fields": fields,
        "footer": {
            "text": f"{footer_prefix} | Thresholds: {t.tier1_days}d / {t.tier2_days}d / {t.tier3_days}d"
        },
    }

    return {"embeds": [embed]}


async def send_discord_webhook(
    webhook_url: str,
    report: DriftReport,
    markdown_report_path: Path | None = None,
    *,
    timeout: float = 30.0,
    transport: httpx.AsyncBaseTransport | None = None,
) -> None:
    """Post the drift summary embed and attach the Markdown report file to Discord."""
    payload = build_discord_payload(report)
    client_kwargs: dict = {
        "timeout": httpx.Timeout(timeout),
        "trust_env": False,
    }
    if transport is not None:
        client_kwargs["transport"] = transport

    files: dict[str, tuple[str, bytes, str]] | None = None
    if markdown_report_path and markdown_report_path.is_file():
        raw_bytes = markdown_report_path.read_bytes()
        if len(raw_bytes) <= 9_500_000:
            files = {
                "files[0]": (
                    markdown_report_path.name,
                    raw_bytes,
                    "text/markdown; charset=utf-8",
                )
            }

    async with httpx.AsyncClient(**client_kwargs) as client:
        for attempt in range(2):
            if files:
                resp = await client.post(
                    webhook_url,
                    data={"payload_json": json.dumps(payload)},
                    files=files,
                )
            else:
                resp = await client.post(webhook_url, json=payload)

            if resp.status_code == 429 and attempt == 0:
                retry_after = float(resp.json().get("retry_after", 2.0))
                await asyncio.sleep(min(retry_after, 10.0))
                continue

            resp.raise_for_status()
            return
