"""Parser and fetcher for the FMHY markdown index (single-page API or local docs/)."""

from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import httpx

from fmhy_drift.models import GitHubRef, WikiEntry

DEFAULT_FMHY_SINGLE_PAGE_URL = "https://api.fmhy.net/single-page"

# Reserved first-path segments on github.com that are NOT owner/repo repositories
GITHUB_NON_REPO_OWNERS = frozenset(
    {
        "about",
        "account",
        "apps",
        "blog",
        "codespaces",
        "collections",
        "contact",
        "copilot",
        "customer-stories",
        "dashboard",
        "enterprise",
        "events",
        "explore",
        "features",
        "gist",
        "git-guides",
        "github-copilot",
        " issues",
        "issues",
        "join",
        "login",
        "logout",
        "marketplace",
        "Mobile",
        "mobile",
        "new",
        "nonprofit",
        "notifications",
        "orgs",
        "organizations",
        "pricing",
        "pulls",
        "readme",
        "search",
        "security",
        "session",
        "settings",
        "signup",
        "site",
        "sponsors",
        "team",
        "terms",
        "topics",
        "trending",
        "users",
        "watching",
    }
)

GITHUB_NON_REPO_NAMES = frozenset(
    {
        "followers",
        "following",
        "orgs",
        "packages",
        "projects",
        "repositories",
        "Sponsoring",
        "sponsoring",
        "sponsors",
        "stars",
    }
)

# Social / chat / donation links commonly appended on FMHY lines that aren't the tool's site
SOCIAL_AND_META_DOMAINS = frozenset(
    {
        "discord.gg",
        "discord.com",
        "discordapp.com",
        "t.me",
        "telegram.me",
        "telegram.org",
        "reddit.com",
        "old.reddit.com",
        "matrix.to",
        "patreon.com",
        "ko-fi.com",
        "buymeacoffee.com",
        "opencollective.com",
        "lemmy.world",
        "lemmy.ml",
        "mastodon.social",
        "x.com",
        "twitter.com",
        "youtube.com",
        "youtu.be",
    }
)

# FMHY internal navigation/mirror domains excluded from external site drift checks
FMHY_SELF_DOMAINS = frozenset(
    {
        "fmhy.net",
        "www.fmhy.net",
        "api.fmhy.net",
        "fmhy.pages.dev",
        "fmhy.vercel.app",
    }
)

HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*$")
BULLET_RE = re.compile(r"^(\s*)[*+-]\s+(.+)$")
FENCE_RE = re.compile(r"^\s*(`{3,}|~{3,})")

# Matches Markdown links `[label](url)` including one level of balanced parentheses inside the URL
MD_LINK_RE = re.compile(
    r"(?<!!)\[([^\[\]]*)\]\(\s*(https?://(?:[^\s()]+|\([^\s()]*\))+)(?:\s+[\"'][^\"']*[\"'])?\s*\)",
    re.IGNORECASE,
)

GITHUB_SLUG_PART_RE = re.compile(r"^[A-Za-z0-9._-]+$")


def _normalize_host(hostname: str | None) -> str:
    if not hostname:
        return ""
    host = hostname.lower().rstrip(".")
    if host.startswith("www."):
        return host[4:]
    return host


def clean_url(raw_url: str) -> str:
    """Normalize HTTP(S) URLs stripped of trailing markdown punctuation and fragments."""
    cleaned = raw_url.strip().rstrip(">")
    parts = urlsplit(cleaned)
    if parts.scheme.lower() not in {"http", "https"} or not parts.hostname:
        return ""
    path = parts.path or "/"
    return urlunsplit((parts.scheme.lower(), parts.netloc, path, parts.query, ""))


def extract_github_repo(url: str) -> tuple[str, str] | None:
    """If `url` points to a GitHub repository (or subpage like /releases), return (owner, repo)."""
    try:
        parts = urlsplit(url.strip())
    except ValueError:
        return None

    host = (parts.hostname or "").lower()
    if host not in {"github.com", "www.github.com"}:
        return None

    segments = [seg for seg in parts.path.split("/") if seg]
    if len(segments) < 2:
        return None

    owner, repo = segments[0], segments[1]
    if repo.endswith(".git"):
        repo = repo[:-4]

    if not owner or not repo:
        return None
    if owner in GITHUB_NON_REPO_OWNERS or owner.lower() in GITHUB_NON_REPO_OWNERS:
        return None
    if repo in GITHUB_NON_REPO_NAMES or repo.lower() in GITHUB_NON_REPO_NAMES:
        return None
    if not GITHUB_SLUG_PART_RE.match(owner) or not GITHUB_SLUG_PART_RE.match(repo):
        return None
    if owner.lower() == "fmhy":
        return None

    return owner, repo


