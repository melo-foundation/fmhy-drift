"""Severe (`CRITICAL` / `HIGH`) vulnerability checker for stale and archived GitHub repos.

Uses Google's OSV.dev batch API (`https://api.osv.dev/v1/querybatch`) to check:
1. Unpatched CVEs/GHSAs affecting the repository's own `HEAD` commit SHA.
2. Runtime dependencies declared in root manifests (`package.json`, `requirements.txt`,
   `go.mod`, `Cargo.toml`) of stale/archived repositories.
"""

from __future__ import annotations

import asyncio
import json
import re
import tomllib
from dataclasses import dataclass, replace
from datetime import UTC, datetime

import httpx

from fmhy_drift.models import RepoActivityResult, RepoVuln, StalenessTier

GITHUB_GRAPHQL_URL = "https://api.github.com/graphql"
OSV_QUERYBATCH_URL = "https://api.osv.dev/v1/querybatch"
OSV_VULN_URL_TMPL = "https://api.osv.dev/v1/vulns/{vuln_id}"

OSV_BATCH_SIZE = 500
MANIFEST_GRAPHQL_BATCH = 25
MAX_MANIFEST_BYTES = 131_072
MAX_VULNS_PER_REPO = 8

SEMVER_CLEAN_RE = re.compile(r"^([~^=v\s]*)(\d+(?:\.\d+){0,3}(?:-[0-9A-Za-z.-]+)?)")
REQ_LINE_RE = re.compile(
    r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)\s*(?:\[[^\]]*\])?\s*(==|~=)\s*([0-9][0-9A-Za-z._*-]*)"
)
GO_REQ_LINE_RE = re.compile(r"^\s*([A-Za-z0-9._/-]+\.[A-Za-z0-9._/-]+)\s+(v\d+\.\d+\.\d+[^\s/]*)")


@dataclass(frozen=True)
class DepSpec:
    """Extracted runtime dependency version or caret/tilde range from a repo manifest."""

    ecosystem: str  # "npm", "PyPI", "Go", "crates.io"
    name: str
    version: str
    range_op: str = ""  # "", "^", "~"

    @property
    def query_key(self) -> tuple[str, str, str]:
        return (self.ecosystem, self.name, self.version)

    @property
    def display_label(self) -> str:
        return f"{self.ecosystem}:{self.name}@{self.range_op}{self.version}"


def _parse_semver_tuple(ver: str) -> tuple[int, int, int] | None:
    cleaned = ver.lstrip("v").split("-", 1)[0].split("+", 1)[0]
    parts = cleaned.split(".")
    try:
        major = int(parts[0])
        minor = int(parts[1]) if len(parts) > 1 else 0
        patch = int(parts[2]) if len(parts) > 2 else 0
        return (major, minor, patch)
    except (ValueError, IndexError):
        return None


def parse_package_json_deps(text: str) -> list[DepSpec]:
    """Extract runtime `dependencies` (excluding `devDependencies`) from `package.json`."""
    try:
        data = json.loads(text)
    except Exception:
        return []
    if not isinstance(data, dict):
        return []

    deps = data.get("dependencies")
    if not isinstance(deps, dict):
        return []

    out: list[DepSpec] = []
    for pkg, raw_spec in deps.items():
        if not isinstance(pkg, str) or not isinstance(raw_spec, str):
            continue
        spec = raw_spec.strip()
        if not spec or spec.startswith(("workspace:", "file:", "link:", "git+", "http:", "https:", "*")):
            continue
        m = SEMVER_CLEAN_RE.match(spec)
        if not m:
            continue
        prefix, ver = m.group(1).strip(), m.group(2)
        if "." not in ver:
            continue
        op = "^" if "^" in prefix else ("~" if "~" in prefix else "")
        out.append(DepSpec(ecosystem="npm", name=pkg, version=ver, range_op=op))
    return out


