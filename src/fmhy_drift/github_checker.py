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
from difflib import SequenceMatcher
from urllib.parse import urljoin, urlsplit

import httpx

from fmhy_drift.models import (
    GitHubRef,
    RepoActivityResult,
    StalenessTier,
    TierThresholds,
)
from fmhy_drift.parser import clean_url, extract_github_repo
from fmhy_drift.site_checker import BOILERPLATE_GITHUB_REPOS, discover_github_repos_in_html

GITHUB_GRAPHQL_URL = "https://api.github.com/graphql"
GITHUB_REST_BASE = "https://api.github.com"
GRAPHQL_BATCH_SIZE = 50
MAX_README_SCAN_CHARS = 3500
MAX_PAGES_HTML_BYTES = 65_536
MAX_PAGES_REDIRECT_HOPS = 5

SOURCE_PRIORITY: dict[str, int] = {
    "explicit_markdown": 5,
    "github_cdn": 4,
    "discovered_on_site": 3,
    "github_pages": 2,
    "forge_pages": 2,
    "domain_heuristic": 1,
}

NON_GITHUB_FORGE_HOSTS = frozenset(
    {
        "codeberg.org",
        "gitlab.com",
        "git.sr.ht",
        "codefloe.com",
        "gitea.com",
        "framagit.org",
        "git.disroot.org",
        "notabug.org",
    }
)

REPL_IGNORED_LINE_RE = re.compile(
    r"\[\!\[|\!\[|<img\b|shields\.io|badge\.svg|"
    r"\bfork(?:ed)?\s+(?:from|of)\b|"
    r"\bspiritual\s+successor\s+(?:to|of)\b",
    re.IGNORECASE,
)

EXCLUDED_CUE_PREFIX_RE = re.compile(
    r"\b(?:originally|previously|formerly|spiritual)\s+$",
    re.IGNORECASE,
)

REPLACEMENT_CUE_RE = re.compile(
    r"(?<!originally )(?<!previously )(?<!formerly )"
    r"("
    r"\b(?:moved|migrated|relocated)\s+to\b|"
    r"\b(?:superseded|replaced)\s+by\b|"
    r"\b(?:continued|continues|maintained|active\s+development(?:\s+continues)?)\s+(?:at|in|on|under)\b|"
    r"\b(?:(?<!spiritual )successor(?!\s+(?:to|of)\b)|new\s+repository|new\s+repo|new\s+home|new\s+location)\b|"
    r"\bin\s+favor\s+of\b|"
    r"\b(?:deprecat\w*|archived|unmaintained|no\s+longer\s+maintained|discontinued|abandoned)"
    r"[^.\n]{0,90}\b(?:(?:active|maintained)\s+fork|moved\s+to|use|see|check\s+out|switch\s+to)\b"
    r")",
    re.IGNORECASE,
)

