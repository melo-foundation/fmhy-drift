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
        "issues",
        "join",
        "login",
        "logout",
        "marketplace",
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
        "sponsoring",
        "sponsors",
        "stars",
    }
)

# Subpaths on github.com/owner/repo/... that are citations/discussions rather than the tool itself
GITHUB_CITATION_SUBPATHS = frozenset(
    {
        "issues",
        "pull",
        "pulls",
        "discussions",
        "wiki",
        "commit",
        "commits",
        "compare",
    }
)

# Social / chat / donation / scan links commonly appended on FMHY lines that aren't the tool's site
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
        "redd.it",
        "matrix.to",
        "patreon.com",
        "ko-fi.com",
        "buymeacoffee.com",
        "opencollective.com",
        "lemmy.world",
        "lemmy.ml",
        "join-lemmy.org",
        "mastodon.social",
        "joinmastodon.org",
        "bsky.app",
        "facebook.com",
        "twitch.tv",
        "x.com",
        "twitter.com",
        "youtube.com",
        "youtu.be",
        "virustotal.com",
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

# Route directories on `<owner>.github.io/<route>/...` that belong to `<owner>/<owner>.github.io`
PAGES_ROUTE_SEGMENTS = frozenset(
    {
        "blog",
        "posts",
        "post",
        "docs",
        "doc",
        "wiki",
        "guide",
        "guides",
        "archive",
        "archives",
        "articles",
        "article",
        "pages",
        "en",
        "en-us",
        "latest",
        "public",
        "assets",
        "static",
    }
)

DIRECT_REPO_LABELS = frozenset(
    {
        "github",
        "gitlab",
        "codeberg",
        "gitea",
        "forgejo",
        "bitbucket",
        "sourcehut",
        "sr.ht",
        "sourceforge",
        "pagure",
        "source",
        "source code",
        "repo",
        "repository",
        "code",
        "git",
    }
)

# Public / official FOSS GitLab instances linked in FMHY
GITLAB_HOSTS = frozenset(
    {
        "gitlab.com",
        "gitlab.gnome.org",
        "gitlab.freedesktop.org",
        "gitlab.futo.org",
        "invent.kde.org",
        "gitlab.torproject.org",
        "gitlab.redox-os.org",
        "gitlab.zrythm.org",
        "framagit.org",
        "0xacab.org",
        "salsa.debian.org",
        "heptapod.net",
        "gitgud.io",
        "code.briarproject.org",
        "git.jami.net",
        "git.openprivacy.ca",
        "i2pgit.org",
    }
)

GITLAB_NON_REPO_FIRST_SEGMENTS = frozenset(
    {
        "-",
        "about",
        "admin",
        "api",
        "dashboard",
        "explore",
        "groups",
        "help",
        "jwt",
        "oauth",
        "orgs",
        " profile",
        "public",
        "search",
        "snippets",
        "topics",
        "users",
    }
)

GITLAB_CITATION_SUBPATHS = frozenset(
    {
        "issues",
        "merge_requests",
        "wikis",
        "snippets",
        "commit",
        "commits",
        "compare",
        "milestones",
        "pipelines",
        "jobs",
    }
)

# Public / official Forgejo & Gitea instances linked in FMHY
FORGEJO_GITEA_HOSTS = frozenset(
    {
        "codeberg.org",
        "gitea.com",
        "gitea.angry.im",
        "forgejo.sny.sh",
        "codefloe.com",
        "git.disroot.org",
        "notabug.org",
        "projects.blender.org",
        "git.sdf.org",
        "source.mapcomplete.org",
        "git.hostux.net",
        "git.private.coffee",
        "git.lolcat.ca",
    }
)

FORGEJO_NON_REPO_FIRST_SEGMENTS = frozenset(
    {
        "admin",
        "api",
        "assets",
        "explore",
        "issues",
        "login",
        "milestones",
        "notifications",
        "org",
        "orgs",
        "pulls",
        "repo",
        "swagger",
        "user",
        "users",
    }
)

FORGEJO_CITATION_SUBPATHS = frozenset(
    {
        "issues",
        "pulls",
        "wiki",
        "commit",
        "commits",
        "compare",
        "activity",
        "stars",
        "watchers",
        "forks",
    }
)

