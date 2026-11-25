"""Tests for holding broken sites until they have been down long enough to report."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from fmhy_drift.hold import apply_hold
from fmhy_drift.models import SignalKind, SiteCheckResult, SiteSignal, SiteStatus
from fmhy_drift.signal_cache import SignalCache

NOW = datetime(2026, 11, 25, tzinfo=UTC)


def _sig(kind: SignalKind, when: datetime | None = None, sev: str = "warning") -> SiteSignal:
    return SiteSignal(kind=kind, severity=sev, source="t", detail="d", observed_at=when)  # type: ignore[arg-type]


def test_hold_dates_outage_from_earliest_evidence_and_never_holds_threats() -> None:
    hist = SignalCache(None, now=NOW)
    res = {
        "https://new-dead.example/": SiteCheckResult(url="https://new-dead.example/", status=SiteStatus.DEAD),
        "https://old-dead.example/": SiteCheckResult(
            url="https://old-dead.example/", status=SiteStatus.DEAD, last_seen_at=NOW - timedelta(days=70)
        ),
        "https://store.example/": SiteCheckResult(
            url="https://store.example/",
            status=SiteStatus.DEAD,
            signals=[_sig(SignalKind.STORE_REMOVED, NOW - timedelta(days=3), "critical")],
        ),
        "https://evil.example/": SiteCheckResult(
            url="https://evil.example/",
            status=SiteStatus.DEAD,
            signals=[_sig(SignalKind.REREGISTERED, NOW - timedelta(days=2), "critical")],
        ),
        "https://stale.example/": SiteCheckResult(
            url="https://stale.example/", status=SiteStatus.FLAGGED, signals=[_sig(SignalKind.STORE_STALE)]
        ),
    }
    out = apply_hold(res, history=hist, now=NOW, hold_days=30)

    assert out["https://new-dead.example/"].held and out["https://new-dead.example/"].down_days == 0
    old = out["https://old-dead.example/"]
    assert not old.held and old.down_days == 70 and old.down_since_source == "last active"
    assert out["https://store.example/"].held and out["https://store.example/"].down_days == 3
    assert not out["https://evil.example/"].held
    assert out["https://stale.example/"].down_since is None and not out["https://stale.example/"].held


def test_hold_clock_persists_across_runs_and_restarts_after_recovery() -> None:
    url = "https://flaky.example/"
    dead = {url: SiteCheckResult(url=url, status=SiteStatus.DEAD)}
    hist = SignalCache(None, now=NOW)
    apply_hold(dead, history=hist, now=NOW, hold_days=30)

    later = NOW + timedelta(days=31)
    for step in range(7, 32, 7):  # flagged on every weekly run
        hist.now = NOW + timedelta(days=step)
        apply_hold(dead, history=hist, now=hist.now, hold_days=30)
    hist.now = later
    out = apply_hold(dead, history=hist, now=later, hold_days=30)
    assert not out[url].held and out[url].down_days == 31

    # Recovered for > 14 days, then broke again: a fresh outage.
    again = later + timedelta(days=30)
    hist.now = again
    out = apply_hold(dead, history=hist, now=again, hold_days=30)
    assert out[url].held and out[url].down_days == 0
