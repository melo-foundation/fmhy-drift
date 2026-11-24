"""Unit tests for FMHY markdown index parsing and GitHub link extraction."""

from __future__ import annotations

from pathlib import Path

from fmhy_drift.parser import (
    extract_github_cdn_repo,
    extract_github_pages_repo,
    extract_github_repo,
    load_local_index,
    parse_markdown_content,
)

SAMPLE_FMHY_MD = """---
title: Adblocking & Privacy
---

# Privacy & Adblocking {#privacy}

::: tip Note
* [Internal FMHY Mirror](https://fmhy.pages.dev/privacy)
:::

## Browser Extensions

* ⭐ **[uBlock Origin](https://ublockorigin.com/)** , [2](https://ublockorigin.org/) - Content blocker / [Discord](https://discord.gg/ublock) / [GitHub](https://github.com/gorhill/uBlock)
* **[MyPagesTool](https://alice.github.io/cool-widget/docs)** - Hosted on GitHub Pages
* [Wiki Link With Parens](https://example.org/wiki/Tool_(software)) - Handles balanced parens
* [Repo Only Entry](https://github.com/gallant/open-tool.git)

```markdown
* [Ignored Code Fence Link](https://dead-in-codeblock.example.com)
```
"""


def test_extract_github_repo_normalizes_and_filters() -> None:
    assert extract_github_repo("https://github.com/gorhill/uBlock/releases/tag/1.60.0") == (
        "gorhill",
        "uBlock",
    )
    assert extract_github_repo("https://github.com/owner/project.git") == ("owner", "project")
    assert extract_github_repo("https://github.com/sponsors/gorhill") is None
    assert extract_github_repo("https://github.com/topics/adblock") is None
    assert extract_github_repo("https://github.com/fmhy/edit") is None


def test_extract_github_pages_repo() -> None:
    assert extract_github_pages_repo("https://alice.github.io/cool-widget/docs") == (
        "alice",
        "cool-widget",
    )
    assert extract_github_pages_repo("https://bob.github.io/") == (
        "bob",
        "bob.github.io",
    )


def test_extract_github_cdn_repo() -> None:
    assert extract_github_cdn_repo(
        "https://raw.githubusercontent.com/uBlockOrigin/uAssets/master/filters/filters.txt"
    ) == ("uBlockOrigin", "uAssets")
    assert extract_github_cdn_repo(
        "https://cdn.jsdelivr.net/gh/pixeltris/TwitchAdSolutions@master/vaft/vaft.user.js"
    ) == ("pixeltris", "TwitchAdSolutions")
    assert extract_github_cdn_repo(
        "https://raw.githack.com/owner/cool-script/main/script.user.js"
    ) == ("owner", "cool-script")
    assert extract_github_cdn_repo(
        "https://cdn.statically.io/gh/org/filterlist/main/list.txt"
    ) == ("org", "filterlist")


def test_parse_markdown_content_extracts_entries_and_skips_noise() -> None:
    entries = parse_markdown_content(SAMPLE_FMHY_MD, source_file="adblock.md")
    # Internal fmhy.pages.dev and fenced code block entries are skipped
    assert len(entries) == 4

    ublock = entries[0]
    assert ublock.title == "uBlock Origin"
    assert ublock.starred is True
    assert ublock.category == "Privacy & Adblocking"
    assert ublock.subcategory == "Browser Extensions"
    # Discord invite skipped from site_urls; primary + [2] mirror preserved
    assert ublock.site_urls == [
        "https://ublockorigin.com/",
        "https://ublockorigin.org/",
    ]
    assert len(ublock.github_refs) == 1
    assert ublock.github_refs[0].full_name == "gorhill/uBlock"
    assert ublock.github_refs[0].source == "explicit_markdown"

    gh_pages_entry = entries[1]
    assert gh_pages_entry.site_urls == ["https://alice.github.io/cool-widget/docs"]
    assert len(gh_pages_entry.github_refs) == 1
    assert gh_pages_entry.github_refs[0].full_name == "alice/cool-widget"
    assert gh_pages_entry.github_refs[0].source == "github_pages"

    parens_entry = entries[2]
    assert parens_entry.site_urls == ["https://example.org/wiki/Tool_(software)"]


def test_abztract_and_blobs_multi_tool_line_and_dotted_pages_repo() -> None:
    line = "* [Abztract](https://abztract.arhamkhnz.com/) or [Blobs](https://lokesh-coder.github.io/blobs.app/) - SVG Shape Generators\n"
    entries = parse_markdown_content(line, source_file="img-tools.md")
    assert len(entries) == 2

    abztract = entries[0]
    assert abztract.title == "Abztract"
    assert abztract.site_urls == ["https://abztract.arhamkhnz.com/"]
    assert [r.full_name for r in abztract.github_refs] == ["arhamkhnz/abztract"]
    assert abztract.github_refs[0].source == "domain_heuristic"

    blobs = entries[1]
    assert blobs.title == "Blobs"
    assert blobs.site_urls == ["https://lokesh-coder.github.io/blobs.app/"]
    assert [r.full_name for r in blobs.github_refs] == ["lokesh-coder/blobs.app"]
    assert blobs.github_refs[0].source == "github_pages"