def parse_requirements_txt_deps(text: str) -> list[DepSpec]:
    """Extract pinned (`==` or `~=`) dependencies from `requirements.txt`."""
    out: list[DepSpec] = []
    for raw_line in text.splitlines():
        line = raw_line.split("#", 1)[0].strip()
        if not line or line.startswith(("-", "git+", "http:", "https:")):
            continue
        m = REQ_LINE_RE.match(line)
        if not m:
            continue
        name, op_raw, ver = m.group(1), m.group(2), m.group(3)
        if "*" in ver:
            continue
        op = "~" if op_raw == "~=" else ""
        out.append(DepSpec(ecosystem="PyPI", name=name, version=ver, range_op=op))
    return out


def parse_go_mod_deps(text: str) -> list[DepSpec]:
    """Extract direct `require` module versions from `go.mod`."""
    out: list[DepSpec] = []
    in_require_block = False
    for raw_line in text.splitlines():
        if "// indirect" in raw_line:
            continue
        line = raw_line.split("//", 1)[0].strip()
        if not line:
            continue
        if line.startswith("require ("):
            in_require_block = True
            continue
        if in_require_block and line == ")":
            in_require_block = False
            continue
        target = line[len("require ") :].strip() if line.startswith("require ") else (line if in_require_block else "")
        if not target:
            continue
        m = GO_REQ_LINE_RE.match(target)
        if m:
            mod_name, ver = m.group(1), m.group(2).lstrip("v")
            out.append(DepSpec(ecosystem="Go", name=mod_name, version=ver, range_op=""))
    return out


def parse_cargo_toml_deps(text: str) -> list[DepSpec]:
    """Extract runtime `[dependencies]` from `Cargo.toml` (skipping dev/build dependencies)."""
    try:
        data = tomllib.loads(text)
    except Exception:
        return []
    if not isinstance(data, dict):
        return []

    deps = data.get("dependencies")
    if not isinstance(deps, dict):
        return []

    out: list[DepSpec] = []
    for crate, spec in deps.items():
        raw_ver = ""
        pkg_name = crate
        if isinstance(spec, str):
            raw_ver = spec.strip()
        elif isinstance(spec, dict):
            if spec.get("path") or spec.get("git"):
                continue
            if isinstance(spec.get("package"), str):
                pkg_name = spec["package"]
            if isinstance(spec.get("version"), str):
                raw_ver = spec["version"].strip()
        if not raw_ver or "*" in raw_ver:
            continue
        m = SEMVER_CLEAN_RE.match(raw_ver)
        if not m:
            continue
        prefix, ver = m.group(1).strip(), m.group(2)
        if "." not in ver:
            continue
        # Note: in Cargo, bare `"1.2.3"` is equivalent to `"^1.2.3"` unless `"=1.2.3"`
        op = "" if "=" in prefix else ("~" if "~" in prefix else "^")
        out.append(DepSpec(ecosystem="crates.io", name=pkg_name, version=ver, range_op=op))
    return out


def _extract_severity(vuln_doc: dict) -> str | None:
    """Return `'CRITICAL'` or `'HIGH'` if the OSV advisory is severe; else `None`."""
    if vuln_doc.get("withdrawn"):
        return None

    db_spec = vuln_doc.get("database_specific")
    if isinstance(db_spec, dict):
        sev = str(db_spec.get("severity") or "").upper().strip()
        if sev in {"CRITICAL", "HIGH"}:
            return sev
        if sev in {"MODERATE", "MEDIUM", "LOW"}:
            return None

    # Check ecosystem_specific or CVSS vector heuristics when database_specific.severity is absent
    for aff in vuln_doc.get("affected") or []:
        if not isinstance(aff, dict):
            continue
        eco_spec = aff.get("ecosystem_specific")
        if isinstance(eco_spec, dict):
            es = str(eco_spec.get("severity") or "").upper().strip()
            if es in {"CRITICAL", "HIGH"}:
                return es

    # Minimal CVSS v3.x / v4 high-impact check if only vector string is provided
    for sev_entry in vuln_doc.get("severity") or []:
        if not isinstance(sev_entry, dict):
            continue
        score_str = str(sev_entry.get("score") or "")
        try:
            num = float(score_str)
            if num >= 9.0:
                return "CRITICAL"
            if num >= 7.0:
                return "HIGH"
        except ValueError:
            pass
        # CVSS:3.1/AV:N/AC:L/PR:N/... with High C/I/A impact
        if score_str.startswith("CVSS:3.") and "/AV:N/AC:L/PR:N/UI:N/" in score_str:
            if "/C:H/I:H/A:H" in score_str:
                return "CRITICAL"
            if "/C:H" in score_str or "/I:H" in score_str:
                return "HIGH"

    return None


