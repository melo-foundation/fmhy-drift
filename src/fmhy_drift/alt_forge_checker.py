"""Activity, staleness, archival, and replacement checker for non-GitHub code forges.

Supports:
- GitLab (`gitlab.com` and self-hosted GitLab instances via REST v4 `/api/v4/projects/...`)
- Codeberg / Forgejo / Gitea (`codeberg.org`, `gitea.com`, `projects.blender.org`, etc. via `/api/v1/repos/...`)
- SourceHut (`git.sr.ht`, `hg.sr.ht`, `sr.ht` via public RSS commit feed)
- Bitbucket Cloud (`bitbucket.org` via `/2.0/repositories/...`)
- Pagure (`pagure.io` via `/api/0/...`)
- SourceForge (`sourceforge.net/projects/...` via Allura `/rest/p/...` & `best_release.json`)
"""

from __future__ import annotations

import asyncio
import email.utils
import re
import xml.etree.ElementTree as ET
from collections import defaultdict
from dataclasses import replace
from datetime import UTC, datetime
from urllib.parse import quote, urlsplit

import httpx

from fmhy_drift.github_checker import (
    MAX_README_SCAN_CHARS,
    SOURCE_PRIORITY,
    _evaluate_repo_state,
    _latest_activity,
    _normalize_homepage,
    _parse_iso_datetime,
    find_archived_replacement_url,
)
from fmhy_drift.models import (
    GitHubRef,
    RepoActivityResult,
    StalenessTier,
    TierThresholds,
)

SRHT_COMMIT_SHA_RE = re.compile(r"/commit/([0-9a-f]{7,40})\b", re.IGNORECASE)
SRHT_SOURCE_REPO_RE = re.compile(
    r"""href=["'](https?://(?:git|hg)\.sr\.ht/~[A-Za-z0-9._-]+/[A-Za-z0-9._-]+)["']""",
    re.IGNORECASE,
)


