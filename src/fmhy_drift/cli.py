"""Command-line entrypoint for the FMHY index drift and GitHub staleness checker."""

from __future__ import annotations

import argparse
from dataclasses import replace
import asyncio
import json
import os
import shutil
import sys
from datetime import UTC, datetime
from pathlib import Path

from fmhy_drift.alt_forge_checker import AltForgeActivityChecker
from fmhy_drift.dns_checker import DnsDeadChecker, enrich_unhealthy_sites_last_active, resolving_urls
from fmhy_drift.github_checker import SOURCE_PRIORITY, GitHubActivityChecker, _parse_iso_datetime
from fmhy_drift.models import (
    DriftReport,
    EntryDriftResult,
    GitHubRef,
    ProxyVerification,
    RepoActivityResult,
    RepoVuln,
    SiteCheckResult,
    SiteSignal,
    SiteStatus,
    StalenessTier,
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
from fmhy_drift.run_diff import annotate_changes
from fmhy_drift.reporter import (
    github_pages_url_from_env,
    render_json_report,
    render_markdown_report,
    send_discord_webhook,
    summarize_report,
)
from fmhy_drift.signal_cache import SignalCache
from fmhy_drift.site_checker import SiteChecker, filter_discovered_for_entry
from fmhy_drift.site_signals import run_site_signals
from fmhy_drift.triage_ui import write_triage_site
from fmhy_drift.vuln_checker import RepoVulnChecker
from fmhy_drift.whitelist import load_whitelist


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    return int(raw) if raw else default


def _env_float(name: str) -> float | None:
    raw = os.environ.get(name, "").strip()
    return float(raw) if raw else None


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
            "If no proxy URL is provided, skip HTTP site scraping instead of failing "
            "and run GitHub repository checks + proxy-free DoH DNS dead-domain checks."
        ),
    )
    parser.add_argument(
        "--dns-check",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="Run proxy-free Cloudflare/Google DoH dead-domain checks when HTTP proxy is not used (default: enabled unless --github-only).",
    )
    parser.add_argument(
        "--vuln-check",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Check stale and archived repositories across GitHub/GitLab/Codeberg/Bitbucket for HIGH/CRITICAL vulnerabilities via OSV.dev (default: enabled).",
    )
    parser.add_argument(
        "--previous-report",
        type=Path,
        default=None,
        help="Previously published fmhy-drift-report.json; rows that are new or changed since it are marked for the New folder.",
    )
    parser.add_argument(
        "--reuse-report",
        type=Path,
        default=None,
        help="Optional path to a previous fmhy-drift-report.json to reuse cached GitHub and DNS results.",
    )
    parser.add_argument(
        "--only-alt-forges",
        action="store_true",
        help="When combined with --reuse-report, only scan non-GitHub code forges (GitLab, Codeberg, sr.ht, etc.) + enrich dead/archived metadata.",
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
        "--store-warn-days-1",
        type=int,
        default=_env_int("STORE_WARN_DAYS_1", 365),
        help="Store/registry listing notice threshold in days since last update (default: 365).",
    )
    parser.add_argument(
        "--store-warn-days-2",
        type=int,
        default=_env_int("STORE_WARN_DAYS_2", 730),
        help="Store/registry listing warning threshold in days since last update (default: 730).",
    )
    parser.add_argument(
        "--store-warn-days-3",
        type=int,
        default=_env_int("STORE_WARN_DAYS_3", 1095),
        help="Store/registry listing critical threshold in days since last update (default: 1095).",
    )
    parser.add_argument(
        "--site-signals",
        action=argparse.BooleanOptionalAction,
        default=_env_bool("SITE_SIGNALS", True),
        help="Run passive site signals (parking DNS, RDAP, reputation, stores, archives, CT, scanners) in every mode except --github-only (default: enabled).",
    )
    parser.add_argument(
        "--signal-cache",
        type=Path,
        default=Path(".cache/signal-cache.json"),
        help="Persistent JSON cache for site signals (default: .cache/signal-cache.json).",
    )
    parser.add_argument(
        "--signal-budget-minutes",
        type=float,
        default=_env_float("SIGNAL_BUDGET_MINUTES"),
        help="Time budget for site signals in minutes (or SIGNAL_BUDGET_MINUTES; default: 30, or 5 with --only-alt-forges).",
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


def _load_reused_report(
    path: Path,
) -> tuple[dict[str, RepoActivityResult], dict[str, SiteCheckResult], int, int]:
    """Load cached GitHub `RepoActivityResult`s, `SiteCheckResult`s, `unique_sites_checked`, and `unique_gh_repos` from `fmhy-drift-report.json`."""
    raw = json.loads(path.read_text(encoding="utf-8"))
    repo_cache: dict[str, RepoActivityResult] = {}
    site_cache: dict[str, SiteCheckResult] = {}
    summary = raw.get("summary") or {}
    prev_sites = int(summary.get("unique_sites_checked") or 0)
    prev_repos = int(summary.get("unique_repos_checked") or 0)

    for entry in raw.get("entries") or []:
        if not isinstance(entry, dict):
            continue
        for sr in entry.get("sites") or []:
            if not isinstance(sr, dict) or not sr.get("url"):
                continue
            try:
                st = SiteStatus(sr["status"])
            except Exception:
                continue
            site_cache.setdefault(
                sr["url"],
                SiteCheckResult(
                    url=sr["url"],
                    status=st,
                    http_status=sr.get("http_status"),
                    final_url=sr.get("final_url"),
                    elapsed_ms=int(sr.get("elapsed_ms") or 0),
                    reason=str(sr.get("reason") or ""),
                    last_seen_at=_parse_iso_datetime(sr.get("last_seen_at")),
                    inactive_days=sr.get("inactive_days"),
                    signals=[
                        sig
                        for sig in (
                            SiteSignal.from_json(raw_sig)
                            for raw_sig in (sr.get("signals") or [])
                            if isinstance(raw_sig, dict)
                        )
                        if sig is not None
                    ],
                ),
            )
        for rr in entry.get("github_repos") or []:
            if not isinstance(rr, dict):
                continue
            forge = str(rr.get("forge") or "github")
            forge_host = str(rr.get("forge_host") or "github.com")
            if forge != "github":
                continue
            full_name = str(rr.get("full_name") or "")
            req_full = str(rr.get("requested_full_name") or full_name)
            if "/" not in req_full:
                continue
            owner, repo = req_full.split("/", 1)
            try:
                tier = StalenessTier(rr["tier"])
            except Exception:
                continue
            vulns = [
                RepoVuln(
                    vuln_id=str(v.get("vuln_id") or ""),
                    severity=v.get("severity") if v.get("severity") in {"CRITICAL", "HIGH"} else "HIGH",  # type: ignore[arg-type]
                    summary=str(v.get("summary") or ""),
                    package_name=v.get("package_name"),
                    advisory_url=str(v.get("advisory_url") or ""),
                )
                for v in (rr.get("vulns") or [])
                if isinstance(v, dict) and v.get("vuln_id")
            ]
            obj = RepoActivityResult(
                owner=owner,
                repo=repo,
                tier=tier,
                last_activity_at=_parse_iso_datetime(rr.get("last_activity_at")),
                inactive_days=rr.get("inactive_days"),
                archived_at=_parse_iso_datetime(rr.get("archived_at")),
                archived_days=rr.get("archived_days"),
                is_archived=bool(rr.get("is_archived")),
                is_disabled=bool(rr.get("is_disabled")),
                latest_release_tag=rr.get("latest_release_tag"),
                canonical_full_name=full_name or None,
                canonical_html_url=rr.get("html_url"),
                homepage_url=rr.get("homepage_url"),
                replacement_url=rr.get("replacement_url"),
                replacement_status=rr.get("replacement_status"),
                vulns=vulns,
                reason=str(rr.get("reason") or ""),
                forge=forge,
                forge_host=forge_host,
            )
            repo_cache.setdefault(req_full.lower(), obj)
            if full_name:
                repo_cache.setdefault(full_name.lower(), obj)

    return repo_cache, site_cache, prev_sites, prev_repos


def _attach_alive_siblings(
    entries: list[WikiEntry], site_results_map: dict[str, SiteCheckResult]
) -> dict[str, SiteCheckResult]:
    """Set `alive_siblings` on unhealthy results: other links of the same FMHY line that are not unhealthy.

    One markdown line can yield several entries (`#1`, `#2`... for "/ [Firefox] / [Chrome]" variants),
    so siblings are grouped by the entry id without its `#n` suffix.
    """
    line_urls: dict[str, list[str]] = {}
    for e in entries:
        group = line_urls.setdefault(e.entry_id.split("#", 1)[0], [])
        for u in e.site_urls:
            if u not in group:
                group.append(u)
    out = dict(site_results_map)
    for e in entries:
        group = line_urls.get(e.entry_id.split("#", 1)[0], [])
        for u in e.site_urls:
            sr = out.get(u)
            if sr is None or not sr.is_unhealthy:
                continue
            alive = [
                o
                for o in group
                if o != u and not (out.get(o) is not None and out[o].is_unhealthy)
            ]
            if alive != sr.alive_siblings:
                out[u] = replace(sr, alive_siblings=alive)
    return out


async def run_drift_pipeline(args: argparse.Namespace) -> int:
    thresholds = TierThresholds(
        tier1_days=args.warn_days_1,
        tier2_days=args.warn_days_2,
        tier3_days=args.warn_days_3,
    )
    store_thresholds = TierThresholds(
        tier1_days=args.store_warn_days_1,
        tier2_days=args.store_warn_days_2,
        tier3_days=args.store_warn_days_3,
    )
    whitelist = load_whitelist(args.whitelist)

    run_site_checks = not args.github_only
    run_signals = bool(args.site_signals) and not args.github_only
    proxy_url: str | None = None
    proxy_verification: ProxyVerification | None = None

    # 1. Enforce mandatory proxy for HTTP site liveness checks (never touch sites from runner IP)
    if run_site_checks:
        try:
            proxy_url = validate_proxy_url(args.proxy)
        except ProxyMissingError as exc:
            if args.skip_without_proxy or args.dns_check is True:
                print(
                    "⏭️ No proxy configured (PROXY_URL unset): skipping direct HTTP site checks "
                    "(never run from a GitHub runner IP).",
                    flush=True,
                )
                run_site_checks = False
            else:
                print(
                    f"Error: {exc}\nHint: Pass --github-only (or --dns-check) to run without an HTTP proxy.",
                    file=sys.stderr,
                )
                return 2
        except ProxyGuardError as exc:
            print(f"Error: {exc}", file=sys.stderr)
            return 2

    run_dns_checks = (
        False
        if run_site_checks
        else (args.dns_check if args.dns_check is not None else (not args.github_only))
    )

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
    elif run_dns_checks:
        print(
            "Mode: GitHub repository activity + proxy-free DoH DNS dead-domain check.",
            flush=True,
        )
    elif run_signals:
        print("Mode: GitHub repository activity + passive site signals.", flush=True)
    else:
        print("Mode: GitHub repository activity check only (site checks skipped).", flush=True)

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
        github_only=(not run_site_checks and not run_dns_checks and not run_signals),
        category_filter=args.category,
        starred_only=args.starred_only,
        max_entries=args.max_entries,
    )

    reused_gh_map: dict[str, RepoActivityResult] = {}
    reused_site_map: dict[str, SiteCheckResult] = {}
    prev_unique_sites = 0
    prev_unique_gh_repos = 0
    incremental_alt_only = bool(
        args.only_alt_forges and args.reuse_report and args.reuse_report.is_file()
    )

    if incremental_alt_only and args.reuse_report is not None:
        (
            reused_gh_map,
            reused_site_map,
            prev_unique_sites,
            prev_unique_gh_repos,
        ) = _load_reused_report(args.reuse_report)
        print(
            f"♻️ Incremental alt-forge mode: loaded {len(reused_gh_map)} cached GitHub repo keys "
            f"and {len(reused_site_map)} flagged DNS/site results from {args.reuse_report}.",
            flush=True,
        )

    # 3. Site checks: full HTTP via verified proxy, OR proxy-free DoH DNS check
    site_results_map: dict[str, SiteCheckResult] = {}
    unique_site_count = 0
    whitelisted_sites_skipped = 0

    if incremental_alt_only:
        raw_urls = list(dict.fromkeys(u for e in entries for u in e.site_urls))
        whitelisted_sites_skipped = sum(1 for u in raw_urls if whitelist.is_url_whitelisted(u))
        unique_site_count = prev_unique_sites or (len(raw_urls) - whitelisted_sites_skipped)
        site_results_map = {
            u: sr
            for u, sr in reused_site_map.items()
            if not whitelist.is_url_whitelisted(u)
        }
        # Reused DNS-dead results are rechecked so a domain that came back drops out on the next push run.
        dns_dead_urls = [
            u
            for u, sr in site_results_map.items()
            if sr.is_unhealthy and ("DNS" in sr.reason or sr.reason.startswith("Domain does not exist"))
        ]
        if dns_dead_urls:
            back_up = await resolving_urls(dns_dead_urls)
            for u in back_up:
                site_results_map.pop(u, None)
            print(
                f"Rechecked {len(dns_dead_urls)} reused DNS-dead URL(s): {len(back_up)} resolve again.",
                flush=True,
            )
        if any(
            sr.is_unhealthy and (sr.last_seen_at is None or ("CT" not in sr.reason and "RDAP" not in sr.reason))
            for sr in site_results_map.values()
        ):
            print(
                f"Enriching {len(site_results_map)} cached dead/parked DNS domains with Wayback + CertSpotter CT + RDAP dates...",
                flush=True,
            )
            site_results_map = await enrich_unhealthy_sites_last_active(
                site_results_map,
                force_refresh=True,
            )
    elif run_site_checks and proxy_url is not None:
        raw_urls = list(dict.fromkeys(u for e in entries for u in e.site_urls))
        urls_to_check = [u for u in raw_urls if not whitelist.is_url_whitelisted(u)]
        whitelisted_sites_skipped = len(raw_urls) - len(urls_to_check)
        unique_site_count = len(urls_to_check)
        print(
            f"Loaded {len(entries)} FMHY entries ({unique_site_count} unique site URLs, "
            f"{whitelisted_sites_skipped} whitelisted). Running HTTP liveness checks via proxy...",
            flush=True,
        )
        site_checker = SiteChecker(
            proxy_url=proxy_url,
            concurrency=args.concurrency,
            timeout=args.timeout,
            discover_github=args.discover_github,
        )
        site_results_map = await site_checker.check_urls(urls_to_check)
    elif run_dns_checks:
        raw_urls = list(dict.fromkeys(u for e in entries for u in e.site_urls))
        urls_to_check = [u for u in raw_urls if not whitelist.is_url_whitelisted(u)]
        whitelisted_sites_skipped = len(raw_urls) - len(urls_to_check)
        unique_site_count = len(urls_to_check)
        print(
            f"Loaded {len(entries)} FMHY entries ({unique_site_count} unique site URLs, "
            f"{whitelisted_sites_skipped} whitelisted). Running proxy-free DoH DNS checks...",
            flush=True,
        )
        dns_checker = DnsDeadChecker()
        site_results_map = await dns_checker.check_urls(urls_to_check)
        print(
            f"DoH DNS check flagged {len(site_results_map)} dead/parked site URL(s).",
            flush=True,
        )
    elif run_signals:
        print(f"Loaded {len(entries)} FMHY entries.", flush=True)
    else:
        print(f"Loaded {len(entries)} FMHY entries with linked repositories.", flush=True)

    # 3b. Passive site signals (third-party APIs only; never contacts listed sites)
    if run_signals:
        raw_urls = list(dict.fromkeys(u for e in entries for u in e.site_urls))
        signal_urls = [u for u in raw_urls if not whitelist.is_url_whitelisted(u)]
        if not (incremental_alt_only or run_site_checks or run_dns_checks):
            whitelisted_sites_skipped = len(raw_urls) - len(signal_urls)
            unique_site_count = len(signal_urls)
        if args.signal_budget_minutes is not None:
            budget_minutes = args.signal_budget_minutes
        else:
            budget_minutes = 5.0 if incremental_alt_only else 30.0
        signal_now = datetime.now(UTC)
        signal_cache = SignalCache(args.signal_cache, now=signal_now)
        try:
            site_results_map = await run_site_signals(
                signal_urls,
                cache=signal_cache,
                now=signal_now,
                thresholds=thresholds,
                store_thresholds=store_thresholds,
                budget_seconds=budget_minutes * 60.0,
                github_token=args.github_token or None,
                gsb_api_key=os.environ.get("GOOGLE_SAFE_BROWSING_API_KEY", "").strip() or None,
                abusech_auth_key=os.environ.get("ABUSECH_AUTH_KEY", "").strip() or None,
                urlscan_api_key=os.environ.get("URLSCAN_API_KEY", "").strip() or None,
                cloudflare_api_token=os.environ.get("CLOUDFLARE_API_TOKEN", "").strip() or None,
                cloudflare_account_id=os.environ.get("CLOUDFLARE_ACCOUNT_ID", "").strip() or None,
                certspotter_api_key=os.environ.get("CERTSPOTTER_API_KEY", "").strip() or None,
                existing=site_results_map,
            )
        finally:
            # Saved even when a checker crashes or the job is cancelled, so the next run resumes from here.
            try:
                signal_cache.save()
                print(f"Signal cache saved to {args.signal_cache}", flush=True)
            except OSError as exc:
                print(f"Warning: could not save signal cache {args.signal_cache}: {exc}", file=sys.stderr)

    site_results_map = _attach_alive_siblings(entries, site_results_map)

    # 4. Merge explicit FMHY repo refs + homepage auto-discovered refs, excluding whitelisted repos
    entry_all_refs: list[list[GitHubRef]] = []
    combined_refs_for_api: list[GitHubRef] = []
    whitelisted_repo_keys_seen: set[str] = set()

    for entry in entries:
        merged: list[GitHubRef] = []
        seen_keys: set[str] = set()

        candidates_for_entry = list(entry.github_refs)
        if run_site_checks and args.discover_github and not incremental_alt_only:
            raw_candidates: list[GitHubRef] = []
            for u in entry.site_urls:
                sr = site_results_map.get(u)
                if sr and sr.discovered_githubs:
                    raw_candidates.extend(sr.discovered_githubs)
            candidates_for_entry.extend(filter_discovered_for_entry(entry, raw_candidates))

        for cand in candidates_for_entry:
            if whitelist.is_repo_key_whitelisted(cand.key) or whitelist.is_repo_whitelisted(
                cand.owner, cand.repo, cand.forge_host
            ):
                whitelisted_repo_keys_seen.add(cand.key)
                continue
            if cand.key not in seen_keys:
                seen_keys.add(cand.key)
                merged.append(cand)

        entry_all_refs.append(merged)
        combined_refs_for_api.extend(merged)

    gh_refs_for_api = [r for r in combined_refs_for_api if r.forge == "github"]
    alt_refs_for_api = [r for r in combined_refs_for_api if r.forge != "github"]
    unique_gh_count = len({r.key for r in gh_refs_for_api})
    unique_alt_count = len({r.key for r in alt_refs_for_api})

    # 5. Check GitHub + Non-GitHub Forge activity, archival status, replacements, and OSV vulns
    gh_checker = GitHubActivityChecker(
        thresholds=thresholds,
        github_token=args.github_token or None,
        proxy_url=proxy_url if (args.proxy_github and proxy_url) else None,
    )
    alt_checker = AltForgeActivityChecker(thresholds=thresholds)

    if incremental_alt_only:
        print(
            f"Skipping full GitHub scan (reusing cached GitHub results); enriching dead/archived GitHub author repos "
            f"and checking {unique_alt_count} non-GitHub forge repositories (GitLab/Codeberg/sr.ht/Bitbucket/Pagure/SourceForge)...",
            flush=True,
        )
        gh_results_map = await gh_checker.enrich_dead_and_archived_author_repos(reused_gh_map)
        alt_results_map = await alt_checker.check_repos(alt_refs_for_api)
        if args.vuln_check and alt_results_map:
            print(
                "Scanning stale & archived non-GitHub forge repositories for HIGH/CRITICAL vulnerabilities (OSV.dev)...",
                flush=True,
            )
            vuln_checker = RepoVulnChecker(
                github_token=args.github_token or None,
                min_inactive_days=thresholds.tier1_days,
            )
            alt_results_map = await vuln_checker.enrich_repo_vulns(alt_results_map)
        repo_results_map = {**gh_results_map, **alt_results_map}
        unique_repo_count = (prev_unique_gh_repos or unique_gh_count) + unique_alt_count
    else:
        unique_repo_count = unique_gh_count + unique_alt_count
        print(
            f"Checking activity across {unique_gh_count} GitHub + {unique_alt_count} non-GitHub forge repositories "
            f"({len(whitelisted_repo_keys_seen)} whitelisted skipped; "
            f"thresholds: {thresholds.tier1_days}d / {thresholds.tier2_days}d / {thresholds.tier3_days}d)...",
            flush=True,
        )
        gh_results_map, alt_results_map = await asyncio.gather(
            gh_checker.check_repos(gh_refs_for_api),
            alt_checker.check_repos(alt_refs_for_api),
        )
        repo_results_map = {**gh_results_map, **alt_results_map}

        if args.vuln_check and repo_results_map:
            print(
                "Scanning stale & archived repositories for HIGH/CRITICAL vulnerabilities (OSV.dev)...",
                flush=True,
            )
            vuln_checker = RepoVulnChecker(
                github_token=args.github_token or None,
                min_inactive_days=thresholds.tier1_days,
            )
            repo_results_map = await vuln_checker.enrich_repo_vulns(repo_results_map)

    # 6. Assemble DriftReport (dropping unverified speculative `domain_heuristic` guesses)
    entry_results: list[EntryDriftResult] = []
    for entry, refs in zip(entries, entry_all_refs, strict=True):
        s_results = (
            [site_results_map[u] for u in entry.site_urls if u in site_results_map]
            if (run_site_checks or run_dns_checks or run_signals)
            else []
        )
        candidate_pairs: list[tuple[int, GitHubRef, RepoActivityResult]] = []
        for idx, r in enumerate(refs):
            rr = repo_results_map.get(r.key)
            if rr is None:
                continue
            if whitelist.is_repo_key_whitelisted(rr.full_name.lower()):
                whitelisted_repo_keys_seen.add(rr.full_name.lower())
                continue
            if r.source == "domain_heuristic" and rr.tier.value == "missing":
                continue
            if (
                r.source in {"github_pages", "forge_pages"}
                and rr.tier.value == "active"
                and "backed by private" in rr.reason
            ):
                continue
            candidate_pairs.append((idx, r, rr))

        # Sort strongest source first (`explicit_markdown` > ... > `domain_heuristic`),
        # deduplicate per entry by canonical `rr.full_name.lower()`, then restore entry order.
        candidate_pairs.sort(key=lambda item: (-SOURCE_PRIORITY.get(item[1].source, 0), item[0]))
        seen_canon: set[str] = set()
        deduped_pairs: list[tuple[int, GitHubRef, RepoActivityResult]] = []
        for idx, r, rr in candidate_pairs:
            canon_key = rr.full_name.lower()
            if canon_key in seen_canon:
                continue
            seen_canon.add(canon_key)
            deduped_pairs.append((idx, r, rr))
        deduped_pairs.sort(key=lambda item: item[0])

        kept_refs = [r for _, r, _ in deduped_pairs]
        r_results = [rr for _, _, rr in deduped_pairs]

        if not run_site_checks and not r_results and not s_results:
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
    if args.previous_report is not None and args.previous_report.is_file():
        try:
            prev_payload = json.loads(args.previous_report.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            prev_payload = None
        payload = annotate_changes(json.loads(json_text), prev_payload, carry=incremental_alt_only)
        json_text = json.dumps(payload, indent=2, ensure_ascii=False)
        if payload.get("changes"):
            ch = payload["changes"]
            print(
                f"Changes since {ch['previous_generated_at']}: {ch['new']} new, "
                f"{ch['changed']} changed, {ch['resolved']} resolved",
                flush=True,
            )

    args.markdown_out.parent.mkdir(parents=True, exist_ok=True)
    args.markdown_out.write_text(markdown_text, encoding="utf-8")

    args.json_out.parent.mkdir(parents=True, exist_ok=True)
    args.json_out.write_text(json_text, encoding="utf-8")

    write_triage_site(args.html_out)

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
    if run_signals:
        cats = summary.signal_category_counts
        print(
            f"Site signals: {cats.get('malicious', 0)} malicious, {cats.get('hijack_risk', 0)} hijack risk, "
            f"{cats.get('stores', 0)} store, {cats.get('suspect', 0)} suspect, {summary.sites_flagged} flagged",
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