SOURCEFORGE_NON_PROJECT_SUBDOMAINS = frozenset(
    {
        "www",
        "sourceforge",
        "sf",
        "lists",
        "downloads",
        "prdownloads",
        "master",
        "vorboss",
        "jaist",
        "ncu",
        "netcologne",
        "ufpr",
    }
)

NUMERIC_OR_MIRROR_LABELS = frozenset(
    {
        "mirror",
        "mirrors",
        "alt",
        "alternative",
        "onion",
        ".onion",
        "tor",
        "i2p",
        "v2",
        "docs",
        "documentation",
        "wiki",
        "releases",
        "download",
    }
)

NON_TOOL_SOCIAL_LABELS = frozenset(
    {
        "discord",
        "telegram",
        "subreddit",
        "reddit",
        "matrix",
        "mastodon",
        "lemmy",
        "x",
        "twitter",
        "bluesky",
        "youtube",
        "twitch",
        "chat",
        "invite",
        "note",
        "warning",
        "vt",
        "virustotal",
    }
)

SLASH_MODIFIER_LABELS = frozenset(
    {
        "gui",
        "cli",
        "webui",
        "web ui",
        "web",
        "app",
        "apps",
        "desktop",
        "desktop app",
        "mobile",
        "mobile app",
        "android",
        "ios",
        "windows",
        "linux",
        "macos",
        "mac",
        "firefox",
        "chrome",
        "safari",
        "extension",
        "extensions",
        "addon",
        "addons",
        "add-ons",
        "userscript",
        "script",
        "scripts",
        "bookmarklet",
        "plugin",
        "plugins",
        "themes",
        "theme",
        "fonts",
        "mods",
        "resources",
        "guide",
        "guides",
        "colab",
        "notebooks",
        "instances",
        "servers",
        "forum",
        "status",
        "downloader",
        "host format",
        "beta",
        "nightly",
        "demo",
        "fork",
        "forks",
        "ports",
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
        "withgoogle",
        "apple",
        "microsoft",
        "visualstudio",
        "office",
        "mozilla",
        "adobe",
        "brave",
        "duckduckgo",
        "youtube",
        "sourceforge",
        "archive",
        "neocities",
        "fandom",
        "wikia",
        "wikidot",
        "miraheze",
        "wikimedia",
        "wikipedia",
        "libretexts",
        "b-cdn",
        "hacks",
        "cfw",
        "qzz",
        "wiki",
        "ucoz",
        "dreamwidth",
        "altervista",
        "duckdns",
        "is-a",
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
        "search",
        "discuss",
        "plugins",
        "modules",
        "images",
        "lite",
        "html",
        "playground",
        "mobile",
        "code",
        "map",
        "maps",
        "paste",
        "report",
        "email",
        "convert",
        "discord",
        "music",
        "radio",
        "pdf",
        "free",
        "live",
        "read",
        "guide",
        "guides",
        "tools",
        "editor",
        "books",
        "games",
        "video",
        "tv",
        "archive",
        "open",
    }
)

HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*$")
BULLET_RE = re.compile(r"^(\s*)[*+-]\s+(.+)$")
FENCE_RE = re.compile(r"^\s*(`{3,}|~{3,})")
BOLD_SPAN_RE = re.compile(r"\*\*(.+?)\*\*|__(.+?)__")

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
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), path, parts.query, ""))


def extract_github_repo(url: str) -> tuple[str, str] | None:
    """If `url` points to a GitHub repository (not an issue/discussion citation), return (owner, repo)."""
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

    if len(segments) >= 3 and segments[2].lower() in GITHUB_CITATION_SUBPATHS:
        return None

    owner, repo = segments[0], segments[1]
    if repo.endswith(".git"):
        repo = repo[:-4]

    if not owner or not repo:
        return None
    if owner.lower() in GITHUB_NON_REPO_OWNERS:
        return None
    if repo.lower() in GITHUB_NON_REPO_NAMES:
        return None
    if not GITHUB_SLUG_PART_RE.match(owner) or not GITHUB_SLUG_PART_RE.match(repo):
        return None
    if owner.lower() == "fmhy":
        return None

    return owner, repo


