"""GitHub repository activity, staleness tier, and archival checker.

Uses batched GitHub GraphQL queries (50 repos/request) when `GITHUB_TOKEN` is available,
with automatic fallback to REST `GET /repos/{owner}/{repo}` for renamed/redirected or
unauthenticated checks.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime

import httpx

from fmhy_drift.models import (
    GitHubRef,
    RepoActivityResult,
    StalenessTier,
    TierThresholds,
)

GITHUB_GRAPHQL_URL = "https://api.github.com/graphql"
GITHUB_REST_BASE = "https://api.github.com"
GRAPHQL_BATCH_SIZE = 50


def _parse_iso_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    normalized = value.strip()
    if normalized.endswith("Z"):
        normalized = normalized[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def _latest_activity(timestamps: list[datetime | None]) -> datetime | None:
    valid = [ts for ts in timestamps if ts is not None]
    return max(valid) if valid else None


def _evaluate_repo_state(
    *,
    owner: str,
    repo: str,
    canonical_full_name: str | None,
    is_archived: bool,
    is_disabled: bool,
    last_activity_at: datetime | None,
    latest_release_tag: str | None,
    thresholds: TierThresholds,
    now: datetime,
) -> RepoActivityResult:
    inactive_days: int | None = None
    if last_activity_at is not None:
        delta = now - last_activity_at
        inactive_days = max(0, int(delta.total_seconds() // 86400))

    rename_note = ""
    if canonical_full_name and canonical_full_name.lower() != f"{owner}/{repo}".lower():
        rename_note = f" (redirected -> {canonical_full_name})"

    if is_disabled:
        return RepoActivityResult(
            owner=owner,
            repo=repo,
            tier=StalenessTier.ARCHIVED,
            last_activity_at=last_activity_at,
            inactive_days=inactive_days,
            is_archived=is_archived,
            is_disabled=True,
            latest_release_tag=latest_release_tag,
            canonical_full_name=canonical_full_name,
            reason=f"Repository is disabled on GitHub{rename_note}",
        )

    if is_archived:
        age_txt = f" ({inactive_days}d since last push)" if inactive_days is not None else ""
        return RepoActivityResult(
            owner=owner,
            repo=repo,
            tier=StalenessTier.ARCHIVED,
            last_activity_at=last_activity_at,
            inactive_days=inactive_days,
            is_archived=True,
            is_disabled=False,
            latest_release_tag=latest_release_tag,
            canonical_full_name=canonical_full_name,
            reason=f"Repository is archived{age_txt}{rename_note}",
        )

    if inactive_days is None:
        return RepoActivityResult(
            owner=owner,
            repo=repo,
            tier=StalenessTier.TIER_3_CRITICAL,
            last_activity_at=None,
            inactive_days=None,
            latest_release_tag=latest_release_tag,
            canonical_full_name=canonical_full_name,
            reason=f"No commits or activity recorded (empty repository){rename_note}",
        )

    tier = thresholds.classify_days(inactive_days)
    reason = f"No push/commit/release for {inactive_days} days{rename_note}"
    return RepoActivityResult(
        owner=owner,
        repo=repo,
        tier=tier,
        last_activity_at=last_activity_at,
        inactive_days=inactive_days,
        is_archived=False,
        is_disabled=False,
        latest_release_tag=latest_release_tag,
        canonical_full_name=canonical_full_name,
        reason=reason,
    )


def _build_graphql_batch_query(batch: list[GitHubRef]) -> str:
    fields: list[str] = []
    for idx, ref in enumerate(batch):
        owner_json = json.dumps(ref.owner)
        repo_json = json.dumps(ref.repo)
        fields.append(
            f"""
  r{idx}: repository(owner: {owner_json}, name: {repo_json}) {{
    nameWithOwner
    isArchived
    isDisabled
    pushedAt
    defaultBranchRef {{
      target {{
        ... on Commit {{
          committedDate
        }}
      }}
    }}
    releases(first: 1, orderBy: {{field: CREATED_AT, direction: DESC}}) {{
      nodes {{
        tagName
        publishedAt
      }}
    }}
  }}"""
        )
    return "query {" + "".join(fields) + "\n}"


class GitHubActivityChecker:
    """Evaluates linked and discovered GitHub repositories against inactivity thresholds."""

    def __init__(
        self,
        *,
        thresholds: TierThresholds | None = None,
        github_token: str | None = None,
        proxy_url: str | None = None,
        concurrency: int = 10,
        timeout: float = 25.0,
        now: datetime | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.thresholds = thresholds or TierThresholds()
        self.github_token = github_token.strip() if github_token else None
        self.proxy_url = proxy_url
        self.concurrency = max(1, concurrency)
        self.timeout = timeout
        self.now = now.astimezone(UTC) if now else datetime.now(UTC)
        self._transport = transport

    def _headers(self) -> dict[str, str]:
        headers = {
            "Accept": "application/vnd.github+json",
            "User-Agent": "fmhy-drift-checker/0.1",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        if self.github_token:
            headers["Authorization"] = f"Bearer {self.github_token}"
        return headers

    async def check_repos(self, refs: list[GitHubRef]) -> dict[str, RepoActivityResult]:
        """Deduplicate `owner/repo` keys and evaluate activity across all repositories."""
        unique_by_key: dict[str, GitHubRef] = {}
        for ref in refs:
            if ref.key not in unique_by_key:
                unique_by_key[ref.key] = ref

        unique_refs = list(unique_by_key.values())
        if not unique_refs:
            return {}

        client_kwargs: dict = {
            "timeout": httpx.Timeout(self.timeout),
            "follow_redirects": True,
            "trust_env": False,
            "headers": self._headers(),
        }
        if self._transport is not None:
            client_kwargs["transport"] = self._transport
        elif self.proxy_url:
            client_kwargs["proxy"] = self.proxy_url

        results: dict[str, RepoActivityResult] = {}

        async with httpx.AsyncClient(**client_kwargs) as client:
            rest_fallbacks: list[GitHubRef] = []

            if self.github_token:
                for start in range(0, len(unique_refs), GRAPHQL_BATCH_SIZE):
                    batch = unique_refs[start : start + GRAPHQL_BATCH_SIZE]
                    unresolved = await self._check_graphql_batch(client, batch, results)
                    rest_fallbacks.extend(unresolved)
            else:
                rest_fallbacks = unique_refs

            if rest_fallbacks:
                sem = asyncio.Semaphore(self.concurrency)

                async def _rest_worker(ref: GitHubRef) -> None:
                    async with sem:
                        results[ref.key] = await self._check_rest_single(client, ref)

                await asyncio.gather(*(_rest_worker(r) for r in rest_fallbacks))

        return results

    async def _check_graphql_batch(
        self,
        client: httpx.AsyncClient,
        batch: list[GitHubRef],
        results: dict[str, RepoActivityResult],
    ) -> list[GitHubRef]:
        """Query up to 50 repos via GraphQL; return any repos needing REST fallback."""
        query = _build_graphql_batch_query(batch)
        try:
            resp = await client.post(GITHUB_GRAPHQL_URL, json={"query": query})
            if resp.status_code != 200:
                return batch
            payload = resp.json()
        except Exception:
            return batch

        data = payload.get("data")
        if not isinstance(data, dict):
            return batch

        unresolved: list[GitHubRef] = []
        for idx, ref in enumerate(batch):
            node = data.get(f"r{idx}")
            if not isinstance(node, dict):
                # Repo might be 301-renamed, 451-DMCA'd, or 404-deleted -> verify via REST
                unresolved.append(ref)
                continue

            pushed_at = _parse_iso_datetime(node.get("pushedAt"))
            commit_date: datetime | None = None
            default_branch = node.get("defaultBranchRef")
            if isinstance(default_branch, dict):
                target = default_branch.get("target")
                if isinstance(target, dict):
                    commit_date = _parse_iso_datetime(target.get("committedDate"))

            release_date: datetime | None = None
            release_tag: str | None = None
            releases = node.get("releases")
            if isinstance(releases, dict):
                nodes = releases.get("nodes")
                if isinstance(nodes, list) and nodes and isinstance(nodes[0], dict):
                    release_date = _parse_iso_datetime(nodes[0].get("publishedAt"))
                    release_tag = nodes[0].get("tagName")

            latest_ts = _latest_activity([pushed_at, commit_date, release_date])
            results[ref.key] = _evaluate_repo_state(
                owner=ref.owner,
                repo=ref.repo,
                canonical_full_name=node.get("nameWithOwner"),
                is_archived=bool(node.get("isArchived")),
                is_disabled=bool(node.get("isDisabled")),
                last_activity_at=latest_ts,
                latest_release_tag=release_tag,
                thresholds=self.thresholds,
                now=self.now,
            )

        return unresolved

    async def _check_rest_single(
        self,
        client: httpx.AsyncClient,
        ref: GitHubRef,
    ) -> RepoActivityResult:
        """Check a single repository via GitHub's REST API (follows 301 repo renames)."""
        url = f"{GITHUB_REST_BASE}/repos/{ref.owner}/{ref.repo}"
        try:
            resp = await client.get(url)
        except httpx.HTTPError as exc:
            return RepoActivityResult(
                owner=ref.owner,
                repo=ref.repo,
                tier=StalenessTier.ERROR,
                reason=f"GitHub API request error: {exc.__class__.__name__}",
            )

        if resp.status_code == 404:
            # Note: GitHub Pages root sites `<owner>.github.io` might come from another repo;
            # still report clearly when the repo doesn't exist.
            return RepoActivityResult(
                owner=ref.owner,
                repo=ref.repo,
                tier=StalenessTier.MISSING,
                reason="Repository not found or private (HTTP 404)",
            )

        if resp.status_code == 451:
            return RepoActivityResult(
                owner=ref.owner,
                repo=ref.repo,
                tier=StalenessTier.MISSING,
                reason="Repository unavailable due to DMCA / legal takedown (HTTP 451)",
            )

        if resp.status_code in {403, 429}:
            remaining = resp.headers.get("x-ratelimit-remaining", "")
            hint = (
                "GitHub API rate limit exceeded (set GITHUB_TOKEN for 5,000 req/hr)"
                if remaining == "0" or resp.status_code == 429
                else f"GitHub API forbidden (HTTP {resp.status_code})"
            )
            return RepoActivityResult(
                owner=ref.owner,
                repo=ref.repo,
                tier=StalenessTier.ERROR,
                reason=hint,
            )

        if resp.status_code != 200:
            return RepoActivityResult(
                owner=ref.owner,
                repo=ref.repo,
                tier=StalenessTier.ERROR,
                reason=f"Unexpected GitHub API status (HTTP {resp.status_code})",
            )

        data = resp.json()
        # Use `pushed_at` (actual git push), NEVER `updated_at` (which updates on stars/watches)
        pushed_at = _parse_iso_datetime(data.get("pushed_at"))
        created_at = _parse_iso_datetime(data.get("created_at"))
        latest_ts = _latest_activity([pushed_at, created_at])

        return _evaluate_repo_state(
            owner=ref.owner,
            repo=ref.repo,
            canonical_full_name=data.get("full_name"),
            is_archived=bool(data.get("archived")),
            is_disabled=bool(data.get("disabled")),
            last_activity_at=latest_ts,
            latest_release_tag=None,
            thresholds=self.thresholds,
            now=self.now,
        )