def extract_github_pages_repo(url: str) -> tuple[str, str] | None:
    """Derive (owner, repo) from `https://<owner>.github.io/<repo>` or `<owner>.github.io`."""
    try:
        parts = urlsplit(url.strip())
    except ValueError:
        return None

    host = (parts.hostname or "").lower()
    if not host.endswith(".github.io"):
        return None

    owner = host[: -len(".github.io")]
    if not owner or "." in owner or not GITHUB_SLUG_PART_RE.match(owner):
        return None
    if owner.lower() == "fmhy":
        return None

    segments = [seg for seg in parts.path.split("/") if seg]
    if segments and GITHUB_SLUG_PART_RE.match(segments[0]) and "." not in segments[0]:
        return owner, segments[0]
    return owner, f"{owner}.github.io"


def _is_social_or_self_url(url: str, *, include_social: bool) -> bool:
    parts = urlsplit(url)
    raw_host = (parts.hostname or "").lower()
    norm_host = _normalize_host(raw_host)

    if raw_host in FMHY_SELF_DOMAINS or norm_host in FMHY_SELF_DOMAINS:
        return True

    if raw_host == "rentry.co" or raw_host == "rentry.org":
        first_seg = next((s for s in parts.path.split("/") if s), "").lower()
        if first_seg.startswith("fmhy"):
            return True

    if not include_social:
        if raw_host in SOCIAL_AND_META_DOMAINS or norm_host in SOCIAL_AND_META_DOMAINS:
            return True
        if norm_host.endswith(".reddit.com") or norm_host.endswith(".discord.com"):
            return True

    return False


def _clean_heading_text(raw: str) -> str:
    # Strip markdown links, VitePress anchors `{#slug}`, and formatting chars
    text = re.sub(r"\{#[^}]+\}", "", raw)
    text = re.sub(r"\[([^\[\]]+)\]\([^)]+\)", r"\1", text)
    text = text.replace("**", "").replace("__", "").strip()
    return text


def _clean_label(raw: str) -> str:
    text = raw.replace("**", "").replace("__", "").replace("`", "").strip()
    return text