def extract_alt_forge_repo(url: str) -> GitHubRef | None:
    """Extract explicit repository `GitHubRef` for GitLab, Codeberg/Forgejo/Gitea, sr.ht, Bitbucket, Pagure, or SourceForge."""
    try:
        parts = urlsplit(url.strip())
    except ValueError:
        return None

    host = _normalize_host(parts.hostname)
    if not host:
        return None

    raw_segments = [seg for seg in parts.path.split("/") if seg]
    if not raw_segments:
        return None

    # 1. GitLab (gitlab.com + known self-hosted FOSS GitLab instances)
    if host in GITLAB_HOSTS:
        first_low = raw_segments[0].lower()
        if first_low in GITLAB_NON_REPO_FIRST_SEGMENTS:
            return None

        if "/-/" in parts.path:
            before_dash, after_dash = parts.path.split("/-/", 1)
            after_segs = [s for s in after_dash.split("/") if s]
            if after_segs and after_segs[0].lower() in GITLAB_CITATION_SUBPATHS:
                return None
            proj_segs = [s for s in before_dash.split("/") if s]
        else:
            # Handle legacy GitLab URLs without `/-/` (e.g. `/owner/repo/wikis/...` or `/owner/repo/tree/main`)
            cut_idx = len(raw_segments)
            for idx in range(2, len(raw_segments)):
                low_s = raw_segments[idx].lower()
                if low_s in GITLAB_CITATION_SUBPATHS:
                    return None
                if low_s in {"tree", "blob", "raw", "releases", "tags", "packages"}:
                    cut_idx = idx
                    break
            proj_segs = raw_segments[:cut_idx]

        if len(proj_segs) < 2:
            return None
        repo = proj_segs[-1][:-4] if proj_segs[-1].endswith(".git") else proj_segs[-1]
        owner = "/".join(proj_segs[:-1])
        if not repo or not all(GITHUB_SLUG_PART_RE.match(s) for s in [*proj_segs[:-1], repo]):
            return None
        return GitHubRef(
            owner=owner,
            repo=repo,
            source="explicit_markdown",
            forge="gitlab",
            forge_host=host,
        )

    # 2. Codeberg / Forgejo / Gitea
    if host in FORGEJO_GITEA_HOSTS:
        if len(raw_segments) < 2:
            return None
        if raw_segments[0].lower() in FORGEJO_NON_REPO_FIRST_SEGMENTS:
            return None
        if len(raw_segments) >= 3 and raw_segments[2].lower() in FORGEJO_CITATION_SUBPATHS:
            return None
        owner = raw_segments[0]
        repo = raw_segments[1][:-4] if raw_segments[1].endswith(".git") else raw_segments[1]
        if not owner or not repo or not GITHUB_SLUG_PART_RE.match(owner) or not GITHUB_SLUG_PART_RE.match(repo):
            return None
        return GitHubRef(
            owner=owner,
            repo=repo,
            source="explicit_markdown",
            forge="forgejo",
            forge_host=host,
        )

    # 3. SourceHut (`git.sr.ht/~owner/repo`, `hg.sr.ht/~owner/repo`, `sr.ht/~owner/repo`)
    if host in {"git.sr.ht", "hg.sr.ht", "sr.ht"}:
        if len(raw_segments) < 2 or not raw_segments[0].startswith("~"):
            return None
        owner = raw_segments[0]
        repo = raw_segments[1][:-4] if raw_segments[1].endswith(".git") else raw_segments[1]
        if len(owner) < 2 or not repo or not GITHUB_SLUG_PART_RE.match(owner[1:]) or not GITHUB_SLUG_PART_RE.match(repo):
            return None
        return GitHubRef(
            owner=owner,
            repo=repo,
            source="explicit_markdown",
            forge="srht",
            forge_host=host,
        )

    # 4. Bitbucket Cloud (`bitbucket.org/owner/repo`)
    if host == "bitbucket.org":
        if len(raw_segments) < 2:
            return None
        if raw_segments[0].lower() in {"account", "dashboard", "product", "repo", "site", "snippets", "workspaces"}:
            return None
        if len(raw_segments) >= 3 and raw_segments[2].lower() in {"issues", "pull-requests", "wiki", "commits"}:
            return None
        owner = raw_segments[0]
        repo = raw_segments[1][:-4] if raw_segments[1].endswith(".git") else raw_segments[1]
        if not GITHUB_SLUG_PART_RE.match(owner) or not GITHUB_SLUG_PART_RE.match(repo):
            return None
        return GitHubRef(
            owner=owner,
            repo=repo,
            source="explicit_markdown",
            forge="bitbucket",
            forge_host="bitbucket.org",
        )

    # 5. Pagure (`pagure.io/ns/repo` or `pagure.io/repo`)
    if host == "pagure.io":
        if raw_segments[0].lower() in {
            "about",
            "api",
            "browse",
            "dashboard",
            "fork",
            "group",
            "groups",
            "login",
            "new",
            "projects",
            "search",
            "settings",
            "ssh_info",
            "user",
            "users",
        }:
            return None
        if len(raw_segments) >= 2 and raw_segments[1].lower() not in {
            "issue",
            "issues",
            "pull-request",
            "pull-requests",
            "blob",
            "tree",
            "commits",
            "releases",
        }:
            owner, repo = raw_segments[0], raw_segments[1]
        else:
            owner, repo = "_", raw_segments[0]
        repo = repo[:-4] if repo.endswith(".git") else repo
        if not GITHUB_SLUG_PART_RE.match(repo):
            return None
        return GitHubRef(
            owner=owner,
            repo=repo,
            source="explicit_markdown",
            forge="pagure",
            forge_host="pagure.io",
        )

    # 6. SourceForge (`sourceforge.net/projects/<slug>` or `sourceforge.net/p/<slug>`)
    if host == "sourceforge.net":
        if len(raw_segments) >= 2 and raw_segments[0].lower() in {"projects", "p"}:
            slug = raw_segments[1].lower()
            if slug and GITHUB_SLUG_PART_RE.match(slug):
                return GitHubRef(
                    owner="projects",
                    repo=slug,
                    source="explicit_markdown",
                    forge="sourceforge",
                    forge_host="sourceforge.net",
                )

    return None


