"""Unit tests for GitHub GraphQL batching, REST fallback, staleness tiers, and archived replacement detection."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import httpx

from fmhy_drift.github_checker import GitHubActivityChecker, find_archived_replacement_url
from fmhy_drift.models import GitHubRef, StalenessTier, TierThresholds

FIXED_NOW = datetime(2026, 11, 24, 12, 0, 0, tzinfo=UTC)


def test_find_archived_replacement_url_from_description_and_readme() -> None:
    # From description pointing to Codeberg
    assert (
        find_archived_replacement_url(
            owner="old",
            repo="app",
            canonical_full_name="old/app",
            description="[DEPRECATED] Moved to https://codeberg.org/new/app",
            homepage_url=None,
            readme_text=None,
        )
        == "https://codeberg.org/new/app"
    )

    # From README migration banner while ignoring unrelated framework links
    readme = """# OldTool
![badge](https://img.shields.io/badge/status-archived-red)
Built with [React](https://github.com/facebook/react).

> **Warning**
> This repository is archived and no longer maintained.
> Active development continues at https://github.com/new-org/new-tool!
"""
    assert (
        find_archived_replacement_url(
            owner="old-org",
            repo="old-tool",
            canonical_full_name="old-org/old-tool",
            description="A cool tool",
            homepage_url="https://old-tool.example.com",
            readme_text=readme,
        )
        == "https://github.com/new-org/new-tool"
    )


def test_github_activity_graphql_rest_and_archived_successor_verification() -> None:
    refs = [
        GitHubRef("org", "active-repo", "explicit_markdown"),
        GitHubRef("org", "tier1-repo", "explicit_markdown"),
        GitHubRef("org", "tier2-repo", "explicit_markdown"),
        GitHubRef("org", "tier3-repo", "discovered_on_site"),
        GitHubRef("org", "archived-repo", "explicit_markdown"),
        GitHubRef("org", "disabled-repo", "explicit_markdown"),
        GitHubRef("old-org", "renamed-repo", "explicit_markdown"),
        GitHubRef("org", "deleted-repo", "explicit_markdown"),
        GitHubRef("pagesuser", "subpage-game", "github_pages", "https://pagesuser.github.io/subpage-game"),
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/graphql":
            import json as _json

            query_str = _json.loads(request.read().decode("utf-8"))["query"]
            if "subHistory" in query_str and "pagesuser.github.io" in query_str:
                return httpx.Response(
                    200,
                    json={
                        "data": {
                            "r0": {
                                "nameWithOwner": "pagesuser/pagesuser.github.io",
                                "description": "User pages monorepo",
                                "homepageUrl": "",
                                "isArchived": False,
                                "archivedAt": None,
                                "isDisabled": False,
                                "pushedAt": "2026-11-20T12:00:00Z",
                                "defaultBranchRef": {
                                    "target": {
                                        "committedDate": "2026-11-20T12:00:00Z",
                                        "subHistory": {
                                            "nodes": [{"committedDate": "2020-11-24T12:00:00Z"}]
                                        },
                                    }
                                },
                            }
                        }
                    },
                )
            # Second-pass README query for archived repos without description link
            if "rmUpper" in query_str:
                return httpx.Response(
                    200,
                    json={
                        "data": {
                            "r0": {
                                "rmUpper": {
                                    "text": "> Archived! Superseded by https://github.com/new-org/successor-repo\n"
                                },
                                "rmLower": None,
                            }
                        }
                    },
                )
            # Successor verification GraphQL query
            if "successor-repo" in query_str:
                return httpx.Response(
                    200,
                    json={
                        "data": {
                            "r0": {
                                "nameWithOwner": "new-org/successor-repo",
                                "description": "Active fork",
                                "homepageUrl": "https://successor.example.org",
                                "isArchived": False,
                                "isDisabled": False,
                                "pushedAt": "2026-11-20T12:00:00Z",  # 4d ago
                                "defaultBranchRef": None,
                                "releases": {"nodes": []},
                            }
                        }
                    },
                )
            # Primary batch query
            return httpx.Response(
                200,
                json={
                    "data": {
                        "r0": {
                            "nameWithOwner": "org/active-repo",
                            "description": "",
                            "homepageUrl": "https://active.example.org",
                            "isArchived": False,
                            "isDisabled": False,
                            "pushedAt": "2026-11-09T12:00:00Z",
                            "defaultBranchRef": {"target": {"committedDate": "2026-11-09T12:00:00Z"}},
                            "releases": {"nodes": []},
                        },
                        "r1": {
                            "nameWithOwner": "org/tier1-repo",
                            "description": "",
                            "homepageUrl": "",
                            "isArchived": False,
                            "isDisabled": False,
                            "pushedAt": "2026-08-11T12:00:00Z",
                            "defaultBranchRef": None,
                            "releases": {"nodes": []},
                        },
                        "r2": {
                            "nameWithOwner": "org/tier2-repo",
                            "description": "",
                            "homepageUrl": "",
                            "isArchived": False,
                            "isDisabled": False,
                            "pushedAt": "2026-04-28T12:00:00Z",
                            "defaultBranchRef": None,
                            "releases": {"nodes": []},
                        },
                        "r3": {
                            "nameWithOwner": "org/tier3-repo",
                            "description": "",
                            "homepageUrl": "",
                            "isArchived": False,
                            "isDisabled": False,
                            "pushedAt": "2025-10-20T12:00:00Z",
                            "defaultBranchRef": None,
                            "releases": {"nodes": []},
                        },
                        "r4": {
                            "nameWithOwner": "org/archived-repo",
                            "description": "Legacy version",
                            "homepageUrl": "https://org.github.io/archived-repo/",
                            "isArchived": True,
                            "archivedAt": "2026-10-25T12:00:00Z",
                            "isDisabled": False,
                            "pushedAt": "2026-10-10T12:00:00Z",
                            "defaultBranchRef": None,
                            "releases": {"nodes": []},
                        },
                        "r5": {
                            "nameWithOwner": "org/disabled-repo",
                            "description": "",
                            "homepageUrl": "",
                            "isArchived": False,
                            "archivedAt": None,
                            "isDisabled": True,
                            "pushedAt": "2026-11-20T12:00:00Z",
                            "defaultBranchRef": None,
                            "releases": {"nodes": []},
                        },
                        "r6": None,
                        "r7": None,
                        "r8": None,
                    }
                },
            )

        if request.url.path == "/repos/old-org/renamed-repo":
            return httpx.Response(
                200,
                json={
                    "full_name": "new-org/renamed-repo",
                    "description": "",
                    "homepage": "renamed.example.com",
                    "archived": False,
                    "disabled": False,
                    "updated_at": "2026-11-24T11:59:00Z",
                    "pushed_at": "2025-07-12T12:00:00Z",
                    "created_at": "2024-01-01T00:00:00Z",
                },
            )

        if request.url.path == "/repos/org/deleted-repo":
            return httpx.Response(404, json={"message": "Not Found"})

        if request.url.path == "/repos/pagesuser/subpage-game":
            return httpx.Response(404, json={"message": "Not Found"})

        if request.url.host == "pagesuser.github.io":
            return httpx.Response(200, headers={"content-type": "text/html"}, text="<html>Hello</html>")

        if request.url.path == "/users/pagesuser/repos":
            return httpx.Response(
                200,
                json=[
                    {"name": "pagesuser.github.io", "description": "User pages monorepo", "homepage": ""}
                ],
            )

        if request.url.path == "/repos/pagesuser/pagesuser.github.io":
            return httpx.Response(
                200,
                json={
                    "full_name": "pagesuser/pagesuser.github.io",
                    "description": "User pages monorepo",
                    "homepage": "",
                    "archived": False,
                    "disabled": False,
                    "pushed_at": "2020-11-24T12:00:00Z",
                    "created_at": "2019-01-01T00:00:00Z",
                },
            )

        return httpx.Response(500)

    checker = GitHubActivityChecker(
        thresholds=TierThresholds(90, 180, 365),
        github_token="ghp_testtoken",
        now=FIXED_NOW,
        transport=httpx.MockTransport(handler),
    )

    results = asyncio.run(checker.check_repos(refs))

    assert results["org/active-repo"].tier == StalenessTier.ACTIVE
    assert results["org/active-repo"].homepage_url == "https://active.example.org/"

    assert results["org/tier1-repo"].tier == StalenessTier.TIER_1_NOTICE
    assert results["org/tier2-repo"].tier == StalenessTier.TIER_2_WARNING
    assert results["org/tier3-repo"].tier == StalenessTier.TIER_3_CRITICAL

    # Archived repo has its GitHub Pages homepage, archivedAt, AND verified replacement repo!
    arch = results["org/archived-repo"]
    assert arch.tier == StalenessTier.ARCHIVED
    assert arch.archived_days == 30
    assert arch.homepage_url == "https://org.github.io/archived-repo/"
    assert arch.replacement_url == "https://github.com/new-org/successor-repo"
    assert arch.replacement_status == "active, last push 4d ago"

    # GitHub Pages subdirectory URL falls back to `pagesuser/pagesuser.github.io` using path commit date!
    pages_sub = results["pagesuser/subpage-game"]
    assert pages_sub.tier == StalenessTier.TIER_3_CRITICAL
    assert pages_sub.full_name == "pagesuser/pagesuser.github.io"
    assert pages_sub.inactive_days == 2191  # 6 years (2020-11-24 -> 2026-11-24), NOT 4 days!

    # Disabled repo is classified with Dead (MISSING), NOT Archived!
    assert results["org/disabled-repo"].tier == StalenessTier.MISSING

    renamed = results["old-org/renamed-repo"]
    assert renamed.tier == StalenessTier.TIER_3_CRITICAL
    assert renamed.full_name == "new-org/renamed-repo"
    assert renamed.homepage_url == "https://renamed.example.com/"

    assert results["org/deleted-repo"].tier == StalenessTier.MISSING


def test_zincsearch_readme_does_not_false_match_openobserve_or_bluge_fork() -> None:
    zinc_readme = """
❗Note: If your use case is of log search (app and security logs) instead of app search then you should check [openobserve/openobserve](https://github.com/openobserve/openobserve) project built in rust.

# ZincSearch
It uses [bluge](https://github.com/blugelabs/bluge) (via the [vcaesar/riot](https://github.com/vcaesar/riot) fork) as the underlying indexing library.
"""
    assert (
        find_archived_replacement_url(
            owner="zincsearch",
            repo="zincsearch",
            canonical_full_name="zincsearch/zincsearch",
            description="ZincSearch . A lightweight alternative to elasticsearch.",
            homepage_url="https://zincsearch-docs.zinc.dev",
            readme_text=zinc_readme,
        )
        is None
    )


def test_github_pages_resolves_via_html_link_or_owner_repo_description() -> None:
    refs = [
        GitHubRef(
            owner="gabgoh",
            repo="COVID",
            source="github_pages",
            discovered_from_url="https://gabgoh.github.io/COVID/index.html",
            entry_title="Epidemic Calculator",
        ),
        GitHubRef(
            owner="alice",
            repo="demo",
            source="github_pages",
            discovered_from_url="https://alice.github.io/demo/",
            entry_title="Cool Demo",
        ),
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "gabgoh.github.io":
            return httpx.Response(200, headers={"content-type": "text/html"}, text="<html>No link</html>")
        if request.url.host == "alice.github.io":
            return httpx.Response(
                200,
                headers={"content-type": "text/html"},
                text='<html><a href="https://github.com/cool-org/real-demo">Source</a></html>',
            )
        if request.url.path == "/repos/gabgoh/COVID":
            return httpx.Response(404)
        if request.url.path == "/repos/alice/demo":
            return httpx.Response(404)
        if request.url.path == "/users/gabgoh/repos":
            return httpx.Response(
                200,
                json=[
                    {"name": "epcalc", "description": "Epidemic Calculator", "homepage": None},
                    {"name": "gabgoh.github.io", "description": "Homepage", "homepage": None},
                ],
            )
        if request.url.path == "/repos/gabgoh/epcalc":
            return httpx.Response(
                200,
                json={
                    "full_name": "gabgoh/epcalc",
                    "description": "Epidemic Calculator",
                    "homepage": "",
                    "archived": False,
                    "disabled": False,
                    "pushed_at": "2020-03-25T12:00:00Z",
                    "created_at": "2020-03-18T12:00:00Z",
                },
            )
        if request.url.path == "/repos/cool-org/real-demo":
            return httpx.Response(
                200,
                json={
                    "full_name": "cool-org/real-demo",
                    "description": "Moved org repo",
                    "homepage": "https://alice.github.io/demo/",
                    "archived": False,
                    "disabled": False,
                    "pushed_at": "2026-11-22T12:00:00Z",
                    "created_at": "2025-01-01T12:00:00Z",
                },
            )
        return httpx.Response(404)

    checker = GitHubActivityChecker(
        thresholds=TierThresholds(90, 180, 365),
        github_token=None,
        now=FIXED_NOW,
        transport=httpx.MockTransport(handler),
    )
    results = asyncio.run(checker.check_repos(refs))

    assert results["gabgoh/covid"].full_name == "gabgoh/epcalc"
    assert results["gabgoh/covid"].tier == StalenessTier.TIER_3_CRITICAL

    assert results["alice/demo"].full_name == "cool-org/real-demo"
    assert results["alice/demo"].tier == StalenessTier.ACTIVE


def test_picoshare_codefloe_and_forked_from_and_cue_tightening() -> None:
    # PicoShare moved to codefloe.com
    picoshare_readme = """# PicoShare
> **Note**
> This GitHub mirror is archived. Active development has moved to
> https://codefloe.com/mtlynch/picoshare
"""
    assert (
        find_archived_replacement_url(
            owner="mtlynch",
            repo="picoshare",
            canonical_full_name="mtlynch/picoshare",
            description="Minimalist file sharing service",
            homepage_url="https://picoshare.dev",
            readme_text=picoshare_readme,
        )
        == "https://codefloe.com/mtlynch/picoshare"
    )

    # Reject "forked from" / "spiritual successor to" / "archiving" / "originally moved to" / earlier paragraph URLs
    noisy_readme = """# WebArchiver
[![Build](https://github.com/other/badge/badge.svg)](https://github.com/other/badge)
Forked from https://github.com/upstream/original-project.
A spiritual successor to https://github.com/legacy/old-project.

Built with [FastAPI](https://github.com/tiangolo/fastapi) for archiving web pages!
Originally moved to GitHub in 2021; this tool is now deprecated, see our docs site for details.
"""
    assert (
        find_archived_replacement_url(
            owner="me",
            repo="web-archiver",
            canonical_full_name="me/web-archiver",
            description="Tool for archiving pages — use CLI flags",
            homepage_url="https://github.com/tiangolo/fastapi",  # Unrelated stem on github.com must be rejected!
            readme_text=noisy_readme,
        )
        is None
    )

    # Accept github.com homepage_url without cue ONLY when repo stem matches under a different owner
    assert (
        find_archived_replacement_url(
            owner="old-dev",
            repo="cool-widget",
            canonical_full_name="old-dev/cool-widget",
            description="A widget",
            homepage_url="https://github.com/new-org/cool-widget-ng",
            readme_text=None,
        )
        == "https://github.com/new-org/cool-widget-ng"
    )


def test_moonleaf_self_redirect_and_archived_successor_rejection() -> None:
    refs = [
        GitHubRef("illufied", "moonleaf", "explicit_markdown"),
        GitHubRef("old-org", "dead-succ-app", "explicit_markdown"),
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/graphql":
            import json as _json

            query_str = _json.loads(request.read().decode("utf-8"))["query"]
            if "rmUpper" in query_str:
                assert "rmTitle" in query_str and "rmRst" in query_str
                return httpx.Response(
                    200,
                    json={
                        "data": {
                            "r0": {
                                "rmUpper": None,
                                "rmLower": None,
                                "rmTitle": {
                                    "text": "Moved to https://github.com/parkuoa/moonleaf"
                                },
                                "rmRst": None,
                            },
                            "r1": {
                                "rmUpper": {
                                    "text": "Superseded by https://github.com/gone-org/missing-app"
                                }
                            },
                        }
                    },
                )
            if "parkuoa" in query_str:
                # GitHub GraphQL returns null on renamed owner alias, triggering REST 301 resolution
                return httpx.Response(200, json={"data": {"r0": None, "r1": None}})
            return httpx.Response(
                200,
                json={
                    "data": {
                        "r0": {
                            "nameWithOwner": "illufied/moonleaf",
                            "description": "Launcher",
                            "homepageUrl": "",
                            "isArchived": True,
                            "archivedAt": "2026-09-01T00:00:00Z",
                            "isDisabled": False,
                            "isEmpty": False,
                            "pushedAt": "2026-08-01T00:00:00Z",
                            "defaultBranchRef": None,
                            "releases": {"nodes": []},
                        },
                        "r1": {
                            "nameWithOwner": "old-org/dead-succ-app",
                            "description": "",
                            "homepageUrl": "",
                            "isArchived": True,
                            "archivedAt": "2026-09-01T00:00:00Z",
                            "isDisabled": False,
                            "isEmpty": False,
                            "pushedAt": "2026-08-01T00:00:00Z",
                            "defaultBranchRef": None,
                            "releases": {"nodes": []},
                        },
                    }
                },
            )

        if request.url.path == "/repos/parkuoa/moonleaf":
            # Redirects right back to illufied/moonleaf!
            return httpx.Response(
                200,
                json={
                    "full_name": "illufied/moonleaf",
                    "description": "Launcher",
                    "homepage": "",
                    "archived": True,
                    "disabled": False,
                    "pushed_at": "2026-08-01T00:00:00Z",
                    "created_at": "2024-01-01T00:00:00Z",
                },
            )
        if request.url.path == "/repos/gone-org/missing-app":
            return httpx.Response(404, json={"message": "Not Found"})
        return httpx.Response(500)

    checker = GitHubActivityChecker(
        thresholds=TierThresholds(90, 180, 365),
        github_token="ghp_testtoken",
        now=FIXED_NOW,
        transport=httpx.MockTransport(handler),
    )
    results = asyncio.run(checker.check_repos(refs))

    # Both self-redirecting `parkuoa/moonleaf -> illufied/moonleaf` and 404 successor must be REJECTED!
    assert results["illufied/moonleaf"].tier == StalenessTier.ARCHIVED
    assert results["illufied/moonleaf"].replacement_url is None
    assert results["old-org/dead-succ-app"].replacement_url is None


def test_pages_redirect_isolation_source_priority_empty_and_future_commit() -> None:
    external_fetched = False
    refs = [
        # domain_heuristic first must NOT clobber explicit_markdown for same repo!
        GitHubRef("shared", "gone-repo", "domain_heuristic"),
        GitHubRef("shared", "gone-repo", "explicit_markdown"),
        GitHubRef("org", "empty-repo", "explicit_markdown"),
        GitHubRef("org", "spoofed-commit", "explicit_markdown"),
        GitHubRef(
            "extuser",
            "custom-page",
            "github_pages",
            discovered_from_url="https://extuser.github.io/custom-page/",
            entry_title="Custom Page",
        ),
        GitHubRef(
            "vintage",
            "vintage.github.io",
            "github_pages",
            discovered_from_url="https://vintage.github.io/",
            entry_title="Vintage Site",
        ),
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal external_fetched
        if request.url.host == "evil-external.example.com":
            external_fetched = True
            return httpx.Response(200, text="should never be fetched!")
        if request.url.host == "extuser.github.io":
            return httpx.Response(
                301,
                headers={"Location": "https://evil-external.example.com/landing"},
            )
        if request.url.host == "vintage.github.io":
            return httpx.Response(
                200,
                headers={"content-type": "text/html"},
                text='<html><a href="https://github.com/twbs/bootstrap">CSS</a></html>',
            )
        if request.url.path == "/graphql":
            import json as _json

            query_str = _json.loads(request.read().decode("utf-8"))["query"]
            if "vintage.github.com" in query_str:
                return httpx.Response(
                    200,
                    json={
                        "data": {
                            "r0": {
                                "nameWithOwner": "vintage/vintage.github.com",
                                "description": "Legacy 2012 pages repo",
                                "homepageUrl": "",
                                "isArchived": False,
                                "isDisabled": False,
                                "isEmpty": False,
                                "pushedAt": "2018-01-01T00:00:00Z",
                                "defaultBranchRef": None,
                                "releases": {"nodes": []},
                            }
                        }
                    },
                )
            if "twbs" in query_str:
                return httpx.Response(
                    200,
                    json={
                        "data": {
                            "r0": {
                                "nameWithOwner": "twbs/bootstrap",
                                "description": "Bootstrap",
                                "homepageUrl": "https://getbootstrap.com",
                                "isArchived": False,
                                "isDisabled": False,
                                "isEmpty": False,
                                "pushedAt": "2026-11-23T00:00:00Z",
                                "defaultBranchRef": None,
                                "releases": {"nodes": []},
                            }
                        }
                    },
                )
            if "extuser.github.io" in query_str or "extuser.github.com" in query_str:
                return httpx.Response(200, json={"data": {"r0": None}})
            return httpx.Response(
                200,
                json={
                    "data": {
                        "r0": None,
                        "r1": {
                            "nameWithOwner": "org/empty-repo",
                            "description": "",
                            "homepageUrl": "",
                            "isArchived": False,
                            "isDisabled": False,
                            "isEmpty": True,
                            "pushedAt": "2026-11-23T12:00:00Z",
                            "defaultBranchRef": None,
                            "releases": {"nodes": []},
                        },
                        "r2": {
                            "nameWithOwner": "org/spoofed-commit",
                            "description": "",
                            "homepageUrl": "",
                            "isArchived": False,
                            "isDisabled": False,
                            "isEmpty": False,
                            "pushedAt": "2023-01-01T00:00:00Z",
                            "defaultBranchRef": {
                                "target": {"committedDate": "2099-01-01T00:00:00Z"}
                            },
                            "releases": {"nodes": []},
                        },
                        "r3": None,
                        "r4": None,
                    }
                },
            )
        if request.url.path in {
            "/repos/shared/gone-repo",
            "/repos/extuser/custom-page",
            "/repos/extuser/extuser.github.io",
            "/repos/extuser/extuser.github.com",
            "/repos/vintage/vintage.github.io",
        }:
            return httpx.Response(404)
        if request.url.path in {"/users/extuser/repos", "/users/vintage/repos"}:
            return httpx.Response(200, json=[])
        if request.url.path == "/repos/twbs/bootstrap":
            return httpx.Response(200, json={"full_name": "twbs/bootstrap", "pushed_at": "2026-11-23T00:00:00Z"})
        return httpx.Response(404)

    checker = GitHubActivityChecker(
        thresholds=TierThresholds(90, 180, 365),
        github_token="ghp_testtoken",
        now=FIXED_NOW,
        transport=httpx.MockTransport(handler),
    )
    results = asyncio.run(checker.check_repos(refs))

    assert not external_fetched
    # explicit_markdown beat domain_heuristic so 404 is preserved as MISSING!
    assert results["shared/gone-repo"].tier == StalenessTier.MISSING
    # Empty repo is flagged as TIER_3_CRITICAL with inactive_days=None despite pushedAt yesterday!
    assert results["org/empty-repo"].tier == StalenessTier.TIER_3_CRITICAL
    assert results["org/empty-repo"].inactive_days is None
    # Future-spoofed 2099 committedDate was clamped to 2023 pushedAt!
    assert results["org/spoofed-commit"].tier == StalenessTier.TIER_3_CRITICAL
    # Redirect leaving *.github.io treated pages_alive=True without fetching external URL!
    assert results["extuser/custom-page"].tier == StalenessTier.ACTIVE
    assert "backed by private" in results["extuser/custom-page"].reason
    # Vintage *.github.io skipped unrelated twbs/bootstrap link and resolved legacy <owner>.github.com!
    assert results["vintage/vintage.github.io"].full_name == "vintage/vintage.github.com"
