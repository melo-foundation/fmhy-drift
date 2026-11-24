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

STATIC_PAGE_EXTENSIONS = (
    ".html",
    ".htm",
    ".php",
    ".asp",
    ".aspx",
    ".md",
    ".txt",
    ".pdf",
    ".xml",
    ".json",
)

MIRROR_OR_SECONDARY_LABELS = frozenset(
    {
        "mirror",
        "mirrors",
        "alt",
        "alternative",
        "onion",
        ".onion",
        "tor",
        "i2p",
        "beta",
        "legacy",
        "old",
        "v2",
        "lite",
        "web",
        "app",
        "cli",
        "gui",
        "desktop",
        "mobile",
        "android",
        "ios",
        "windows",
        "linux",
        "mac",
        "macos",
        "extension",
        "addon",
        "userscript",
        "github",
        "gitlab",
        "codeberg",
        "source",
        "repo",
        "docs",
        "documentation",
        "wiki",
        "guide",
        "releases",
        "download",
    }
)

SHARED_HOSTING_SECOND_LEVEL = frozenset(
    {
        "co",
        "com",
        "org",
        "net",
        "edu",
        "gov",
        "ac",
        "github",
        "gitlab",
        "bitbucket",
        "codeberg",
        "vercel",
        "netlify",
        "pages",
        "workers",
        "cloudflare",
        "onrender",
        "herokuapp",
        "fly",
        "railway",
        "surge",
        "glitch",
        "replit",
        "itch",
        "substack",
        "medium",
        "wordpress",
        "blogspot",
        "tumblr",
        "notion",
        "gitbook",
        "readthedocs",
        "carrd",
        "weebly",
        "wixsite",
        "firebaseapp",
        "web",
        "appspot",
        "azurewebsites",
        "amazonaws",
        "cloudfront",
        "google",
        "apple",
        "microsoft",
        "mozilla",
        "sourceforge",
        "archive",
    }
)

GENERIC_SUBDOMAINS = frozenset(
    {
        "www",
        "app",
        "api",
        "docs",
        "wiki",
        "blog",
        "mail",
        "chat",
        "forum",
        "forums",
        "community",
        "status",
        "cdn",
        "dl",
        "download",
        "downloads",
        "files",
        "media",
        "play",
        "web",
        "beta",
        "dev",
        "staging",
        "demo",
        "portal",
        "auth",
        "login",
        "store",
        "shop",
        "news",
        "help",
        "support",
    }
)


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
    if (
        segments
        and GITHUB_SLUG_PART_RE.match(segments[0])
        and not segments[0].lower().endswith(STATIC_PAGE_EXTENSIONS)
    ):
        return owner, segments[0]
    return owner, f"{owner}.github.io"


def extract_custom_domain_repo_candidate(url: str, entry_title: str) -> tuple[str, str] | None:
    """Infer speculative `(owner, repo)` from `<project>.<owner>.<tld>` (e.g. `abztract.arhamkhnz.com`)."""
    try:
        parts = urlsplit(url.strip())
    except ValueError:
        return None

    host = (parts.hostname or "").lower().rstrip(".")
    if host.startswith("www."):
        host = host[4:]

    labels = host.split(".")
    if len(labels) != 3:
        return None

    sub, middle, _tld = labels
    if sub in GENERIC_SUBDOMAINS or middle in SHARED_HOSTING_SECOND_LEVEL:
        return None
    if len(sub) < 3 or len(middle) < 3:
        return None
    if not GITHUB_SLUG_PART_RE.match(sub) or not GITHUB_SLUG_PART_RE.match(middle):
        return None

    # Require `sub` to match the entry title so unrelated subdomains aren't guessed
    norm_title = re.sub(r"[^a-z0-9]+", "", entry_title.lower())
    norm_sub = re.sub(r"[^a-z0-9]+", "", sub.lower())
    if not norm_title or (norm_sub not in norm_title and norm_title not in norm_sub):
        return None

    return middle, sub


