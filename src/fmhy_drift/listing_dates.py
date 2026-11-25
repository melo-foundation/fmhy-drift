"""Date since which each host has been continuously listed in the FMHY markdown source.

Used by the RDAP checker: a domain whose registration date is newer than its FMHY listing date was dropped and
re-registered after FMHY linked it.

Primary source: dated snapshots of the source history (monthly for 2 years, quarterly before). `fmhy/edit` history
starts at its root commit on 2024-08-07; the 2021-08 .. 2024-08 history lives in the archived `fmhy/archive` repo
(markdown at the repo root, old file names). For each host present at the current commit, "listed since" is the date
of the first snapshot of the latest unbroken run of snapshots that contain it. A site that FMHY dropped and later
re-listed (for example under a new owner) starts a new run at the re-listing, so the old listing cannot make the
new registration look like a hijack. The host was present at that snapshot's commit, so the date is a valid
"present since"; the true add date lies between the previous snapshot and this one (snapshot granularity).
A snapshot that fails to load breaks every run, which can only move dates later (conservative).

Fallback for hosts present only at the current commit: GitHub GraphQL blame. Blame reports the *last* commit that
touched a line, not the commit that added the URL, so it is an UPPER bound on the add date (any later edit of the
line moves it forward). Later dates can only hide a re-registration, never invent one.
"""

from __future__ import annotations

import asyncio
import re
import time
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import quote

import httpx

from fmhy_drift.domains import is_public_dns_host, site_key, url_host
from fmhy_drift.signal_cache import SignalCache

GITHUB_API = "https://api.github.com"
GITHUB_GRAPHQL = "https://api.github.com/graphql"
GITHUB_RAW = "https://raw.githubusercontent.com"

CACHE_NS = "listing_dates"
CACHE_SCHEMA = 1
SNAP_NS = "listing_snapshots"  # immutable: keyed by repo@commit and by repo + past calendar date

# History before fmhy/edit's root commit (2024-08-07T12:23:35Z, "hello world, again").
ARCHIVE_REPO = "fmhy/archive"
ARCHIVE_BEFORE = datetime(2024, 8, 7, 12, 23, 35, tzinfo=UTC)
HISTORY_START = datetime(2021, 8, 1, tzinfo=UTC)
MONTHLY_MONTHS = 24
SNAPSHOT_CONCURRENCY = 8

# Basenames that are not recommendations, in every historical layout (UnsafeSites.md, posts.md, README.md...).
_EXCLUDE_BASENAME_RE = re.compile(
    r"unsafe|^posts|sandbox|startpage|feedback|readme|contributing|license|changelog|code_of_conduct",
    re.IGNORECASE,
)
# Directories (relative to the markdown root) whose .md files are part of the wiki.
_SNAPSHOT_SUBDIRS = ("", "other", ".vitepress/notes")

# Recommendation pages + the shared notes that VitePress inlines into them. `posts/` (monthly changelogs) and
# `unsafe.md` mention sites that are removed or warned against, and posts are never re-edited, so their old blame
# dates would make a later, legitimate re-listing look old.
INCLUDE_DIRS = ("docs", "docs/other", "docs/.vitepress/notes")
EXCLUDE_FILES = frozenset(
    {
        "docs/unsafe.md",
        "docs/posts.md",
        "docs/sandbox.md",
        "docs/startpage.md",
        "docs/feedback.md",
    }
)

URL_RE = re.compile(r"https?://[^\s<>()\[\]\"'`|\\]+", re.IGNORECASE)

# GraphQL blame costs 1 point per query regardless of file size (measured), but the server aborts queries that run
# ~10 s. Large docs pages (100-200 KB, ~1-2k ranges) take ~4 s each, so they go alone; small notes are aliased together.
BATCH_MAX_BYTES = 60_000
BATCH_MAX_FILES = 25
BLAME_CONCURRENCY = 3
MAX_ATTEMPTS = 3


