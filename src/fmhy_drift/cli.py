"""Command-line entrypoint for the FMHY index drift and GitHub staleness checker."""

from __future__ import annotations

import argparse
import asyncio
import os
import shutil
import sys
from datetime import UTC, datetime
from pathlib import Path

from fmhy_drift.github_checker import GitHubActivityChecker
from fmhy_drift.models import (
    DriftReport,
    EntryDriftResult,
    GitHubRef,
    ProxyVerification,
    SiteCheckResult,
    TierThresholds,
    WikiEntry,
)
from fmhy_drift.parser import (
    DEFAULT_FMHY_SINGLE_PAGE_URL,
    fetch_remote_index,
    load_local_index,
)
from fmhy_drift.proxy_guard import (
    ProxyGuardError,
    ProxyMissingError,
    validate_proxy_url,
    verify_proxy_egress,
)
from fmhy_drift.reporter import (
    github_pages_url_from_env,
    render_json_report,
    render_markdown_report,
    send_discord_webhook,
    summarize_report,
)
from fmhy_drift.site_checker import SiteChecker, filter_discovered_for_entry
from fmhy_drift.triage_ui import TRIAGE_HTML
from fmhy_drift.whitelist import load_whitelist


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    return int(raw) if raw else default


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name, "").strip().lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "on"}


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="fmhy-drift",
        description=(
            "Check FMHY index sites for liveness (strictly via proxy, never direct runner IP) "
            "and/or audit linked GitHub repositories for inactivity, archival, and replacements."
        ),
    )
    parser.add_argument(
        "--github-only",
        action="store_true",
        default=_env_bool("GITHUB_ONLY", False),
        help="Only check linked GitHub repositories via GitHub API (skips site liveness; no proxy needed).",
    )
    parser.add_argument(
        "--proxy",
        default=os.environ.get("PROXY_URL", ""),
        help="HTTP/HTTPS/SOCKS5 proxy URL required for site liveness checks (or set PROXY_URL).",
    )
    parser.add_argument(
        "--skip-without-proxy",
        action="store_true",
        help=(
            "If no proxy URL is provided, skip site liveness checks instead of failing "
            "and run only the GitHub repository activity checks."
        ),
    )
    parser.add_argument(
        "--whitelist",
        type=Path,
        default=Path("whitelist.json"),
        help="Path to whitelist.json for ignoring known-good archived/stable repos and URLs.",
    )
    parser.add_argument(
        "--input",
        type=Path,
        default=None,
        help="Optional local FMHY markdown file or docs/ directory. Defaults to fetching api.fmhy.net/single-page.",
    )
    parser.add_argument(
        "--index-url",
        default=os.environ.get("FMHY_INDEX_URL", DEFAULT_FMHY_SINGLE_PAGE_URL),
        help=f"Remote FMHY single-page markdown URL (default: {DEFAULT_FMHY_SINGLE_PAGE_URL}).",
    )
    parser.add_argument(
        "--proxy-index",
        action="store_true",
        help="Also fetch the FMHY index markdown itself through PROXY_URL.",
    )
    parser.add_argument(
        "--proxy-github",
        action="store_true",
        help="Also route GitHub API calls through PROXY_URL (default uses direct GitHub API).",
    )
    parser.add_argument(
        "--github-token",
        default=os.environ.get("GITHUB_TOKEN", ""),
        help="GitHub token for batched GraphQL activity checks (5,000 pts/hr; or set GITHUB_TOKEN).",
    )
    parser.add_argument(
        "--warn-days-1",
        type=int,
        default=_env_int("WARN_DAYS_1", 90),
        help="Tier 1 (Notice) inactivity threshold in days (default: 90 / ~3 months).",
    )
    parser.add_argument(
        "--warn-days-2",
        type=int,
        default=_env_int("WARN_DAYS_2", 180),
        help="Tier 2 (Warning) inactivity threshold in days (default: 180 / ~6 months).",
    )
    parser.add_argument(
        "--warn-days-3",
        type=int,
        default=_env_int("WARN_DAYS_3", 365),
        help="Tier 3 (Critical) inactivity threshold in days (default: 365 / 1 year).",
    )
    parser.add_argument(
        "--discover-github",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Auto-discover linked GitHub repositories on live site homepages when site checks run.",
    )
    parser.add_argument(
        "--include-social",
        action="store_true",
        help="Include Discord/Reddit/Telegram links in site liveness checks (skipped by default).",
    )
    parser.add_argument(
        "--category",
        default=os.environ.get("CATEGORY_FILTER", ""),
        help="Case-insensitive substring filter on category/subcategory name.",
    )
    parser.add_argument(
        "--starred-only",
        action="store_true",
        help="Only check ⭐ recommended entries.",
    )
    parser.add_argument(
        "--max-entries",
        type=int,
        default=_env_int("MAX_ENTRIES", 0),
        help="Optional cap on number of entries to evaluate (0 = all).",
    )
    parser.add_argument(
        "--concurrency",
        type=int,
        default=_env_int("CONCURRENCY", 25),
        help="Max concurrent site liveness checks through proxy (default: 25).",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=20.0,
        help="Per-site HTTP timeout in seconds (default: 20).",
    )
    parser.add_argument(
        "--markdown-out",
        type=Path,
        default=Path("reports/fmhy-drift-report.md"),
        help="Output path for the Markdown report (default: reports/fmhy-drift-report.md).",
    )
    parser.add_argument(
        "--json-out",
        type=Path,
        default=Path("reports/fmhy-drift-report.json"),
        help="Output path for the JSON report (default: reports/fmhy-drift-report.json).",
    )
    parser.add_argument(
        "--html-out",
        type=Path,
        default=Path("reports/index.html"),
        help="Output path for the interactive Triage & Discord Report Builder HTML UI.",
    )
    parser.add_argument(
        "--discord-webhook",
        default=os.environ.get("DISCORD_WEBHOOK_URL", ""),
        help="Optional Discord webhook URL to post run summary embed.",
    )
    return parser