def _parse_any_datetime(value: str | int | float | None) -> datetime | None:
    """Parse ISO-8601, RFC-2822 (`pubDate`), `'YYYY-MM-DD HH:MM:SS'`, or Unix epoch timestamps."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(float(value), tz=UTC)
        except (ValueError, OSError, OverflowError):
            return None
    text = str(value).strip()
    if not text:
        return None
    if text.isdigit():
        try:
            return datetime.fromtimestamp(float(text), tz=UTC)
        except (ValueError, OSError, OverflowError):
            return None

    iso = _parse_iso_datetime(text)
    if iso is not None:
        return iso

    try:
        dt_rfc = email.utils.parsedate_to_datetime(text)
        return dt_rfc.astimezone(UTC) if dt_rfc.tzinfo else dt_rfc.replace(tzinfo=UTC)
    except Exception:
        pass

    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(text[:19], fmt).replace(tzinfo=UTC)
        except ValueError:
            continue
    return None


class AltForgeActivityChecker:
    """Evaluates repository activity, archival, and replacements across non-GitHub forges."""

    def __init__(
        self,
        *,
        thresholds: TierThresholds | None = None,
        concurrency: int = 12,
        per_host_concurrency: int = 4,
        timeout: float = 18.0,
        now: datetime | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.thresholds = thresholds or TierThresholds()
        self.concurrency = max(1, concurrency)
        self.per_host_concurrency = max(1, per_host_concurrency)
        self.timeout = timeout
        self.now = now or datetime.now(UTC)
        self._transport = transport

    async def check_repos(self, refs: list[GitHubRef]) -> dict[str, RepoActivityResult]:
        """Deduplicate non-GitHub `GitHubRef`s by `ref.key` and evaluate activity."""
        unique_by_key: dict[str, GitHubRef] = {}
        for ref in refs:
            if ref.forge == "github":
                continue
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
            "headers": {
                "Accept": "application/json, application/xml, text/xml, */*",
                "User-Agent": "fmhy-drift-checker/0.1",
            },
        }
        if self._transport is not None:
            client_kwargs["transport"] = self._transport

        global_sem = asyncio.Semaphore(self.concurrency)
        host_sems: dict[str, asyncio.Semaphore] = defaultdict(
            lambda: asyncio.Semaphore(self.per_host_concurrency)
        )
        results: dict[str, RepoActivityResult] = {}

        async with httpx.AsyncClient(**client_kwargs) as client:

            async def _worker(ref: GitHubRef) -> None:
                async with global_sem, host_sems[ref.forge_host]:
                    results[ref.key] = await self._check_one(client, ref)

            await asyncio.gather(*(_worker(r) for r in unique_refs))
            await self._enrich_missing_wayback(client, results)

        return results

    async def _check_one(self, client: httpx.AsyncClient, ref: GitHubRef) -> RepoActivityResult:
        try:
            if ref.forge == "gitlab":
                return await self._check_gitlab(client, ref)
            if ref.forge == "forgejo":
                return await self._check_forgejo(client, ref)
            if ref.forge == "srht":
                return await self._check_srht(client, ref)
            if ref.forge == "bitbucket":
                return await self._check_bitbucket(client, ref)
            if ref.forge == "pagure":
                return await self._check_pagure(client, ref)
            if ref.forge == "sourceforge":
                return await self._check_sourceforge(client, ref)
        except Exception as exc:
            return RepoActivityResult(
                owner=ref.owner,
                repo=ref.repo,
                tier=StalenessTier.ERROR,
                reason=f"{ref.forge_host} API error: {exc.__class__.__name__}",
                forge=ref.forge,
                forge_host=ref.forge_host,
            )
        return RepoActivityResult(
            owner=ref.owner,
            repo=ref.repo,
            tier=StalenessTier.ERROR,
            reason=f"Unsupported forge: {ref.forge}",
            forge=ref.forge,
            forge_host=ref.forge_host,
        )

    def _finalize(
        self,
        ref: GitHubRef,
        res: RepoActivityResult,
        *,
        canonical_full_name: str | None = None,
        canonical_html_url: str | None = None,
        commit_oid: str | None = None,
        replacement_url: str | None = None,
        replacement_status: str | None = None,
    ) -> RepoActivityResult:
        out = replace(
            res,
            forge=ref.forge,
            forge_host=ref.forge_host,
            canonical_full_name=canonical_full_name,
            canonical_html_url=canonical_html_url,
        )
        if commit_oid:
            out = replace(out, head_commit_oid=commit_oid)
        if replacement_url:
            out = replace(
                out,
                replacement_url=replacement_url,
                replacement_status=replacement_status or "replacement link in repo",
            )
        return out

    async def _check_pages_url_alive(self, url: str | None, allowed_suffix: str) -> bool:
        """Check if a static forge Pages URL (`*.gitlab.io`, `*.codeberg.page`, `*.bitbucket.io`) responds 200 without leaving the forge domain."""
        if not url:
            return False
        kwargs: dict = {
            "timeout": httpx.Timeout(8.0),
            "follow_redirects": False,
            "trust_env": False,
            "headers": {"User-Agent": "fmhy-drift-checker/0.1"},
        }
        if self._transport is not None:
            kwargs["transport"] = self._transport
        cur = url
        async with httpx.AsyncClient(**kwargs) as pg_client:
            for _ in range(4):
                host = (urlsplit(cur).hostname or "").lower()
                if not host.endswith(allowed_suffix):
                    return False
                try:
                    resp = await pg_client.get(cur)
                except Exception:
                    return False
                if resp.status_code == 200:
                    return True
                if resp.status_code in {301, 302, 303, 307, 308}:
                    loc = resp.headers.get("location", "").strip()
                    if not loc:
                        return False
                    if loc.startswith("/"):
                        p = urlsplit(cur)
                        cur = f"{p.scheme}://{p.netloc}{loc}"
                    elif loc.startswith(("http://", "https://")):
                        cur = loc
                    else:
                        return False
                    continue
                return False
        return False

    async def _check_gitlab(
        self,
        client: httpx.AsyncClient,
        ref: GitHubRef,
        *,
        allow_pages_fallback: bool = True,
    ) -> RepoActivityResult:
        proj_path = f"{ref.owner}/{ref.repo}"
        encoded = quote(proj_path, safe="")
        base_api = f"https://{ref.forge_host}/api/v4/projects/{encoded}"
        resp = await client.get(base_api)

        if resp.status_code == 404 and allow_pages_fallback and ref.source == "forge_pages":
            for cand_repo in (f"{ref.owner}.gitlab.io", ref.owner):
                if cand_repo.lower() == ref.repo.lower():
                    continue
                alt_ref = replace(ref, repo=cand_repo)
                sub = await self._check_gitlab(client, alt_ref, allow_pages_fallback=False)
                if sub.tier != StalenessTier.MISSING:
                    return replace(sub, owner=ref.owner, repo=ref.repo)
            if await self._check_pages_url_alive(ref.discovered_from_url, ".gitlab.io"):
                return RepoActivityResult(
                    owner=ref.owner,
                    repo=ref.repo,
                    tier=StalenessTier.ACTIVE,
                    reason="GitLab Pages site is live (backed by private GitLab repository)",
                    forge=ref.forge,
                    forge_host=ref.forge_host,
                )

        if resp.status_code in {404, 410}:
            return RepoActivityResult(
                owner=ref.owner,
                repo=ref.repo,
                tier=StalenessTier.MISSING,
                reason=f"Repository not found or private on {ref.forge_host} (HTTP {resp.status_code})",
                forge=ref.forge,
                forge_host=ref.forge_host,
            )
        if resp.status_code == 451:
            return RepoActivityResult(
                owner=ref.owner,
                repo=ref.repo,
                tier=StalenessTier.MISSING,
                reason=f"Repository unavailable due to legal takedown on {ref.forge_host} (HTTP 451)",
                forge=ref.forge,
                forge_host=ref.forge_host,
            )
        if resp.status_code != 200:
            return RepoActivityResult(
                owner=ref.owner,
                repo=ref.repo,
                tier=StalenessTier.ERROR,
                reason=f"{ref.forge_host} HTTP {resp.status_code}",
                forge=ref.forge,
                forge_host=ref.forge_host,
            )

        data = resp.json()
        proj_id = data.get("id")
        path_ns = data.get("path_with_namespace") or proj_path
        canon_full = f"{ref.forge_host}/{path_ns}"
        web_url = data.get("web_url") or f"https://{canon_full}"
        is_archived = bool(data.get("archived"))
        is_empty = bool(data.get("empty_repo"))
        description = (data.get("description") or "").strip()

        commit_dt: datetime | None = None
        commit_oid: str | None = None
        release_dt: datetime | None = None
        release_tag: str | None = None

        if proj_id and not is_empty:
            c_resp = await client.get(
                f"https://{ref.forge_host}/api/v4/projects/{proj_id}/repository/commits",
                params={"per_page": "1"},
            )
            if c_resp.status_code == 200 and isinstance(c_resp.json(), list) and c_resp.json():
                c0 = c_resp.json()[0]
                if isinstance(c0, dict):
                    commit_dt = _parse_any_datetime(
                        c0.get("committed_date") or c0.get("created_at")
                    )
                    if commit_dt and commit_dt > self.now:
                        commit_dt = None
                    if isinstance(c0.get("id"), str):
                        commit_oid = c0["id"]

            r_resp = await client.get(
                f"https://{ref.forge_host}/api/v4/projects/{proj_id}/releases",
                params={"per_page": "1"},
            )
            if r_resp.status_code == 200 and isinstance(r_resp.json(), list) and r_resp.json():
                r0 = r_resp.json()[0]
                if isinstance(r0, dict):
                    release_dt = _parse_any_datetime(r0.get("released_at") or r0.get("created_at"))
                    release_tag = r0.get("tag_name")

        fallback_dt = _parse_any_datetime(data.get("last_activity_at") or data.get("created_at"))
        latest_ts = (
            None
            if is_empty
            else (_latest_activity([commit_dt, release_dt]) or fallback_dt)
        )

        repl_url: str | None = None
        if is_archived:
            readme_txt: str | None = None
            default_branch = data.get("default_branch") or "main"
            if proj_id:
                for fname in ("README.md", "readme.md", "README.rst", "README"):
                    rm_resp = await client.get(
                        f"https://{ref.forge_host}/api/v4/projects/{proj_id}/repository/files/{quote(fname, safe='')}/raw",
                        params={"ref": default_branch},
                    )
                    if rm_resp.status_code == 200:
                        readme_txt = rm_resp.text[:MAX_README_SCAN_CHARS]
                        break
            ns_parts = path_ns.rsplit("/", 1)
            eff_o = ns_parts[0] if len(ns_parts) == 2 else ref.owner
            eff_r = ns_parts[-1]
            repl_url = find_archived_replacement_url(
                owner=eff_o,
                repo=eff_r,
                canonical_full_name=None,
                description=description,
                homepage_url=None,
                readme_text=readme_txt,
            )

        res = _evaluate_repo_state(
            owner=ref.owner,
            repo=ref.repo,
            canonical_full_name=None,
            is_archived=is_archived,
            is_disabled=False,
            archived_at=None,
            last_activity_at=latest_ts,
            latest_release_tag=release_tag,
            homepage_url=None,
            description=description,
            thresholds=self.thresholds,
            now=self.now,
        )
        return self._finalize(
            ref,
            res,
            canonical_full_name=canon_full,
            canonical_html_url=web_url,
            commit_oid=commit_oid,
            replacement_url=repl_url,
        )

    async def _check_forgejo(
        self,
        client: httpx.AsyncClient,
        ref: GitHubRef,
        *,
        allow_pages_fallback: bool = True,
    ) -> RepoActivityResult:
        api_base = f"https://{ref.forge_host}/api/v1/repos/{ref.owner}/{ref.repo}"
        resp = await client.get(api_base)

        if resp.status_code == 404 and allow_pages_fallback and ref.source == "forge_pages":
            for cand_repo in ("pages", ref.owner):
                if cand_repo.lower() == ref.repo.lower():
                    continue
                alt_ref = replace(ref, repo=cand_repo)
                sub = await self._check_forgejo(client, alt_ref, allow_pages_fallback=False)
                if sub.tier != StalenessTier.MISSING:
                    return replace(sub, owner=ref.owner, repo=ref.repo)
            if await self._check_pages_url_alive(ref.discovered_from_url, ".codeberg.page"):
                return RepoActivityResult(
                    owner=ref.owner,
                    repo=ref.repo,
                    tier=StalenessTier.ACTIVE,
                    reason="Codeberg Pages site is live (backed by private repository)",
                    forge=ref.forge,
                    forge_host=ref.forge_host,
                )

        if resp.status_code in {404, 410}:
            return RepoActivityResult(
                owner=ref.owner,
                repo=ref.repo,
                tier=StalenessTier.MISSING,
                reason=f"Repository not found or private on {ref.forge_host} (HTTP {resp.status_code})",
                forge=ref.forge,
                forge_host=ref.forge_host,
            )
        if resp.status_code == 451:
            return RepoActivityResult(
                owner=ref.owner,
                repo=ref.repo,
                tier=StalenessTier.MISSING,
                reason=f"Repository unavailable due to legal takedown on {ref.forge_host} (HTTP 451)",
                forge=ref.forge,
                forge_host=ref.forge_host,
            )
        if resp.status_code != 200:
            return RepoActivityResult(
                owner=ref.owner,
                repo=ref.repo,
                tier=StalenessTier.ERROR,
                reason=f"{ref.forge_host} HTTP {resp.status_code}",
                forge=ref.forge,
                forge_host=ref.forge_host,
            )

        data = resp.json()
        full_slug = data.get("full_name") or f"{ref.owner}/{ref.repo}"
        canon_full = f"{ref.forge_host}/{full_slug}"
        web_url = data.get("html_url") or f"https://{canon_full}"
        is_archived = bool(data.get("archived"))
        archived_at = _parse_any_datetime(data.get("archived_at"))
        if archived_at and archived_at.year < 2000:
            # Gitea/Forgejo uses "0001-01-01T00:00:00Z" or "1970-01-01T00:00:00Z" when unset
            archived_at = None
        is_empty = bool(data.get("empty"))
        homepage = _normalize_homepage(data.get("website"))
        description = (data.get("description") or "").strip()

        commit_dt: datetime | None = None
        commit_oid: str | None = None
        release_dt: datetime | None = None
        release_tag: str | None = None

        if not is_empty:
            c_resp = await client.get(
                f"https://{ref.forge_host}/api/v1/repos/{full_slug}/commits",
                params={"limit": "1"},
            )
            if c_resp.status_code == 200 and isinstance(c_resp.json(), list) and c_resp.json():
                c0 = c_resp.json()[0]
                if isinstance(c0, dict):
                    commit_oid = c0.get("sha") if isinstance(c0.get("sha"), str) else None
                    c_inner = c0.get("commit") if isinstance(c0.get("commit"), dict) else {}
                    committer = (
                        c_inner.get("committer")
                        if isinstance(c_inner.get("committer"), dict)
                        else {}
                    )
                    commit_dt = _parse_any_datetime(committer.get("date") or c0.get("created"))
                    if commit_dt and commit_dt > self.now:
                        commit_dt = None

            r_resp = await client.get(
                f"https://{ref.forge_host}/api/v1/repos/{full_slug}/releases",
                params={"limit": "1"},
            )
            if r_resp.status_code == 200 and isinstance(r_resp.json(), list) and r_resp.json():
                r0 = r_resp.json()[0]
                if isinstance(r0, dict):
                    release_dt = _parse_any_datetime(r0.get("published_at") or r0.get("created_at"))
                    release_tag = r0.get("tag_name")

        fallback_dt = _parse_any_datetime(data.get("updated_at") or data.get("created_at"))
        latest_ts = (
            None
            if is_empty
            else (_latest_activity([commit_dt, release_dt]) or fallback_dt)
        )

        repl_url: str | None = None
        if is_archived:
            readme_txt = None
            for fname in ("README.md", "readme.md", "README.org", "README"):
                rm_resp = await client.get(
                    f"https://{ref.forge_host}/api/v1/repos/{full_slug}/raw/{fname}"
                )
                if rm_resp.status_code == 200:
                    readme_txt = rm_resp.text[:MAX_README_SCAN_CHARS]
                    break
            eff_o, eff_r = (
                full_slug.split("/", 1) if "/" in full_slug else (ref.owner, ref.repo)
            )
            repl_url = find_archived_replacement_url(
                owner=eff_o,
                repo=eff_r,
                canonical_full_name=None,
                description=description,
                homepage_url=homepage,
                readme_text=readme_txt,
            )

        res = _evaluate_repo_state(
            owner=ref.owner,
            repo=ref.repo,
            canonical_full_name=None,
            is_archived=is_archived,
            is_disabled=False,
            archived_at=archived_at,
            last_activity_at=latest_ts,
            latest_release_tag=release_tag,
            homepage_url=homepage,
            description=description,
            thresholds=self.thresholds,
            now=self.now,
        )
        return self._finalize(
            ref,
            res,
            canonical_full_name=canon_full,
            canonical_html_url=web_url,
            commit_oid=commit_oid,
            replacement_url=repl_url,
        )

    async def _check_srht(self, client: httpx.AsyncClient, ref: GitHubRef) -> RepoActivityResult:
        rss_hosts = (
            ["hg.sr.ht"]
            if ref.forge_host == "hg.sr.ht"
            else ["git.sr.ht", "hg.sr.ht"]
        )
        rss_resp: httpx.Response | None = None
        chosen_host = ref.forge_host
        target_owner, target_repo = ref.owner, ref.repo

        for h in rss_hosts:
            r = await client.get(f"https://{h}/{target_owner}/{target_repo}/log/rss.xml")
            if r.status_code == 200:
                rss_resp = r
                chosen_host = h
                break

        # Project hub fallback (`https://sr.ht/~owner/Project/` -> `/sources`)
        if rss_resp is None and ref.forge_host == "sr.ht":
            hub_resp = await client.get(f"https://sr.ht/{ref.owner}/{ref.repo}/sources")
            if hub_resp.status_code == 200:
                m = SRHT_SOURCE_REPO_RE.search(hub_resp.text)
                if m:
                    found_url = m.group(1)
                    parts = urlsplit(found_url)
                    segs = [s for s in parts.path.split("/") if s]
                    if len(segs) >= 2 and parts.hostname:
                        chosen_host = parts.hostname.lower()
                        target_owner, target_repo = segs[0], segs[1]
                        r2 = await client.get(
                            f"https://{chosen_host}/{target_owner}/{target_repo}/log/rss.xml"
                        )
                        if r2.status_code == 200:
                            rss_resp = r2

        if rss_resp is None:
            return RepoActivityResult(
                owner=ref.owner,
                repo=ref.repo,
                tier=StalenessTier.MISSING,
                reason=f"Repository not found on {ref.forge_host} (HTTP 404)",
                forge=ref.forge,
                forge_host=ref.forge_host,
            )

        commit_dt: datetime | None = None
        commit_oid: str | None = None
        desc = ""
        try:
            root = ET.fromstring(rss_resp.text)
            chan = root.find("channel")
            if chan is not None:
                desc = (chan.findtext("description") or "").strip()
                first_item = chan.find("item")
                if first_item is not None:
                    commit_dt = _parse_any_datetime(first_item.findtext("pubDate"))
                    guid = first_item.findtext("guid") or first_item.findtext("link") or ""
                    sha_m = SRHT_COMMIT_SHA_RE.search(guid)
                    if sha_m:
                        commit_oid = sha_m.group(1)
                if commit_dt is None:
                    commit_dt = _parse_any_datetime(chan.findtext("lastBuildDate"))
        except ET.ParseError:
            pass

        canon_full = f"{chosen_host}/{target_owner}/{target_repo}"
        web_url = f"https://{canon_full}"
        res = _evaluate_repo_state(
            owner=ref.owner,
            repo=ref.repo,
            canonical_full_name=None,
            is_archived=False,
            is_disabled=False,
            archived_at=None,
            last_activity_at=commit_dt,
            latest_release_tag=None,
            homepage_url=None,
            description=desc,
            thresholds=self.thresholds,
            now=self.now,
        )
        return self._finalize(
            ref,
            res,
            canonical_full_name=canon_full,
            canonical_html_url=web_url,
            commit_oid=commit_oid,
        )

    async def _check_bitbucket(
        self,
        client: httpx.AsyncClient,
        ref: GitHubRef,
        *,
        allow_pages_fallback: bool = True,
    ) -> RepoActivityResult:
        api_url = f"https://api.bitbucket.org/2.0/repositories/{ref.owner}/{ref.repo}"
        resp = await client.get(api_url)

        if resp.status_code == 404 and allow_pages_fallback and ref.source == "forge_pages":
            fallback_repo = f"{ref.owner}.bitbucket.io"
            if fallback_repo.lower() != ref.repo.lower():
                sub = await self._check_bitbucket(
                    client,
                    replace(ref, repo=fallback_repo),
                    allow_pages_fallback=False,
                )
                if sub.tier != StalenessTier.MISSING:
                    return replace(sub, owner=ref.owner, repo=ref.repo)
            if await self._check_pages_url_alive(ref.discovered_from_url, ".bitbucket.io"):
                return RepoActivityResult(
                    owner=ref.owner,
                    repo=ref.repo,
                    tier=StalenessTier.ACTIVE,
                    reason="Bitbucket Pages site is live (backed by private repository)",
                    forge=ref.forge,
                    forge_host=ref.forge_host,
                )

        if resp.status_code in {404, 410}:
            return RepoActivityResult(
                owner=ref.owner,
                repo=ref.repo,
                tier=StalenessTier.MISSING,
                reason=f"Repository not found or private on Bitbucket (HTTP {resp.status_code})",
                forge=ref.forge,
                forge_host=ref.forge_host,
            )
        if resp.status_code != 200:
            return RepoActivityResult(
                owner=ref.owner,
                repo=ref.repo,
                tier=StalenessTier.ERROR,
                reason=f"Bitbucket API HTTP {resp.status_code}",
                forge=ref.forge,
                forge_host=ref.forge_host,
            )

        data = resp.json()
        full_slug = data.get("full_name") or f"{ref.owner}/{ref.repo}"
        canon_full = f"bitbucket.org/{full_slug}"
        links = data.get("links") if isinstance(data.get("links"), dict) else {}
        html_obj = links.get("html") if isinstance(links.get("html"), dict) else {}
        web_url = html_obj.get("href") or f"https://{canon_full}"
        homepage = _normalize_homepage(data.get("website"))
        description = (data.get("description") or "").strip()

        commit_dt: datetime | None = None
        commit_oid: str | None = None
        c_resp = await client.get(f"{api_url}/commits", params={"pagelen": "1"})
        if c_resp.status_code == 200 and isinstance(c_resp.json(), dict):
            vals = c_resp.json().get("values")
            if isinstance(vals, list) and vals and isinstance(vals[0], dict):
                commit_dt = _parse_any_datetime(vals[0].get("date"))
                if isinstance(vals[0].get("hash"), str):
                    commit_oid = vals[0]["hash"]

        latest_ts = commit_dt or _parse_any_datetime(data.get("updated_on") or data.get("created_on"))
        res = _evaluate_repo_state(
            owner=ref.owner,
            repo=ref.repo,
            canonical_full_name=None,
            is_archived=False,
            is_disabled=False,
            archived_at=None,
            last_activity_at=latest_ts,
            latest_release_tag=None,
            homepage_url=homepage,
            description=description,
            thresholds=self.thresholds,
            now=self.now,
        )
        return self._finalize(
            ref,
            res,
            canonical_full_name=canon_full,
            canonical_html_url=web_url,
            commit_oid=commit_oid,
        )

    async def _check_pagure(self, client: httpx.AsyncClient, ref: GitHubRef) -> RepoActivityResult:
        slug = ref.repo if ref.owner == "_" else f"{ref.owner}/{ref.repo}"
        resp = await client.get(f"https://pagure.io/api/0/{slug}")
        if resp.status_code == 404:
            return RepoActivityResult(
                owner=ref.owner,
                repo=ref.repo,
                tier=StalenessTier.MISSING,
                reason="Repository not found on Pagure (HTTP 404)",
                forge=ref.forge,
                forge_host=ref.forge_host,
            )
        if resp.status_code != 200:
            return RepoActivityResult(
                owner=ref.owner,
                repo=ref.repo,
                tier=StalenessTier.ERROR,
                reason=f"Pagure API HTTP {resp.status_code}",
                forge=ref.forge,
                forge_host=ref.forge_host,
            )

        data = resp.json()
        fullname = data.get("fullname") or slug
        canon_full = f"pagure.io/{fullname}"
        web_url = data.get("full_url") or f"https://{canon_full}"
        latest_ts = _parse_any_datetime(data.get("date_modified") or data.get("date_created"))
        description = (data.get("description") or "").strip()

        res = _evaluate_repo_state(
            owner=ref.owner,
            repo=ref.repo,
            canonical_full_name=None,
            is_archived=False,
            is_disabled=False,
            archived_at=None,
            last_activity_at=latest_ts,
            latest_release_tag=None,
            homepage_url=None,
            description=description,
            thresholds=self.thresholds,
            now=self.now,
        )
        return self._finalize(
            ref,
            res,
            canonical_full_name=canon_full,
            canonical_html_url=web_url,
        )

    async def _check_sourceforge(
        self,
        client: httpx.AsyncClient,
        ref: GitHubRef,
    ) -> RepoActivityResult:
        slug = ref.repo.lower()
        api_url = f"https://sourceforge.net/rest/p/{slug}"
        resp = await client.get(api_url)
        if resp.status_code in {404, 410}:
            return RepoActivityResult(
                owner="projects",
                repo=slug,
                tier=StalenessTier.MISSING,
                reason=f"Project not found on SourceForge (HTTP {resp.status_code})",
                forge=ref.forge,
                forge_host=ref.forge_host,
            )
        if resp.status_code != 200:
            return RepoActivityResult(
                owner="projects",
                repo=slug,
                tier=StalenessTier.ERROR,
                reason=f"SourceForge API HTTP {resp.status_code}",
                forge=ref.forge,
                forge_host=ref.forge_host,
            )

        data = resp.json()
        status_str = str(data.get("status") or "").lower()
        moved_url = _normalize_homepage(data.get("moved_to_url"))
        homepage = _normalize_homepage(data.get("external_homepage"))
        desc = (data.get("short_description") or data.get("summary") or "").strip()

        release_dt: datetime | None = None
        release_tag: str | None = None
        rel_resp = await client.get(f"https://sourceforge.net/projects/{slug}/best_release.json")
        if rel_resp.status_code == 200 and isinstance(rel_resp.json(), dict):
            rel_obj = rel_resp.json().get("release")
            if isinstance(rel_obj, dict):
                release_dt = _parse_any_datetime(rel_obj.get("date"))
                release_tag = rel_obj.get("filename")

        act_dt: datetime | None = None
        act_resp = await client.get(
            f"https://sourceforge.net/rest/p/{slug}/activity",
            params={"limit": "1"},
        )
        if act_resp.status_code == 200 and isinstance(act_resp.json(), dict):
            tl = act_resp.json().get("timeline")
            if isinstance(tl, list) and tl and isinstance(tl[0], dict):
                act_dt = _parse_any_datetime(tl[0].get("published"))

        last_upd = _parse_any_datetime(data.get("last_updated") or data.get("creation_date"))
        latest_ts = _latest_activity([release_dt, act_dt, last_upd])

        repl_url = moved_url or find_archived_replacement_url(
            owner="sourceforge",
            repo=slug,
            canonical_full_name=None,
            description=desc,
            homepage_url=homepage,
            readme_text=None,
        )
        is_archived = bool(moved_url) or status_str in {"moved", "abandoned", "inactive"}

        res = _evaluate_repo_state(
            owner="projects",
            repo=slug,
            canonical_full_name=f"sourceforge.net/projects/{slug}",
            is_archived=is_archived,
            is_disabled=False,
            archived_at=None,
            last_activity_at=latest_ts,
            latest_release_tag=release_tag,
            homepage_url=homepage,
            description=desc,
            thresholds=self.thresholds,
            now=self.now,
        )
        return self._finalize(
            ref,
            res,
            canonical_full_name=f"sourceforge.net/projects/{slug}",
            canonical_html_url=f"https://sourceforge.net/projects/{slug}/",
            replacement_url=repl_url,
            replacement_status="moved from SourceForge" if repl_url else None,
        )

    async def _enrich_missing_wayback(
        self,
        client: httpx.AsyncClient,
        results: dict[str, RepoActivityResult],
    ) -> None:
        missing_keys = [
            k
            for k, r in results.items()
            if r.tier == StalenessTier.MISSING and r.inactive_days is None
        ]
        if not missing_keys:
            return
        sem = asyncio.Semaphore(4)

        async def _one(key: str) -> None:
            rr = results[key]
            async with sem:
                try:
                    resp = await client.get(
                        "https://web.archive.org/cdx/search/cdx",
                        params={
                            "url": rr.html_url,
                            "output": "json",
                            "fl": "timestamp,statuscode",
                            "filter": "statuscode:200",
                            "limit": "-1",
                        },
                        timeout=6.0,
                    )
                    if resp.status_code != 200:
                        return
                    rows = resp.json()
                    if isinstance(rows, list) and len(rows) >= 2:
                        ts = str(rows[-1][0])
                        if len(ts) >= 8:
                            dt = datetime.strptime(ts[:8], "%Y%m%d").replace(tzinfo=UTC)
                            days = max(0, int((self.now - dt).total_seconds() // 86400))
                            results[key] = replace(
                                rr,
                                last_activity_at=dt,
                                inactive_days=days,
                                reason=f"{rr.reason} — last archived alive {dt.strftime('%Y-%m-%d')}",
                            )
                except Exception:
                    return

        try:
            await asyncio.wait_for(
                asyncio.gather(*(_one(k) for k in missing_keys)),
                timeout=20.0,
            )
        except TimeoutError:
            pass
