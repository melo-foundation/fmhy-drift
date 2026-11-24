"""Markdown, JSON, HTML triage, and Discord webhook report generators for FMHY drift runs."""

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
    is_github_pages_url,
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
    repos_archived_with_replacement: int
    repos_with_severe_vulns: int
    repos_missing: int
    repos_error: int
    newly_discovered_github_links: int
    whitelisted_repos_skipped: int
    whitelisted_sites_skipped: int


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
            repos_by_key.setdefault(rr.full_name.lower(), rr)

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
        repos_archived_with_replacement=sum(
            1 for r in repo_vals if r.tier == StalenessTier.ARCHIVED and r.replacement_url
        ),
        repos_with_severe_vulns=sum(1 for r in repo_vals if r.vulns),
        repos_missing=sum(1 for r in repo_vals if r.tier == StalenessTier.MISSING),
        repos_error=sum(1 for r in repo_vals if r.tier == StalenessTier.ERROR),
        newly_discovered_github_links=newly_discovered_count,
        whitelisted_repos_skipped=report.whitelisted_repos_skipped,
        whitelisted_sites_skipped=report.whitelisted_sites_skipped,
    )


def format_duration_human(inactive_days: int | None) -> str:
    """Format `inactive_days` as a readable span like `3y 4m (1,220d)` or `5m 12d (162d)`."""
    if inactive_days is None:
        return "unknown age"
    years, rem = divmod(inactive_days, 365)
    if years >= 1:
        months = min(11, rem // 30)
        span = f"{years}y {months}m" if months else f"{years}y"
        return f"{span} ({inactive_days}d)"
    months = min(11, rem // 30)
    days = rem - months * 30
    if months >= 1:
        span = f"{months}m {days}d" if days else f"{months}m"
        return f"{span} ({inactive_days}d)"
    return f"{inactive_days}d"


def _repo_staleness_sort_key(rr: RepoActivityResult) -> tuple[int, int]:
    # Missing/404 without timestamp sorts first (1, 0), else (0, inactive_days) descending
    if rr.inactive_days is None:
        return (1, 0)
    return (0, rr.inactive_days)


def _entry_location(item: EntryDriftResult) -> str:
    star = "⭐ " if item.entry.starred else ""
    cat = item.entry.category
    if item.entry.subcategory:
        cat = f"{cat} > {item.entry.subcategory}"
    return f"{star}**{item.entry.title}** (`{cat}` — `{item.entry.entry_id}`)"


SOURCE_LABELS: dict[str, str] = {
    "explicit_markdown": "Listed in FMHY",
    "github_pages": "Guessed: github.io (not in FMHY)",
    "github_cdn": "Guessed: raw/CDN (not in FMHY)",
    "domain_heuristic": "Guessed: subdomain (not in FMHY)",
    "discovered_on_site": "Scraped on site (not in FMHY)",
}


def _repo_source_label(item: EntryDriftResult, rr: RepoActivityResult) -> str:
    req_key = f"{rr.owner.lower()}/{rr.repo.lower()}"
    canon_key = rr.full_name.lower()
    raw_src = next(
        (ref.source for ref in item.all_github_refs if ref.key in {req_key, canon_key}),
        "explicit_markdown",
    )
    return SOURCE_LABELS.get(raw_src, raw_src)


def _format_web_links_inline(item: EntryDriftResult, rr: RepoActivityResult) -> str:
    links = item.web_links_for_repo(rr)
    if not links:
        return ""
    parts: list[str] = []
    for u in links:
        label = "Pages" if is_github_pages_url(u) else "Site"
        parts.append(f"{label}: <{u}>")
    return " | " + " , ".join(parts)


def _format_replacement_inline(rr: RepoActivityResult) -> str:
    if not rr.replacement_url:
        return ""
    status = f" ({rr.replacement_status})" if rr.replacement_status else ""
    return f" | 🔀 **New Repo:** <{rr.replacement_url}>{status}"


def _format_vulns_inline(rr: RepoActivityResult) -> str:
    if not rr.vulns:
        return ""
    items = []
    for v in rr.vulns[:3]:
        pkg_txt = f" in `{v.package_name}`" if v.package_name else ""
        link = f"[{v.vuln_id}]({v.advisory_url})" if v.advisory_url else v.vuln_id
        items.append(f"{link} ({v.severity}{pkg_txt})")
    more = f" +{len(rr.vulns) - 3} more" if len(rr.vulns) > 3 else ""
    return " | 🛡️ **Vulns:** " + ", ".join(items) + more


def render_markdown_report(report: DriftReport, *, pages_url: str | None = None) -> str:
    """Render a human-readable Markdown report organized into Dead vs. Archived vs. Stale."""
    summary = summarize_report(report)
    t = report.thresholds
    ts_str = report.generated_at.strftime("%Y-%m-%d %H:%M:%S UTC")
    pv = report.proxy_verification

    dns_only_sites = pv is None and report.unique_sites_checked > 0
    if pv is not None:
        mode_line = (
            f"- **Proxy Verification:** `{pv.redacted_proxy}` "
            f"(egress fingerprint `{pv.egress_ip_sha256_prefix}`, "
            f"isolated from runner: `{pv.verified_distinct_from_host}`)"
        )
    elif dns_only_sites:
        mode_line = (
            "- **Mode:** `GitHub + Proxy-Free DoH DNS` "
            "_(direct HTTP scraping skipped; dead/parked domains checked via Cloudflare & Google DoH)_"
        )
    else:
        mode_line = "- **Mode:** `GitHub-Only` _(site liveness & homepage scraping skipped — no proxy used)_"

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
    ]
    if pages_url:
        lines.append(f"- **Interactive Triage & Whitelist UI:** {pages_url}")

    lines.extend(
        [
            "",
            "## Summary",
            "",
            "| Metric | Count |",
            "| :--- | ---: |",
            f"| Entries Evaluated | {summary.total_entries} |",
        ]
    )

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
    elif dns_only_sites:
        lines.extend(
            [
                f"| Unique Site URLs Checked (Proxy-Free DoH DNS) | {summary.unique_sites_checked} |",
                f"| 🔴 Domains Dead in DNS (NXDOMAIN / Sinkhole / Empty) | {summary.sites_dead} |",
                f"| 🟣 Domains Parked in DNS (Parking CNAME) | {summary.sites_parked} |",
            ]
        )

    lines.extend(
        [
            f"| Unique GitHub Repos Checked | {summary.unique_repos_checked} |",
            f"| 🟢 Repos Active (`< {t.tier1_days}d`) | {summary.repos_active} |",
            f"| 💀 Dead GitHub Repos (404 / DMCA / Disabled) | {summary.repos_missing} |",
            f"| 🛡️ Old/Archived Repos With Severe Vulns (HIGH/CRIT) | {summary.repos_with_severe_vulns} |",
            f"| 📦 Archived GitHub Repos | {summary.repos_archived} _(🔀 {summary.repos_archived_with_replacement} w/ replacement link)_ |",
            f"| 🔴 Tier 3 Critical (`>= {t.tier3_days}d` inactive) | {summary.repos_tier3_critical} |",
            f"| 🟠 Tier 2 Warning (`{t.tier2_days}–{t.tier3_days - 1}d` inactive) | {summary.repos_tier2_warning} |",
            f"| 🟡 Tier 1 Notice (`{t.tier1_days}–{t.tier2_days - 1}d` inactive) | {summary.repos_tier1_notice} |",
            f"| 🛡️ Whitelisted Repos Skipped | {summary.whitelisted_repos_skipped} |",
        ]
    )
    if pv is not None:
        lines.append(
            f"| 🔎 Newly Discovered GitHub Links on Sites | {summary.newly_discovered_github_links} |"
        )
    if summary.repos_error:
        lines.append(f"| ⚠️ GitHub API Errors / Rate-Limited | {summary.repos_error} |")

    # Section 1: Dead / Missing / Disabled GitHub Repositories (longest dead/disabled first)
    lines.extend(["", "## 1. 💀 Dead GitHub Repositories (404, DMCA 451, or Disabled)", ""])
    dead_repos = _collect_entries_by_tiers(report.entries, {StalenessTier.MISSING})
    if not dead_repos:
        lines.append("_No dead/404/DMCA GitHub repositories detected._")
    else:
        for item, matched_repos in dead_repos:
            lines.append(f"- {_entry_location(item)}")
            for rr in matched_repos:
                src_lbl = _repo_source_label(item, rr)
                web_str = _format_web_links_inline(item, rr)
                if "last archived alive" in rr.reason:
                    base_reason = rr.reason.split(" — last archived alive", 1)[0]
                    if rr.inactive_days is not None:
                        seen_str = (
                            f" (`{rr.last_activity_at.strftime('%Y-%m-%d')}`)"
                            if rr.last_activity_at
                            else ""
                        )
                        age_note = f" — last seen alive **{format_duration_human(rr.inactive_days)}** ago{seen_str}"
                    else:
                        base_reason = rr.reason.replace("last archived alive", "last seen alive")
                        age_note = ""
                    lines.append(
                        f"  - 💀 [{rr.full_name}]({rr.html_url}) `[{src_lbl}]` — {base_reason}{age_note}{web_str}"
                    )
                else:
                    age_note = ""
                    if rr.inactive_days is not None:
                        last_str = (
                            f", last push `{rr.last_activity_at.strftime('%Y-%m-%d')}`"
                            if rr.last_activity_at
                            else ""
                        )
                        age_note = f" — **{format_duration_human(rr.inactive_days)}** without updates{last_str}"
                    lines.append(
                        f"  - 💀 [{rr.full_name}]({rr.html_url}) `[{src_lbl}]` — {rr.reason}{age_note}{web_str}"
                    )

    # Section 2: Archived GitHub Repositories (longest unupdated at the top)
    lines.extend(["", "## 2. 📦 Archived GitHub Repositories", ""])
    archived_repos = _collect_entries_by_tiers(report.entries, {StalenessTier.ARCHIVED})
    if not archived_repos:
        lines.append("_No archived GitHub repositories detected._")
    else:
        with_repl = [
            (it, [r for r in rs if r.replacement_url])
            for it, rs in archived_repos
            if any(r.replacement_url for r in rs)
        ]
        without_repl = [
            (it, [r for r in rs if not r.replacement_url])
            for it, rs in archived_repos
            if any(not r.replacement_url for r in rs)
        ]

        if with_repl:
            lines.extend(["### 2a. 🔀 Archived With Replacement / New Repo Link", ""])
            for item, matched_repos in with_repl:
                lines.append(f"- {_entry_location(item)}")
                for rr in matched_repos:
                    src_lbl = _repo_source_label(item, rr)
                    web_str = _format_web_links_inline(item, rr)
                    repl_str = _format_replacement_inline(rr)
                    arch_str = (
                        f"archived **{format_duration_human(rr.archived_days)}** ago (`{rr.archived_at.strftime('%Y-%m-%d')}`), "
                        if rr.archived_at and rr.archived_days is not None
                        else "archived, "
                    )
                    last_str = (
                        rr.last_activity_at.strftime("%Y-%m-%d")
                        if rr.last_activity_at
                        else "unknown"
                    )
                    lines.append(
                        f"  - 📦 [{rr.full_name}]({rr.html_url}) `[{src_lbl}]` — {arch_str}"
                        f"last push **{format_duration_human(rr.inactive_days)}** ago (`{last_str}`)"
                        f"{web_str}{repl_str}{_format_vulns_inline(rr)}"
                    )
            lines.append("")

        if without_repl:
            lines.extend(["### 2b. 📦 Archived (No Replacement Link Detected)", ""])
            for item, matched_repos in without_repl:
                lines.append(f"- {_entry_location(item)}")
                for rr in matched_repos:
                    src_lbl = _repo_source_label(item, rr)
                    web_str = _format_web_links_inline(item, rr)
                    arch_str = (
                        f"archived **{format_duration_human(rr.archived_days)}** ago (`{rr.archived_at.strftime('%Y-%m-%d')}`), "
                        if rr.archived_at and rr.archived_days is not None
                        else "archived, "
                    )
                    last_str = (
                        rr.last_activity_at.strftime("%Y-%m-%d")
                        if rr.last_activity_at
                        else "unknown"
                    )
                    lines.append(
                        f"  - 📦 [{rr.full_name}]({rr.html_url}) `[{src_lbl}]` — {arch_str}"
                        f"last push **{format_duration_human(rr.inactive_days)}** ago (`{last_str}`)"
                        f"{web_str}{_format_vulns_inline(rr)}"
                    )

    # Sections 3–5: Stale GitHub Repositories by Inactivity Tier (longest unupdated at top)
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
                src_lbl = _repo_source_label(item, rr)
                last_str = (
                    rr.last_activity_at.strftime("%Y-%m-%d")
                    if rr.last_activity_at
                    else "never"
                )
                web_str = _format_web_links_inline(item, rr)
                lines.append(
                    f"  - [{rr.full_name}]({rr.html_url}) `[{src_lbl}]` — **{format_duration_human(rr.inactive_days)}** "
                    f"without updates (last activity `{last_str}`){web_str}{_format_vulns_inline(rr)}"
                )

    # Section 6: Dead / Parked / TLS Error sites
    lines.extend(["", "## 6. 🌐 Unhealthy Sites (Dead, Parked, or TLS Error)", ""])
    if pv is None and not dns_only_sites:
        lines.append(
            "_Skipped in GitHub-only mode (configure `PROXY_URL` or `--dns-check` to enable site checks)._"
        )
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

    # Section 7: Newly Discovered GitHub Repos on Live Sites
    lines.extend(["", "## 7. 🔎 Newly Discovered GitHub Repositories on Live Sites", ""])
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
        matched_repos = sorted(
            (r for r in item.repo_results if r.tier in tiers),
            key=_repo_staleness_sort_key,
            reverse=True,
        )
        if matched_repos:
            matched_list.append((item, matched_repos))
    matched_list.sort(key=lambda pair: _repo_staleness_sort_key(pair[1][0]), reverse=True)
    return matched_list


def render_json_report(report: DriftReport) -> str:
    """Serialize the full `DriftReport` and summary metrics to formatted JSON."""
    summary = summarize_report(report)
    pv = report.proxy_verification
    mode_str = (
        "full"
        if pv is not None
        else ("github_and_dns" if report.unique_sites_checked > 0 else "github_only")
    )
    payload = {
        "generated_at": report.generated_at.isoformat(),
        "mode": mode_str,
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
                "raw_line": item.entry.raw_line,
                "web_urls": list(item.entry.site_urls),
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
                        "requested_full_name": f"{rr.owner}/{rr.repo}",
                        "html_url": rr.html_url,
                        "tier": rr.tier.value,
                        "inactive_days": rr.inactive_days,
                        "last_activity_at": (
                            rr.last_activity_at.isoformat() if rr.last_activity_at else None
                        ),
                        "archived_days": rr.archived_days,
                        "archived_at": rr.archived_at.isoformat() if rr.archived_at else None,
                        "is_archived": rr.is_archived,
                        "is_disabled": rr.is_disabled,
                        "latest_release_tag": rr.latest_release_tag,
                        "homepage_url": rr.homepage_url,
                        "replacement_url": rr.replacement_url,
                        "replacement_status": rr.replacement_status,
                        "vulns": [
                            {
                                "vuln_id": v.vuln_id,
                                "severity": v.severity,
                                "summary": v.summary,
                                "package_name": v.package_name,
                                "advisory_url": v.advisory_url,
                            }
                            for v in rr.vulns
                        ],
                        "reason": rr.reason,
                        "source": next(
                            (
                                ref.source
                                for ref in item.all_github_refs
                                if ref.key
                                in {
                                    f"{rr.owner.lower()}/{rr.repo.lower()}",
                                    rr.full_name.lower(),
                                }
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


def github_pages_url_from_env() -> str | None:
    repo = os.environ.get("GITHUB_REPOSITORY", "").strip()
    if "/" not in repo:
        return None
    owner, name = repo.split("/", 1)
    return f"https://{owner.lower()}.github.io/{name}/"


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
        color = 0xE74C3C
    elif s.sites_dead > 0 or s.repos_tier2_warning > 0:
        color = 0xE67E22
    elif s.repos_tier1_notice > 0:
        color = 0xF1C40F
    else:
        color = 0x2ECC71

    if pv is not None:
        intro = (
            f"Checked **{s.total_entries}** entries (`{s.unique_sites_checked}` unique sites via proxy, "
            f"`{s.unique_repos_checked}` unique GitHub repos)."
        )
    else:
        intro = (
            f"Checked **{s.unique_repos_checked}** unique GitHub repositories across "
            f"**{s.total_entries}** FMHY entries _(GitHub-only mode)_."
        )

    description_lines = [intro]
    pages_url = github_pages_url_from_env()
    if pages_url:
        description_lines.append(f"👉 **[Open Triage & Whitelist UI]({pages_url})**")
    run_url = _github_run_url()
    if run_url:
        description_lines.append(f"[View GitHub Actions Run]({run_url})")

    fields: list[dict] = [
        {
            "name": "🌐 Site Liveness",
            "value": (
                f"🟢 Alive: **{s.sites_alive}**\n"
                f"🔴 Dead: **{s.sites_dead}**\n"
                f"🟣 Parked: **{s.sites_parked}** | 🔐 TLS: **{s.sites_tls_error}**\n"
                f"🛡️ WAF/429: **{s.sites_waf_or_rate_limited}**"
                if pv is not None
                else "⏭️ Skipped _(GitHub-only / no proxy)_"
            ),
            "inline": True,
        },
        {
            "name": "🐙 GitHub Activity",
            "value": (
                f"💀 Dead/404/DMCA: **{s.repos_missing}**\n"
                f"📦 Archived: **{s.repos_archived}** _(🔀 {s.repos_archived_with_replacement} moved)_\n"
                f"🔴 `>={t.tier3_days}d`: **{s.repos_tier3_critical}** | "
                f"🟠 `>={t.tier2_days}d`: **{s.repos_tier2_warning}** | "
                f"🟡 `>={t.tier1_days}d`: **{s.repos_tier1_notice}**\n"
                f"🟢 Active: **{s.repos_active}** | 🛡️ Whitelisted: **{s.whitelisted_repos_skipped}**"
            ),
            "inline": True,
        },
    ]

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
        "description": "\n".join(description_lines),
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
                retry_after = 2.0
                try:
                    body = resp.json()
                    if isinstance(body, dict):
                        retry_after = float(body.get("retry_after", 2.0))
                except (ValueError, TypeError):
                    hdr = resp.headers.get("Retry-After", "").strip()
                    if hdr:
                        try:
                            retry_after = float(hdr)
                        except ValueError:
                            pass
                await asyncio.sleep(min(max(0.0, retry_after), 10.0))
                continue

            resp.raise_for_status()
            return
