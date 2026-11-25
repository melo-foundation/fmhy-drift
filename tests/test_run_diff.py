"""Tests for marking new / changed rows against the previously published report."""

from __future__ import annotations

from fmhy_drift.run_diff import annotate_changes


def _report(repos: list[dict], sites: list[dict]) -> dict:
    return {
        "generated_at": "2026-11-24T00:00:00+00:00",
        "entries": [{"entry_id": "e1", "github_repos": repos, "sites": sites}],
    }


def _repo(name: str, tier: str, **kw) -> dict:
    return {"requested_full_name": name, "full_name": name, "tier": tier, "vulns": [], **kw}


def _site(url: str, status: str, kinds: list[tuple[str, str]] = ()) -> dict:
    return {"url": url, "status": status, "signals": [{"kind": k, "severity": s} for k, s in kinds]}


def test_no_previous_report_marks_nothing() -> None:
    cur = annotate_changes(_report([_repo("a/b", "archived")], []), None)
    assert cur["changes"] is None
    assert "change" not in cur["entries"][0]["github_repos"][0]


def test_new_changed_unchanged_and_resolved() -> None:
    prev = _report(
        [
            _repo("same/repo", "tier3_critical"),
            _repo("tier/move", "tier1_notice"),
            _repo("was/active", "active"),
            _repo("fixed/repo", "archived"),
        ],
        [
            _site("https://same.example/", "flagged", [("archive_moved", "notice")]),
            _site("https://grows.example/", "flagged", [("domain_expired", "warning")]),
            _site("https://gone.example/", "dead"),
        ],
    )
    cur = _report(
        [
            _repo("same/repo", "tier3_critical", inactive_days=999),
            _repo("tier/move", "tier2_warning"),
            _repo("was/active", "tier1_notice"),
            _repo("fixed/repo", "active"),
            _repo("brand/new", "missing"),
            _repo("quiet/new", "active"),
        ],
        [
            _site("https://same.example/", "flagged", [("archive_moved", "notice")]),
            _site("https://grows.example/", "flagged", [("domain_expired", "warning"), ("reregistered", "critical")]),
            _site("https://fresh.example/", "parked", [("parked_dns", "warning")]),
            _site("https://alive.example/", "alive"),
        ],
    )
    out = annotate_changes(cur, prev)
    repos = {r["requested_full_name"]: r for r in out["entries"][0]["github_repos"]}
    sites = {s["url"]: s for s in out["entries"][0]["sites"]}

    assert "change" not in repos["same/repo"]
    assert repos["tier/move"]["change"] == "changed"
    assert repos["tier/move"]["change_from"] == "tier1_notice"
    assert repos["was/active"]["change"] == "new"
    assert repos["brand/new"]["change"] == "new"
    assert "change" not in repos["fixed/repo"]
    assert "change" not in repos["quiet/new"]

    assert "change" not in sites["https://same.example/"]
    assert sites["https://grows.example/"]["change"] == "changed"
    assert sites["https://fresh.example/"]["change"] == "new"
    assert "change" not in sites["https://alive.example/"]

    assert out["changes"] == {
        "previous_generated_at": "2026-11-24T00:00:00+00:00",
        "new": 3,
        "changed": 2,
        "resolved": 2,
    }


def test_carry_keeps_existing_marks_and_baseline() -> None:
    published = _report(
        [_repo("kept/mark", "archived", change="new", change_from=None)],
        [_site("https://x.example/", "flagged", [("domain_expired", "warning")])],
    )
    published["entries"][0]["sites"][0].update(change="new", change_from=None)
    published["changes"] = {"previous_generated_at": "2026-11-20T00:00:00+00:00", "new": 2, "changed": 0, "resolved": 1}
    published["generated_at"] = "2026-11-25T00:00:00+00:00"
    cur = _report(
        [_repo("kept/mark", "archived"), _repo("fresh/one", "missing")],
        [_site("https://x.example/", "flagged", [("domain_held", "warning")])],
    )
    out = annotate_changes(cur, published, carry=True)
    repos = {r["requested_full_name"]: r for r in out["entries"][0]["github_repos"]}
    site = out["entries"][0]["sites"][0]
    assert repos["kept/mark"]["change"] == "new"
    assert repos["fresh/one"]["change"] == "new"
    assert site["change"] == "new"
    assert out["changes"]["previous_generated_at"] == "2026-11-20T00:00:00+00:00"
    assert out["changes"]["resolved"] == 1
