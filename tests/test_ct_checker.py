"""Tests for CT renewal-cadence lapse detection (`check_cert_cadence`)."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import httpx

from fmhy_drift.ct_checker import CT_NAMESPACE, check_cert_cadence, covers, evaluate_cadence
from fmhy_drift.models import SignalKind
from fmhy_drift.signal_cache import SignalCache

NOW = datetime(2026, 11, 25, tzinfo=UTC)
ALLOWED = {"api.certspotter.com", "crt.sh"}


def _d(s: str) -> datetime:
    return datetime.fromisoformat(s).replace(tzinfo=UTC)


def _crt(nb: str, na: str, names: str) -> dict:
    return {"not_before": nb + "T00:00:00", "not_after": na + "T00:00:00", "name_value": names, "common_name": ""}


LAPSED_ROWS = [
    _crt("2025-06-01", "2025-08-30", "lapsed.example\nwww.lapsed.example"),
    _crt("2025-06-01", "2025-08-30", "lapsed.example\nwww.lapsed.example"),  # RSA+ECDSA twin
    _crt("2025-08-01", "2025-10-30", "lapsed.example"),
    _crt("2025-10-01", "2025-12-30", "lapsed.example"),
    _crt("2025-12-01", "2026-03-01", "lapsed.example"),
    _crt("2026-01-01", "2026-04-01", "api.lapsed.example"),  # other name: ignored
]


def test_covers_handles_wildcards() -> None:
    assert covers("*.example.com", "a.example.com")
    assert not covers("*.example.com", "a.b.example.com")
    assert not covers("*.example.com", "example.com")
    assert covers("EXAMPLE.com.", "example.com")


def test_evaluate_cadence() -> None:
    certs = [(_d(r["not_before"]), _d(r["not_after"])) for r in LAPSED_ROWS[:5]]
    lapsed, detail, last = evaluate_cadence(certs, now=NOW)
    assert lapsed
    assert detail == "last cert issued 2025-12-01, expired 2026-03-01, prior cadence ~61d"
    assert last == _d("2025-12-01")
    # Too soon after the last renewal (61 + 45 days): no signal.
    assert not evaluate_cadence(certs, now=_d("2026-03-10"))[0]
    # A long-lived cert still valid: no signal.
    assert not evaluate_cadence(certs + [(_d("2025-12-01"), _d("2026-12-01"))], now=NOW)[0]
    # Only yearly certs, no short cadence: no signal.
    yearly = [(_d("2022-01-01"), _d("2023-01-01")), (_d("2023-01-01"), _d("2024-01-01")), (_d("2024-01-01"), _d("2025-01-01"))]
    assert not evaluate_cadence(yearly, now=NOW)[0]


def test_cert_cadence_end_to_end() -> None:
    contacted: set[str] = set()
    crt_queries: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        contacted.add(request.url.host)
        if request.url.host == "api.certspotter.com":
            domain = request.url.params.get("domain")
            assert request.url.params.get("match_wildcards") == "true"
            if domain == "live.example":
                return httpx.Response(
                    200,
                    json=[{"id": "1", "not_before": "2026-10-01T00:00:00Z", "not_after": "2026-12-30T00:00:00Z",
                           "revoked": False, "dns_names": ["live.example"]}],
                )
            return httpx.Response(200, json=[])
        if request.url.host == "crt.sh":
            q = request.url.params.get("q", "")
            crt_queries.append(q)
            if q == "www.lapsed.example":
                return httpx.Response(200, json=[{**r, "name_value": r["name_value"].replace("lapsed.example", "www.lapsed.example")} for r in LAPSED_ROWS])
            if q == "*.wild.example":
                return httpx.Response(200, json=[
                    {**_crt(nb, na, "*.wild.example")} for nb, na in
                    [("2025-06-01", "2025-08-30"), ("2025-08-01", "2025-10-30"), ("2025-10-01", "2025-12-30")]
                ])
            if q == "slow.example":
                return httpx.Response(502)
            return httpx.Response(200, json=[])
        raise AssertionError(f"unexpected {request.url}")

    cache = SignalCache(None, now=NOW)
    out = asyncio.run(
        check_cert_cadence(
            [
                "https://live.example/",
                "https://www.lapsed.example/x",
                "https://sub.wild.example/",
                "https://slow.example/",
                "https://someone.github.io/",
            ],
            cache=cache,
            now=NOW,
            transport=httpx.MockTransport(handler),
            certspotter_api_key="",
            crtsh_interval=0,
        )
    )
    assert contacted <= ALLOWED
    assert set(out) == {"https://www.lapsed.example/x", "https://sub.wild.example/"}
    sig = out["https://www.lapsed.example/x"][0]
    assert sig.kind == SignalKind.CERT_LAPSED and sig.severity == "warning"
    assert sig.detail.startswith("last cert issued 2025-12-01, expired 2026-03-01")
    assert "live.example" not in crt_queries
    assert "*.wild.example" in crt_queries
    assert cache.get(CT_NAMESPACE, "slow.example", ttl=timedelta(days=14)) is None
    assert cache.get(CT_NAMESPACE, "live.example", ttl=timedelta(days=14)) == {"lapsed": False}


def test_cert_cadence_budget_and_429() -> None:
    calls = {"cs": 0, "crt": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "api.certspotter.com":
            calls["cs"] += 1
            return httpx.Response(429, headers={"Retry-After": "3600"})
        calls["crt"] += 1
        return httpx.Response(200, json=[])

    out = asyncio.run(
        check_cert_cadence(
            [f"https://h{i}.example/" for i in range(10)],
            cache=SignalCache(None, now=NOW),
            now=NOW,
            transport=httpx.MockTransport(handler),
            max_queries=3,
            crtsh_interval=0,
        )
    )
    assert out == {}
    assert calls["cs"] == 1
    assert calls["crt"] == 0  # crt.sh alone never decides a host
