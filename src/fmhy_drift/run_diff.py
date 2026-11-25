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
    return (sr.get("status"), tuple(sigs), bool(sr.get("held")))


def _site_state_label(state: tuple) -> str:
    status, sigs = state[0], state[1]
    if len(state) > 2 and state[2]:
        return "holding"
    kinds = ", ".join(k for k, _ in sigs)
    return f"{status}: {kinds}" if kinds else str(status)


# Repos in these tiers are not shown as flagged, so moving into them is not news.
_QUIET_REPO_TIERS = {"active", "error"}


def _is_flagged_repo(state: tuple) -> bool:
    return state[0] not in _QUIET_REPO_TIERS or bool(state[4])


_FLAGGED_SITE_STATUSES = {"dead", "parked", "tls_error", "flagged"}


def _is_flagged_site(state: tuple) -> bool:
    """Flagged and visible: held sites are tracked but not shown, so leaving the hold reads as new."""
    if len(state) > 2 and state[2]:
        return False
    return state[0] in _FLAGGED_SITE_STATUSES or bool(state[1])


def annotate_changes(
    current: dict[str, Any], previous: dict[str, Any] | None, *, carry: bool = False
) -> dict[str, Any]:
    """Add `change` ("new" | "changed") and `change_from` to rows of `current`, plus a top-level `changes` summary.

    With no previous report nothing is marked (a first run would otherwise mark everything new).
    `carry=True` is for incremental runs that refresh a published report: rows keep the marks they
    already had (still relative to that report's baseline) and only unmarked rows are diffed.
    """
    if not previous or not isinstance(previous.get("entries"), list):
        current["changes"] = None
        return current

    prev_repos: dict[str, tuple] = {}
    prev_sites: dict[str, tuple] = {}
    prev_repo_marks: dict[str, tuple] = {}
    prev_site_marks: dict[str, tuple] = {}
    for entry in previous["entries"]:
        if not isinstance(entry, dict):
            continue
        for rr in entry.get("github_repos") or []:
            if isinstance(rr, dict) and _repo_key(rr):
                prev_repos.setdefault(_repo_key(rr), _repo_state(rr))
                if rr.get("change"):
                    prev_repo_marks.setdefault(_repo_key(rr), (rr["change"], rr.get("change_from")))
        for sr in entry.get("sites") or []:
            if isinstance(sr, dict) and sr.get("url"):
                prev_sites.setdefault(str(sr["url"]), _site_state(sr))
                if sr.get("change"):
                    prev_site_marks.setdefault(str(sr["url"]), (sr["change"], sr.get("change_from")))

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
            if carry and key in prev_repo_marks:
                rr["change"], rr["change_from"] = prev_repo_marks[key]
            elif old is None or not _is_flagged_repo(old):
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
            if carry and url in prev_site_marks:
                sr["change"], sr["change_from"] = prev_site_marks[url]
            elif old is None or not _is_flagged_site(old):
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
    prev_changes = previous.get("changes") if isinstance(previous.get("changes"), dict) else None
    current["changes"] = {
        "previous_generated_at": (
            prev_changes.get("previous_generated_at")
            if carry and prev_changes
            else previous.get("generated_at")
        ),
        "new": counts["new"],
        "changed": counts["changed"],
        "resolved": resolved_repos
        + resolved_sites
        + (int(prev_changes.get("resolved") or 0) if carry and prev_changes else 0),
    }
    return current


def main(argv: list[str] | None = None) -> int:
    """`python -m fmhy_drift.run_diff CURRENT.json CANDIDATE_PREVIOUS.json`.

    Annotates CURRENT in place when CANDIDATE is strictly older. Exit 0 = annotated,
    1 = candidate not older (try an earlier run), 2 = unreadable input.
    """
    import json
    import sys
    from pathlib import Path

    args = sys.argv[1:] if argv is None else argv
    if len(args) != 2:
        print("usage: python -m fmhy_drift.run_diff CURRENT.json PREVIOUS.json", file=sys.stderr)
        return 2
    cur_path, prev_path = Path(args[0]), Path(args[1])
    try:
        cur = json.loads(cur_path.read_text(encoding="utf-8"))
        prev = json.loads(prev_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        print(f"run_diff: {exc}", file=sys.stderr)
        return 2
    if str(prev.get("generated_at") or "") >= str(cur.get("generated_at") or ""):
        return 1
    out = annotate_changes(cur, prev)
    cur_path.write_text(json.dumps(out, indent=2, ensure_ascii=False), encoding="utf-8")
    ch = out["changes"]
    print(f"Changes since {ch['previous_generated_at']}: {ch['new']} new, {ch['changed']} changed, {ch['resolved']} resolved")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