def extract_alt_forge_pages_repo(url: str, entry_title: str = "") -> GitHubRef | None:
    """Derive `GitHubRef(source='forge_pages')` from `*.gitlab.io`, `*.codeberg.page`, `*.bitbucket.io`, or `*.sourceforge.io`."""
    try:
        parts = urlsplit(url.strip())
    except ValueError:
        return None

    host = (parts.hostname or "").lower().rstrip(".")
    segments = [seg for seg in parts.path.split("/") if seg]
    norm_title = re.sub(r"[^a-z0-9]+", "", entry_title.lower())

    def _pick_subpath_repo(default_repo: str) -> str:
        if segments and GITHUB_SLUG_PART_RE.match(segments[0]):
            seg0_low = segments[0].lower()
            if not seg0_low.endswith(STATIC_PAGE_EXTENSIONS):
                if seg0_low not in PAGES_ROUTE_SEGMENTS or (norm_title and norm_title == seg0_low):
                    return segments[0]
        return default_repo

    if host.endswith(".gitlab.io"):
        owner = host[: -len(".gitlab.io")]
        if owner and "." not in owner and GITHUB_SLUG_PART_RE.match(owner):
            repo = _pick_subpath_repo(f"{owner}.gitlab.io")
            return GitHubRef(
                owner=owner,
                repo=repo,
                source="forge_pages",
                discovered_from_url=url,
                entry_title=entry_title or None,
                forge="gitlab",
                forge_host="gitlab.com",
            )

    if ".pages.gitlab." in host:
        owner, gl_suffix = host.split(".pages.", 1)
        if owner and "." not in owner and gl_suffix in GITLAB_HOSTS:
            repo = _pick_subpath_repo(owner)
            return GitHubRef(
                owner=owner,
                repo=repo,
                source="forge_pages",
                discovered_from_url=url,
                entry_title=entry_title or None,
                forge="gitlab",
                forge_host=gl_suffix,
            )

    if host.endswith(".codeberg.page"):
        owner = host[: -len(".codeberg.page")]
        if owner and "." not in owner and GITHUB_SLUG_PART_RE.match(owner):
            repo = _pick_subpath_repo("pages")
            return GitHubRef(
                owner=owner,
                repo=repo,
                source="forge_pages",
                discovered_from_url=url,
                entry_title=entry_title or None,
                forge="forgejo",
                forge_host="codeberg.org",
            )

    if host.endswith(".bitbucket.io"):
        owner = host[: -len(".bitbucket.io")]
        if owner and "." not in owner and GITHUB_SLUG_PART_RE.match(owner):
            repo = _pick_subpath_repo(f"{owner}.bitbucket.io")
            return GitHubRef(
                owner=owner,
                repo=repo,
                source="forge_pages",
                discovered_from_url=url,
                entry_title=entry_title or None,
                forge="bitbucket",
                forge_host="bitbucket.org",
            )

    for sf_suffix in (".sourceforge.io", ".sourceforge.net"):
        if host.endswith(sf_suffix):
            slug = host[: -len(sf_suffix)]
            if (
                slug
                and "." not in slug
                and slug not in SOURCEFORGE_NON_PROJECT_SUBDOMAINS
                and GITHUB_SLUG_PART_RE.match(slug)
            ):
                return GitHubRef(
                    owner="projects",
                    repo=slug,
                    source="forge_pages",
                    discovered_from_url=url,
                    entry_title=entry_title or None,
                    forge="sourceforge",
                    forge_host="sourceforge.net",
                )

    return None