def _include_path(path: str) -> bool:
    """Current-layout rule (paths under `docs/`), the same one snapshots use."""
    return (
        path.startswith("docs/")
        and path not in EXCLUDE_FILES
        and _include_rel(path[len("docs/") :])
    )


def _include_rel(rel: str) -> bool:
    """`rel` is relative to the markdown root (`docs/` or the repo root in old layouts)."""
    if not rel.endswith(".md"):
        return False
    parent, _, base = rel.rpartition("/")
    return parent in _SNAPSHOT_SUBDIRS and not _EXCLUDE_BASENAME_RE.search(base)


def hosts_in_text(text: str) -> set[str]:
    out: set[str] = set()
    for line in text.split("\n"):
        if "http" in line:
            out |= _hosts_in_line(line)
    return out


def snapshot_dates(now: datetime) -> list[datetime]:
    """Calendar snapshot points, newest first: the 1st of the last 24 months, then the 1st of each quarter."""
    out: list[datetime] = []
    y, m = now.year, now.month
    for i in range(400):
        d = datetime(y, m, 1, tzinfo=UTC)
        if d < HISTORY_START:
            break
        if d < now and (i < MONTHLY_MONTHS or m in (1, 4, 7, 10)):
            out.append(d)
        m -= 1
        if m == 0:
            y, m = y - 1, 12
    return out


def compute_listed_since(
    snapshots: list[tuple[datetime, set[str] | None]],
    head_hosts: set[str],
    head_date: datetime,
    blame_dates: dict[str, datetime],
) -> dict[str, datetime]:
    """Start of the latest unbroken run of snapshots (`None` hosts = failed snapshot) containing each head host.

    Hosts absent from the newest historical snapshot fall back to their blame date (or the head commit date).
    """
    alive = set(head_hosts)
    start = dict.fromkeys(head_hosts, head_date)
    for date, hosts in sorted(snapshots, key=lambda x: x[0], reverse=True):
        if date >= head_date:
            continue
        if not hosts:
            break
        alive &= hosts
        if not alive:
            break
        for h in alive:
            start[h] = date
    return {
        h: (blame_dates.get(h, head_date) if start[h] == head_date else start[h])
        for h in head_hosts
    }


def _hosts_in_line(line: str) -> set[str]:
    out: set[str] = set()
    for m in URL_RE.finditer(line):
        host = url_host(m.group(0).rstrip(".,;:*_~"))
        if is_public_dns_host(host):
            out.add(site_key(host))
    return out


def map_hosts_to_dates(text: str, ranges: list[dict[str, Any]]) -> dict[str, str]:
    """Map each site key in `text` to the earliest blame `committedDate` (ISO string) among lines that mention it."""
    lines = text.split("\n")
    line_date: list[str | None] = [None] * (len(lines) + 1)
    for r in ranges:
        try:
            start, end = int(r["startingLine"]), int(r["endingLine"])
            date = str(r["commit"]["committedDate"])
        except (KeyError, TypeError, ValueError):
            continue
        for i in range(max(1, start), min(end, len(lines)) + 1):
            line_date[i] = date
    out: dict[str, str] = {}
    for idx, line in enumerate(lines, start=1):
        if "http" not in line:
            continue
        date = line_date[idx]
        if date is None:
            continue
        for key in _hosts_in_line(line):
            prev = out.get(key)
            if prev is None or _parse_dt(date) < _parse_dt(prev):
                out[key] = date
    return out


def _parse_dt(raw: str) -> datetime:
    dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def _blame_query(paths: list[str]) -> str:
    parts = []
    for i, _ in enumerate(paths):
        parts.append(
            f"f{i}: blame(path: $p{i}) {{ ranges {{ startingLine endingLine commit {{ committedDate }} }} }}"
        )
    var_decl = ", ".join(f"$p{i}: String!" for i in range(len(paths)))
    return (
        f"query($owner: String!, $name: String!, $oid: GitObjectID!, {var_decl}) {{ "
        "rateLimit { cost remaining } "
        "repository(owner: $owner, name: $name) { object(oid: $oid) { ... on Commit { "
        + " ".join(parts)
        + " } } } }"
    )


