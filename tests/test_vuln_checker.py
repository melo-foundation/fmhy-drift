"""Unit tests for OSV.dev severe (`CRITICAL` / `HIGH`) vulnerability scanning on stale/archived repos."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import httpx

from fmhy_drift.models import RepoActivityResult, StalenessTier
from fmhy_drift.vuln_checker import (
    RepoVulnChecker,
    parse_cargo_toml_deps,
    parse_go_mod_deps,
    parse_package_json_deps,
    parse_requirements_txt_deps,
)


def test_manifest_parsers_skip_dev_dependencies_and_extract_versions() -> None:
    pkg_json = """{
      "dependencies": {
        "next": "12.1.0",
        "electron": "^13.1.0",
        "local-pkg": "workspace:*"
      },
      "devDependencies": {
        "jest": "26.0.0"
      }
    }"""
    npm_deps = parse_package_json_deps(pkg_json)
    assert [(d.name, d.version, d.range_op) for d in npm_deps] == [
        ("next", "12.1.0", ""),
        ("electron", "13.1.0", "^"),
    ]

    req_txt = "Flask==2.0.1 # comment\nrequests~=2.25.0\npytest>=7.0.0\n"
    py_deps = parse_requirements_txt_deps(req_txt)
    assert [(d.name, d.version, d.range_op) for d in py_deps] == [
        ("Flask", "2.0.1", ""),
        ("requests", "2.25.0", "~"),
    ]

    go_mod = """module example.com/app
    require (
        github.com/gin-gonic/gin v1.7.0
        golang.org/x/sys v0.1.0 // indirect
    )
    """
    go_deps = parse_go_mod_deps(go_mod)
    assert [(d.name, d.version) for d in go_deps] == [("github.com/gin-gonic/gin", "1.7.0")]

    cargo_toml = """
    [dependencies]
    hyper = "0.14.10"
    tokio = { version = "=1.18.0", features = ["full"] }
    [dev-dependencies]
    criterion = "0.3.0"
    """
    rs_deps = parse_cargo_toml_deps(cargo_toml)
    assert [(d.name, d.version, d.range_op) for d in rs_deps] == [
        ("hyper", "0.14.10", "^"),
        ("tokio", "1.18.0", ""),
    ]


def test_vuln_checker_flags_critical_and_high_and_skips_moderate_or_active_repos() -> None:
    repo_results = {
        "org/stale-app": RepoActivityResult(
            owner="org",
            repo="stale-app",
            tier=StalenessTier.TIER_3_CRITICAL,
            last_activity_at=datetime(2023, 5, 1, tzinfo=UTC),
            inactive_days=1303,
            head_commit_oid="deadbeef1234567890",
        ),
        "org/active-app": RepoActivityResult(
            owner="org",
            repo="active-app",
            tier=StalenessTier.ACTIVE,
            last_activity_at=datetime(2026, 11, 20, tzinfo=UTC),
            inactive_days=4,
            head_commit_oid="cafebabe1234567890",
        ),
    }

    def gh_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "data": {
                    "r0": {
                        "pkgJson": {
                            "byteSize": 120,
                            "text": '{"dependencies": {"electron": "^13.1.0", "lodash": "4.17.20"}}',
                        },
                        "reqTxt": None,
                        "goMod": None,
                        "cargoToml": None,
                    }
                }
            },
        )

    def osv_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/querybatch":
            import json as _json

            queries = _json.loads(request.read().decode("utf-8"))["queries"]
            out = []
            for q in queries:
                if q.get("commit") == "deadbeef1234567890":
                    out.append({"vulns": [{"id": "GHSA-crit-self"}]})
                elif (q.get("package") or {}).get("name") == "electron":
                    out.append({"vulns": [{"id": "GHSA-high-elec"}]})
                elif (q.get("package") or {}).get("name") == "lodash":
                    out.append({"vulns": [{"id": "GHSA-mod-lodash"}]})
                else:
                    out.append({})
            return httpx.Response(200, json={"results": out})

        if request.url.path == "/v1/vulns/GHSA-crit-self":
            return httpx.Response(
                200,
                json={
                    "id": "GHSA-crit-self",
                    "aliases": ["CVE-2025-99999"],
                    "summary": "Remote code execution in stale-app",
                    "published": "2025-02-01T00:00:00Z",
                    "database_specific": {"severity": "CRITICAL"},
                },
            )
        if request.url.path == "/v1/vulns/GHSA-high-elec":
            return httpx.Response(
                200,
                json={
                    "id": "GHSA-high-elec",
                    "aliases": ["CVE-2023-44444"],
                    "summary": "Sandbox escape in Electron 13",
                    "published": "2022-09-01T00:00:00Z",
                    "database_specific": {"severity": "HIGH"},
                    "affected": [
                        {
                            "package": {"name": "electron", "ecosystem": "npm"},
                            "ranges": [{"type": "SEMVER", "events": [{"introduced": "0"}, {"fixed": "15.0.0"}]}],
                        }
                    ],
                },
            )
        if request.url.path == "/v1/vulns/GHSA-mod-lodash":
            return httpx.Response(
                200,
                json={
                    "id": "GHSA-mod-lodash",
                    "summary": "Moderate issue",
                    "database_specific": {"severity": "MODERATE"},
                },
            )
        return httpx.Response(404)

    checker = RepoVulnChecker(
        github_token="ghp_test",
        gh_transport=httpx.MockTransport(gh_handler),
        osv_transport=httpx.MockTransport(osv_handler),
    )
    enriched = asyncio.run(checker.enrich_repo_vulns(repo_results))

    # Active repo wasn't scanned
    assert enriched["org/active-app"].vulns == []

    # Stale repo captured CRITICAL self CVE + HIGH Electron CVE, and skipped MODERATE lodash!
    stale_vulns = enriched["org/stale-app"].vulns
    assert len(stale_vulns) == 2
    assert stale_vulns[0].vuln_id == "CVE-2025-99999"
    assert stale_vulns[0].severity == "CRITICAL"
    assert stale_vulns[0].package_name is None
    assert stale_vulns[1].vuln_id == "CVE-2023-44444"
    assert stale_vulns[1].severity == "HIGH"
    assert stale_vulns[1].package_name == "npm:electron@^13.1.0"