def extract_github_pages_repo(url: str, entry_title: str = "") -> tuple[str, str] | None:
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
    if segments and GITHUB_SLUG_PART_RE.match(segments[0]):
        seg0_low = segments[0].lower()
        norm_title = re.sub(r"[^a-z0-9]+", "", entry_title.lower())
        if not seg0_low.endswith(STATIC_PAGE_EXTENSIONS):
            if seg0_low not in PAGES_ROUTE_SEGMENTS or (norm_title and norm_title == seg0_low):
                return owner, segments[0]

    return owner, f"{owner}.github.io"


def extract_github_cdn_repo(url: str) -> tuple[str, str] | None:
    """Derive (owner, repo) from raw/CDN/Colab URLs (raw.githubusercontent, jsDelivr, githack, Colab)."""
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
        if len(segments) >= 3 and segments[0].lower() == "gh":
            owner = segments[1]
            repo = segments[2].split("@", 1)[0]
    elif host == "cdn.statically.io":
        if len(segments) >= 3 and segments[0].lower() == "gh":
            owner, repo = segments[1], segments[2].split("@", 1)[0]
    elif host == "colab.research.google.com":
        if len(segments) >= 3 and segments[0].lower() == "github":
            owner, repo = segments[1], segments[2]

    if repo.endswith(".git"):
        repo = repo[:-4]

    if not owner or not repo:
        return None
    if not GITHUB_SLUG_PART_RE.match(owner) or not GITHUB_SLUG_PART_RE.match(repo):
        return None
    if owner.lower() == "fmhy":
        return None

    return owner, repo


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

    sub, middle, tld = labels
    if tld in {"onion", "edu", "gov", "mil", "int"}:
        return None
    if sub in GENERIC_SUBDOMAINS or middle in SHARED_HOSTING_SECOND_LEVEL:
        return None
    if len(sub) < 4 or len(middle) < 3:
        return None
    if not GITHUB_SLUG_PART_RE.match(sub) or not GITHUB_SLUG_PART_RE.match(middle):
        return None

    norm_title = re.sub(r"[^a-z0-9]+", "", entry_title.lower())
    norm_sub = re.sub(r"[^a-z0-9]+", "", sub.lower())
    norm_middle = re.sub(r"[^a-z0-9]+", "", middle.lower())

    # Reject `<section>.<brand>.com` titled `"<Brand> <Section>"` (e.g. `search.brave.com`)
    if norm_middle and norm_middle in norm_title and norm_title != norm_sub:
        return None

    # Require exact normalized title match (`abztract` == `abztract`)
    if not norm_title or norm_sub != norm_title:
        return None

    return middle, sub


def _is_social_or_self_url(url: str, *, include_social: bool) -> bool:
    parts = urlsplit(url)
    raw_host = (parts.hostname or "").lower()
    norm_host = _normalize_host(raw_host)

    if raw_host in FMHY_SELF_DOMAINS or norm_host in FMHY_SELF_DOMAINS:
        return True

    if raw_host in {"rentry.co", "rentry.org"}:
        first_seg = next((s for s in parts.path.split("/") if s), "").lower()
        if first_seg.startswith("fmhy"):
            return True

    if not include_social:
        if raw_host in SOCIAL_AND_META_DOMAINS or norm_host in SOCIAL_AND_META_DOMAINS:
            return True
        if (
            norm_host.endswith(".reddit.com")
            or norm_host.endswith(".discord.com")
            or norm_host.endswith(".youtube.com")
        ):
            return True

    return False


def _has_explicit_alt_forge(site_urls: list[str]) -> bool:
    for u in site_urls:
        host = (urlsplit(u).hostname or "").lower()
        if host in {"gitlab.com", "codeberg.org", "git.sr.ht"} or host.startswith("gitlab."):
            return True
    return False