def _parse_iso(ts: str | None) -> datetime | None:
    if not ts:
        return None
    s = ts.strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
        return dt.astimezone(UTC) if dt.tzinfo else dt.replace(tzinfo=UTC)
    except ValueError:
        return None


def _range_still_vulnerable_or_postdates_repo(
    vuln_doc: dict,
    dep: DepSpec,
    repo_last_activity: datetime | None,
) -> bool:
    """Guard caret/tilde ranges (`^` / `~`) so old advisories fixed within range before repo's last push don't false-flag."""
    if not dep.range_op:
        # Exact pinned version is always vulnerable
        return True

    published_at = _parse_iso(vuln_doc.get("published"))
    if published_at and repo_last_activity and published_at > repo_last_activity:
        # Advisory was published AFTER the repo's last commit/release -> repo's lockfile/builds predate the fix!
        return True

    base_tuple = _parse_semver_tuple(dep.version)
    if base_tuple is None:
        return True

    # Check if ANY `fixed` version in OSV falls inside the allowed `^` or `~` window
    fixed_versions: list[tuple[int, int, int]] = []
    for aff in vuln_doc.get("affected") or []:
        if not isinstance(aff, dict):
            continue
        pkg = aff.get("package") or {}
        if str(pkg.get("name") or "").lower() != dep.name.lower():
            continue
        for rng in aff.get("ranges") or []:
            if not isinstance(rng, dict):
                continue
            for ev in rng.get("events") or []:
                if isinstance(ev, dict) and isinstance(ev.get("fixed"), str):
                    ft = _parse_semver_tuple(ev["fixed"])
                    if ft is not None:
                        fixed_versions.append(ft)

    if not fixed_versions:
        # Unfixed vulnerability!
        return True

    for f_maj, f_min, _f_pat in fixed_versions:
        if dep.range_op == "^":
            if base_tuple[0] > 0 and f_maj == base_tuple[0] and (f_maj, f_min) >= (base_tuple[0], base_tuple[1]):
                return False
            if base_tuple[0] == 0 and f_maj == 0 and f_min == base_tuple[1]:
                return False
        elif dep.range_op == "~":
            if f_maj == base_tuple[0] and f_min == base_tuple[1]:
                return False

    return True


def _preferred_vuln_id_and_url(vuln_doc: dict) -> tuple[str, str]:
    raw_id = str(vuln_doc.get("id") or "")
    aliases = [str(a) for a in (vuln_doc.get("aliases") or []) if isinstance(a, str)]
    cve_id = next((a for a in aliases if a.startswith("CVE-")), None)
    ghsa_id = raw_id if raw_id.startswith("GHSA-") else next((a for a in aliases if a.startswith("GHSA-")), None)

    display_id = cve_id or raw_id
    if ghsa_id:
        url = f"https://github.com/advisories/{ghsa_id}"
    else:
        url = f"https://osv.dev/vulnerability/{raw_id}"
    return display_id, url