def _batches(files: list[tuple[str, str, int]]) -> list[list[tuple[str, str, int]]]:
    out: list[list[tuple[str, str, int]]] = []
    cur: list[tuple[str, str, int]] = []
    cur_bytes = 0
    for f in sorted(files, key=lambda x: x[2]):
        if cur and (cur_bytes + f[2] > BATCH_MAX_BYTES or len(cur) >= BATCH_MAX_FILES):
            out.append(cur)
            cur, cur_bytes = [], 0
        cur.append(f)
        cur_bytes += f[2]
    if cur:
        out.append(cur)
    return out


async def fetch_listing_dates(
    *,
    github_token: str | None,
    cache: SignalCache,
    now: datetime,
    transport: httpx.AsyncBaseTransport | None = None,
    repo: str = "fmhy/edit",
    ref: str = "main",
    history: bool = True,
) -> dict[str, datetime]:
    """Return `site_key(host)` -> earliest FMHY blame date (an upper bound on when the host was first listed)."""
    if not github_token:
        print(
            "listing dates: skipped (set GITHUB_TOKEN to enable FMHY blame listing dates)",
            flush=True,
        )
        return {}
    owner, name = repo.split("/", 1)
    started = time.monotonic()
    headers = {
        "User-Agent": "fmhy-drift-checker/0.1",
        "Authorization": f"bearer {github_token}",
        "Accept": "application/vnd.github+json",
    }
    stats = {
        "files": 0,
        "cached": 0,
        "blamed": 0,
        "failed": 0,
        "stale_used": 0,
        "cost": 0,
        "remaining": None,
    }
    per_file: dict[str, dict[str, str]] = {}
    head_date: list[datetime] = [now]
    snapshots: list[tuple[datetime, set[str] | None]] = []

    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(30.0),
            follow_redirects=True,
            trust_env=False,
            headers=headers,
            transport=transport,
        ) as client:
            commit_oid, files = await _list_files(client, owner, name, ref, head_date)
            stats["files"] = len(files)

            todo: list[tuple[str, str, int]] = []
            for path, blob, size in files:
                row = cache.get_any_age(CACHE_NS, path)
                data = row[0] if row else None
                if (
                    isinstance(data, dict)
                    and data.get("blob") == blob
                    and data.get("schema") == CACHE_SCHEMA
                    and isinstance(data.get("hosts"), dict)
                ):
                    per_file[path] = data["hosts"]
                    stats["cached"] += 1
                else:
                    todo.append((path, blob, size))

            sem = asyncio.Semaphore(BLAME_CONCURRENCY)

            async def _run_batch(batch: list[tuple[str, str, int]]) -> None:
                async with sem:
                    ok = await _blame_batch(
                        client, owner, name, commit_oid, batch, cache, per_file, stats
                    )
                if ok or len(batch) == 1:
                    return
                # A batch can time out as a whole; retry its files one by one.
                for f in batch:
                    async with sem:
                        await _blame_batch(
                            client, owner, name, commit_oid, [f], cache, per_file, stats
                        )

            await asyncio.gather(*(_run_batch(b) for b in _batches(todo)))

            for path, _blob, _size in todo:
                if path in per_file:
                    continue
                stats["failed"] += 1
                row = cache.get_any_age(CACHE_NS, path)
                if (
                    row
                    and isinstance(row[0], dict)
                    and isinstance(row[0].get("hosts"), dict)
                ):
                    per_file[path] = row[0]["hosts"]
                    stats["stale_used"] += 1

            if history:
                snapshots = await _load_snapshots(
                    client, cache, now, repo, head_date[0], stats
                )
    except Exception as exc:  # network / API shape errors must never break the run
        print(
            f"listing dates: aborted ({type(exc).__name__}: {exc}); using cached files only",
            flush=True,
        )
        for path, row in _cached_rows(cache):
            per_file.setdefault(path, row)

    blame: dict[str, datetime] = {}
    for hosts in per_file.values():
        for key, raw in hosts.items():
            try:
                dt = _parse_dt(raw)
            except ValueError:
                continue
            if key not in blame or dt < blame[key]:
                blame[key] = dt
    merged = compute_listed_since(snapshots, set(blame), head_date[0], blame)
    newest = max(
        (d for d, h in snapshots if h and d < head_date[0]), default=head_date[0]
    )
    blame_only = sum(1 for v in merged.values() if v >= newest)
    print(
        f"listing dates: {len(merged)} hosts ({blame_only} blame-only) from {stats['files']} files "
        f"({stats['cached']} cached, {stats['blamed']} blamed, {stats['failed']} failed, "
        f"{stats['stale_used']} stale) graphql cost {stats['cost']} remaining {stats['remaining']}; "
        f"{stats.get('snapshots', 0)} snapshots ({stats.get('snap_cached', 0)} cached, "
        f"{stats.get('snap_failed', 0)} failed), {stats.get('api_requests', 0)} history API + "
        f"{stats.get('raw_files', 0)} raw requests; {time.monotonic() - started:.1f}s",
        flush=True,
    )
    return merged