def parse_markdown_content(
    markdown: str,
    *,
    source_file: str = "single-page.md",
    default_category: str = "Uncategorized",
    include_social: bool = False,
) -> list[WikiEntry]:
    """Parse FMHY markdown content into structured `WikiEntry` items."""
    lines = markdown.splitlines()
    entries: list[WikiEntry] = []

    in_frontmatter = False
    in_fence = False
    category = default_category
    h2 = ""
    h3 = ""

    for idx, line in enumerate(lines, start=1):
        stripped = line.strip()

        # Handle YAML frontmatter at top of file
        if idx == 1 and stripped == "---":
            in_frontmatter = True
            continue
        if in_frontmatter:
            if stripped in {"---", "..."}:
                in_frontmatter = False
            continue

        # Handle fenced code blocks
        if FENCE_RE.match(line):
            in_fence = not in_fence
            continue
        if in_fence:
            continue

        # Track headings
        heading_match = HEADING_RE.match(line)
        if heading_match:
            level = len(heading_match.group(1))
            heading_title = _clean_heading_text(heading_match.group(2))
            if not heading_title:
                continue
            if level == 1:
                category = heading_title
                h2 = ""
                h3 = ""
            elif level == 2:
                h2 = heading_title
                h3 = ""
            elif level >= 3:
                h3 = heading_title
            continue

        # Only extract FMHY bullet items
        bullet_match = BULLET_RE.match(line)
        if not bullet_match:
            continue

        body = bullet_match.group(2)
        matches = MD_LINK_RE.findall(body)
        if not matches:
            continue

        starred = ("⭐" in body) or ("🌟" in body) or (":star:" in body) or (":glowing_star:" in body)

        site_urls: list[str] = []
        seen_sites: set[str] = set()
        github_refs: list[GitHubRef] = []
        seen_gh_keys: set[str] = set()
        primary_title = ""

        for raw_label, raw_url in matches:
            url = clean_url(raw_url)
            if not url:
                continue

            label = _clean_label(raw_label)
            if not primary_title and label and not label.isdigit():
                primary_title = label

            gh_repo = extract_github_repo(url)
            if gh_repo is not None:
                ref = GitHubRef(owner=gh_repo[0], repo=gh_repo[1], source="explicit_markdown")
                if ref.key not in seen_gh_keys:
                    seen_gh_keys.add(ref.key)
                    github_refs.append(ref)
                continue

            # Ignore non-repo github.com links (e.g. github.com/orgs/..., gist, etc.) as site targets
            host = (urlsplit(url).hostname or "").lower()
            if host in {"github.com", "www.github.com", "gist.github.com"}:
                continue

            if _is_social_or_self_url(url, include_social=include_social):
                continue

            if url not in seen_sites:
                seen_sites.add(url)
                site_urls.append(url)

            gh_pages = extract_github_pages_repo(url)
            if gh_pages is not None:
                ref = GitHubRef(
                    owner=gh_pages[0],
                    repo=gh_pages[1],
                    source="github_pages",
                    discovered_from_url=url,
                )
                if ref.key not in seen_gh_keys:
                    seen_gh_keys.add(ref.key)
                    github_refs.append(ref)

        if not site_urls and not github_refs:
            continue

        if not primary_title:
            if site_urls:
                primary_title = urlsplit(site_urls[0]).hostname or site_urls[0]
            else:
                primary_title = github_refs[0].full_name

        subcategory = " > ".join(part for part in (h2, h3) if part)
        entry_id = f"{source_file}:{idx}"

        entries.append(
            WikiEntry(
                entry_id=entry_id,
                title=primary_title,
                category=category,
                subcategory=subcategory,
                source_file=source_file,
                line_number=idx,
                starred=starred,
                site_urls=site_urls,
                github_refs=github_refs,
                raw_line=line.strip(),
            )
        )

    return entries


def load_local_index(
    path: Path,
    *,
    include_social: bool = False,
) -> list[WikiEntry]:
    """Load FMHY markdown from a single `.md` file or a directory of `.md` files."""
    if path.is_file():
        default_cat = path.stem.replace("-", " ").replace("_", " ").title()
        return parse_markdown_content(
            path.read_text(encoding="utf-8"),
            source_file=path.name,
            default_category=default_cat,
            include_social=include_social,
        )

    if path.is_dir():
        entries: list[WikiEntry] = []
        for md_file in sorted(path.rglob("*.md")):
            if md_file.name.lower() in {"readme.md", "contributing.md", "changelog.md"}:
                continue
            rel_name = str(md_file.relative_to(path))
            default_cat = md_file.stem.replace("-", " ").replace("_", " ").title()
            entries.extend(
                parse_markdown_content(
                    md_file.read_text(encoding="utf-8"),
                    source_file=rel_name,
                    default_category=default_cat,
                    include_social=include_social,
                )
            )
        return entries

    raise FileNotFoundError(f"FMHY input path does not exist: {path}")


async def fetch_remote_index(
    url: str = DEFAULT_FMHY_SINGLE_PAGE_URL,
    *,
    proxy_url: str | None = None,
    include_social: bool = False,
    timeout: float = 45.0,
    transport: httpx.AsyncBaseTransport | None = None,
) -> list[WikiEntry]:
    """Fetch the compiled FMHY single-page markdown from `api.fmhy.net/single-page`."""
    client_kwargs: dict = {
        "timeout": httpx.Timeout(timeout),
        "follow_redirects": True,
        "trust_env": False,
        "headers": {
            "User-Agent": "fmhy-drift-checker/0.1 (+https://github.com/fmhy)",
            "Accept": "text/markdown, text/plain, */*",
        },
    }
    if transport is not None:
        client_kwargs["transport"] = transport
    elif proxy_url:
        client_kwargs["proxy"] = proxy_url

    async with httpx.AsyncClient(**client_kwargs) as client:
        resp = await client.get(url)
        resp.raise_for_status()
        content = resp.text

    return parse_markdown_content(
        content,
        source_file="single-page",
        default_category="FMHY",
        include_social=include_social,
    )