FORGE_REPO_URL_RE = re.compile(
    r"https?://(?:www\.)?"
    r"(?:github\.com|codeberg\.org|gitlab\.com|git\.sr\.ht|codefloe\.com|gitea\.com|framagit\.org|git\.disroot\.org|notabug\.org)"
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


def _repo_stem(name: str) -> str:
    base = name.lower()
    if base.endswith(".git"):
        base = base[:-4]
    base = re.sub(r"[-_.]?(?:ng|v\d+|next|reborn|continued|revived)$", "", base)
    return re.sub(r"[^a-z0-9]+", "", base)


def _find_cue_matches(text: str) -> list[re.Match[str]]:
    return [
        m
        for m in REPLACEMENT_CUE_RE.finditer(text)
        if not EXCLUDED_CUE_PREFIX_RE.search(text[: m.start()])
    ]


def _clean_forge_candidate(raw_url: str, self_owner: str, self_repo: str) -> str | None:
    """Validate and canonicalize a candidate successor repository URL across supported git forges."""
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

    if host in NON_GITHUB_FORGE_HOSTS:
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


def _extract_valid_candidate(
    text: str,
    *,
    owner: str,
    repo: str,
    eff_owner: str,
    eff_repo: str,
) -> str | None:
    orig_url = f"https://github.com/{owner.lower()}/{repo.lower()}"
    for raw_match in FORGE_REPO_URL_RE.findall(text):
        cand = _clean_forge_candidate(raw_match, eff_owner, eff_repo)
        if cand and cand.lower() != orig_url:
            return cand
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
    """Inspect repo description, README header, and homepageUrl for a successor repo link."""
    eff_owner, eff_repo = owner, repo
    if canonical_full_name and "/" in canonical_full_name:
        eff_owner, eff_repo = canonical_full_name.split("/", 1)

    # 1. Check repository description first (must mention a replacement/move cue)
    if description:
        for desc_line in description.splitlines():
            if REPL_IGNORED_LINE_RE.search(desc_line):
                continue
            cue_matches = _find_cue_matches(desc_line)
            if not cue_matches:
                continue
            cue_start = cue_matches[0].start()
            cand = _extract_valid_candidate(
                desc_line[cue_start:],
                owner=owner,
                repo=repo,
                eff_owner=eff_owner,
                eff_repo=eff_repo,
            ) or _extract_valid_candidate(
                desc_line[:cue_start],
                owner=owner,
                repo=repo,
                eff_owner=eff_owner,
                eff_repo=eff_repo,
            )
            if cand:
                return cand

    # 2. Scan top of README by paragraph/callout block for explicit replacement cues
    if readme_text:
        head = readme_text[:MAX_README_SCAN_CHARS]
        cleaned_lines = [
            ln for ln in head.splitlines() if not REPL_IGNORED_LINE_RE.search(ln)
        ]
        paragraphs = re.split(r"\n\s*\n", "\n".join(cleaned_lines))
        for para in paragraphs:
            if not _find_cue_matches(para):
                continue
            para_lines = para.splitlines()
            for idx, line in enumerate(para_lines):
                cue_matches = _find_cue_matches(line)
                if not cue_matches:
                    continue
                cue_start = cue_matches[0].start()
                # Prefer URLs after cue_match.start() on the cue line, then rest of cue line,
                # then immediately following line in the same paragraph (never earlier lines!).
                cand = (
                    _extract_valid_candidate(
                        line[cue_start:],
                        owner=owner,
                        repo=repo,
                        eff_owner=eff_owner,
                        eff_repo=eff_repo,
                    )
                    or _extract_valid_candidate(
                        line[:cue_start],
                        owner=owner,
                        repo=repo,
                        eff_owner=eff_owner,
                        eff_repo=eff_repo,
                    )
                    or (
                        _extract_valid_candidate(
                            para_lines[idx + 1],
                            owner=owner,
                            repo=repo,
                            eff_owner=eff_owner,
                            eff_repo=eff_repo,
                        )
                        if idx + 1 < len(para_lines)
                        else None
                    )
                )
                if cand:
                    return cand

    # 3. Check if homepageUrl itself points directly to a different forge repository
    if homepage_url:
        orig_url = f"https://github.com/{owner.lower()}/{repo.lower()}"
        self_stems = {s for s in (_repo_stem(eff_repo), _repo_stem(repo)) if s}
        self_owners = {eff_owner.lower(), owner.lower()}
        for raw_match in FORGE_REPO_URL_RE.findall(homepage_url):
            cand = _clean_forge_candidate(raw_match, eff_owner, eff_repo)
            if not cand or cand.lower() == orig_url:
                continue
            gh = extract_github_repo(cand)
            if gh is not None:
                # On github.com, only accept homepage_url without a cue if repo stem matches under a different owner
                if gh[0].lower() in self_owners or _repo_stem(gh[1]) not in self_stems:
                    continue
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
    archived_at: datetime | None = None,
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

    archived_days: int | None = None
    if archived_at is not None:
        archived_days = max(0, int((now - archived_at).total_seconds() // 86400))

    rename_note = ""
    if canonical_full_name and canonical_full_name.lower() != f"{owner}/{repo}".lower():
        rename_note = f" (resolved -> {canonical_full_name})"

    if is_disabled:
        return RepoActivityResult(
            owner=owner,
            repo=repo,
            tier=StalenessTier.MISSING,
            last_activity_at=last_activity_at,
            inactive_days=inactive_days,
            archived_at=archived_at,
            archived_days=archived_days,
            is_archived=is_archived,
            is_disabled=True,
            latest_release_tag=latest_release_tag,
            canonical_full_name=canonical_full_name,
            homepage_url=homepage_url,
            description=description,
            reason=f"Repository is disabled on GitHub{rename_note}",
        )

    if is_archived:
        parts_txt: list[str] = []
        if archived_at is not None and archived_days is not None:
            parts_txt.append(f"archived on {archived_at.strftime('%Y-%m-%d')} ({archived_days}d ago)")
        else:
            parts_txt.append("archived")
        if inactive_days is not None:
            parts_txt.append(f"last push {inactive_days}d ago")
        summary_txt = ", ".join(parts_txt)
        return RepoActivityResult(
            owner=owner,
            repo=repo,
            tier=StalenessTier.ARCHIVED,
            last_activity_at=last_activity_at,
            inactive_days=inactive_days,
            archived_at=archived_at,
            archived_days=archived_days,
            is_archived=True,
            is_disabled=False,
            latest_release_tag=latest_release_tag,
            canonical_full_name=canonical_full_name,
            homepage_url=homepage_url,
            description=description,
            reason=f"Repository {summary_txt}{rename_note}",
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
        owner_json = json.dumps(ref.owner, ensure_ascii=False)
        repo_json = json.dumps(ref.repo, ensure_ascii=False)
        fields.append(
            f"""
  r{idx}: repository(owner: {owner_json}, name: {repo_json}) {{
    nameWithOwner
    description
    homepageUrl
    isArchived
    archivedAt
    isDisabled
    isEmpty
    pushedAt
    defaultBranchRef {{
      target {{
        ... on Commit {{
          oid
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
    """Batch-fetch `HEAD:README.md` / `HEAD:readme.md` / `HEAD:Readme.md` / `HEAD:README.rst` blobs."""
    fields: list[str] = []
    for idx, (_, owner, repo) in enumerate(batch):
        owner_json = json.dumps(owner, ensure_ascii=False)
        repo_json = json.dumps(repo, ensure_ascii=False)
        fields.append(
            f"""
  r{idx}: repository(owner: {owner_json}, name: {repo_json}) {{
    rmUpper: object(expression: "HEAD:README.md") {{
      ... on Blob {{ text }}
    }}
    rmLower: object(expression: "HEAD:readme.md") {{
      ... on Blob {{ text }}
    }}
    rmTitle: object(expression: "HEAD:Readme.md") {{
      ... on Blob {{ text }}
    }}
    rmRst: object(expression: "HEAD:README.rst") {{
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
        self._owner_repos_cache: dict[str, list[dict]] = {}
        self._owner_repos_locks: dict[str, asyncio.Lock] = {}
        self._resolved_target_cache: dict[tuple[str, str, str], RepoActivityResult | None] = {}
        self._resolved_target_locks: dict[tuple[str, str, str], asyncio.Lock] = {}
        self._pages_client: httpx.AsyncClient | None = None

    def _get_pages_client(self) -> httpx.AsyncClient:
        if self._pages_client is None:
            pg_kwargs: dict = {
                "timeout": httpx.Timeout(8.0),
                "follow_redirects": False,
                "trust_env": False,
                "headers": {"User-Agent": "fmhy-drift-checker/0.1"},
            }
            if self.proxy_url:
                pg_kwargs["proxy"] = self.proxy_url
            self._pages_client = httpx.AsyncClient(**pg_kwargs)
        return self._pages_client

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
        """Deduplicate `owner/repo` keys by source priority, evaluate activity, and detect replacements."""
        unique_by_key: dict[str, GitHubRef] = {}
        for ref in refs:
            existing = unique_by_key.get(ref.key)
            if existing is None or SOURCE_PRIORITY.get(ref.source, 0) > SOURCE_PRIORITY.get(
                existing.source, 0
            ):
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
        domain_heuristic_keys = {
            k for k, r in unique_by_key.items() if r.source == "domain_heuristic"
        }

        try:
            async with httpx.AsyncClient(**client_kwargs) as client:
                await self._populate_repo_results(client, unique_refs, results)
                if self.check_archived_replacements:
                    await self._enrich_archived_replacements(client, results)
                    await self._enrich_author_similar_repos(
                        client,
                        results,
                        exclude_keys=domain_heuristic_keys,
                    )
                try:
                    await asyncio.wait_for(
                        self._enrich_missing_last_seen_wayback(
                            client,
                            results,
                            exclude_keys=domain_heuristic_keys,
                        ),
                        timeout=25.0,
                    )
                except TimeoutError:
                    pass
        finally:
            if self._pages_client is not None:
                await self._pages_client.aclose()
                self._pages_client = None

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
                    blob = (
                        node.get("rmUpper")
                        or node.get("rmLower")
                        or node.get("rmTitle")
                        or node.get("rmRst")
                    )
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

        # Verify GitHub replacement repositories (deduplicated by sref.key) so we know if active!
        gh_successor_refs_by_key: dict[str, GitHubRef] = {}
        key_to_succ_gh_key: dict[str, str] = {}
        for key, repl_url in found_urls.items():
            gh = extract_github_repo(repl_url)
            if gh is not None:
                sref = GitHubRef(gh[0], gh[1], "explicit_markdown")
                key_to_succ_gh_key[key] = sref.key
                if sref.key not in results and sref.key not in gh_successor_refs_by_key:
                    gh_successor_refs_by_key[sref.key] = sref

        gh_successor_refs = list(gh_successor_refs_by_key.values())
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
                else:
                    self_names = {
                        rr.full_name.lower(),
                        f"{rr.owner.lower()}/{rr.repo.lower()}",
                    }
                    if (
                        succ_rr.tier in {StalenessTier.MISSING, StalenessTier.ARCHIVED}
                        or succ_rr.full_name.lower() in self_names
                    ):
                        continue
                    if succ_rr.inactive_days is not None:
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
        payload: dict | None = None
        for attempt in range(2):
            try:
                resp = await client.post(GITHUB_GRAPHQL_URL, json={"query": query})
                if resp.status_code in {502, 503, 504} and attempt == 0:
                    await asyncio.sleep(0.05 if self._transport is not None else 1.0)
                    continue
                if resp.status_code != 200:
                    break
                payload = resp.json()
                break
            except Exception:
                break

        data = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(data, dict):
            return [r for r in batch if r.source != "domain_heuristic"]

        unresolved: list[GitHubRef] = []
        for idx, ref in enumerate(batch):
            node = data.get(f"r{idx}")
            if not isinstance(node, dict):
                # Speculative custom-domain guesses (`abztract.arhamkhnz.com` -> `arhamkhnz/abztract`)
                # that don't exist in GraphQL are simply absent, not broken explicit links.
                if ref.source == "domain_heuristic":
                    continue
                unresolved.append(ref)
                continue

            pushed_at = _parse_iso_datetime(node.get("pushedAt"))
            commit_date: datetime | None = None
            commit_oid: str | None = None
            default_branch = node.get("defaultBranchRef")
            if isinstance(default_branch, dict):
                target = default_branch.get("target")
                if isinstance(target, dict):
                    commit_date = _parse_iso_datetime(target.get("committedDate"))
                    if isinstance(target.get("oid"), str):
                        commit_oid = target["oid"]
                    if commit_date is not None and commit_date > self.now:
                        commit_date = pushed_at

            release_date: datetime | None = None
            release_tag: str | None = None
            releases = node.get("releases")
            if isinstance(releases, dict):
                nodes = releases.get("nodes")
                if isinstance(nodes, list) and nodes and isinstance(nodes[0], dict):
                    release_date = _parse_iso_datetime(nodes[0].get("publishedAt"))
                    release_tag = nodes[0].get("tagName")

            if bool(node.get("isEmpty")):
                latest_ts = None
            else:
                latest_ts = _latest_activity([pushed_at, commit_date, release_date])

            res_obj = _evaluate_repo_state(
                owner=ref.owner,
                repo=ref.repo,
                canonical_full_name=node.get("nameWithOwner"),
                is_archived=bool(node.get("isArchived")),
                is_disabled=bool(node.get("isDisabled")),
                archived_at=_parse_iso_datetime(node.get("archivedAt")),
                last_activity_at=latest_ts,
                latest_release_tag=release_tag,
                homepage_url=_normalize_homepage(node.get("homepageUrl")),
                description=(node.get("description") or "").strip(),
                thresholds=self.thresholds,
                now=self.now,
            )
            if commit_oid:
                res_obj = replace(res_obj, head_commit_oid=commit_oid)
            results[ref.key] = res_obj

        return unresolved

    async def _check_rest_single(
        self,
        client: httpx.AsyncClient,
        ref: GitHubRef,
        *,
        allow_pages_fallback: bool = True,
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
            if ref.source == "domain_heuristic":
                return RepoActivityResult(
                    owner=ref.owner,
                    repo=ref.repo,
                    tier=StalenessTier.MISSING,
                    reason="Speculative domain-heuristic repository not found (HTTP 404)",
                )
            if allow_pages_fallback and ref.source == "github_pages":
                resolved = await self._resolve_github_pages_repo(client, ref)
                if resolved is not None:
                    return resolved

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
        if pushed_at is None and data.get("size") == 0:
            latest_ts = None
        else:
            latest_ts = _latest_activity([pushed_at, created_at])

        return _evaluate_repo_state(
            owner=ref.owner,
            repo=ref.repo,
            canonical_full_name=data.get("full_name"),
            is_archived=bool(data.get("archived")),
            is_disabled=bool(data.get("disabled")),
            archived_at=None,
            last_activity_at=latest_ts,
            latest_release_tag=None,
            homepage_url=_normalize_homepage(data.get("homepage")),
            description=(data.get("description") or "").strip(),
            thresholds=self.thresholds,
            now=self.now,
        )

    async def _get_owner_repos(self, client: httpx.AsyncClient, owner: str) -> list[dict]:
        """Fetch and cache up to 100 public repositories for `owner`."""
        key = owner.lower()
        lock = self._owner_repos_locks.setdefault(key, asyncio.Lock())
        async with lock:
            if key in self._owner_repos_cache:
                return self._owner_repos_cache[key]
            try:
                resp = await client.get(
                    f"{GITHUB_REST_BASE}/users/{owner}/repos",
                    params={"per_page": "100", "type": "owner"},
                )
                if resp.status_code == 200 and isinstance(resp.json(), list):
                    self._owner_repos_cache[key] = resp.json()
                    return self._owner_repos_cache[key]
            except Exception:
                pass
            self._owner_repos_cache[key] = []
            return []

    async def _evaluate_resolved_target(
        self,
        client: httpx.AsyncClient,
        orig_ref: GitHubRef,
        target_owner: str,
        target_repo: str,
        *,
        subpath: str | None = None,
    ) -> RepoActivityResult | None:
        """Evaluate and cache a resolved `(target_owner, target_repo)` via GraphQL or REST."""
        cache_key = (target_owner.lower(), target_repo.lower(), subpath or "")
        lock = self._resolved_target_locks.setdefault(cache_key, asyncio.Lock())
        async with lock:
            if cache_key not in self._resolved_target_cache:
                self._resolved_target_cache[cache_key] = await self._fetch_resolved_target_uncached(
                    client,
                    target_owner,
                    target_repo,
                    subpath=subpath,
                )
            cached = self._resolved_target_cache[cache_key]

        if cached is None:
            return None

        canon = cached.full_name
        base_reason = re.sub(r" \(resolved -> [^)]+\)$", "", cached.reason)
        adj_reason = (
            f"{base_reason} (resolved -> {canon})"
            if canon.lower() != orig_ref.key
            else base_reason
        )
        return replace(
            cached,
            owner=orig_ref.owner,
            repo=orig_ref.repo,
            canonical_full_name=canon,
            reason=adj_reason,
        )

    async def _fetch_resolved_target_uncached(
        self,
        client: httpx.AsyncClient,
        target_owner: str,
        target_repo: str,
        *,
        subpath: str | None = None,
    ) -> RepoActivityResult | None:
        target_ref = GitHubRef(target_owner, target_repo, "explicit_markdown")
        if self.github_token:
            if subpath:
                owner_json = json.dumps(target_owner, ensure_ascii=False)
                repo_json = json.dumps(target_repo, ensure_ascii=False)
                subpath_json = json.dumps(subpath, ensure_ascii=False)
                query = f"""query {{
  r0: repository(owner: {owner_json}, name: {repo_json}) {{
    nameWithOwner
    description
    homepageUrl
    isArchived
    archivedAt
    isDisabled
    isEmpty
    pushedAt
    defaultBranchRef {{
      target {{
        ... on Commit {{
          committedDate
          subHistory: history(first: 1, path: {subpath_json}) {{
            nodes {{ committedDate }}
          }}
        }}
      }}
    }}
  }}
}}"""
                try:
                    resp = await client.post(GITHUB_GRAPHQL_URL, json={"query": query})
                    node = (
                        ((resp.json().get("data") or {}).get("r0"))
                        if resp.status_code == 200
                        else None
                    )
                    if isinstance(node, dict):
                        pushed_at = _parse_iso_datetime(node.get("pushedAt"))
                        sub_commit: datetime | None = None
                        dref = node.get("defaultBranchRef")
                        if isinstance(dref, dict) and isinstance(dref.get("target"), dict):
                            nodes = ((dref["target"].get("subHistory") or {}).get("nodes")) or []
                            if nodes and isinstance(nodes[0], dict):
                                sub_commit = _parse_iso_datetime(nodes[0].get("committedDate"))
                                if sub_commit is not None and sub_commit > self.now:
                                    sub_commit = pushed_at
                        effective_ts = None if bool(node.get("isEmpty")) else (sub_commit or pushed_at)
                        return _evaluate_repo_state(
                            owner=target_owner,
                            repo=target_repo,
                            canonical_full_name=node.get("nameWithOwner"),
                            is_archived=bool(node.get("isArchived")),
                            is_disabled=bool(node.get("isDisabled")),
                            archived_at=_parse_iso_datetime(node.get("archivedAt")),
                            last_activity_at=effective_ts,
                            latest_release_tag=None,
                            homepage_url=_normalize_homepage(node.get("homepageUrl")),
                            description=(node.get("description") or "").strip(),
                            thresholds=self.thresholds,
                            now=self.now,
                        )
                except Exception:
                    pass
            else:
                gql_map: dict[str, RepoActivityResult] = {}
                await self._check_graphql_batch(client, [target_ref], gql_map)
                if target_ref.key in gql_map:
                    gql_res = gql_map[target_ref.key]
                    return replace(gql_res, canonical_full_name=gql_res.full_name)

        rest_res = await self._check_rest_single(
            client,
            target_ref,
            allow_pages_fallback=False,
        )
        if rest_res.tier not in {StalenessTier.MISSING, StalenessTier.ERROR}:
            return replace(rest_res, canonical_full_name=rest_res.full_name)
        return None

    async def _fetch_github_pages_safely(
        self,
        client: httpx.AsyncClient,
        pages_url: str,
    ) -> tuple[int | None, str, str, bool]:
        """Fetch `*.github.io` with manual redirect validation (never leaving `*.github.io`!) and 64KB stream cap.

        Returns `(final_status, body_text, final_url, left_github_io)`.
        """
        active_client = client if self._transport is not None else self._get_pages_client()
        current_url = pages_url

        for hop in range(MAX_PAGES_REDIRECT_HOPS + 1):
            async with active_client.stream(
                "GET",
                current_url,
                follow_redirects=False,
            ) as pg_resp:
                status = pg_resp.status_code
                if status in {301, 302, 307, 308}:
                    loc = pg_resp.headers.get("location", "").strip()
                    if not loc or hop >= MAX_PAGES_REDIRECT_HOPS:
                        return (status, "", current_url, True)
                    next_url = urljoin(current_url, loc)
                    next_parts = urlsplit(next_url)
                    next_host = (next_parts.hostname or "").lower()
                    if next_parts.scheme.lower() not in {"http", "https"} or not next_host.endswith(
                        ".github.io"
                    ):
                        # Redirect leaves `*.github.io` -> treat Pages site as alive and NEVER fetch external host!
                        return (status, "", current_url, True)
                    current_url = next_url
                    continue

                if 200 <= status < 300:
                    chunks: list[bytes] = []
                    total = 0
                    async for chunk in pg_resp.aiter_bytes():
                        take = chunk[: MAX_PAGES_HTML_BYTES - total]
                        if take:
                            chunks.append(take)
                            total += len(take)
                        if total >= MAX_PAGES_HTML_BYTES:
                            break
                    body_text = b"".join(chunks).decode(
                        pg_resp.encoding or "utf-8",
                        errors="replace",
                    )
                    return (status, body_text, str(pg_resp.url), False)

                return (status, "", str(pg_resp.url), False)

        return (None, "", current_url, True)

    async def _resolve_github_pages_repo(
        self,
        client: httpx.AsyncClient,
        ref: GitHubRef,
    ) -> RepoActivityResult | None:
        """Resolve a `*.github.io` URL when `<owner>/<seg0>` isn't a direct repository."""
        pages_url = ref.discovered_from_url
        pages_alive: bool | None = None
        norm_pages = (pages_url or "").rstrip("/").lower()
        norm_title = re.sub(r"[^a-z0-9]+", " ", (ref.entry_title or "").lower()).strip()
        compact_title = norm_title.replace(" ", "")
        compact_ref_repo = re.sub(r"[^a-z0-9]+", "", ref.repo.lower())
        root_io = f"{ref.owner.lower()}.github.io"
        root_com = f"{ref.owner.lower()}.github.com"

        # Step 1: Check `*.github.io` directly without ever leaving `*.github.io`
        if (
            pages_url
            and urlsplit(pages_url).hostname
            and urlsplit(pages_url).hostname.lower().endswith(".github.io")
        ):
            try:
                status, body_text, final_pages_url, left_gh_io = await self._fetch_github_pages_safely(
                    client,
                    pages_url,
                )
                if left_gh_io:
                    pages_alive = True
                elif status in {404, 410}:
                    return RepoActivityResult(
                        owner=ref.owner,
                        repo=ref.repo,
                        tier=StalenessTier.MISSING,
                        reason=f"GitHub Pages site returns HTTP {status} ({pages_url})",
                    )
                elif status is not None and 200 <= status < 400:
                    pages_alive = True
                    discovered = discover_github_repos_in_html(body_text, final_pages_url)
                    checked_cands = 0
                    for cand in discovered:
                        if cand.key == ref.key:
                            continue
                        if checked_cands >= 5:
                            break
                        checked_cands += 1
                        same_owner_ok = cand.owner.lower() == ref.owner.lower() and (
                            ref.repo.lower() != root_io
                            or cand.repo.lower() in {root_com, ref.owner.lower()}
                        )
                        compact_cand_repo = re.sub(r"[^a-z0-9]+", "", cand.repo.lower())
                        name_matches = bool(compact_cand_repo) and (
                            (ref.repo.lower() != root_io and compact_cand_repo == compact_ref_repo)
                            or (len(compact_title) >= 3 and compact_cand_repo == compact_title)
                        )
                        resolved = await self._evaluate_resolved_target(
                            client,
                            ref,
                            cand.owner,
                            cand.repo,
                        )
                        if resolved is None:
                            continue
                        hp_norm = (resolved.homepage_url or "").rstrip("/").lower()
                        hp_matches = bool(hp_norm and norm_pages) and norm_pages.startswith(hp_norm)
                        if same_owner_ok or hp_matches or name_matches:
                            return resolved
            except Exception:
                pass

        # Step 2: Match `<owner>`'s public repositories by `homepage` or FMHY `entry_title`
        owner_repos = await self._get_owner_repos(client, ref.owner)

        if owner_repos:
            # 2a. Exact `homepage` URL match
            if norm_pages:
                for rdata in owner_repos:
                    hp = (_normalize_homepage(rdata.get("homepage")) or "").rstrip("/").lower()
                    if hp and (hp == norm_pages or norm_pages.startswith(hp + "/")):
                        resolved = await self._evaluate_resolved_target(
                            client, ref, ref.owner, rdata["name"]
                        )
                        if resolved is not None:
                            return resolved

            # 2b. Repo `description` or `name` matches FMHY entry title (e.g. "Epidemic Calculator" -> `gabgoh/epcalc`)
            if norm_title and len(norm_title) >= 4:
                for rdata in owner_repos:
                    rname = str(rdata.get("name") or "")
                    if rname.lower() in {root_io, root_com}:
                        continue
                    rdesc = re.sub(r"[^a-z0-9]+", " ", str(rdata.get("description") or "").lower()).strip()
                    rname_compact = re.sub(r"[^a-z0-9]+", "", rname.lower())
                    if (rdesc and rdesc == norm_title) or (rname_compact == compact_title):
                        resolved = await self._evaluate_resolved_target(
                            client, ref, ref.owner, rname
                        )
                        if resolved is not None:
                            return resolved

        # Step 3: Check `<owner>/<owner>.github.io` and legacy pre-2013 `<owner>/<owner>.github.com`
        root_pages_repo = f"{ref.owner}.github.io"
        if ref.repo.lower() != root_io:
            resolved_root = await self._evaluate_resolved_target(
                client,
                ref,
                ref.owner,
                root_pages_repo,
                subpath=ref.repo,
            )
            if resolved_root is not None:
                return resolved_root

        legacy_pages_repo = f"{ref.owner}.github.com"
        if ref.repo.lower() != root_com:
            legacy_subpath = None if ref.repo.lower() == root_io else ref.repo
            resolved_legacy = await self._evaluate_resolved_target(
                client,
                ref,
                ref.owner,
                legacy_pages_repo,
                subpath=legacy_subpath,
            )
            if resolved_legacy is not None:
                return resolved_legacy

        # Step 4: If the `*.github.io` URL itself returned 200 OK (or redirected externally), backing repo is private/external
        if pages_alive is True:
            return RepoActivityResult(
                owner=ref.owner,
                repo=ref.repo,
                tier=StalenessTier.ACTIVE,
                reason="GitHub Pages site is live (backed by private or external repo)",
            )

        return None

    async def _enrich_missing_last_seen_wayback(
        self,
        client: httpx.AsyncClient,
        results: dict[str, RepoActivityResult],
        *,
        exclude_keys: set[str] | None = None,
        wb_transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        """For 404/451 dead repos, check Wayback Machine CDX for when the repo was last seen alive."""
        if self._transport is not None and wb_transport is None:
            # Skip external archive.org calls under unit-test MockTransports tailored to GitHub API
            return

        excluded = exclude_keys or set()
        missing_keys = [
            k
            for k, r in results.items()
            if k not in excluded
            and r.tier == StalenessTier.MISSING
            and r.inactive_days is None
            and "domain-heuristic" not in r.reason
        ]
        if not missing_keys:
            return

        sem = asyncio.Semaphore(6)
        consecutive_failures = 0
        circuit_tripped = False
        wb_kwargs: dict = {
            "timeout": httpx.Timeout(6.0),
            "follow_redirects": True,
            "trust_env": False,
            "headers": {"User-Agent": "fmhy-drift-checker/0.1"},
        }
        if wb_transport is not None:
            wb_kwargs["transport"] = wb_transport

        async with httpx.AsyncClient(**wb_kwargs) as wb_client:

            async def _lookup(key: str) -> None:
                nonlocal consecutive_failures, circuit_tripped
                if circuit_tripped:
                    return
                rr = results[key]
                async with sem:
                    if circuit_tripped:
                        return
                    try:
                        resp = await wb_client.get(
                            "https://web.archive.org/cdx/search/cdx",
                            params={
                                "url": f"https://github.com/{rr.owner}/{rr.repo}",
                                "output": "json",
                                "fl": "timestamp,statuscode",
                                "filter": "statuscode:200",
                                "limit": "-1",
                            },
                        )
                        if resp.status_code in {429, 503}:
                            circuit_tripped = True
                            return
                        if resp.status_code != 200:
                            consecutive_failures += 1
                            if consecutive_failures >= 4:
                                circuit_tripped = True
                            return
                        consecutive_failures = 0
                        rows = resp.json()
                        if not isinstance(rows, list) or len(rows) < 2:
                            return
                        ts_raw = str(rows[-1][0])
                        if len(ts_raw) >= 8:
                            dt = datetime.strptime(ts_raw[:8], "%Y%m%d").replace(tzinfo=UTC)
                            days = max(0, int((self.now - dt).total_seconds() // 86400))
                            results[key] = replace(
                                rr,
                                last_activity_at=dt,
                                inactive_days=days,
                                reason=f"{rr.reason} — last archived alive {dt.strftime('%Y-%m-%d')}",
                            )
                    except Exception:
                        consecutive_failures += 1
                        if consecutive_failures >= 4:
                            circuit_tripped = True
                        return

            try:
                await asyncio.wait_for(
                    asyncio.gather(*(_lookup(k) for k in missing_keys)),
                    timeout=25.0,
                )
            except TimeoutError:
                pass

    async def enrich_dead_and_archived_author_repos(
        self,
        results: dict[str, RepoActivityResult],
        *,
        exclude_keys: set[str] | None = None,
    ) -> dict[str, RepoActivityResult]:
        """Standalone helper to enrich dead (`404`) and archived GitHub repos with same/similar repos the author works in."""
        updated = dict(results)
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

        async with httpx.AsyncClient(**client_kwargs) as client:
            await self._enrich_author_similar_repos(
                client,
                updated,
                exclude_keys=exclude_keys,
            )
        return updated

    async def _enrich_author_similar_repos(
        self,
        client: httpx.AsyncClient,
        results: dict[str, RepoActivityResult],
        *,
        exclude_keys: set[str] | None = None,
    ) -> None:
        """For dead (`404`) or archived (no replacement) GitHub repos, check the owner's owned & contributed-to repos for same/similar names."""
        excluded = exclude_keys or set()
        candidate_keys = [
            k
            for k, rr in results.items()
            if rr.forge == "github"
            and k not in excluded
            and not rr.replacement_url
            and rr.tier in {StalenessTier.MISSING, StalenessTier.ARCHIVED}
            and "domain-heuristic" not in rr.reason
        ]
        if not candidate_keys:
            return

        owner_to_keys: dict[str, list[str]] = {}
        owner_display: dict[str, str] = {}
        for k in candidate_keys:
            rr = results[k]
            eff_owner = (
                rr.canonical_full_name.split("/", 1)[0]
                if (rr.canonical_full_name and "/" in rr.canonical_full_name)
                else rr.owner
            )
            low_o = eff_owner.lower()
            owner_to_keys.setdefault(low_o, []).append(k)
            owner_display.setdefault(low_o, eff_owner)

        owner_candidates: dict[str, list[dict]] = {lo: [] for lo in owner_to_keys}
        unique_low_owners = list(owner_to_keys.keys())

        if self.github_token:
            for start in range(0, len(unique_low_owners), 15):
                batch = unique_low_owners[start : start + 15]
                q_parts: list[str] = []
                for idx, lo in enumerate(batch):
                    login_json = json.dumps(owner_display[lo], ensure_ascii=False)
                    q_parts.append(
                        f"""
  o{idx}: repositoryOwner(login: {login_json}) {{
    repositories(first: 60, ownerAffiliations: [OWNER, COLLABORATOR, ORGANIZATION_MEMBER], isArchived: false, orderBy: {{field: PUSHED_AT, direction: DESC}}) {{
      nodes {{ nameWithOwner name isArchived isDisabled isEmpty pushedAt }}
    }}
    ... on User {{
      repositoriesContributedTo(first: 50, includeUserRepositories: true, contributionTypes: [COMMIT, PULL_REQUEST, REPOSITORY], orderBy: {{field: PUSHED_AT, direction: DESC}}) {{
        nodes {{ nameWithOwner name isArchived isDisabled isEmpty pushedAt }}
      }}
    }}
  }}"""
                    )
                query = "query {" + "".join(q_parts) + "\n}"
                try:
                    resp = await client.post(GITHUB_GRAPHQL_URL, json={"query": query})
                    if resp.status_code != 200:
                        continue
                    data = (resp.json() or {}).get("data") or {}
                except Exception:
                    continue

                for idx, lo in enumerate(batch):
                    node = data.get(f"o{idx}")
                    if not isinstance(node, dict):
                        continue
                    seen_nwo: set[str] = set()
                    merged_nodes: list[dict] = []
                    for field_name in ("repositories", "repositoriesContributedTo"):
                        conn = node.get(field_name)
                        nodes = conn.get("nodes") if isinstance(conn, dict) else None
                        if not isinstance(nodes, list):
                            continue
                        for item in nodes:
                            if not isinstance(item, dict):
                                continue
                            nwo = str(item.get("nameWithOwner") or "")
                            if not nwo or nwo.lower() in seen_nwo:
                                continue
                            seen_nwo.add(nwo.lower())
                            merged_nodes.append(item)
                    owner_candidates[lo] = merged_nodes
        else:
            sem = asyncio.Semaphore(min(8, self.concurrency))

            async def _fetch_rest_owner(lo: str) -> None:
                disp = owner_display[lo]
                seen_nwo: set[str] = set()
                merged: list[dict] = []
                async with sem:
                    try:
                        r1 = await client.get(
                            f"{GITHUB_REST_BASE}/users/{disp}/repos",
                            params={"per_page": "100", "type": "all", "sort": "pushed"},
                        )
                        if r1.status_code == 200 and isinstance(r1.json(), list):
                            for item in r1.json():
                                if not isinstance(item, dict):
                                    continue
                                nwo = str(item.get("full_name") or "")
                                if not nwo or nwo.lower() in seen_nwo:
                                    continue
                                seen_nwo.add(nwo.lower())
                                merged.append(
                                    {
                                        "nameWithOwner": nwo,
                                        "name": item.get("name") or nwo.split("/")[-1],
                                        "isArchived": bool(item.get("archived")),
                                        "isDisabled": bool(item.get("disabled")),
                                        "isEmpty": bool(
                                            item.get("size") == 0 and not item.get("pushed_at")
                                        ),
                                        "pushedAt": item.get("pushed_at"),
                                    }
                                )
                        # Also inspect public contribution events (`PushEvent` / `PullRequestEvent`) for external org repos
                        target_names = [results[k].repo for k in owner_to_keys[lo]]
                        ev_resp = await client.get(
                            f"{GITHUB_REST_BASE}/users/{disp}/events/public",
                            params={"per_page": "40"},
                        )
                        if ev_resp.status_code == 200 and isinstance(ev_resp.json(), list):
                            ext_slugs: list[str] = []
                            for ev in ev_resp.json():
                                if not isinstance(ev, dict):
                                    continue
                                if ev.get("type") not in {
                                    "PushEvent",
                                    "PullRequestEvent",
                                    "CreateEvent",
                                }:
                                    continue
                                r_obj = ev.get("repo")
                                slug = str(r_obj.get("name") or "") if isinstance(r_obj, dict) else ""
                                if "/" not in slug or slug.lower() in seen_nwo:
                                    continue
                                cand_rname = slug.split("/", 1)[1]
                                if any(
                                    _score_similar_repo_name(tname, cand_rname, same_owner=False) > 0
                                    for tname in target_names
                                ):
                                    seen_nwo.add(slug.lower())
                                    ext_slugs.append(slug)
                            for slug in ext_slugs[:5]:
                                rr_ext = await client.get(f"{GITHUB_REST_BASE}/repos/{slug}")
                                if rr_ext.status_code == 200 and isinstance(rr_ext.json(), dict):
                                    d = rr_ext.json()
                                    merged.append(
                                        {
                                            "nameWithOwner": d.get("full_name") or slug,
                                            "name": d.get("name") or slug.split("/")[-1],
                                            "isArchived": bool(d.get("archived")),
                                            "isDisabled": bool(d.get("disabled")),
                                            "isEmpty": bool(
                                                d.get("size") == 0 and not d.get("pushed_at")
                                            ),
                                            "pushedAt": d.get("pushed_at"),
                                        }
                                    )
                    except Exception:
                        pass
                owner_candidates[lo] = merged

            await asyncio.gather(*(_fetch_rest_owner(lo) for lo in unique_low_owners))

        for lo, keys in owner_to_keys.items():
            cands = owner_candidates.get(lo) or []
            if not cands:
                continue
            for k in keys:
                rr = results[k]
                best = _pick_best_similar_author_repo(
                    rr=rr,
                    eff_owner=owner_display[lo],
                    candidates=cands,
                    thresholds=self.thresholds,
                    now=self.now,
                )
                if best is not None:
                    repl_url, repl_status = best
                    results[k] = replace(
                        rr,
                        replacement_url=repl_url,
                        replacement_status=repl_status,
                    )


SIMILAR_AFFIX_RE = re.compile(
    r"(?:^[-_.]*(?:new|open|libre|modern|go|py|rs)[-_.]+"
    r"|[-_.]*(?:v?\d+|ng|next|new|reborn|redux|revamped|rewrite|reloaded|plus|pro|lite|ce|"
    r"rs|go|py|js|ts|kt|cli|gui|app|web|desktop|mobile|android|ios|official|community)$)",
    re.IGNORECASE,
)


def _alnum_slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", name.lower())


def _core_repo_stem(name: str) -> str:
    cur = name.strip().lower()
    for _ in range(2):
        stripped = SIMILAR_AFFIX_RE.sub("", cur)
        alnum = _alnum_slug(stripped)
        if len(alnum) >= 4:
            cur = stripped
        else:
            break
    return _alnum_slug(cur)


def _score_similar_repo_name(target_repo: str, cand_repo: str, *, same_owner: bool) -> int:
    t_alnum = _alnum_slug(target_repo)
    c_alnum = _alnum_slug(cand_repo)
    if not t_alnum or not c_alnum:
        return 0
    if t_alnum == c_alnum and len(t_alnum) >= 3:
        return 100

    t_stem = _core_repo_stem(target_repo)
    c_stem = _core_repo_stem(cand_repo)
    if len(t_stem) >= 4 and t_stem == c_stem:
        return 92

    min_prefix_len = 4 if same_owner else 5
    if len(t_stem) >= min_prefix_len and len(c_stem) >= min_prefix_len:
        shorter, longer = (
            (t_stem, c_stem) if len(t_stem) <= len(c_stem) else (c_stem, t_stem)
        )
        if (longer.startswith(shorter) or longer.endswith(shorter)) and (
            len(shorter) / len(longer) >= 0.60
        ):
            return 84

    if len(t_stem) >= 6 and len(c_stem) >= 6:
        ratio = SequenceMatcher(None, t_stem, c_stem).ratio()
        if ratio >= 0.84:
            return 78

    return 0


def _pick_best_similar_author_repo(
    *,
    rr: RepoActivityResult,
    eff_owner: str,
    candidates: list[dict],
    thresholds: TierThresholds,
    now: datetime,
) -> tuple[str, str] | None:
    self_full_names = {
        rr.full_name.lower(),
        f"{rr.owner.lower()}/{rr.repo.lower()}",
        f"{eff_owner.lower()}/{rr.repo.lower()}",
    }
    eff_repo = (
        rr.canonical_full_name.split("/", 1)[1]
        if (rr.canonical_full_name and "/" in rr.canonical_full_name)
        else rr.repo
    )

    scored: list[tuple[int, datetime, str, bool, int | None]] = []
    for cand in candidates:
        nwo = str(cand.get("nameWithOwner") or "").strip()
        if "/" not in nwo or nwo.lower() in self_full_names:
            continue
        if cand.get("isArchived") or cand.get("isDisabled") or cand.get("isEmpty"):
            continue

        c_owner, c_name = nwo.split("/", 1)
        same_owner = c_owner.lower() == eff_owner.lower()
        score = max(
            _score_similar_repo_name(eff_repo, c_name, same_owner=same_owner),
            _score_similar_repo_name(rr.repo, c_name, same_owner=same_owner),
        )
        if score <= 0:
            continue

        pushed_dt = _parse_iso_datetime(cand.get("pushedAt"))
        if pushed_dt is None:
            continue
        # Never suggest an older abandoned repo that predates the dead/archived repo's own last push
        if rr.last_activity_at is not None and pushed_dt <= rr.last_activity_at:
            continue
        cand_days = max(0, int((now - pushed_dt).total_seconds() // 86400))
        if cand_days >= thresholds.tier3_days:
            continue

        scored.append((score, pushed_dt, nwo, same_owner, cand_days))

    if not scored:
        return None

    scored.sort(key=lambda item: (item[0], item[1]), reverse=True)
    _, _, best_nwo, is_same_owner, best_days = scored[0]
    act_word = (
        "active"
        if (best_days is not None and best_days < thresholds.tier1_days)
        else f"last push {best_days}d ago"
    )
    if best_days is not None and best_days < thresholds.tier1_days:
        act_word = f"active, last push {best_days}d ago"

    prefix = (
        "author's similar repo"
        if is_same_owner
        else f"similar repo @{eff_owner} works in"
    )
    return f"https://github.com/{best_nwo}", f"{prefix} — {act_word}"