async def _resolve_snapshot_commit(
    client: httpx.AsyncClient,
    cache: SignalCache,
    repo: str,
    until: datetime,
    now: datetime,
    stats: dict,
) -> tuple[str, datetime] | None:
    key = f"until:{repo}:{until.date().isoformat()}"
    hit = cache.get_any_age(SNAP_NS, key)
    if hit and isinstance(hit[0], dict) and "sha" in hit[0]:
        if not hit[0]["sha"]:
            return None  # no commit existed yet at that date
        return str(hit[0]["sha"]), _parse_dt(str(hit[0]["date"]))
    stats["api_requests"] = stats.get("api_requests", 0) + 1
    resp = await client.get(
        f"{GITHUB_API}/repos/{repo}/commits",
        params={"until": until.strftime("%Y-%m-%dT%H:%M:%SZ"), "per_page": "1"},
    )
    if resp.status_code != 200:
        return None
    rows = resp.json()
    if not isinstance(rows, list):
        return None
    if not rows:
        if until < now - timedelta(days=1):
            cache.put(SNAP_NS, key, {"sha": None})
        return None
    sha = str(rows[0]["sha"])
    date = _parse_dt(str(rows[0]["commit"]["committer"]["date"]))
    if until < now - timedelta(
        days=1
    ):  # a past calendar point always maps to the same commit
        cache.put(SNAP_NS, key, {"sha": sha, "date": date.isoformat()})
    return sha, date


async def _snapshot_files(
    client: httpx.AsyncClient, repo: str, sha: str, stats: dict
) -> list[tuple[str, str]]:
    """(path, blob sha) of wiki markdown at `sha`, detecting the layout: `docs/` if present, else the repo root."""
    stats["api_requests"] = stats.get("api_requests", 0) + 1
    resp = await client.get(f"{GITHUB_API}/repos/{repo}/git/trees/{sha}")
    resp.raise_for_status()
    root = resp.json().get("tree", [])
    docs = next(
        (e for e in root if e.get("path") == "docs" and e.get("type") == "tree"), None
    )
    if docs is None:
        return [
            (str(e["path"]), str(e["sha"]))
            for e in root
            if e.get("type") == "blob" and _include_rel(str(e.get("path", "")))
        ]
    stats["api_requests"] += 1
    resp = await client.get(
        f"{GITHUB_API}/repos/{repo}/git/trees/{docs['sha']}", params={"recursive": "1"}
    )
    resp.raise_for_status()
    return [
        ("docs/" + str(e["path"]), str(e["sha"]))
        for e in resp.json().get("tree", [])
        if e.get("type") == "blob" and _include_rel(str(e.get("path", "")))
    ]