def extract_github_cdn_repo(url: str) -> tuple[str, str] | None:
    """Derive (owner, repo) from raw/CDN URLs (raw.githubusercontent, jsDelivr, githack, statically)."""
    try:
        parts = urlsplit(url.strip())
    except ValueError:
        return None

    host = (parts.hostname or "").lower()
    segments = [seg for seg in parts.path.split("/") if seg]

    owner = ""
    repo = ""

    if host in {
        "raw.githubusercontent.com",
        "codeload.github.com",
        "raw.githack.com",
        "rawcdn.githack.com",
    }:
        if len(segments) >= 2:
            owner, repo = segments[0], segments[1]
    elif host in {"cdn.jsdelivr.net", "fastly.jsdelivr.net", "gcore.jsdelivr.net"}:
        # Format: /gh/<owner>/<repo>@<version>/file... or /gh/<owner>/<repo>/file...
        if len(segments) >= 3 and segments[0].lower() == "gh":
            owner = segments[1]
            repo = segments[2].split("@", 1)[0]
    elif host == "cdn.statically.io":
        # Format: /gh/<owner>/<repo>/<branch>/file...
        if len(segments) >= 3 and segments[0].lower() == "gh":
            owner, repo = segments[1], segments[2].split("@", 1)[0]

    if repo.endswith(".git"):
        repo = repo[:-4]

    if not owner or not repo:
        return None
    if not GITHUB_SLUG_PART_RE.match(owner) or not GITHUB_SLUG_PART_RE.match(repo):
        return None
    if owner.lower() == "fmhy":
        return None

    return owner, repo


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
    # Strip markdown formatting and zero-width word-joiner characters used in FMHY
    text = (
        raw.replace("**", "")
        .replace("__", "")
        .replace("`", "")
        .replace("\u2060", "")
        .replace("\u200b", "")
        .replace("\ufeff", "")
        .strip()
    )
    return text


def _is_mirror_label(label: str) -> bool:
    cleaned = label.strip().strip("[]()").lower()
    if not cleaned or cleaned.isdigit():
        return True
    return cleaned in MIRROR_OR_SECONDARY_LABELS


