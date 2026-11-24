"""Unit tests for GitHub GraphQL batching, REST fallback, and 3m/6m/1y staleness tiers."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import httpx

from fmhy_drift.github_checker import GitHubActivityChecker
from fmhy_drift.models import GitHubRef, StalenessTier, TierThresholds

FIXED_NOW = datetime(2026, 11, 24, 12, 0, 0, tzinfo=UTC)


def test_github_activity_graphql_and_rest_fallback() -> None:
    refs = [
        GitHubRef("org", "active-repo", "explicit_markdown"),       # 15 days ago -> ACTIVE
        GitHubRef("org", "tier1-repo", "explicit_markdown"),        # 105 days ago -> TIER_1_NOTICE (>=90d)
        GitHubRef("org", "tier2-repo", "explicit_markdown"),        # 210 days ago -> TIER_2_WARNING (>=180d)
        GitHubRef("org", "tier3-repo", "discovered_on_site"),       # 400 days ago -> TIER_3_CRITICAL (>=365d)
        GitHubRef("org", "archived-repo", "explicit_markdown"),     # Archived -> ARCHIVED
        GitHubRef("old-org", "renamed-repo", "explicit_markdown"),  # GraphQL null -> REST 200 redirected
        GitHubRef("org", "deleted-repo", "explicit_markdown"),      # GraphQL null -> REST 404 MISSING
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/graphql":
            return httpx.Response(
                200,
                json={
                    "data": {
                        "r0": {
                            "nameWithOwner": "org/active-repo",
                            "isArchived": False,
                            "isDisabled": False,
                            "pushedAt": "2026-11-09T12:00:00Z",
                            "defaultBranchRef": {"target": {"committedDate": "2026-11-09T12:00:00Z"}},
                            "releases": {"nodes": []},
                        },
                        "r1": {
                            "nameWithOwner": "org/tier1-repo",
                            "isArchived": False,
                            "isDisabled": False,
                            "pushedAt": "2026-08-11T12:00:00Z",  # 105 days before Nov 24
                            "defaultBranchRef": None,
                            "releases": {"nodes": []},
                        },
                        "r2": {
                            "nameWithOwner": "org/tier2-repo",
                            "isArchived": False,
                            "isDisabled": False,
                            "pushedAt": "2026-04-28T12:00:00Z",  # 210 days before Nov 24
                            "defaultBranchRef": None,
                            "releases": {"nodes": []},
                        },
                        "r3": {
                            "nameWithOwner": "org/tier3-repo",
                            "isArchived": False,
                            "isDisabled": False,
                            "pushedAt": "2025-10-20T12:00:00Z",  # 400 days before Nov 24
                            "defaultBranchRef": None,
                            "releases": {"nodes": []},
                        },
                        "r4": {
                            "nameWithOwner": "org/archived-repo",
                            "isArchived": True,
                            "isDisabled": False,
                            "pushedAt": "2026-11-20T12:00:00Z",
                            "defaultBranchRef": None,
                            "releases": {"nodes": []},
                        },
                        "r5": None,
                        "r6": None,
                    }
                },
            )

        if request.url.path == "/repos/old-org/renamed-repo":
            # Simulates a repo starred today (`updated_at`) whose last push was 500 days ago
            return httpx.Response(
                200,
                json={
                    "full_name": "new-org/renamed-repo",
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
    assert results["org/active-repo"].inactive_days == 15

    assert results["org/tier1-repo"].tier == StalenessTier.TIER_1_NOTICE
    assert results["org/tier1-repo"].inactive_days == 105

    assert results["org/tier2-repo"].tier == StalenessTier.TIER_2_WARNING
    assert results["org/tier2-repo"].inactive_days == 210

    assert results["org/tier3-repo"].tier == StalenessTier.TIER_3_CRITICAL
    assert results["org/tier3-repo"].inactive_days == 400

    assert results["org/archived-repo"].tier == StalenessTier.ARCHIVED

    # Verifies `updated_at` (star timestamp) was ignored in favor of `pushed_at`, and rename captured
    renamed = results["old-org/renamed-repo"]
    assert renamed.tier == StalenessTier.TIER_3_CRITICAL
    assert renamed.full_name == "new-org/renamed-repo"
    assert "redirected -> new-org/renamed-repo" in renamed.reason

    assert results["org/deleted-repo"].tier == StalenessTier.MISSING