async def _load_snapshots(
    client: httpx.AsyncClient,
    cache: SignalCache,
    now: datetime,
    repo: str,
    head_date: datetime,
    stats: dict,
) -> list[tuple[datetime, set[str] | None]]:
    def repo_for(d: datetime) -> str:
        return ARCHIVE_REPO if repo == "fmhy/edit" and d < ARCHIVE_BEFORE else repo

    sem = asyncio.Semaphore(SNAPSHOT_CONCURRENCY)
    blob_hosts: dict[str, set[str]] = {}

    async def _resolve(d: datetime) -> tuple[str, str, datetime] | None:
        r = repo_for(d)
        async with sem:
            try:
                got = await _resolve_snapshot_commit(client, cache, r, d, now, stats)
            except (httpx.HTTPError, ValueError, KeyError, TypeError):
                return None
        return (r, got[0], got[1]) if got else None

    points = [
        p
        for p in await asyncio.gather(*(_resolve(d) for d in snapshot_dates(now)))
        if p
    ]
    unique: dict[str, tuple[str, str, datetime]] = {}
    for r, sha, date in points:
        if date < head_date:
            unique.setdefault(f"{r}@{sha}", (r, sha, date))
    stats["snapshots"] = len(unique)

    async def _blob_hosts(r: str, sha: str, path: str, blob: str) -> set[str] | None:
        if blob in blob_hosts:
            return blob_hosts[blob]
        async with sem:
            text = await _raw_text_url(client, f"{GITHUB_RAW}/{r}/{sha}/{quote(path)}")
        stats["raw_files"] = stats.get("raw_files", 0) + 1
        if text is None:
            return None
        hosts = hosts_in_text(text)
        blob_hosts[blob] = hosts
        return hosts

    async def _one(
        key: str, r: str, sha: str, date: datetime
    ) -> tuple[datetime, set[str] | None]:
        hit = cache.get_any_age(SNAP_NS, key)
        if hit and isinstance(hit[0], dict) and isinstance(hit[0].get("hosts"), str):
            stats["snap_cached"] = stats.get("snap_cached", 0) + 1
            return date, set(filter(None, hit[0]["hosts"].split("\n")))
        try:
            async with sem:
                files = await _snapshot_files(client, r, sha, stats)
        except (httpx.HTTPError, ValueError, KeyError, TypeError):
            files = []
        parts = await asyncio.gather(*(_blob_hosts(r, sha, p, b) for p, b in files))
        if not files or any(p is None for p in parts):
            stats["snap_failed"] = stats.get("snap_failed", 0) + 1
            return (
                date,
                None,
            )  # breaks every run; not cached, so the next run retries it
        hosts: set[str] = set().union(*parts)
        cache.put(
            SNAP_NS,
            key,
            {
                "date": date.isoformat(),
                "files": len(files),
                "hosts": "\n".join(sorted(hosts)),
            },
        )
        return date, hosts

    return list(
        await asyncio.gather(*(_one(k, r, sha, d) for k, (r, sha, d) in unique.items()))
    )


def _cached_rows(cache: SignalCache) -> list[tuple[str, dict[str, str]]]:
    rows = cache._ns.get(CACHE_NS, {})
    out = []
    for path in rows:
        hit = cache.get_any_age(CACHE_NS, path)
        if hit and isinstance(hit[0], dict) and isinstance(hit[0].get("hosts"), dict):
            out.append((path, hit[0]["hosts"]))
    return out


async def _list_files(
    client: httpx.AsyncClient,
    owner: str,
    name: str,
    ref: str,
    head_date: list[datetime],
) -> tuple[str, list[tuple[str, str, int]]]:
    resp = await client.get(f"{GITHUB_API}/repos/{owner}/{name}/commits/{ref}")
    resp.raise_for_status()
    commit = resp.json()
    commit_oid = str(commit["sha"])
    head_date[0] = _parse_dt(str(commit["commit"]["committer"]["date"]))
    tree_oid = str(commit["commit"]["tree"]["sha"])
    resp = await client.get(
        f"{GITHUB_API}/repos/{owner}/{name}/git/trees/{tree_oid}",
        params={"recursive": "1"},
    )
    resp.raise_for_status()
    tree = resp.json()
    files = [
        (str(e["path"]), str(e["sha"]), int(e.get("size") or 0))
        for e in tree.get("tree", [])
        if e.get("type") == "blob" and _include_path(str(e.get("path", "")))
    ]
    return commit_oid, files


