"""Persistent whitelist loader for ignoring known-good archived/stable repos and URLs."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from fmhy_drift.parser import clean_url, extract_alt_forge_repo, extract_github_repo


def normalize_url_entry(raw: str) -> str | None:
    """Normalize URL for whitelist comparison (lowercase scheme+host, strip fragment & trailing `/`)."""
    text = raw.strip()
    if not text:
        return None
    cleaned = clean_url(text)
    target = cleaned or text
    try:
        parts = urlsplit(target)
    except ValueError:
        return target.split("#", 1)[0].rstrip("/") or None

    if not parts.scheme or not parts.hostname:
        return target.split("#", 1)[0].rstrip("/") or None

    scheme = parts.scheme.lower()
    host = parts.hostname.lower()
    host_fmt = f"[{host}]" if ":" in host else host
    netloc = f"{host_fmt}:{parts.port}" if parts.port is not None else host_fmt
    if parts.username is not None:
        userinfo = (
            f"{parts.username}:{parts.password}"
            if parts.password is not None
            else parts.username
        )
        netloc = f"{userinfo}@{netloc}"

    path = parts.path.rstrip("/")
    return urlunsplit((scheme, netloc, path, parts.query, "")).rstrip("/")


@dataclass
class Whitelist:
    """Normalized sets of whitelisted `owner/repo` keys and site URLs."""

    repos: set[str] = field(default_factory=set)
    urls: set[str] = field(default_factory=set)
    notes: dict[str, str] = field(default_factory=dict)

    def is_repo_whitelisted(self, owner: str, repo: str, forge_host: str = "github.com") -> bool:
        if forge_host.lower() in {"github.com", "www.github.com", ""}:
            return f"{owner.lower()}/{repo.lower()}" in self.repos
        if forge_host.lower() == "sourceforge.net":
            return f"sourceforge.net/projects/{repo.lower()}" in self.repos
        return f"{forge_host.lower()}/{owner.lower()}/{repo.lower()}" in self.repos

    def is_repo_key_whitelisted(self, key: str) -> bool:
        return key.strip().lower() in self.repos

    def is_url_whitelisted(self, url: str) -> bool:
        norm = normalize_url_entry(url)
        return (url in self.urls) or (bool(norm) and norm in self.urls)


def normalize_repo_entry(raw: str) -> str | None:
    text = raw.strip()
    if not text:
        return None
    if text.lower().startswith(("http://", "https://")):
        if "github.com" in text.lower():
            extracted = extract_github_repo(text)
            if extracted:
                return f"{extracted[0].lower()}/{extracted[1].lower()}"
            return None
        alt = extract_alt_forge_repo(text)
        if alt is not None:
            return alt.key
        return None
    if "github.com/" in text.lower():
        extracted = extract_github_repo("https://" + text.lstrip("/"))
        if extracted:
            return f"{extracted[0].lower()}/{extracted[1].lower()}"
        return None
    parts = [p for p in text.strip("/").split("/") if p]
    if len(parts) == 2:
        repo = parts[1][:-4] if parts[1].endswith(".git") else parts[1]
        return f"{parts[0].lower()}/{repo.lower()}"
    if len(parts) >= 3 and "." in parts[0]:
        parts[-1] = parts[-1][:-4] if parts[-1].endswith(".git") else parts[-1]
        return "/".join(p.lower() for p in parts)
    return None


def load_whitelist(path: Path | None) -> Whitelist:
    """Load `whitelist.json` if it exists; return an empty `Whitelist` if missing."""
    if path is None or not path.is_file():
        return Whitelist()

    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        return Whitelist()

    repos: set[str] = set()
    for item in data.get("repos", []):
        if isinstance(item, str):
            norm = normalize_repo_entry(item)
            if norm:
                repos.add(norm)

    urls: set[str] = set()
    for item in data.get("urls", []):
        if isinstance(item, str) and item.strip():
            norm = normalize_url_entry(item)
            if norm:
                urls.add(norm)

    raw_notes = data.get("notes", {})
    notes = (
        {str(k).lower(): str(v) for k, v in raw_notes.items() if isinstance(v, str)}
        if isinstance(raw_notes, dict)
        else {}
    )

    return Whitelist(repos=repos, urls=urls, notes=notes)
