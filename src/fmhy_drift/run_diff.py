"""Mark report rows that are new or changed relative to the previously published report.

Works on the serialized JSON payload so it can diff against any earlier report version.
A row's state signature ignores volatile fields (dates, day counts, detail text) so only
real transitions count: a new flag, a tier move, a status change, a new signal kind or severity.
"""

from __future__ import annotations

from typing import Any


def _repo_key(rr: dict) -> str:
    return str(rr.get("requested_full_name") or rr.get("full_name") or "").lower()


def _repo_state(rr: dict) -> tuple:
    vuln_ids = sorted(str(v.get("vuln_id") or "") for v in rr.get("vulns") or [] if isinstance(v, dict))
    return (
        rr.get("tier"),
        bool(rr.get("is_archived")),
        bool(rr.get("is_disabled")),
        rr.get("replacement_url") or None,
        tuple(vuln_ids),
    )


def _repo_state_label(state: tuple) -> str:
    tier, archived, disabled, replacement, vulns = state
    parts = [str(tier)]
    if disabled:
        parts.append("disabled")
    if replacement:
        parts.append("replacement")
    if vulns:
        parts.append(f"{len(vulns)} vulns")
    return ", ".join(parts)


def _site_state(sr: dict) -> tuple:
    sigs = sorted(
        (str(s.get("kind")), str(s.get("severity")))
        for s in sr.get("signals") or []
        if isinstance(s, dict) and s.get("kind")
    )
    return (sr.get("status"), tuple(sigs))


def _site_state_label(state: tuple) -> str:
    status, sigs = state
    kinds = ", ".join(k for k, _ in sigs)
    return f"{status}: {kinds}" if kinds else str(status)


# Repos in these tiers are not shown as flagged, so moving into them is not news.
_QUIET_REPO_TIERS = {"active", "error"}


def _is_flagged_repo(state: tuple) -> bool:
    return state[0] not in _QUIET_REPO_TIERS or bool(state[4])


_FLAGGED_SITE_STATUSES = {"dead", "parked", "tls_error", "flagged"}


def _is_flagged_site(state: tuple) -> bool:
    return state[0] in _FLAGGED_SITE_STATUSES or bool(state[1])


def annotate_changes(current: dict[str, Any], previous: dict[str, Any] | None) -> dict[str, Any]:
    """Add `change` ("new" | "changed") and `change_from` to rows of `current`, plus a top-level `changes` summary.

    With no previous report nothing is marked (a first run would otherwise mark everything new).
    """
    if not previous or not isinstance(previous.get("entries"), list):
        current["changes"] = None
        return current

    prev_repos: dict[str, tuple] = {}
    prev_sites: dict[str, tuple] = {}
    for entry in previous["entries"]:
        if not isinstance(entry, dict):
            continue
        for rr in entry.get("github_repos") or []:
            if isinstance(rr, dict) and _repo_key(rr):
                prev_repos.setdefault(_repo_key(rr), _repo_state(rr))
        for sr in entry.get("sites") or []:
            if isinstance(sr, dict) and sr.get("url"):
                prev_sites.setdefault(str(sr["url"]), _site_state(sr))

    counts = {"new": 0, "changed": 0}
    cur_repos: dict[str, tuple] = {}
    cur_sites: dict[str, tuple] = {}
    for entry in current.get("entries") or []:
        for rr in entry.get("github_repos") or []:
            key = _repo_key(rr)
            state = _repo_state(rr)
            cur_repos.setdefault(key, state)
            if not _is_flagged_repo(state):
                continue
            old = prev_repos.get(key)
            if old is None or not _is_flagged_repo(old):
                rr["change"] = "new"
                rr["change_from"] = _repo_state_label(old) if old else None
            elif old != state:
                rr["change"] = "changed"
                rr["change_from"] = _repo_state_label(old)
            else:
                continue
            counts[rr["change"]] += 1
        for sr in entry.get("sites") or []:
            url = str(sr.get("url") or "")
            state = _site_state(sr)
            cur_sites.setdefault(url, state)
            if not _is_flagged_site(state):
                continue
            old = prev_sites.get(url)
            if old is None or not _is_flagged_site(old):
                sr["change"] = "new"
                sr["change_from"] = _site_state_label(old) if old else None
            elif old != state:
                sr["change"] = "changed"
                sr["change_from"] = _site_state_label(old)
            else:
                continue
            counts[sr["change"]] += 1

    resolved_repos = sum(
        1
        for k, st in prev_repos.items()
        if _is_flagged_repo(st) and not (k in cur_repos and _is_flagged_repo(cur_repos[k]))
    )
    resolved_sites = sum(
        1
        for u, st in prev_sites.items()
        if _is_flagged_site(st) and not (u in cur_sites and _is_flagged_site(cur_sites[u]))
    )
    current["changes"] = {
        "previous_generated_at": previous.get("generated_at"),
        "new": counts["new"],
        "changed": counts["changed"],
        "resolved": resolved_repos + resolved_sites,
    }
    return current