def _clean_heading_text(raw: str) -> str:
    text = re.sub(r"\{#[^}]+\}", "", raw)
    text = re.sub(r"<[^>]+>", "", text)
    text = re.sub(r"\s*/\s*\[\d+\]\([^)]+\)", "", text)
    text = re.sub(r"\[([^\[\]]+)\]\([^)]+\)", r"\1", text)
    text = text.replace("**", "").replace("__", "").replace("`", "")
    text = re.sub(r"^[\s►▷▸▹◄◁•·\u2060\u200b\ufeff]+", "", text)
    return text.strip()


def _clean_label(raw: str) -> str:
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


def _is_numeric_or_mirror_label(label: str) -> bool:
    cleaned = label.strip().strip("[]()").lower()
    if not cleaned or cleaned.isdigit():
        return True
    return cleaned in NUMERIC_OR_MIRROR_LABELS


def _split_bullet_description(body: str) -> tuple[str, str]:
    """Split an FMHY bullet into `(left_tools_part, right_desc_part)` on first ` - ` / ` – ` / ` — `."""
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
        elif depth_bracket == 0 and depth_paren == 0 and body[i : i + 3] in {" - ", " – ", " — "}:
            return body[:i], body[i + 3 :]
    return body, ""


def _bold_intervals(text: str) -> list[tuple[int, int]]:
    return [(m.start(), m.end()) for m in BOLD_SPAN_RE.finditer(text)]