def _filter_entries(
    entries: list[WikiEntry],
    *,
    github_only: bool,
    category_filter: str,
    starred_only: bool,
    max_entries: int,
) -> list[WikiEntry]:
    filtered = entries
    if github_only:
        filtered = [e for e in filtered if e.github_refs]
    if category_filter.strip():
        needle = category_filter.strip().lower()
        filtered = [
            e
            for e in filtered
            if needle in e.category.lower() or needle in e.subcategory.lower()
        ]
    if starred_only:
        filtered = [e for e in filtered if e.starred]
    if max_entries > 0:
        filtered = filtered[:max_entries]
    return filtered


def _write_github_step_summary(markdown_text: str) -> None:
    summary_path = os.environ.get("GITHUB_STEP_SUMMARY", "").strip()
    if not summary_path:
        return
    path = Path(summary_path)
    encoded = markdown_text.encode("utf-8")
    if len(encoded) > 950_000:
        truncated = encoded[:900_000].decode("utf-8", errors="ignore")
        truncated += "\n\n> _Truncated in Step Summary; open the Triage UI or download `fmhy-drift-report.md` artifact._\n"
        path.write_text(truncated, encoding="utf-8")
    else:
        path.write_text(markdown_text, encoding="utf-8")


async def run_drift_pipeline(args: argparse.Namespace) -> int:
    thresholds = TierThresholds(
        tier1_days=args.warn_days_1,
        tier2_days=args.warn_days_2,
        tier3_days=args.warn_days_3,
    )
    whitelist = load_whitelist(args.whitelist)

    run_site_checks = not args.github_only
    proxy_url: str | None = None
    proxy_verification: ProxyVerification | None = None

    # 1. Enforce mandatory proxy for site liveness checks (never touch sites from runner IP)
    if run_site_checks:
        try:
            proxy_url = validate_proxy_url(args.proxy)
        except ProxyMissingError as exc:
            if args.skip_without_proxy:
                print(
                    "⏭️ No proxy configured (PROXY_URL unset): skipping site liveness checks "
                    "(never run from a GitHub runner IP) and running GitHub repository checks only.",
                    flush=True,
                )
                run_site_checks = False
            else:
                print(
                    f"Error: {exc}\nHint: Pass --github-only to check linked GitHub repositories without a proxy.",
                    file=sys.stderr,
                )
                return 2
        except ProxyGuardError as exc:
            print(f"Error: {exc}", file=sys.stderr)
            return 2

    if run_site_checks and proxy_url is not None:
        print("Verifying proxy egress isolation...", flush=True)
        try:
            proxy_verification = await verify_proxy_egress(proxy_url)
        except ProxyGuardError as exc:
            print(f"Error: {exc}", file=sys.stderr)
            return 2

        print(
            f"Proxy verified: {proxy_verification.redacted_proxy} "
            f"(fingerprint={proxy_verification.egress_ip_sha256_prefix}, "
            f"isolated={proxy_verification.verified_distinct_from_host})",
            flush=True,
        )
    else:
        print("Mode: GitHub repository activity check only (site liveness skipped).", flush=True)

    # 2. Load FMHY index
    if args.input is not None:
        index_source = str(args.input.resolve())
        entries = load_local_index(args.input, include_social=args.include_social)
    else:
        index_source = args.index_url
        entries = await fetch_remote_index(
            args.index_url,
            proxy_url=proxy_url if (args.proxy_index and proxy_url) else None,
            include_social=args.include_social,
        )

    entries = _filter_entries(
        entries,
        github_only=not run_site_checks,
        category_filter=args.category,
        starred_only=args.starred_only,
        max_entries=args.max_entries,
    )

    # 3. Optional site liveness checks (strictly via verified proxy, skipping whitelisted URLs)
    site_results_map: dict[str, SiteCheckResult] = {}
    unique_site_count = 0
    whitelisted_sites_skipped = 0

    if run_site_checks and proxy_url is not None:
        raw_urls = list(dict.fromkeys(u for e in entries for u in e.site_urls))
        urls_to_check = [u for u in raw_urls if not whitelist.is_url_whitelisted(u)]
        whitelisted_sites_skipped = len(raw_urls) - len(urls_to_check)
        unique_site_count = len(urls_to_check)
        print(
            f"Loaded {len(entries)} FMHY entries ({unique_site_count} unique site URLs, "
            f"{whitelisted_sites_skipped} whitelisted). Running liveness checks via proxy...",
            flush=True,
        )
        site_checker = SiteChecker(
            proxy_url=proxy_url,
            concurrency=args.concurrency,
            timeout=args.timeout,
            discover_github=args.discover_github,
        )
        site_results_map = await site_checker.check_urls(urls_to_check)
    else:
        print(f"Loaded {len(entries)} FMHY entries with linked GitHub repositories.", flush=True)

    # 4. Merge explicit FMHY GitHub refs + homepage auto-discovered refs, excluding whitelisted repos
    entry_all_refs: list[list[GitHubRef]] = []
    combined_refs_for_api: list[GitHubRef] = []
    whitelisted_repo_keys_seen: set[str] = set()

    for entry in entries:
        merged: list[GitHubRef] = []
        seen_keys: set[str] = set()

        candidates_for_entry = list(entry.github_refs)
        if run_site_checks and args.discover_github:
            raw_candidates: list[GitHubRef] = []
            for u in entry.site_urls:
                sr = site_results_map.get(u)
                if sr and sr.discovered_githubs:
                    raw_candidates.extend(sr.discovered_githubs)
            candidates_for_entry.extend(filter_discovered_for_entry(entry, raw_candidates))

        for cand in candidates_for_entry:
            if whitelist.is_repo_whitelisted(cand.owner, cand.repo):
                whitelisted_repo_keys_seen.add(cand.key)
                continue
            if cand.key not in seen_keys:
                seen_keys.add(cand.key)
                merged.append(cand)

        entry_all_refs.append(merged)
        combined_refs_for_api.extend(merged)

    unique_repo_count = len({r.key for r in combined_refs_for_api})
    print(
        f"Checking activity across {unique_repo_count} unique GitHub repositories "
        f"({len(whitelisted_repo_keys_seen)} whitelisted skipped; "
        f"thresholds: {thresholds.tier1_days}d / {thresholds.tier2_days}d / {thresholds.tier3_days}d)...",
        flush=True,
    )

    # 5. Check GitHub activity, archival status, and archived replacement links
    gh_checker = GitHubActivityChecker(
        thresholds=thresholds,
        github_token=args.github_token or None,
        proxy_url=proxy_url if (args.proxy_github and proxy_url) else None,
    )
    repo_results_map = await gh_checker.check_repos(combined_refs_for_api)

    # 6. Assemble DriftReport (dropping unverified speculative `domain_heuristic` guesses)
    entry_results: list[EntryDriftResult] = []
    for entry, refs in zip(entries, entry_all_refs, strict=True):
        s_results = (
            [site_results_map[u] for u in entry.site_urls if u in site_results_map]
            if run_site_checks
            else []
        )
        kept_refs: list[GitHubRef] = []
        r_results = []
        for r in refs:
            rr = repo_results_map.get(r.key)
            if rr is None:
                continue
            if r.source == "domain_heuristic" and rr.tier.value == "missing":
                continue
            kept_refs.append(r)
            r_results.append(rr)

        if not run_site_checks and not r_results:
            continue

        entry_results.append(
            EntryDriftResult(
                entry=entry,
                site_results=s_results,
                all_github_refs=kept_refs,
                repo_results=r_results,
            )
        )

    report = DriftReport(
        generated_at=datetime.now(UTC),
        index_source=index_source,
        thresholds=thresholds,
        proxy_verification=proxy_verification,
        entries=entry_results,
        unique_sites_checked=unique_site_count,
        unique_repos_checked=unique_repo_count,
        whitelisted_repos_skipped=len(whitelisted_repo_keys_seen),
        whitelisted_sites_skipped=whitelisted_sites_skipped,
    )

    pages_url = github_pages_url_from_env()
    markdown_text = render_markdown_report(report, pages_url=pages_url)
    json_text = render_json_report(report)

    args.markdown_out.parent.mkdir(parents=True, exist_ok=True)
    args.markdown_out.write_text(markdown_text, encoding="utf-8")

    args.json_out.parent.mkdir(parents=True, exist_ok=True)
    args.json_out.write_text(json_text, encoding="utf-8")

    args.html_out.parent.mkdir(parents=True, exist_ok=True)
    args.html_out.write_text(TRIAGE_HTML, encoding="utf-8")

    # Copy whitelist.json alongside index.html so static GitHub Pages bundle has a local fallback
    wl_dest = args.html_out.parent / "whitelist.json"
    if args.whitelist and args.whitelist.is_file() and args.whitelist.resolve() != wl_dest.resolve():
        shutil.copyfile(args.whitelist, wl_dest)
    elif not wl_dest.exists():
        wl_dest.write_text('{\n  "repos": [],\n  "urls": [],\n  "notes": {}\n}\n', encoding="utf-8")

    _write_github_step_summary(markdown_text)

    summary = summarize_report(report)
    print(
        f"Done! GitHub: {summary.repos_active} active, "
        f"{summary.repos_missing} dead/404, "
        f"{summary.repos_archived} archived ({summary.repos_archived_with_replacement} w/ replacement), "
        f"{summary.repos_tier3_critical} >= {thresholds.tier3_days}d, "
        f"{summary.repos_tier2_warning} >= {thresholds.tier2_days}d, "
        f"{summary.repos_tier1_notice} >= {thresholds.tier1_days}d",
        flush=True,
    )
    print(
        f"Reports written to {args.markdown_out}, {args.json_out}, and {args.html_out}",
        flush=True,
    )

    webhook = args.discord_webhook.strip()
    if webhook:
        print("Sending summary to Discord webhook...", flush=True)
        await send_discord_webhook(webhook, report, args.markdown_out)
        print("Discord notification sent.", flush=True)

    return 0


def main(argv: list[str] | None = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)
    try:
        return asyncio.run(run_drift_pipeline(args))
    except ValueError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
