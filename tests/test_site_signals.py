"""Orchestrator, CLI wiring, reporter output, and reused-report round trip for passive site signals."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest

import fmhy_drift.cli as cli_mod
import fmhy_drift.site_signals as ss
from fmhy_drift.cli import _load_reused_report, main
from fmhy_drift.models import (
    DriftReport,
    EntryDriftResult,
    SignalKind,
    SiteCheckResult,
    SiteSignal,
    SiteStatus,
    TierThresholds,
    WikiEntry,
)
from fmhy_drift.reporter import (
    build_discord_payload,
    render_json_report,
    render_markdown_report,
)
from fmhy_drift.signal_cache import SignalCache
from fmhy_drift.triage_ui import FOLDER_ROUTE_SLUGS

NOW = datetime(2026, 11, 25, tzinfo=UTC)


def _sig(kind: SignalKind, severity: str = "warning", when: datetime | None = None, **kw) -> SiteSignal:
    return SiteSignal(
        kind=kind,
        severity=severity,  # type: ignore[arg-type]
        source=kw.get("source", "test"),
        detail=kw.get("detail", f"{kind.value} detail"),
        observed_at=when,
        evidence_url=kw.get("evidence_url", "https://evidence.example/x"),
    )


# ------------------------------------------------------------------------------------------ merge / status


def test_status_rules() -> None:
    assert ss.status_for_signals(SiteStatus.DEAD, [_sig(SignalKind.REREGISTERED)]) == SiteStatus.DEAD
    assert ss.status_for_signals(SiteStatus.TLS_ERROR, [_sig(SignalKind.MALICIOUS)]) == SiteStatus.TLS_ERROR
    assert ss.status_for_signals(None, [_sig(SignalKind.STORE_REMOVED)]) == SiteStatus.DEAD
    assert ss.status_for_signals(SiteStatus.ALIVE, [_sig(SignalKind.SCAN_DOWN)]) == SiteStatus.DEAD
    assert ss.status_for_signals(None, [_sig(SignalKind.PARKED_DNS)]) == SiteStatus.PARKED
    assert ss.status_for_signals(None, [_sig(SignalKind.REREGISTERED)]) == SiteStatus.FLAGGED
    # Newer SCAN_UP keeps archive/CT suspects FLAGGED; an older one does not hold back a newer scanner "down".
    up_new = _sig(SignalKind.SCAN_UP, "info", datetime(2026, 11, 20, tzinfo=UTC))
    arch_old = _sig(SignalKind.ARCHIVE_DOWN, "warning", datetime(2026, 1, 1, tzinfo=UTC))
    assert ss.status_for_signals(None, [arch_old, up_new, _sig(SignalKind.PARKED_DNS)]) == SiteStatus.FLAGGED
    arch_new = _sig(SignalKind.ARCHIVE_DOWN, "warning", datetime(2026, 11, 24, tzinfo=UTC))
    assert ss.status_for_signals(None, [arch_new, up_new, _sig(SignalKind.PARKED_DNS)]) == SiteStatus.PARKED


def test_merge_keeps_existing_fields_and_rebuilds_signal_derived_rows() -> None:
    existing = {
        "https://dead.example/": SiteCheckResult(
            url="https://dead.example/",
            status=SiteStatus.DEAD,
            reason="Domain does not exist (DNS NXDOMAIN confirmed on Cloudflare & Google)",
            inactive_days=400,
        ),
        # From a reused report: built from signals last run, no longer signalled -> dropped.
        "https://old-flag.example/": SiteCheckResult(
            url="https://old-flag.example/",
            status=SiteStatus.FLAGGED,
            reason="reregistered: x",
            signals=[_sig(SignalKind.REREGISTERED)],
        ),
        "https://alive.example/": SiteCheckResult(url="https://alive.example/", status=SiteStatus.ALIVE, http_status=200),
    }
    sig = _sig(SignalKind.DOMAIN_EXPIRED, "critical")
    merged = ss.merge_site_signals(
        existing,
        {
            "https://dead.example/": [sig, sig],
            "https://alive.example/": [_sig(SignalKind.MALICIOUS, "critical")],
            "https://new.example/": [_sig(SignalKind.CERT_LAPSED)],
            "https://up-only.example/": [_sig(SignalKind.SCAN_UP, "info")],
        },
    )
    dead = merged["https://dead.example/"]
    assert dead.status == SiteStatus.DEAD and dead.inactive_days == 400 and dead.signals == [sig]
    assert "NXDOMAIN" in dead.reason
    assert "https://old-flag.example/" not in merged
    alive = merged["https://alive.example/"]
    assert alive.status == SiteStatus.FLAGGED and alive.http_status == 200
    assert alive.reason.startswith("malicious:")
    assert merged["https://new.example/"].status == SiteStatus.FLAGGED
    assert "https://up-only.example/" not in merged


# ------------------------------------------------------------------------------------------ orchestrator


class _Recorder:
    def __init__(self) -> None:
        self.calls: dict[str, dict] = {}

    def install(self, monkeypatch: pytest.MonkeyPatch, results: dict[str, dict[str, list[SiteSignal]]]) -> None:
        def make(name: str, positional: bool = True):
            async def fake(*args, **kwargs):
                urls = list(args[0]) if (positional and args) else None
                rec = self.calls.setdefault(name, {"urls": [], "kwargs": []})
                rec["urls"].append(urls)
                rec["kwargs"].append(kwargs)
                return results.get(name, {})

            return fake

        monkeypatch.setattr(ss, "check_dns_parking", make("dns"))
        monkeypatch.setattr(ss, "check_storefronts", make("store"))
        monkeypatch.setattr(ss, "check_reputation", make("rep"))
        monkeypatch.setattr(ss, "check_rdap", make("rdap"))
        monkeypatch.setattr(ss, "check_archives", make("archive"))
        monkeypatch.setattr(ss, "check_cert_cadence", make("ct"))
        monkeypatch.setattr(ss, "confirm_with_scanners", make("scan"))

        async def fake_listing(**kwargs):
            self.calls.setdefault("listing", {"urls": [], "kwargs": []})["kwargs"].append(kwargs)
            return {"rereg.example": datetime(2023, 1, 1, tzinfo=UTC)}

        monkeypatch.setattr(ss, "fetch_listing_dates", fake_listing)


def test_orchestrator_routes_urls_and_prioritises_suspects(monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    store = "https://addons.mozilla.org/en-US/firefox/addon/ublock-origin/"
    store_host = "https://greasyfork.org/"
    nx = "https://nx.example/"
    sink = "https://sink.example/"
    rereg = "https://rereg.example/"
    plain = "https://plain.example/"
    cached_ct = "https://ct-hit.example/"
    urls = [store, store_host, nx, sink, rereg, plain, cached_ct]
    existing = {
        nx: SiteCheckResult(url=nx, status=SiteStatus.DEAD, reason="Domain does not exist (DNS NXDOMAIN confirmed)"),
        sink: SiteCheckResult(url=sink, status=SiteStatus.DEAD, reason="DNS sinkholed to 0.0.0.0"),
    }
    cache = SignalCache(None, now=NOW)
    cache.put("ct", "ct-hit.example", {"lapsed": True, "detail": "x"})
    rec = _Recorder()
    rec.install(
        monkeypatch,
        {
            "store": {store: [_sig(SignalKind.STORE_STALE, "notice")]},
            "rdap": {rereg: [_sig(SignalKind.REREGISTERED, "critical")]},
            "ct": {cached_ct: [_sig(SignalKind.CERT_LAPSED)]},
            "scan": {rereg: [_sig(SignalKind.SCAN_UP, "info", NOW)]},
        },
    )
    out = asyncio.run(
        ss.run_site_signals(
            urls,
            cache=cache,
            now=NOW,
            thresholds=TierThresholds(),
            store_thresholds=TierThresholds(365, 730, 1095),
            budget_seconds=60,
            github_token="tok",
            gsb_api_key="g",
            abusech_auth_key=None,
            urlscan_api_key="u",
            cloudflare_api_token=None,
            cloudflare_account_id=None,
            certspotter_api_key=None,
            existing=existing,
        )
    )
    domain_urls = [nx, sink, rereg, plain, cached_ct]
    assert rec.calls["dns"]["urls"] == [domain_urls]
    assert rec.calls["rdap"]["urls"] == [domain_urls]
    assert rec.calls["rdap"]["kwargs"][0]["listed_since"] == {"rereg.example": datetime(2023, 1, 1, tzinfo=UTC)}
    assert rec.calls["listing"]["kwargs"][0]["github_token"] == "tok"
    assert rec.calls["store"]["urls"] == [[store]]
    assert rec.calls["store"]["kwargs"][0]["thresholds"] == TierThresholds(365, 730, 1095)
    assert rec.calls["rep"]["urls"] == [urls]
    assert rec.calls["rep"]["kwargs"][0]["gsb_api_key"] == "g"
    # Archive: pass 1 = suspects only (DNS-dead first), pass 2 = every domain URL with suspects prioritised.
    arch_urls = rec.calls["archive"]["urls"]
    assert arch_urls[0][:2] == [nx, sink] and set(arch_urls[0]) == {nx, sink, rereg, cached_ct}
    assert arch_urls[1] == domain_urls
    for call_urls in arch_urls + rec.calls["ct"]["urls"] + rec.calls["dns"]["urls"]:
        assert store not in call_urls and store_host not in call_urls
    # CT skips DNS-dead (own enrichment); scanners skip only NXDOMAIN.
    assert set(rec.calls["ct"]["urls"][0]) == {rereg, cached_ct}
    assert rec.calls["ct"]["kwargs"][0]["max_queries"] == ss.CT_MAX_QUERIES_UNAUTH
    assert nx not in rec.calls["scan"]["urls"][0] and sink in rec.calls["scan"]["urls"][0]
    assert rec.calls["scan"]["kwargs"][0]["urlscan_api_key"] == "u"
    # Every deadline lies within the budget.
    for name in ("dns", "store", "rep", "rdap", "archive", "ct", "scan"):
        for kw in rec.calls[name]["kwargs"]:
            assert kw["deadline"] is not None

    assert out[nx].status == SiteStatus.DEAD  # untouched existing result
    assert out[store].status == SiteStatus.FLAGGED
    assert out[rereg].status == SiteStatus.FLAGGED
    assert {s.kind for s in out[rereg].signals} == {SignalKind.REREGISTERED, SignalKind.SCAN_UP}
    assert out[cached_ct].signal_kinds == {SignalKind.CERT_LAPSED}
    assert plain not in out
    log = capsys.readouterr().out
    assert "site-signals: phase A" in log and "phase B" in log and "phase C" in log


def test_orchestrator_survives_a_crashing_checker(monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    rec = _Recorder()
    rec.install(monkeypatch, {"rep": {"https://bad.example/": [_sig(SignalKind.MALICIOUS, "critical")]}})

    async def boom(*_a, **_k):
        raise RuntimeError("kaput")

    monkeypatch.setattr(ss, "check_rdap", boom)
    out = asyncio.run(
        ss.run_site_signals(
            ["https://bad.example/"],
            cache=SignalCache(None, now=NOW),
            now=NOW,
            thresholds=TierThresholds(),
            store_thresholds=None,
            budget_seconds=30,
            github_token=None,
            gsb_api_key=None,
            abusech_auth_key=None,
            urlscan_api_key=None,
            cloudflare_api_token=None,
            cloudflare_account_id=None,
            certspotter_api_key=None,
            existing={},
        )
    )
    assert out["https://bad.example/"].signal_kinds == {SignalKind.MALICIOUS}
    assert "rdap failed (RuntimeError: kaput)" in capsys.readouterr().out


def test_orchestrator_real_checkers_never_contact_listed_hosts(tmp_path: Path) -> None:
    listed = {"listed-one.example", "listed-two.example", "addons.mozilla.org"}
    touched: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        touched.append(request.url.host)
        if request.url.host == "addons.mozilla.org":
            return httpx.Response(404, json={"detail": "Not found."})  # the store API itself, not a listed site
        return httpx.Response(404, json={})

    urls = [
        "https://listed-one.example/",
        "https://www.listed-two.example/path",
        "https://addons.mozilla.org/firefox/addon/some-addon/",
    ]
    cache = SignalCache(tmp_path / "c.json", now=NOW)
    out = asyncio.run(
        ss.run_site_signals(
            urls,
            cache=cache,
            now=NOW,
            thresholds=TierThresholds(),
            store_thresholds=TierThresholds(365, 730, 1095),
            budget_seconds=20,
            github_token=None,
            gsb_api_key=None,
            abusech_auth_key=None,
            urlscan_api_key=None,
            cloudflare_api_token=None,
            cloudflare_account_id=None,
            certspotter_api_key=None,
            existing={},
            transport=httpx.MockTransport(handler),
        )
    )
    assert touched
    assert not (set(touched) & (listed - {"addons.mozilla.org"}))
    assert not any(h.endswith(("listed-one.example", "listed-two.example")) for h in touched)
    assert out["https://addons.mozilla.org/firefox/addon/some-addon/"].signal_kinds == {SignalKind.STORE_REMOVED}


# ------------------------------------------------------------------------------------------ reporter


def _signal_report() -> DriftReport:
    entry = WikiEntry(
        entry_id="tools.md:3",
        title="SketchyTool",
        category="Tools",
        subcategory="",
        source_file="tools.md",
        line_number=3,
        starred=False,
        site_urls=["https://sketchy.example/"],
    )
    store_entry = WikiEntry(
        entry_id="tools.md:4",
        title="OldAddon",
        category="Tools",
        subcategory="",
        source_file="tools.md",
        line_number=4,
        starred=False,
        site_urls=["https://addons.mozilla.org/firefox/addon/old/"],
    )
    flagged = SiteCheckResult(
        url="https://sketchy.example/",
        status=SiteStatus.FLAGGED,
        reason="malicious: listed",
        signals=[
            _sig(SignalKind.MALICIOUS, "critical", NOW, source="google_safe_browsing", detail="MALWARE"),
            _sig(SignalKind.REREGISTERED, "critical", datetime(2025, 3, 2, tzinfo=UTC), detail="registered 2025-03-02"),
            _sig(SignalKind.ARCHIVE_DOWN, "warning", datetime(2026, 6, 1, tzinfo=UTC)),
        ],
    )
    removed = SiteCheckResult(
        url="https://addons.mozilla.org/firefox/addon/old/",
        status=SiteStatus.DEAD,
        reason="store_removed: listing gone",
        signals=[_sig(SignalKind.STORE_REMOVED, "critical", None, source="amo", detail="HTTP 404 (AMO)")],
    )
    return DriftReport(
        generated_at=NOW,
        index_source="tools.md",
        thresholds=TierThresholds(),
        proxy_verification=None,
        entries=[
            EntryDriftResult(entry=entry, site_results=[flagged]),
            EntryDriftResult(entry=store_entry, site_results=[removed]),
        ],
        unique_sites_checked=2,
        unique_repos_checked=0,
    )


def test_reporter_json_markdown_and_discord_include_signals() -> None:
    report = _signal_report()
    payload = json.loads(render_json_report(report))
    summary = payload["summary"]
    assert summary["sites_flagged"] == 1
    assert summary["sites_dead"] == 1
    assert summary["signal_counts"]["malicious"] == 1
    assert summary["signal_counts"]["store_removed"] == 1
    assert summary["signal_counts"]["cert_lapsed"] == 0
    assert summary["signal_category_counts"] == {"malicious": 1, "hijack_risk": 1, "stores": 1, "suspect": 1}
    site = payload["entries"][0]["sites"][0]
    assert site["status"] == "flagged"
    assert site["signals"][0]["kind"] == "malicious"
    assert site["signals"][0]["evidence_url"] == "https://evidence.example/x"

    md = render_markdown_report(report)
    for heading in ("## 7. ☣️ Malicious Sites", "## 8. 🪝 Hijack Risk", "## 9. 🛒 Store Listings", "## 10. ❓ Suspect"):
        assert heading in md
    assert "**[critical]** `malicious`: <https://sketchy.example/> — MALWARE (google_safe_browsing, 2026-11-25)" in md
    assert "[evidence](https://evidence.example/x)" in md
    assert "`store_removed`: <https://addons.mozilla.org/firefox/addon/old/>" in md
    assert "| ☣️ Malicious Sites | 1 |" in md
    # FLAGGED is not "unhealthy": section 6 lists only the dead store listing.
    sec6 = md.split("## 6.", 1)[1].split("## 7.", 1)[0]
    assert "sketchy.example" not in sec6 and "addons.mozilla.org" in sec6

    fields = build_discord_payload(report)["embeds"][0]["fields"]
    sig_field = next(f for f in fields if f["name"] == "🧭 Site Signals")
    assert "Malicious: **1**" in sig_field["value"] and "Flagged: **1**" in sig_field["value"]


def test_reused_report_round_trips_signals_and_flagged(tmp_path: Path) -> None:
    path = tmp_path / "prev.json"
    path.write_text(render_json_report(_signal_report()), encoding="utf-8")
    _, sites, prev_sites, _ = _load_reused_report(path)
    assert prev_sites == 2
    sr = sites["https://sketchy.example/"]
    assert sr.status == SiteStatus.FLAGGED
    assert [s.kind for s in sr.signals] == [SignalKind.MALICIOUS, SignalKind.REREGISTERED, SignalKind.ARCHIVE_DOWN]
    assert sr.signals[0].observed_at == NOW
    assert sr.signals[0].evidence_url == "https://evidence.example/x"


# ------------------------------------------------------------------------------------------ CLI wiring


def _write_index(tmp_path: Path) -> Path:
    md = tmp_path / "sample.md"
    md.write_text(
        "# Tools\n"
        "* **[SketchyTool](https://sketchy.example/)** - no repo\n"
        "* **[CleanTool](https://clean.example/)** - no repo\n",
        encoding="utf-8",
    )
    return md


def _cli_args(tmp_path: Path, md: Path, *extra: str) -> list[str]:
    return [
        "--skip-without-proxy",
        "--no-dns-check",
        "--no-vuln-check",
        "--whitelist",
        str(tmp_path / "none.json"),
        "--input",
        str(md),
        "--json-out",
        str(tmp_path / "out" / "r.json"),
        "--markdown-out",
        str(tmp_path / "out" / "r.md"),
        "--html-out",
        str(tmp_path / "out" / "index.html"),
        "--signal-cache",
        str(tmp_path / "cache" / "signals.json"),
        *extra,
    ]


def test_cli_runs_site_signals_and_saves_cache(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    for name, val in {
        "GOOGLE_SAFE_BROWSING_API_KEY": "gsb",
        "ABUSECH_AUTH_KEY": "abc",
        "URLSCAN_API_KEY": "us",
        "CLOUDFLARE_API_TOKEN": "cft",
        "CLOUDFLARE_ACCOUNT_ID": "cfa",
        "CERTSPOTTER_API_KEY": "cs",
        "GITHUB_TOKEN": "ght",
        "STORE_WARN_DAYS_1": "400",
    }.items():
        monkeypatch.setenv(name, val)
    monkeypatch.delenv("SIGNAL_BUDGET_MINUTES", raising=False)
    monkeypatch.delenv("PROXY_URL", raising=False)
    captured: dict = {}

    async def fake_signals(urls, **kwargs):
        captured["urls"] = list(urls)
        captured.update(kwargs)
        kwargs["cache"].put("test", "k", {"v": 1})
        return {
            "https://sketchy.example/": SiteCheckResult(
                url="https://sketchy.example/",
                status=SiteStatus.FLAGGED,
                reason="malicious: MALWARE",
                signals=[_sig(SignalKind.MALICIOUS, "critical", NOW)],
            )
        }

    monkeypatch.setattr(cli_mod, "run_site_signals", fake_signals)
    md = _write_index(tmp_path)
    assert main(_cli_args(tmp_path, md)) == 0

    assert captured["urls"] == ["https://sketchy.example/", "https://clean.example/"]
    assert captured["budget_seconds"] == 30 * 60
    assert captured["store_thresholds"] == TierThresholds(400, 730, 1095)
    assert captured["gsb_api_key"] == "gsb"
    assert captured["abusech_auth_key"] == "abc"
    assert captured["urlscan_api_key"] == "us"
    assert captured["cloudflare_api_token"] == "cft"
    assert captured["cloudflare_account_id"] == "cfa"
    assert captured["certspotter_api_key"] == "cs"
    assert captured["github_token"] == "ght"
    assert captured["existing"] == {}
    saved = json.loads((tmp_path / "cache" / "signals.json").read_text(encoding="utf-8"))
    assert saved["namespaces"]["test"]["k"]["data"] == {"v": 1}

    report = json.loads((tmp_path / "out" / "r.json").read_text(encoding="utf-8"))
    assert [e["title"] for e in report["entries"]] == ["SketchyTool"]
    assert report["entries"][0]["sites"][0]["signals"][0]["kind"] == "malicious"
    assert report["summary"]["sites_flagged"] == 1
    assert report["summary"]["unique_sites_checked"] == 2
    for slug in ("malicious", "hijack-risk", "stores", "suspect"):
        assert slug in FOLDER_ROUTE_SLUGS
        assert (tmp_path / "out" / slug / "index.html").is_file()


def test_cli_signal_budget_flags_and_cache_saved_on_crash(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PROXY_URL", raising=False)
    budgets: list[float] = []

    async def crash(urls, **kwargs):
        budgets.append(kwargs["budget_seconds"])
        kwargs["cache"].put("test", "partial", {"v": 2})
        raise RuntimeError("boom")

    monkeypatch.setattr(cli_mod, "run_site_signals", crash)
    md = _write_index(tmp_path)
    monkeypatch.setenv("SIGNAL_BUDGET_MINUTES", "12")
    with pytest.raises(RuntimeError):
        main(_cli_args(tmp_path, md))
    assert budgets == [720.0]
    saved = json.loads((tmp_path / "cache" / "signals.json").read_text(encoding="utf-8"))
    assert saved["namespaces"]["test"]["partial"]["data"] == {"v": 2}

    with pytest.raises(RuntimeError):
        main(_cli_args(tmp_path, md, "--signal-budget-minutes", "2.5"))
    assert budgets[-1] == 150.0


def test_cli_skips_site_signals_in_github_only_and_when_disabled(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PROXY_URL", raising=False)
    calls: list[int] = []

    async def fake(urls, **kwargs):
        calls.append(1)
        return kwargs["existing"]

    monkeypatch.setattr(cli_mod, "run_site_signals", fake)
    md = _write_index(tmp_path)
    assert main(_cli_args(tmp_path, md, "--no-site-signals")) == 0
    args = _cli_args(tmp_path, md)
    args.remove("--skip-without-proxy")
    args.remove("--no-dns-check")
    assert main(["--github-only", *args]) == 0
    assert calls == []
    assert not (tmp_path / "cache" / "signals.json").exists()


def test_cli_incremental_mode_uses_five_minute_budget(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PROXY_URL", raising=False)
    monkeypatch.delenv("SIGNAL_BUDGET_MINUTES", raising=False)
    captured: dict = {}

    async def fake(urls, **kwargs):
        captured.update(kwargs)
        return kwargs["existing"]

    async def no_enrich(results, **_kwargs):
        return results

    monkeypatch.setattr(cli_mod, "run_site_signals", fake)
    monkeypatch.setattr(cli_mod, "enrich_unhealthy_sites_last_active", no_enrich)
    md = _write_index(tmp_path)
    prev = tmp_path / "prev.json"
    prev.write_text(render_json_report(_signal_report()), encoding="utf-8")
    assert main(_cli_args(tmp_path, md, "--reuse-report", str(prev), "--only-alt-forges")) == 0
    assert captured["budget_seconds"] == 5 * 60
    assert captured["existing"]["https://sketchy.example/"].status == SiteStatus.FLAGGED


def test_deleted_domain_signal_gets_site_last_active_date() -> None:
    from datetime import UTC, datetime

    from fmhy_drift.models import SignalKind, SiteCheckResult, SiteSignal, SiteStatus
    from fmhy_drift.site_signals import merge_site_signals

    url = "https://gone.example.com/"
    seen = datetime(2026, 9, 28, tzinfo=UTC)
    existing = {url: SiteCheckResult(url=url, status=SiteStatus.DEAD, reason="NXDOMAIN", last_seen_at=seen)}
    sig = SiteSignal(
        kind=SignalKind.DOMAIN_EXPIRED,
        severity="warning",
        source="rdap",
        detail="not registered (registry RDAP 404); deleted by 2026-11-25",
    )
    out = merge_site_signals(existing, {url: [sig]})
    got = out[url].signals[0]
    assert got.detail.endswith("; site last active 2026-09-28")
    assert got.observed_at == seen