def test_prowlarr_bobarr_openconnect_and_ffmpeg_real_fmhy_lines() -> None:
    md = """# ► Torrents & VPN
## ▷ Clients
* ⭐ **[Prowlarr](https://github.com/Prowlarr/Prowlarr)**, **[Jackett](https://github.com/Jackett/Jackett)**, [Bobarr](https://github.com/iam4x/bobarr) / [Discord](https://discord.gg/PFwM4zk), [nefarious](https://lardbit.github.io/nefarious/) or [Watcher](https://nosmokingbandit.github.io/) - Autodownload Torrents
* [OpenConnect](https://gitlab.com/openconnect/openconnect) - SSL VPN / [GUI](https://openconnect.github.io/openconnect-gui/)
* [FFmpeg Cheat Sheet](https://gist.github.com/steven2358/ba153c642fe2bb1e47485962df07c730), [FFmpeg Commander](https://ffmpeg-commander.com/) or [FFmpeg Buddy](https://evanhahn.github.io/ffmpeg-buddy/) - Command Lists
* ⭐ **[Stability Matrix](https://lykos.ai/)** / [Discord](https://discord.com/invite/TUrgfECxHz) / [GitHub](https://github.com/LykosAI/StabilityMatrix)
* ⭐ **[SponsorBlock](https://sponsor.ajay.app/)** - Skip YouTube Sponsors / [GitHub](https://github.com/ajayyy/SponsorBlock) / [Warning](https://github.com/arkenfox/user.js/issues/1729#issuecomment-1)
* **Note** - Remember to [bind](https://wispydocs.pages.dev/torrenting/) your VPN
* GetIntoPC - Caught with [malware](https://www.virustotal.com/gui/file/abc)
"""
    entries = parse_markdown_content(md, source_file="torrenting.md")
    by_title = {e.title: e for e in entries}

    # Heading `►` and `▷` prefixes stripped cleanly!
    assert by_title["Prowlarr"].category == "Torrents & VPN"
    assert by_title["Prowlarr"].subcategory == "Clients"

    # Prowlarr and Bobarr are completely separate entries, and only bolded Prowlarr/Jackett are starred!
    assert by_title["Prowlarr"].starred is True
    assert [r.full_name for r in by_title["Prowlarr"].github_refs] == ["Prowlarr/Prowlarr"]
    assert by_title["Prowlarr"].site_urls == []

    assert by_title["Jackett"].starred is True
    assert [r.full_name for r in by_title["Jackett"].github_refs] == ["Jackett/Jackett"]

    assert by_title["Bobarr"].starred is False
    assert [r.full_name for r in by_title["Bobarr"].github_refs] == ["iam4x/bobarr"]

    # Stability Matrix keeps its GitHub link instead of losing it to `/ [Discord]`!
    assert "Discord" not in by_title
    assert [r.full_name for r in by_title["Stability Matrix"].github_refs] == [
        "LykosAI/StabilityMatrix"
    ]

    # SponsorBlock keeps ONLY explicit `ajayyy/SponsorBlock` (drops `ajay/sponsor` heuristic AND issue citation)!
    assert [r.full_name for r in by_title["SponsorBlock"].github_refs] == ["ajayyy/SponsorBlock"]
    assert "SponsorBlock (Warning)" not in by_title

    # Note callout and Unsafe prose lines are ignored!
    assert "bind" not in by_title
    assert "malware" not in by_title

    # OpenConnect main link stays GitLab; `/ [GUI](openconnect-gui)` becomes `OpenConnect (GUI)`!
    assert by_title["OpenConnect"].github_refs == []
    assert by_title["OpenConnect"].site_urls == ["https://gitlab.com/openconnect/openconnect"]
    assert [r.full_name for r in by_title["OpenConnect (GUI)"].github_refs] == [
        "openconnect/openconnect-gui"
    ]

    # FFmpeg Buddy is its own entry, not merged under FFmpeg Cheat Sheet!
    assert "FFmpeg Cheat Sheet" not in by_title
    assert [r.full_name for r in by_title["FFmpeg Buddy"].github_refs] == ["evanhahn/ffmpeg-buddy"]


def test_load_local_index_directory(tmp_path: Path) -> None:
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "README.md").write_text("* [Ignored Readme](https://readme.example.com)\n")
    (docs / "gaming.md").write_text("* ⭐ [Heroic](https://heroicgameslauncher.com/)\n")

    entries = load_local_index(docs)
    assert len(entries) == 1
    assert entries[0].title == "Heroic"
    assert entries[0].category == "Gaming"
