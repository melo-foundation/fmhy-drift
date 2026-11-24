"""Persistent whitelist loader for ignoring known-good archived/stable repos and URLs."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from fmhy_drift.parser import clean_url, extract_github_repo


@dataclass
class Whitelist:
    """Normalized sets of whitelisted `owner/repo` keys and site URLs."""

    repos: set[str] = field(default_factory=set)
    urls: set[str] = field(default_factory=set)
    notes: dict[str, str] = field(default_factory=dict)

    def is_repo_whitelisted(self, owner: str, repo: str) -> bool:
        return f"{owner.lower()}/{repo.lower()}" in self.repos

    def is_url_whitelisted(self, url: str) -> bool:
        cleaned = clean_url(url)
        return (url in self.urls) or (bool(cleaned) and cleaned in self.urls)


def normalize_repo_entry(raw: str) -> str | None:
    text = raw.strip()
    if not text:
        return None
    if "github.com" in text.lower():
        extracted = extract_github_repo(text)
        if extracted:
            return f"{extracted[0].lower()}/{extracted[1].lower()}"
        return None
    parts = [p for p in text.strip("/").split("/") if p]
    if len(parts) == 2:
        repo = parts[1][:-4] if parts[1].endswith(".git") else parts[1]
        return f"{parts[0].lower()}/{repo.lower()}"
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
            cleaned = clean_url(item)
            urls.add(cleaned or item.strip())

    raw_notes = data.get("notes", {})
    notes = (
        {str(k).lower(): str(v) for k, v in raw_notes.items() if isinstance(v, str)}
        if isinstance(raw_notes, dict)
        else {}
    )

    return Whitelist(repos=repos, urls=urls, notes=notes)
