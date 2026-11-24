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
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/graphql":
            import json as _json

            query_str = _json.loads(request.read().decode("utf-8"))["query"]
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
                            "isDisabled": False,
                            "pushedAt": "2026-11-20T12:00:00Z",
                            "defaultBranchRef": None,
                            "releases": {"nodes": []},
                        },
                        "r5": {
                            "nameWithOwner": "org/disabled-repo",
                            "description": "",
                            "homepageUrl": "",
                            "isArchived": False,
                            "isDisabled": True,
                            "pushedAt": "2026-11-20T12:00:00Z",
                            "defaultBranchRef": None,
                            "releases": {"nodes": []},
                        },
                        "r6": None,
                        "r7": None,
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

    # Archived repo has its GitHub Pages homepage AND verified replacement repo populated!
    arch = results["org/archived-repo"]
    assert arch.tier == StalenessTier.ARCHIVED
    assert arch.homepage_url == "https://org.github.io/archived-repo/"
    assert arch.replacement_url == "https://github.com/new-org/successor-repo"
    assert arch.replacement_status == "active, last push 4d ago"

    # Disabled repo is classified with Dead (MISSING), NOT Archived!
    assert results["org/disabled-repo"].tier == StalenessTier.MISSING

    renamed = results["old-org/renamed-repo"]
    assert renamed.tier == StalenessTier.TIER_3_CRITICAL
    assert renamed.full_name == "new-org/renamed-repo"
    assert renamed.homepage_url == "https://renamed.example.com/"

    assert results["org/deleted-repo"].tier == StalenessTier.MISSING