def _split_bullet_description(body: str) -> tuple[str, str]:
    """Split an FMHY bullet into `(left_tools_part, right_desc_part)` on the first ` - ` outside links."""
    depth_bracket = 0
    depth_paren = 0
    for i in range(len(body) - 2):
        ch = body[i]
        if ch == "[":
            depth_bracket += 1
        elif ch == "]" and depth_bracket > 0:
            depth_bracket -= 1
        elif ch == "(":
            depth_paren += 1
        elif ch == ")" and depth_paren > 0:
            depth_paren -= 1
        elif depth_bracket == 0 and depth_paren == 0 and body[i : i + 3] == " - ":
            return body[:i], body[i + 3 :]
    return body, ""


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

        starred = (
            ("⭐" in body) or ("🌟" in body) or (":star:" in body) or (":glowing_star:" in body)
        )

        left_part, right_part = _split_bullet_description(body)
        left_matches = MD_LINK_RE.findall(left_part)
        right_matches = MD_LINK_RE.findall(right_part) if right_part else []

        # Group `left_part` links into distinct tools (splitting `[Tool A](...) or [Tool B](...)`,
        # while keeping `[2]`, `[Mirror]`, `[GitHub]` attached to the current tool).
        tool_groups: list[dict] = []

        def _ingest_link(group: dict, raw_url: str, title_hint: str) -> None:
            url = clean_url(raw_url)
            if not url:
                return

            gh_repo = extract_github_repo(url)
            if gh_repo is not None:
                ref = GitHubRef(owner=gh_repo[0], repo=gh_repo[1], source="explicit_markdown")
                if ref.key not in group["seen_gh"]:
                    group["seen_gh"].add(ref.key)
                    group["github_refs"].append(ref)
                return

            host = (urlsplit(url).hostname or "").lower()
            if host in {"github.com", "www.github.com", "gist.github.com"}:
                return
            if _is_social_or_self_url(url, include_social=include_social):
                return

            if url not in group["seen_sites"]:
                group["seen_sites"].add(url)
                group["site_urls"].append(url)

            gh_pages = extract_github_pages_repo(url)
            if gh_pages is not None:
                ref = GitHubRef(
                    owner=gh_pages[0],
                    repo=gh_pages[1],
                    source="github_pages",
                    discovered_from_url=url,
                )
                if ref.key not in group["seen_gh"]:
                    group["seen_gh"].add(ref.key)
                    group["github_refs"].append(ref)
                return

            gh_cdn = extract_github_cdn_repo(url)
            if gh_cdn is not None:
                ref = GitHubRef(
                    owner=gh_cdn[0],
                    repo=gh_cdn[1],
                    source="github_cdn",
                    discovered_from_url=url,
                )
                if ref.key not in group["seen_gh"]:
                    group["seen_gh"].add(ref.key)
                    group["github_refs"].append(ref)
                return

            dom_cand = extract_custom_domain_repo_candidate(url, title_hint)
            if dom_cand is not None:
                ref = GitHubRef(
                    owner=dom_cand[0],
                    repo=dom_cand[1],
                    source="domain_heuristic",
                    discovered_from_url=url,
                )
                if ref.key not in group["seen_gh"]:
                    group["seen_gh"].add(ref.key)
                    group["github_refs"].append(ref)

        for raw_label, raw_url in left_matches:
            label = _clean_label(raw_label)
            if not tool_groups or (not _is_mirror_label(label) and tool_groups[-1]["title"]):
                tool_groups.append(
                    {
                        "title": "" if _is_mirror_label(label) else label,
                        "site_urls": [],
                        "seen_sites": set(),
                        "github_refs": [],
                        "seen_gh": set(),
                    }
                )
            elif not tool_groups[-1]["title"] and not _is_mirror_label(label):
                tool_groups[-1]["title"] = label

            _ingest_link(tool_groups[-1], raw_url, tool_groups[-1]["title"] or label)

        if not tool_groups:
            tool_groups.append(
                {
                    "title": "",
                    "site_urls": [],
                    "seen_sites": set(),
                    "github_refs": [],
                    "seen_gh": set(),
                }
            )

        # Attach `right_part` links (e.g. `/ [GitHub](...)`) to the matching tool group
        for raw_label, raw_url in right_matches:
            label = _clean_label(raw_label)
            target_group = tool_groups[0]
            if len(tool_groups) > 1:
                url_low = raw_url.lower()
                for grp in tool_groups:
                    t_clean = re.sub(r"[^a-z0-9]+", "", grp["title"].lower())
                    if t_clean and len(t_clean) >= 3 and t_clean in re.sub(r"[^a-z0-9]+", "", url_low):
                        target_group = grp
                        break
            if not target_group["title"] and not _is_mirror_label(label):
                target_group["title"] = label
            _ingest_link(target_group, raw_url, target_group["title"] or label)

        subcategory = " > ".join(part for part in (h2, h3) if part)
        valid_groups = [g for g in tool_groups if g["site_urls"] or g["github_refs"]]

        for sub_idx, grp in enumerate(valid_groups):
            primary_title = grp["title"]
            if not primary_title:
                if grp["site_urls"]:
                    primary_title = urlsplit(grp["site_urls"][0]).hostname or grp["site_urls"][0]
                else:
                    primary_title = grp["github_refs"][0].full_name

            suffix = f"#{sub_idx + 1}" if len(valid_groups) > 1 else ""
            entries.append(
                WikiEntry(
                    entry_id=f"{source_file}:{idx}{suffix}",
                    title=primary_title,
                    category=category,
                    subcategory=subcategory,
                    source_file=source_file,
                    line_number=idx,
                    starred=starred,
                    site_urls=grp["site_urls"],
                    github_refs=grp["github_refs"],
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
