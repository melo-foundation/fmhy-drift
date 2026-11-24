"""GitHub repository activity, staleness tier, archival, and replacement repo checker.

Uses batched GitHub GraphQL queries (50 repos/request) when `GITHUB_TOKEN` is available,
with automatic REST fallback for renamed/redirected or unauthenticated checks, plus
README/description successor link detection for archived repositories.
"""

from __future__ import annotations

import asyncio
import json
import re
from dataclasses import replace
from datetime import UTC, datetime
from urllib.parse import urlsplit

import httpx

from fmhy_drift.models import (
    GitHubRef,
    RepoActivityResult,
    StalenessTier,
    TierThresholds,
)
from fmhy_drift.parser import clean_url, extract_github_repo
from fmhy_drift.site_checker import BOILERPLATE_GITHUB_REPOS

GITHUB_GRAPHQL_URL = "https://api.github.com/graphql"
GITHUB_REST_BASE = "https://api.github.com"
GRAPHQL_BATCH_SIZE = 50
MAX_README_SCAN_CHARS = 3500

REPLACEMENT_CUE_RE = re.compile(
    r"\b("
    r"moved|migrat\w*|supersed\w*|replac\w*|continu\w*|successor|"
    r"new\s+repo\w*|new\s+home|new\s+location|active\s+at|maintained\s+at|"
    r"now\s+at|hosted\s+at|development\s+at|fork\w*|instead|"
    r"use\s+\S+|switch\s+to|rebrand\w*|renam\w*|see\s+https?://"
    r")\b",
    re.IGNORECASE,
)

FORGE_REPO_URL_RE = re.compile(
    r"https?://(?:www\.)?"
    r"(?:github\.com|codeberg\.org|gitlab\.com|git\.sr\.ht)"
    r"/[~A-Za-z0-9._-]+/[A-Za-z0-9._-]+",
    re.IGNORECASE,
)


def _normalize_homepage(raw: str | None) -> str | None:
    if not raw or not raw.strip():
        return None
    candidate = raw.strip()
    if not candidate.startswith(("http://", "https://")):
        if "." in candidate and " " not in candidate:
            candidate = f"https://{candidate}"
        else:
            return None
    cleaned = clean_url(candidate)
    return cleaned or None


def _clean_forge_candidate(raw_url: str, self_owner: str, self_repo: str) -> str | None:
    """Validate and canonicalize a candidate successor repository URL on GitHub/Codeberg/GitLab/sr.ht."""
    cleaned = raw_url.strip().rstrip(".,;:)>]\"'")
    parts = urlsplit(cleaned)
    host = (parts.hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]

    self_key = f"{self_owner.lower()}/{self_repo.lower()}"

    if host == "github.com":
        gh = extract_github_repo(cleaned)
        if gh is None:
            return None
        cand_key = f"{gh[0].lower()}/{gh[1].lower()}"
        if cand_key == self_key or cand_key in BOILERPLATE_GITHUB_REPOS:
            return None
        return f"https://github.com/{gh[0]}/{gh[1]}"

    if host in {"codeberg.org", "gitlab.com", "git.sr.ht"}:
        segs = [s for s in parts.path.split("/") if s]
        if len(segs) < 2:
            return None
        owner, repo = segs[0], segs[1]
        if repo.endswith(".git"):
            repo = repo[:-4]
        if owner.lower() in {"explore", "users", "orgs", "groups", "topics"}:
            return None
        return f"https://{host}/{owner}/{repo}"

    return None


