"""Hold broken sites until they have been down long enough to be worth reporting.

A site is "down-type" when it is dead / parked / TLS-broken, or carries a signal that means the listed
link no longer works (store removed, domain expired / held, scanner down, archive down, cert lapsed).
Threats (malicious, re-registered, drop-caught) are never held: they are reported immediately.

`down_since` is the earliest evidence of the outage: the first run that flagged the URL (persisted in a
small history file between runs), the site's last-active date from DNS enrichment, or the date carried
by a down-type signal (store removal first seen, expiry date, hold start, ...).
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta

from fmhy_drift.models import SignalKind, SiteCheckResult, SiteStatus
from fmhy_drift.signal_cache import SignalCache

HISTORY_NS = "flag_history"
# History rows are rewritten every run while a URL stays flagged. A URL unflagged for longer than this
# (weekly runs, so two missed runs) recovered in between: its next outage starts a fresh clock.
HISTORY_KEEP = timedelta(days=14)

DOWN_STATUSES = frozenset({SiteStatus.DEAD, SiteStatus.PARKED, SiteStatus.TLS_ERROR})
DOWN_SIGNAL_KINDS = frozenset(
    {
        SignalKind.STORE_REMOVED,
        SignalKind.DOMAIN_EXPIRED,
        SignalKind.DOMAIN_HELD,
        SignalKind.SCAN_DOWN,
        SignalKind.ARCHIVE_DOWN,
        SignalKind.CERT_LAPSED,
        SignalKind.PARKED_DNS,
    }
)
NEVER_HELD_KINDS = frozenset(
    {SignalKind.MALICIOUS, SignalKind.REREGISTERED, SignalKind.DROPCATCH_REGISTRAR}
)

_SOURCE_LABELS = {
    SignalKind.STORE_REMOVED: "store removal first seen",
    SignalKind.DOMAIN_EXPIRED: "domain expiry",
    SignalKind.DOMAIN_HELD: "domain hold",
    SignalKind.SCAN_DOWN: "scanner",
    SignalKind.ARCHIVE_DOWN: "archive",
    SignalKind.CERT_LAPSED: "TLS cert expiry",
    SignalKind.PARKED_DNS: "parking first seen",
}


def is_down_type(sr: SiteCheckResult) -> bool:
    return sr.status in DOWN_STATUSES or bool(sr.signal_kinds & DOWN_SIGNAL_KINDS)


def apply_hold(
    results: dict[str, SiteCheckResult],
    *,
    history: SignalCache,
    now: datetime,
    hold_days: int,
) -> dict[str, SiteCheckResult]:
    """Set `down_since`, `down_since_source`, `down_days`, `held` on down-type results and update `history`."""
    out = dict(results)
    for url, sr in results.items():
        if not is_down_type(sr):
            continue
        prev = history.get(HISTORY_NS, url, ttl=HISTORY_KEEP)
        first_flagged = None
        if isinstance(prev, dict) and prev.get("first_flagged"):
            try:
                first_flagged = datetime.fromisoformat(prev["first_flagged"])
            except ValueError:
                first_flagged = None
        if first_flagged is None:
            first_flagged = now
        history.put(HISTORY_NS, url, {"first_flagged": first_flagged.isoformat()})

        candidates: list[tuple[datetime, str]] = [(first_flagged, "first flagged")]
        if sr.status in DOWN_STATUSES and sr.last_seen_at is not None:
            candidates.append((sr.last_seen_at, "last active"))
        for sig in sr.signals:
            if sig.kind in DOWN_SIGNAL_KINDS and sig.observed_at is not None and sig.observed_at <= now:
                candidates.append((sig.observed_at, _SOURCE_LABELS.get(sig.kind, sig.kind.value)))
        since, source = min(candidates, key=lambda c: c[0])
        days = max(0, int((now - since).total_seconds() // 86400))
        held = days < hold_days and not (sr.signal_kinds & NEVER_HELD_KINDS)
        out[url] = replace(sr, down_since=since, down_since_source=source, down_days=days, held=held)
    return out
