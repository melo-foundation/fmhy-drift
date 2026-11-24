"""Data models for FMHY index entries, site liveness results, and GitHub activity."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Literal
from urllib.parse import urlsplit


class SiteStatus(StrEnum):
    """Classification of a site liveness check."""

    ALIVE = "alive"
    DEAD = "dead"
    PARKED = "parked"
    TLS_ERROR = "tls_error"
    WAF_OR_RATE_LIMITED = "waf_or_rate_limited"


class StalenessTier(StrEnum):
    """GitHub repository activity/staleness classification."""

    ACTIVE = "active"
    TIER_1_NOTICE = "tier1_notice"
    TIER_2_WARNING = "tier2_warning"
    TIER_3_CRITICAL = "tier3_critical"
    ARCHIVED = "archived"
    MISSING = "missing"  # Dead: 404 deleted/private, 451 DMCA, or disabled by GitHub
    ERROR = "error"


GitHubRefSource = Literal[
    "explicit_markdown",
    "github_pages",
    "forge_pages",
    "github_cdn",
    "domain_heuristic",
    "discovered_on_site",
]


@dataclass(frozen=True)
class TierThresholds:
    """Inactivity thresholds in days for the three warning levels."""

    tier1_days: int = 90
    tier2_days: int = 180
    tier3_days: int = 365

    def __post_init__(self) -> None:
        if self.tier1_days <= 0:
            raise ValueError("tier1_days must be > 0")
        if not (self.tier1_days < self.tier2_days < self.tier3_days):
            raise ValueError(
                f"Thresholds must be strictly increasing: got "
                f"{self.tier1_days}, {self.tier2_days}, {self.tier3_days}"
            )

    def classify_days(self, inactive_days: int) -> StalenessTier:
        if inactive_days >= self.tier3_days:
            return StalenessTier.TIER_3_CRITICAL
        if inactive_days >= self.tier2_days:
            return StalenessTier.TIER_2_WARNING
        if inactive_days >= self.tier1_days:
            return StalenessTier.TIER_1_NOTICE
        return StalenessTier.ACTIVE


@dataclass(frozen=True)
class GitHubRef:
    """Reference to a Git/code forge repository (GitHub, GitLab, Codeberg, sr.ht, etc.)."""

    owner: str
    repo: str
    source: GitHubRefSource
    discovered_from_url: str | None = None
    entry_title: str | None = None
    forge: str = "github"
    forge_host: str = "github.com"

    @property
    def full_name(self) -> str:
        if self.forge == "github":
            return f"{self.owner}/{self.repo}"
        if self.forge == "sourceforge":
            return f"sourceforge.net/projects/{self.repo}"
        return f"{self.forge_host}/{self.owner}/{self.repo}"

    @property
    def key(self) -> str:
        return self.full_name.lower()

    @property
    def html_url(self) -> str:
        if self.forge == "github":
            return f"https://github.com/{self.owner}/{self.repo}"
        if self.forge == "sourceforge":
            return f"https://sourceforge.net/projects/{self.repo}"
        return f"https://{self.forge_host}/{self.owner}/{self.repo}"


@dataclass
class WikiEntry:
    """Single bullet entry parsed from the FMHY index."""

    entry_id: str
    title: str
    category: str
    subcategory: str
    source_file: str
    line_number: int
    starred: bool
    site_urls: list[str] = field(default_factory=list)
    github_refs: list[GitHubRef] = field(default_factory=list)
    raw_line: str = ""


@dataclass
class SiteCheckResult:
    """Liveness check outcome for a single URL (via proxy or proxy-free DoH DNS)."""

    url: str
    status: SiteStatus
    http_status: int | None = None
    final_url: str | None = None
    elapsed_ms: int = 0
    reason: str = ""
    last_seen_at: datetime | None = None
    inactive_days: int | None = None
    discovered_githubs: list[GitHubRef] = field(default_factory=list)

    @property
    def is_unhealthy(self) -> bool:
        return self.status in {
            SiteStatus.DEAD,
            SiteStatus.PARKED,
            SiteStatus.TLS_ERROR,
        }


@dataclass(frozen=True)
class RepoVuln:
    """High or Critical vulnerability affecting a stale/archived repo or its dependencies."""

    vuln_id: str
    severity: Literal["CRITICAL", "HIGH"]
    summary: str
    package_name: str | None = None
    advisory_url: str = ""


@dataclass
class RepoActivityResult:
    """Activity, archival, homepage, replacement, and vulnerability check outcome for a repo."""

    owner: str
    repo: str
    tier: StalenessTier
    last_activity_at: datetime | None = None
    inactive_days: int | None = None
    archived_at: datetime | None = None
    archived_days: int | None = None
    is_archived: bool = False
    is_disabled: bool = False
    latest_release_tag: str | None = None
    head_commit_oid: str | None = None
    canonical_full_name: str | None = None
    canonical_html_url: str | None = None
    homepage_url: str | None = None
    description: str = ""
    replacement_url: str | None = None
    replacement_status: str | None = None
    vulns: list[RepoVuln] = field(default_factory=list)
    reason: str = ""
    forge: str = "github"
    forge_host: str = "github.com"

    @property
    def full_name(self) -> str:
        if self.canonical_full_name:
            return self.canonical_full_name
        if self.forge == "github":
            return f"{self.owner}/{self.repo}"
        if self.forge == "sourceforge":
            return f"sourceforge.net/projects/{self.repo}"
        return f"{self.forge_host}/{self.owner}/{self.repo}"

    @property
    def requested_key(self) -> str:
        if self.forge == "github":
            return f"{self.owner.lower()}/{self.repo.lower()}"
        if self.forge == "sourceforge":
            return f"sourceforge.net/projects/{self.repo.lower()}"
        return f"{self.forge_host.lower()}/{self.owner.lower()}/{self.repo.lower()}"

    @property
    def html_url(self) -> str:
        if self.canonical_html_url:
            return self.canonical_html_url
        if self.forge == "github":
            return f"https://github.com/{self.full_name}"
        if self.forge == "sourceforge":
            return f"https://sourceforge.net/projects/{self.repo}"
        fn = self.full_name
        if fn.lower().startswith(f"{self.forge_host.lower()}/"):
            return f"https://{fn}"
        return f"https://{self.forge_host}/{fn}"

    @property
    def is_flagged(self) -> bool:
        return self.tier in {
            StalenessTier.TIER_1_NOTICE,
            StalenessTier.TIER_2_WARNING,
            StalenessTier.TIER_3_CRITICAL,
            StalenessTier.ARCHIVED,
            StalenessTier.MISSING,
        }


def is_github_pages_url(url: str) -> bool:
    host = (urlsplit(url).hostname or "").lower()
    return (
        host.endswith(".github.io")
        or host.endswith(".gitlab.io")
        or host.endswith(".codeberg.page")
        or host.endswith(".bitbucket.io")
        or host.endswith(".sourceforge.io")
        or ".pages.gitlab." in host
    )


@dataclass
class EntryDriftResult:
    """Combined site liveness + GitHub activity evaluation for one FMHY entry."""

    entry: WikiEntry
    site_results: list[SiteCheckResult] = field(default_factory=list)
    all_github_refs: list[GitHubRef] = field(default_factory=list)
    repo_results: list[RepoActivityResult] = field(default_factory=list)

    @property
    def all_sites_unhealthy(self) -> bool:
        return bool(self.site_results) and all(r.is_unhealthy for r in self.site_results)

    @property
    def any_site_unhealthy(self) -> bool:
        return any(r.is_unhealthy for r in self.site_results)

    @property
    def newly_discovered_refs(self) -> list[GitHubRef]:
        explicit_keys = {ref.key for ref in self.entry.github_refs}
        return [
            ref
            for ref in self.all_github_refs
            if ref.source == "discovered_on_site" and ref.key not in explicit_keys
        ]

    def web_links_for_repo(self, rr: RepoActivityResult | None = None) -> list[str]:
        """Return deduplicated website and GitHub Pages URLs attached to this entry/repo."""
        seen: set[str] = set()
        links: list[str] = []
        for u in self.entry.site_urls:
            key = u.rstrip("/").lower()
            if key not in seen:
                seen.add(key)
                links.append(u)
        if rr and rr.homepage_url:
            hp = rr.homepage_url.strip()
            hp_host = (urlsplit(hp).hostname or "").lower()
            # Don't attach homepage_url if it just points back to the repo host itself
            same_repo_url = hp.rstrip("/").lower() == rr.html_url.rstrip("/").lower()
            if hp_host not in {"github.com", "www.github.com"} and not same_repo_url:
                key = hp.rstrip("/").lower()
                if key not in seen:
                    seen.add(key)
                    links.append(hp)
        return links


@dataclass
class ProxyVerification:
    """Metadata proving liveness checks exited via a proxy distinct from host IP."""

    redacted_proxy: str
    egress_ip_sha256_prefix: str
    verified_distinct_from_host: bool


@dataclass
class DriftReport:
    """Full run report across all evaluated FMHY entries."""

    generated_at: datetime
    index_source: str
    thresholds: TierThresholds
    proxy_verification: ProxyVerification | None
    entries: list[EntryDriftResult]
    unique_sites_checked: int
    unique_repos_checked: int
    whitelisted_repos_skipped: int = 0
    whitelisted_sites_skipped: int = 0
