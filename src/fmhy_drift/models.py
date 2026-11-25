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
    # Site not proven dead, but carries one or more passive `SiteSignal`s (hijack, malware, store, suspect).
    FLAGGED = "flagged"


class SignalKind(StrEnum):
    """Passive site-health signal produced without contacting the target site from the runner IP."""

    PARKED_DNS = "parked_dns"  # NS / A / CNAME points at a domain-parking provider
    DOMAIN_EXPIRED = "domain_expired"  # RDAP expiration passed, redemption / pendingDelete, or deleted (registry 404)
    DOMAIN_HELD = "domain_held"  # clientHold / serverHold on an unexpired domain (registrar / registry suspension)
    REREGISTERED = "reregistered"  # RDAP registration date is newer than the FMHY listing date
    DROPCATCH_REGISTRAR = "dropcatch_registrar"  # registrar is a known drop-catch / backorder service
    MALICIOUS = "malicious"  # listed by Google Safe Browsing / URLhaus / OpenPhish
    STORE_REMOVED = "store_removed"  # store / registry listing returns 404 / delisted
    STORE_STALE = "store_stale"  # store / registry listing last updated >= tier1 days ago
    CERT_LAPSED = "cert_lapsed"  # no CT-logged TLS renewal for a long time while DNS still resolves
    ARCHIVE_DOWN = "archive_down"  # latest Wayback / Common Crawl captures are consistently non-200
    ARCHIVE_MOVED = "archive_moved"  # latest captures redirect to a different registrable domain
    SCAN_DOWN = "scan_down"  # third-party scanner (urlscan.io / Cloudflare) confirmed site down
    SCAN_UP = "scan_up"  # third-party scanner confirmed site up (clears suspect signals)


SignalSeverity = Literal["critical", "warning", "notice", "info"]

# Dead links with these signals are threats, not cleanup items, even when sibling links still work.
LINK_CLEANUP_EXCLUDED_KINDS = frozenset(
    {SignalKind.MALICIOUS, SignalKind.REREGISTERED, SignalKind.DROPCATCH_REGISTRAR}
)

# Signal kinds that prove the site is unusable on their own.
DEAD_SIGNAL_KINDS = frozenset(
    {SignalKind.STORE_REMOVED, SignalKind.SCAN_DOWN}
)


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


@dataclass(frozen=True)
class SiteSignal:
    """One passive health signal attached to a site URL."""

    kind: SignalKind
    severity: SignalSeverity
    source: str  # e.g. "rdap", "google_safe_browsing", "urlhaus", "openphish", "amo", "wayback"
    detail: str  # short human-readable fact, e.g. "registered 2025-03-02, listed on FMHY since 2023-01-10"
    observed_at: datetime | None = None  # the date the signal refers to (registration, last update, last capture)
    evidence_url: str | None = None  # link a human can open to verify

    def to_json(self) -> dict:
        return {
            "kind": self.kind.value,
            "severity": self.severity,
            "source": self.source,
            "detail": self.detail,
            "observed_at": self.observed_at.isoformat() if self.observed_at else None,
            "evidence_url": self.evidence_url,
        }

    @classmethod
    def from_json(cls, raw: dict) -> SiteSignal | None:
        try:
            kind = SignalKind(raw["kind"])
        except (KeyError, ValueError):
            return None
        sev = raw.get("severity")
        if sev not in {"critical", "warning", "notice", "info"}:
            sev = "notice"
        obs = raw.get("observed_at")
        obs_dt: datetime | None = None
        if isinstance(obs, str) and obs:
            try:
                obs_dt = datetime.fromisoformat(obs.replace("Z", "+00:00"))
            except ValueError:
                obs_dt = None
        return cls(
            kind=kind,
            severity=sev,  # type: ignore[arg-type]
            source=str(raw.get("source") or ""),
            detail=str(raw.get("detail") or ""),
            observed_at=obs_dt,
            evidence_url=raw.get("evidence_url") or None,
        )


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
    signals: list[SiteSignal] = field(default_factory=list)
    # Other links on the same FMHY line that are not dead / parked / TLS-broken (set per run by the CLI).
    alive_siblings: list[str] = field(default_factory=list)
    # Outage tracking for down-type results (set per run by `hold.apply_hold`).
    down_since: datetime | None = None
    down_since_source: str = ""
    down_days: int | None = None
    held: bool = False

    @property
    def signal_kinds(self) -> set[SignalKind]:
        return {s.kind for s in self.signals}

    @property
    def is_link_cleanup(self) -> bool:
        """Dead link whose FMHY line still has working links: remove the link, keep the entry.

        Active threats (malicious, re-registered, drop-caught) stay in their own categories.
        """
        if self.held or not self.alive_siblings or not self.is_unhealthy:
            return False
        return not (self.signal_kinds & LINK_CLEANUP_EXCLUDED_KINDS)

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
    hold_days: int = 30