def _is_match_bolded(
    m: re.Match[str],
    intervals: list[tuple[int, int]],
) -> bool:
    raw_label = m.group(1)
    if "**" in raw_label or "__" in raw_label:
        return True
    s, e = m.start(), m.end()
    return any(bs <= s and e <= be for bs, be in intervals)


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

        if idx == 1 and stripped == "---":
            in_frontmatter = True
            continue
        if in_frontmatter:
            if stripped in {"---", "..."}:
                in_frontmatter = False
            continue

        if FENCE_RE.match(line):
            in_fence = not in_fence
            continue
        if in_fence:
            continue

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

        bullet_match = BULLET_RE.match(line)
        if not bullet_match:
            continue

        body = bullet_match.group(2)
        if "[" not in body or "http" not in body.lower():
            continue

        left_part, right_part = _split_bullet_description(body)
        left_iter = list(MD_LINK_RE.finditer(left_part))
        right_iter = list(MD_LINK_RE.finditer(right_part)) if right_part else []
        if not left_iter and not right_iter:
            continue

        clean_left = re.sub(
            r"[*_`~⭐🌟🌐↪️⚠️🔑🔒📱💻🖥️\u2060\u200b\ufeff:a-z0-9_-]*",
            lambda m: "" if (m.group(0).startswith(":") and m.group(0).endswith(":")) else m.group(0),
            left_part,
        )
        clean_left = re.sub(r"[*_`~⭐🌟🌐↪️⚠️\u2060\u200b\ufeff]+", "", clean_left).strip()

        # Skip `* **Note** - ...` / `* **Warning** - ...` prose callouts
        if clean_left.lower() in {"note", "warning", "tip", "important", "caution", "info"}:
            continue

        # If `left_part` has no links: keep `**Collection** - [Subtool1](...)` lines,
        # but skip prose sentences like `* GetIntoPC - Caught with [malware](...)`
        is_header_collection = False
        header_parent_title = ""
        if not left_iter:
            if not re.match(r"^[\s*_\u2060\u200b]*\[", right_part):
                continue
            is_header_collection = True
            header_parent_title = clean_left

        line_has_star = (
            ("⭐" in body) or ("🌟" in body) or (":star:" in body) or (":glowing_star:" in body)
        )
        left_bold_spans = _bold_intervals(left_part)
        any_bolded_left = any(_is_match_bolded(m, left_bold_spans) for m in left_iter)

        def _new_group(title: str, bolded: bool = False) -> dict:
            return {
                "title": title,
                "bolded": bolded,
                "site_urls": [],
                "seen_sites": set(),
                "github_refs": [],
                "seen_gh": {},  # key -> index in github_refs
            }

        def _add_gh_ref(group: dict, ref: GitHubRef) -> None:
            existing_idx = group["seen_gh"].get(ref.key)
            if existing_idx is None:
                group["seen_gh"][ref.key] = len(group["github_refs"])
                group["github_refs"].append(ref)
            elif ref.source == "explicit_markdown" and group["github_refs"][existing_idx].source != "explicit_markdown":
                # Upgrade heuristic ref to `explicit_markdown` when explicit [GitHub] link also present!
                group["github_refs"][existing_idx] = ref

        def _ingest_link(
            group: dict,
            raw_url: str,
            title_hint: str,
            *,
            allow_domain_heuristic: bool = True,
        ) -> bool:
            """Parse and attach `raw_url` to `group`; return True if anything was kept."""
            url = clean_url(raw_url)
            if not url:
                return False

            gh_repo = extract_github_repo(url)
            if gh_repo is not None:
                _add_gh_ref(group, GitHubRef(owner=gh_repo[0], repo=gh_repo[1], source="explicit_markdown"))
                return True

            alt_repo = extract_alt_forge_repo(url)
            if alt_repo is not None:
                _add_gh_ref(group, alt_repo)
                return True

            host = (urlsplit(url).hostname or "").lower()
            if host in {"github.com", "www.github.com", "gist.github.com"}:
                return False
            if _is_social_or_self_url(url, include_social=include_social):
                return False

            if url not in group["seen_sites"]:
                group["seen_sites"].add(url)
                group["site_urls"].append(url)

            gh_pages = extract_github_pages_repo(url, entry_title=title_hint)
            if gh_pages is not None:
                _add_gh_ref(
                    group,
                    GitHubRef(
                        owner=gh_pages[0],
                        repo=gh_pages[1],
                        source="github_pages",
                        discovered_from_url=url,
                        entry_title=title_hint or None,
                    ),
                )
                return True

            alt_pages = extract_alt_forge_pages_repo(url, entry_title=title_hint)
            if alt_pages is not None:
                _add_gh_ref(group, alt_pages)
                return True

            gh_cdn = extract_github_cdn_repo(url)
            if gh_cdn is not None:
                _add_gh_ref(
                    group,
                    GitHubRef(
                        owner=gh_cdn[0],
                        repo=gh_cdn[1],
                        source="github_cdn",
                        discovered_from_url=url,
                    ),
                )
                return True

            if allow_domain_heuristic:
                dom_cand = extract_custom_domain_repo_candidate(url, title_hint)
                if dom_cand is not None:
                    _add_gh_ref(
                        group,
                        GitHubRef(
                            owner=dom_cand[0],
                            repo=dom_cand[1],
                            source="domain_heuristic",
                            discovered_from_url=url,
                        ),
                    )
            return True

        left_groups: list[dict] = []
        current_primary_group: dict | None = None

        for idx_m, m in enumerate(left_iter):
            raw_label, raw_url = m.group(1), m.group(2)
            label = _clean_label(raw_label)
            norm_lbl = label.strip().strip("[]()").lower()
            bolded = _is_match_bolded(m, left_bold_spans)

            if norm_lbl in NON_TOOL_SOCIAL_LABELS and not include_social:
                # Never open an empty group for `/ [Discord](...)` or `/ [Telegram](...)`!
                continue

            if norm_lbl in DIRECT_REPO_LABELS and left_groups:
                target = current_primary_group or left_groups[-1]
                url_low = re.sub(r"[^a-z0-9]+", "", raw_url.lower())
                for grp in left_groups:
                    t_clean = re.sub(r"[^a-z0-9]+", "", grp["title"].lower())
                    if t_clean and len(t_clean) >= 3 and t_clean in url_low:
                        target = grp
                        break
                _ingest_link(target, raw_url, target["title"] or label)
                continue

            if _is_numeric_or_mirror_label(label) and left_groups:
                _ingest_link(left_groups[-1], raw_url, left_groups[-1]["title"] or label)
                continue

            prev_end_idx = left_iter[idx_m - 1].end() if idx_m > 0 else 0
            between = left_part[prev_end_idx : m.start()] if idx_m > 0 else ""

            if (
                idx_m > 0
                and current_primary_group is not None
                and current_primary_group["title"]
                and "/" in between
                and "," not in between
                and " or " not in between.lower()
                and norm_lbl in SLASH_MODIFIER_LABELS
            ):
                mod_title = f"{current_primary_group['title']} ({label})"
                cand_grp = _new_group(mod_title, bolded=bolded)
                if _ingest_link(cand_grp, raw_url, label, allow_domain_heuristic=False):
                    left_groups.append(cand_grp)
                continue

            cand_grp = _new_group("" if _is_numeric_or_mirror_label(label) else label, bolded=bolded)
            if _ingest_link(cand_grp, raw_url, cand_grp["title"] or label):
                left_groups.append(cand_grp)
                current_primary_group = cand_grp

        primary_parent_title = (
            header_parent_title
            if is_header_collection
            else (left_groups[0]["title"] if left_groups else clean_left)
        )

        right_groups: list[dict] = []
        last_right_group: dict | None = None

        for idx_r, m in enumerate(right_iter):
            raw_label, raw_url = m.group(1), m.group(2)
            label = _clean_label(raw_label)
            norm_lbl = label.strip().strip("[]()").lower()

            if norm_lbl in NON_TOOL_SOCIAL_LABELS and not include_social:
                continue

            prev_end_idx = right_iter[idx_r - 1].end() if idx_r > 0 else 0
            between = right_part[prev_end_idx : m.start()] if idx_r > 0 else ""
            if "/" in between:
                last_right_group = None

            if norm_lbl in DIRECT_REPO_LABELS and left_groups:
                url_low = re.sub(r"[^a-z0-9]+", "", raw_url.lower())
                matched_grp: dict | None = None
                for grp in left_groups:
                    t_clean = re.sub(r"[^a-z0-9]+", "", grp["title"].lower())
                    if t_clean and len(t_clean) >= 3 and t_clean in url_low:
                        matched_grp = grp
                        break
                if matched_grp is None:
                    unrepoed = [
                        g
                        for g in left_groups
                        if not any(r.source == "explicit_markdown" for r in g["github_refs"])
                    ]
                    matched_grp = unrepoed[0] if len(unrepoed) == 1 else left_groups[0]
                _ingest_link(matched_grp, raw_url, matched_grp["title"] or label)
            elif _is_numeric_or_mirror_label(label):
                if last_right_group is not None:
                    _ingest_link(
                        last_right_group,
                        raw_url,
                        last_right_group["title"] or label,
                        allow_domain_heuristic=False,
                    )
                elif left_groups:
                    _ingest_link(left_groups[0], raw_url, left_groups[0]["title"] or label)
            else:
                comp_title = f"{primary_parent_title} ({label})" if primary_parent_title else label
                comp_grp = _new_group(comp_title, bolded=is_header_collection)
                if _ingest_link(comp_grp, raw_url, label, allow_domain_heuristic=False):
                    right_groups.append(comp_grp)
                    last_right_group = comp_grp

        all_groups = left_groups + right_groups
        valid_groups: list[dict] = []

        for grp in all_groups:
            # If a group already has an explicit GitHub link (or explicit GitLab/Codeberg/sr.ht repo),
            # drop speculative `domain_heuristic` and `github_pages` guesses on that same group!
            has_explicit_gh = any(r.source == "explicit_markdown" for r in grp["github_refs"])
            has_alt_forge = _has_explicit_alt_forge(grp["site_urls"])
            if has_explicit_gh or has_alt_forge:
                grp["github_refs"] = [
                    r
                    for r in grp["github_refs"]
                    if r.source in {"explicit_markdown", "github_cdn"}
                ]
            if grp["site_urls"] or grp["github_refs"]:
                valid_groups.append(grp)

        subcategory = " > ".join(part for part in (h2, h3) if part)

        for sub_idx, grp in enumerate(valid_groups):
            primary_title = grp["title"]
            if not primary_title:
                if grp["site_urls"]:
                    primary_title = urlsplit(grp["site_urls"][0]).hostname or grp["site_urls"][0]
                else:
                    primary_title = grp["github_refs"][0].full_name

            if not line_has_star:
                entry_starred = False
            elif is_header_collection:
                entry_starred = bool(grp["bolded"])
            elif any_bolded_left:
                entry_starred = bool(grp["bolded"])
            else:
                entry_starred = sub_idx == 0 and grp in left_groups

            suffix = f"#{sub_idx + 1}" if len(valid_groups) > 1 else ""
            entries.append(
                WikiEntry(
                    entry_id=f"{source_file}:{idx}{suffix}",
                    title=primary_title,
                    category=category,
                    subcategory=subcategory,
                    source_file=source_file,
                    line_number=idx,
                    starred=entry_starred,
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