def find_archived_replacement_url(
    *,
    owner: str,
    repo: str,
    canonical_full_name: str | None,
    description: str,
    homepage_url: str | None,
    readme_text: str | None,
) -> str | None:
    """Inspect repo description, homepageUrl, and README header for a successor repo link."""
    eff_owner, eff_repo = owner, repo
    if canonical_full_name and "/" in canonical_full_name:
        eff_owner, eff_repo = canonical_full_name.split("/", 1)

    # 1. Check repository description first (must mention a replacement/move cue)
    if description and REPLACEMENT_CUE_RE.search(description):
        for raw_match in FORGE_REPO_URL_RE.findall(description):
            cand = _clean_forge_candidate(raw_match, eff_owner, eff_repo)
            if cand and cand.lower() != f"https://github.com/{owner.lower()}/{repo.lower()}":
                return cand

    # 2. Check if homepageUrl itself points directly to a different forge repository
    if homepage_url:
        for raw_match in FORGE_REPO_URL_RE.findall(homepage_url):
            cand = _clean_forge_candidate(raw_match, eff_owner, eff_repo)
            if cand and cand.lower() != f"https://github.com/{owner.lower()}/{repo.lower()}":
                return cand

    # 3. Scan top of README by paragraph/callout block for explicit replacement cues
    if readme_text:
        head = readme_text[:MAX_README_SCAN_CHARS]
        # Strip image/badge markdown so badge URLs never pollute cue matching
        cleaned_lines = [
            ln
            for ln in head.splitlines()
            if "shields.io" not in ln.lower() and not ln.lstrip().startswith("![")
        ]
        paragraphs = re.split(r"\n\s*\n", "\n".join(cleaned_lines))
        for para in paragraphs:
            if not REPLACEMENT_CUE_RE.search(para):
                continue
            for line in para.splitlines():
                if REPLACEMENT_CUE_RE.search(line):
                    for raw_match in FORGE_REPO_URL_RE.findall(line):
                        cand = _clean_forge_candidate(raw_match, eff_owner, eff_repo)
                        if (
                            cand
                            and cand.lower()
                            != f"https://github.com/{owner.lower()}/{repo.lower()}"
                        ):
                            return cand
            for raw_match in FORGE_REPO_URL_RE.findall(para):
                cand = _clean_forge_candidate(raw_match, eff_owner, eff_repo)
                if cand and cand.lower() != f"https://github.com/{owner.lower()}/{repo.lower()}":
                    return cand

    return None


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
    homepage_url: str | None,
    description: str,
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
        # Disabled repositories cannot be accessed on GitHub -> classify as Dead (MISSING)
        return RepoActivityResult(
            owner=owner,
            repo=repo,
            tier=StalenessTier.MISSING,
            last_activity_at=last_activity_at,
            inactive_days=inactive_days,
            is_archived=is_archived,
            is_disabled=True,
            latest_release_tag=latest_release_tag,
            canonical_full_name=canonical_full_name,
            homepage_url=homepage_url,
            description=description,
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
            homepage_url=homepage_url,
            description=description,
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
            homepage_url=homepage_url,
            description=description,
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
        homepage_url=homepage_url,
        description=description,
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
    description
    homepageUrl
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


def _build_graphql_readme_query(batch: list[tuple[str, str, str]]) -> str:
    """Batch-fetch `HEAD:README.md` / `HEAD:readme.md` blobs for archived repositories."""
    fields: list[str] = []
    for idx, (_, owner, repo) in enumerate(batch):
        owner_json = json.dumps(owner)
        repo_json = json.dumps(repo)
        fields.append(
            f"""
  r{idx}: repository(owner: {owner_json}, name: {repo_json}) {{
    rmUpper: object(expression: "HEAD:README.md") {{
      ... on Blob {{ text }}
    }}
    rmLower: object(expression: "HEAD:readme.md") {{
      ... on Blob {{ text }}
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
        check_archived_replacements: bool = True,
        now: datetime | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.thresholds = thresholds or TierThresholds()
        self.github_token = github_token.strip() if github_token else None
        self.proxy_url = proxy_url
        self.concurrency = max(1, concurrency)
        self.timeout = timeout
        self.check_archived_replacements = check_archived_replacements
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
        """Deduplicate `owner/repo` keys, evaluate activity, and detect archived replacements."""
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
            await self._populate_repo_results(client, unique_refs, results)
            if self.check_archived_replacements:
                await self._enrich_archived_replacements(client, results)

        return results

    async def _populate_repo_results(
        self,
        client: httpx.AsyncClient,
        unique_refs: list[GitHubRef],
        results: dict[str, RepoActivityResult],
    ) -> None:
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

    async def _enrich_archived_replacements(
        self,
        client: httpx.AsyncClient,
        results: dict[str, RepoActivityResult],
    ) -> None:
        """Scan archived repos' descriptions, homepages, and README headers for successor links."""
        archived_keys = [
            key for key, rr in results.items() if rr.tier == StalenessTier.ARCHIVED
        ]
        if not archived_keys:
            return

        # First check description + homepageUrl without extra API calls; collect ones needing README
        needs_readme: list[tuple[str, str, str]] = []
        found_urls: dict[str, str] = {}

        for key in archived_keys:
            rr = results[key]
            direct = find_archived_replacement_url(
                owner=rr.owner,
                repo=rr.repo,
                canonical_full_name=rr.canonical_full_name,
                description=rr.description,
                homepage_url=rr.homepage_url,
                readme_text=None,
            )
            if direct:
                found_urls[key] = direct
            else:
                eff_owner, eff_repo = rr.owner, rr.repo
                if rr.canonical_full_name and "/" in rr.canonical_full_name:
                    eff_owner, eff_repo = rr.canonical_full_name.split("/", 1)
                needs_readme.append((key, eff_owner, eff_repo))

        # Batch-fetch READMEs for remaining archived repos
        readmes: dict[str, str] = {}
        if needs_readme and self.github_token:
            for start in range(0, len(needs_readme), 25):
                batch = needs_readme[start : start + 25]
                query = _build_graphql_readme_query(batch)
                try:
                    resp = await client.post(GITHUB_GRAPHQL_URL, json={"query": query})
                    if resp.status_code != 200:
                        continue
                    data = resp.json().get("data") or {}
                except Exception:
                    continue
                for idx, (key, _, _) in enumerate(batch):
                    node = data.get(f"r{idx}")
                    if not isinstance(node, dict):
                        continue
                    blob = node.get("rmUpper") or node.get("rmLower")
                    if isinstance(blob, dict) and isinstance(blob.get("text"), str):
                        readmes[key] = blob["text"]
        elif needs_readme:
            sem = asyncio.Semaphore(self.concurrency)

            async def _fetch_rest_readme(key: str, owner: str, repo: str) -> None:
                async with sem:
                    try:
                        resp = await client.get(
                            f"{GITHUB_REST_BASE}/repos/{owner}/{repo}/readme",
                            headers={
                                **self._headers(),
                                "Accept": "application/vnd.github.raw+json",
                            },
                        )
                        if resp.status_code == 200:
                            readmes[key] = resp.text[:MAX_README_SCAN_CHARS]
                    except Exception:
                        return

            await asyncio.gather(*(_fetch_rest_readme(k, o, r) for k, o, r in needs_readme))

        for key, readme_txt in readmes.items():
            rr = results[key]
            cand = find_archived_replacement_url(
                owner=rr.owner,
                repo=rr.repo,
                canonical_full_name=rr.canonical_full_name,
                description=rr.description,
                homepage_url=rr.homepage_url,
                readme_text=readme_txt,
            )
            if cand:
                found_urls[key] = cand

        if not found_urls:
            return

        # Verify GitHub replacement repositories so we know if the new repo is active!
        gh_successor_refs: list[GitHubRef] = []
        key_to_succ_gh_key: dict[str, str] = {}
        for key, repl_url in found_urls.items():
            gh = extract_github_repo(repl_url)
            if gh is not None:
                sref = GitHubRef(gh[0], gh[1], "explicit_markdown")
                key_to_succ_gh_key[key] = sref.key
                if sref.key not in results:
                    gh_successor_refs.append(sref)

        succ_results: dict[str, RepoActivityResult] = {}
        if gh_successor_refs:
            await self._populate_repo_results(client, gh_successor_refs, succ_results)

        for key, repl_url in found_urls.items():
            rr = results[key]
            succ_gh_key = key_to_succ_gh_key.get(key)
            if succ_gh_key:
                succ_rr = results.get(succ_gh_key) or succ_results.get(succ_gh_key)
                if succ_rr is None:
                    status_str = "GitHub repo"
                elif succ_rr.tier == StalenessTier.MISSING:
                    status_str = "dead / 404"
                elif succ_rr.tier == StalenessTier.ARCHIVED:
                    status_str = "also archived"
                elif succ_rr.inactive_days is not None:
                    state_word = (
                        "active"
                        if succ_rr.tier == StalenessTier.ACTIVE
                        else f"stale ({succ_rr.tier.value})"
                    )
                    status_str = f"{state_word}, last push {succ_rr.inactive_days}d ago"
                else:
                    status_str = succ_rr.tier.value
            else:
                host = (urlsplit(repl_url).hostname or "external").lower()
                status_str = f"moved to {host}"

            results[key] = replace(
                rr,
                replacement_url=repl_url,
                replacement_status=status_str,
            )

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
                homepage_url=_normalize_homepage(node.get("homepageUrl")),
                description=(node.get("description") or "").strip(),
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
            homepage_url=_normalize_homepage(data.get("homepage")),
            description=(data.get("description") or "").strip(),
            thresholds=self.thresholds,
            now=self.now,
        )