async def _post_graphql(client: httpx.AsyncClient, payload: dict) -> dict | None:
    delay = 2.0
    for attempt in range(MAX_ATTEMPTS):
        try:
            resp = await client.post(
                GITHUB_GRAPHQL, json=payload, timeout=httpx.Timeout(60.0)
            )
        except httpx.HTTPError:
            resp = None
        if resp is not None and resp.status_code == 200:
            try:
                body = resp.json()
            except ValueError:
                body = None
            if isinstance(body, dict) and (
                body.get("data") is not None or attempt == MAX_ATTEMPTS - 1
            ):
                return body
        elif resp is not None and resp.status_code in (401, 404):
            return None
        elif resp is not None and resp.status_code in (403, 429):
            retry_after = resp.headers.get("retry-after")
            try:
                delay = min(60.0, float(retry_after)) if retry_after else delay
            except ValueError:
                pass
        if attempt < MAX_ATTEMPTS - 1:
            await asyncio.sleep(delay)
            delay *= 2
    return None


async def _blame_batch(
    client: httpx.AsyncClient,
    owner: str,
    name: str,
    commit_oid: str,
    batch: list[tuple[str, str, int]],
    cache: SignalCache,
    per_file: dict[str, dict[str, str]],
    stats: dict,
) -> bool:
    """Blame every file in `batch` with one aliased GraphQL query. Returns False if any file is missing."""
    variables: dict[str, Any] = {"owner": owner, "name": name, "oid": commit_oid}
    for i, (path, _blob, _size) in enumerate(batch):
        variables[f"p{i}"] = path
    body = await _post_graphql(
        client,
        {"query": _blame_query([p for p, _, _ in batch]), "variables": variables},
    )
    if not body:
        return False
    data = body.get("data") or {}
    rl = data.get("rateLimit") or {}
    if isinstance(rl.get("cost"), int):
        stats["cost"] += rl["cost"]
        stats["remaining"] = rl.get("remaining")
    obj = ((data.get("repository") or {}).get("object")) or {}

    texts = await asyncio.gather(
        *(_raw_text(client, owner, name, commit_oid, p) for p, _, _ in batch)
    )
    all_ok = True
    for i, (path, blob, _size) in enumerate(batch):
        blame = obj.get(f"f{i}")
        text = texts[i]
        if (
            not isinstance(blame, dict)
            or not isinstance(blame.get("ranges"), list)
            or text is None
        ):
            all_ok = False
            continue
        hosts = map_hosts_to_dates(text, blame["ranges"])
        per_file[path] = hosts
        cache.put(
            CACHE_NS, path, {"schema": CACHE_SCHEMA, "blob": blob, "hosts": hosts}
        )
        stats["blamed"] += 1
    return all_ok


async def _raw_text(
    client: httpx.AsyncClient, owner: str, name: str, commit_oid: str, path: str
) -> str | None:
    return await _raw_text_url(
        client, f"{GITHUB_RAW}/{owner}/{name}/{commit_oid}/{path}"
    )


async def _raw_text_url(client: httpx.AsyncClient, url: str) -> str | None:
    for attempt in range(MAX_ATTEMPTS):
        try:
            resp = await client.get(url)
            if resp.status_code == 200:
                return resp.text
            if resp.status_code == 404:
                return None
        except httpx.HTTPError:
            pass
        if attempt < MAX_ATTEMPTS - 1:
            await asyncio.sleep(1.5 * (attempt + 1))
    return None