def _build_manifest_graphql_query(batch: list[tuple[str, str, str]]) -> str:
    fields: list[str] = []
    for idx, (_, owner, repo) in enumerate(batch):
        o_json = json.dumps(owner, ensure_ascii=False)
        r_json = json.dumps(repo, ensure_ascii=False)
        fields.append(
            f"""
  r{idx}: repository(owner: {o_json}, name: {r_json}) {{
    pkgJson: object(expression: "HEAD:package.json") {{ ... on Blob {{ byteSize text }} }}
    reqTxt: object(expression: "HEAD:requirements.txt") {{ ... on Blob {{ byteSize text }} }}
    goMod: object(expression: "HEAD:go.mod") {{ ... on Blob {{ byteSize text }} }}
    cargoToml: object(expression: "HEAD:Cargo.toml") {{ ... on Blob {{ byteSize text }} }}
  }}"""
        )
    return "query {" + "".join(fields) + "\n}"


class RepoVulnChecker:
    """Scans stale and archived repositories for HIGH and CRITICAL vulnerabilities via OSV.dev."""

    def __init__(
        self,
        *,
        github_token: str | None = None,
        min_inactive_days: int = 90,
        timeout: float = 20.0,
        gh_transport: httpx.AsyncBaseTransport | None = None,
        osv_transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.github_token = github_token.strip() if github_token else None
        self.min_inactive_days = min_inactive_days
        self.timeout = timeout
        self._gh_transport = gh_transport
        self._osv_transport = osv_transport

    def _is_candidate_repo(self, rr: RepoActivityResult) -> bool:
        if rr.tier in {StalenessTier.MISSING, StalenessTier.ERROR, StalenessTier.ACTIVE}:
            return False
        if rr.is_archived or rr.tier == StalenessTier.ARCHIVED:
            return True
        return rr.inactive_days is not None and rr.inactive_days >= self.min_inactive_days

    async def enrich_repo_vulns(
        self,
        repo_results: dict[str, RepoActivityResult],
    ) -> dict[str, RepoActivityResult]:
        """Populate `vulns` on stale/archived entries in `repo_results` and return updated map."""
        target_keys = [k for k, rr in repo_results.items() if self._is_candidate_repo(rr)]
        if not target_keys:
            return repo_results

        # 1. Fetch dependency manifests via batched GraphQL (if GITHUB_TOKEN is available)
        repo_deps: dict[str, list[DepSpec]] = {k: [] for k in target_keys}
        if self.github_token:
            await self._fetch_manifests_graphql(repo_results, target_keys, repo_deps)

        # 2. Build deduplicated OSV querybatch list:
        #    a) Direct repo HEAD commit queries (`{"commit": oid}`)
        #    b) Deduplicated dependency queries (`{"package": {...}, "version": ...}`)
        osv_kwargs: dict = {
            "timeout": httpx.Timeout(self.timeout),
            "follow_redirects": True,
            "trust_env": False,
            "headers": {"User-Agent": "fmhy-drift-checker/0.1"},
        }
        if self._osv_transport is not None:
            osv_kwargs["transport"] = self._osv_transport

        commit_keys: list[str] = []
        commit_queries: list[dict] = []
        for k in target_keys:
            oid = repo_results[k].head_commit_oid
            if oid and len(oid) >= 7:
                commit_keys.append(k)
                commit_queries.append({"commit": oid})

        unique_dep_map: dict[tuple[str, str, str], DepSpec] = {}
        for deps in repo_deps.values():
            for d in deps:
                unique_dep_map.setdefault(d.query_key, d)

        dep_keys_list = list(unique_dep_map.keys())
        dep_queries = [
            {"package": {"name": name, "ecosystem": eco}, "version": ver}
            for (eco, name, ver) in dep_keys_list
        ]

        async with httpx.AsyncClient(**osv_kwargs) as osv_client:
            commit_vuln_ids = await self._run_osv_querybatch(osv_client, commit_queries)
            dep_vuln_ids = await self._run_osv_querybatch(osv_client, dep_queries)

            all_vuln_ids: set[str] = set()
            for vids in commit_vuln_ids:
                all_vuln_ids.update(vids)
            for vids in dep_vuln_ids:
                all_vuln_ids.update(vids)

            if not all_vuln_ids:
                return repo_results

            vuln_docs = await self._fetch_osv_vuln_details(osv_client, sorted(all_vuln_ids))

        # 3. Map severe (`CRITICAL` / `HIGH`) advisories back onto each repository
        commit_hits_by_repo: dict[str, list[str]] = {
            rk: vids for rk, vids in zip(commit_keys, commit_vuln_ids, strict=True) if vids
        }
        dep_hits_by_tuple: dict[tuple[str, str, str], list[str]] = {
            dk: vids for dk, vids in zip(dep_keys_list, dep_vuln_ids, strict=True) if vids
        }

        updated = dict(repo_results)
        for k in target_keys:
            rr = updated[k]
            collected: list[RepoVuln] = []
            seen_display_ids: set[str] = set()

            # 3a. Direct repo commit vulnerabilities
            for vid in commit_hits_by_repo.get(k, []):
                doc = vuln_docs.get(vid)
                if not doc:
                    continue
                sev = _extract_severity(doc)
                if sev not in {"CRITICAL", "HIGH"}:
                    continue
                disp_id, adv_url = _preferred_vuln_id_and_url(doc)
                if disp_id in seen_display_ids:
                    continue
                seen_display_ids.add(disp_id)
                summary = str(doc.get("summary") or doc.get("details") or "")[:140].strip()
                collected.append(
                    RepoVuln(
                        vuln_id=disp_id,
                        severity=sev,  # type: ignore[arg-type]
                        summary=summary or "Unpatched repository vulnerability",
                        package_name=None,
                        advisory_url=adv_url,
                    )
                )

            # 3b. Runtime dependency vulnerabilities
            for dep in repo_deps.get(k, []):
                for vid in dep_hits_by_tuple.get(dep.query_key, []):
                    doc = vuln_docs.get(vid)
                    if not doc:
                        continue
                    sev = _extract_severity(doc)
                    if sev not in {"CRITICAL", "HIGH"}:
                        continue
                    if not _range_still_vulnerable_or_postdates_repo(doc, dep, rr.last_activity_at):
                        continue
                    disp_id, adv_url = _preferred_vuln_id_and_url(doc)
                    if disp_id in seen_display_ids:
                        continue
                    seen_display_ids.add(disp_id)
                    summary = str(doc.get("summary") or doc.get("details") or "")[:140].strip()
                    collected.append(
                        RepoVuln(
                            vuln_id=disp_id,
                            severity=sev,  # type: ignore[arg-type]
                            summary=summary or f"Vulnerable {dep.display_label}",
                            package_name=dep.display_label,
                            advisory_url=adv_url,
                        )
                    )

            if collected:
                # Sort CRITICAL ahead of HIGH
                collected.sort(key=lambda v: (0 if v.severity == "CRITICAL" else 1, v.vuln_id))
                updated[k] = replace(rr, vulns=collected[:MAX_VULNS_PER_REPO])

        return updated

    async def _fetch_manifests_graphql(
        self,
        repo_results: dict[str, RepoActivityResult],
        target_keys: list[str],
        repo_deps: dict[str, list[DepSpec]],
    ) -> None:
        items: list[tuple[str, str, str]] = []
        for k in target_keys:
            rr = repo_results[k]
            if "/" in rr.full_name:
                o, r = rr.full_name.split("/", 1)
            else:
                o, r = rr.owner, rr.repo
            items.append((k, o, r))

        gh_kwargs: dict = {
            "timeout": httpx.Timeout(self.timeout),
            "follow_redirects": True,
            "trust_env": False,
            "headers": {
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {self.github_token}",
                "User-Agent": "fmhy-drift-checker/0.1",
            },
        }
        if self._gh_transport is not None:
            gh_kwargs["transport"] = self._gh_transport

        sem = asyncio.Semaphore(4)

        async def _fetch_batch(batch: list[tuple[str, str, str]]) -> None:
            query = _build_manifest_graphql_query(batch)
            async with sem:
                try:
                    resp = await gh_client.post(GITHUB_GRAPHQL_URL, json={"query": query})
                    if resp.status_code != 200:
                        return
                    data = (resp.json() or {}).get("data") or {}
                except Exception:
                    return

            for idx, (key, _, _) in enumerate(batch):
                node = data.get(f"r{idx}")
                if not isinstance(node, dict):
                    continue
                deps: list[DepSpec] = []
                pkg_blob = node.get("pkgJson")
                if isinstance(pkg_blob, dict) and isinstance(pkg_blob.get("text"), str):
                    if (pkg_blob.get("byteSize") or 0) <= MAX_MANIFEST_BYTES:
                        deps.extend(parse_package_json_deps(pkg_blob["text"]))

                req_blob = node.get("reqTxt")
                if isinstance(req_blob, dict) and isinstance(req_blob.get("text"), str):
                    if (req_blob.get("byteSize") or 0) <= MAX_MANIFEST_BYTES:
                        deps.extend(parse_requirements_txt_deps(req_blob["text"]))

                go_blob = node.get("goMod")
                if isinstance(go_blob, dict) and isinstance(go_blob.get("text"), str):
                    if (go_blob.get("byteSize") or 0) <= MAX_MANIFEST_BYTES:
                        deps.extend(parse_go_mod_deps(go_blob["text"]))

                cargo_blob = node.get("cargoToml")
                if isinstance(cargo_blob, dict) and isinstance(cargo_blob.get("text"), str):
                    if (cargo_blob.get("byteSize") or 0) <= MAX_MANIFEST_BYTES:
                        deps.extend(parse_cargo_toml_deps(cargo_blob["text"]))

                repo_deps[key] = deps

        async with httpx.AsyncClient(**gh_kwargs) as gh_client:
            batches = [
                items[start : start + MANIFEST_GRAPHQL_BATCH]
                for start in range(0, len(items), MANIFEST_GRAPHQL_BATCH)
            ]
            await asyncio.gather(*(_fetch_batch(b) for b in batches))

    async def _run_osv_querybatch(
        self,
        osv_client: httpx.AsyncClient,
        queries: list[dict],
    ) -> list[list[str]]:
        if not queries:
            return []
        all_results: list[list[str]] = []
        for start in range(0, len(queries), OSV_BATCH_SIZE):
            chunk = queries[start : start + OSV_BATCH_SIZE]
            try:
                resp = await osv_client.post(OSV_QUERYBATCH_URL, json={"queries": chunk})
                if resp.status_code != 200:
                    all_results.extend([] for _ in chunk)
                    continue
                raw_list = (resp.json() or {}).get("results") or []
                for idx in range(len(chunk)):
                    entry = raw_list[idx] if idx < len(raw_list) else {}
                    vulns = (entry.get("vulns") if isinstance(entry, dict) else None) or []
                    vids = [
                        str(v["id"])
                        for v in vulns
                        if isinstance(v, dict) and isinstance(v.get("id"), str)
                    ]
                    all_results.append(vids)
            except Exception:
                all_results.extend([] for _ in chunk)
        return all_results

    async def _fetch_osv_vuln_details(
        self,
        osv_client: httpx.AsyncClient,
        vuln_ids: list[str],
    ) -> dict[str, dict]:
        sem = asyncio.Semaphore(32)
        docs: dict[str, dict] = {}

        async def _get_one(vid: str) -> None:
            async with sem:
                try:
                    resp = await osv_client.get(OSV_VULN_URL_TMPL.format(vuln_id=vid))
                    if resp.status_code == 200 and isinstance(resp.json(), dict):
                        docs[vid] = resp.json()
                except Exception:
                    return

        try:
            await asyncio.wait_for(
                asyncio.gather(*(_get_one(v) for v in vuln_ids)),
                timeout=120.0,
            )
        except TimeoutError:
            pass
        return docs
