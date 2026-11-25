"""Storefront checker tests: MockTransport only, response shapes captured from the live platforms."""

from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from fmhy_drift import storefront_checker as sc
from fmhy_drift.models import SignalKind, TierThresholds
from fmhy_drift.signal_cache import SignalCache
from fmhy_drift.storefront_checker import (
    CACHE_NS,
    check_storefronts,
    is_storefront_url,
    parse_storefront_url,
)

NOW = datetime(2026, 11, 25, tzinfo=UTC)
TH = TierThresholds()

ALLOWED_HOSTS = {
    "addons.mozilla.org",
    "api.greasyfork.org",
    "api.sleazyfork.org",
    "itunes.apple.com",
    "play.google.com",
    "chromewebstore.google.com",
    "f-droid.org",
    "aur.archlinux.org",
    "microsoftedge.microsoft.com",
    "store.steampowered.com",
    "pypi.org",
    "registry.npmjs.org",
    "flathub.org",
    "marketplace.visualstudio.com",
    "openuserjs.org",
}


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _fast(_s: float) -> None:
        return None

    monkeypatch.setattr(sc, "_sleep", _fast)


def _days_ago(n: int) -> str:
    return (NOW - timedelta(days=n)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _run(urls: list[str], handler, cache: SignalCache | None = None, **kw):
    seen: list[httpx.Request] = []

    def wrapped(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        assert req.url.host in ALLOWED_HOSTS, f"unexpected host {req.url.host}"
        return handler(req)

    cache = cache or SignalCache(None, now=NOW)
    stats: dict = {}
    out = asyncio.run(
        check_storefronts(
            urls, cache=cache, now=NOW, thresholds=TH, transport=httpx.MockTransport(wrapped), stats=stats, **kw
        )
    )
    return out, seen, cache, stats


# ------------------------------------------------------------------------------------------- URL parsing


@pytest.mark.parametrize(
    ("url", "key"),
    [
        ("https://play.google.com/store/apps/details?id=org.wikipedia.beta", "play:org.wikipedia.beta"),
        ("https://apps.apple.com/us/app/google-gemini/id6477489729", "apple:6477489729"),
        ("https://apps.apple.com/app/id1158763303", "apple:1158763303"),
        ("https://greasyfork.org/en/scripts/18450-steam-bypass-age-confirmation-prompts", "greasyfork:18450"),
        ("https://greasyfork.org/scripts/578540", "greasyfork:578540"),
        ("https://sleazyfork.org/zh-CN/scripts/604891-x", "greasyfork:604891"),
        ("https://addons.mozilla.org/en-US/firefox/addon/ublock-origin/", "amo:ublock-origin"),
        ("https://addons.mozilla.org/addon/downthemall/", "amo:downthemall"),
        (
            "https://chrome.google.com/webstore/detail/free-download-manager-chr/ahmpjcflkgiildlgicmcieglgoilbfdp/RK%3D2/RS%3Dx",
            "cws:ahmpjcflkgiildlgicmcieglgoilbfdp",
        ),
        ("https://chromewebstore.google.com/detail/gpgbiinpmelaihndlegbgfkmnpofgfei", "cws:gpgbiinpmelaihndlegbgfkmnpofgfei"),
        ("https://f-droid.org/fr/packages/org.woheller69.whobird/", "fdroid:org.woheller69.whobird"),
        ("https://apt.izzysoft.de/fdroid/index/apk/jdm.apps.quizflow/", "izzy:jdm.apps.quizflow"),
        ("https://flathub.org/en/apps/info.febvre.Komikku", "flathub:info.febvre.Komikku"),
        ("https://store.steampowered.com/app/607260/McOsu/", "steam:607260"),
        ("https://marketplace.visualstudio.com/items?itemName=icrawl.discord-vscode", "vsmarket:icrawl.discord-vscode"),
        ("https://open-vsx.org/extension/crsx/ag-usage", "openvsx:crsx/ag-usage"),
        ("https://www.npmjs.com/package/@scope/pkg", "npm:@scope/pkg"),
        ("https://hub.docker.com/_/nginx", "docker:library/nginx"),
        ("https://aur.archlinux.org/packages/yay", "aur:yay"),
        ("https://obsidian.md/plugins?id=dataview", "obsidian:dataview"),
    ],
)
def test_parse_item_urls(url: str, key: str) -> None:
    item = parse_storefront_url(url)
    assert item is not None and item.key == key
    assert is_storefront_url(url)


@pytest.mark.parametrize(
    "url",
    [
        "https://play.google.com/store/apps/dev?id=5578181639208826441",
        "https://play.google.com/store/apps/developer?id=Deutscher+Volkshochschul-Verband",
        "https://play.google.com/books",
        "https://greasyfork.org/",
        "https://addons.mozilla.org/firefox/extensions/",
        "https://addons.mozilla.org/android/",
        "https://chromewebstore.google.com/",
        "https://store.steampowered.com/search/?maxprice=free&category1=997",
        "https://store.steampowered.com/curator/8485829/",
        "https://snapcraft.io/store",
        "https://hub.docker.com/",
        "https://obsidian.md/canvas",
        "https://f-droid.org/",
        "https://example.com/store/apps/details?id=a.b",
    ],
)
def test_non_item_pages_are_skipped(url: str) -> None:
    assert parse_storefront_url(url) is None
    assert not is_storefront_url(url)


# ------------------------------------------------------------------------------------------- platforms


def test_amo_tiers_and_removed() -> None:
    ages = {"fresh": 10, "t1": 100, "t2": 200, "t3": 400}

    def handler(req: httpx.Request) -> httpx.Response:
        slug = req.url.path.rstrip("/").split("/")[-1]
        if slug == "gone":
            return httpx.Response(404, json={"detail": "Not found."})
        return httpx.Response(
            200,
            json={
                "slug": slug,
                "status": "public",
                "is_disabled": False,
                "last_updated": _days_ago(ages[slug]),
                "url": f"https://addons.mozilla.org/en-US/firefox/addon/{slug}/",
            },
        )

    urls = [f"https://addons.mozilla.org/en-US/firefox/addon/{s}/" for s in [*ages, "gone"]]
    out, _, _, _ = _run(urls, handler)
    sev = {u.split("/")[-2]: [(s.kind, s.severity) for s in ss] for u, ss in out.items()}
    assert "fresh" not in sev
    assert sev["t1"] == [(SignalKind.STORE_STALE, "notice")]
    assert sev["t2"] == [(SignalKind.STORE_STALE, "warning")]
    assert sev["t3"] == [(SignalKind.STORE_STALE, "critical")]
    assert sev["gone"] == [(SignalKind.STORE_REMOVED, "critical")]
    t3 = out[urls[3]][0]
    assert t3.detail == f"last updated {(NOW - timedelta(days=400)):%Y-%m-%d} (AMO)"
    assert t3.source == "amo" and t3.observed_at is not None


def test_greasyfork_confirms_404_on_sleazyfork_and_follows_merge_redirect() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        host, path = req.url.host, req.url.path
        if path == "/scripts/11.json":
            return httpx.Response(404, content=b"")  # deleted everywhere
        if path == "/scripts/604891.json":
            if host == "api.sleazyfork.org":
                return httpx.Response(200, json={"id": 604891, "deleted": False, "code_updated_at": _days_ago(1)})
            return httpx.Response(404, content=b"")
        if path == "/scripts/394039.json":
            return httpx.Response(301, headers={"Location": f"https://{host}/en/scripts/519037"})
        if path == "/scripts/519037.json":
            return httpx.Response(
                200,
                json={"id": 519037, "deleted": False, "code_updated_at": _days_ago(5),
                      "url": "https://greasyfork.org/scripts/519037-nexus-no-wait"},
            )
        if path == "/scripts/5.json":
            return httpx.Response(200, json={"id": 5, "deleted": True, "code_updated_at": _days_ago(9)})
        return httpx.Response(500)

    urls = [
        "https://greasyfork.org/en/scripts/11",
        "https://greasyfork.org/scripts/604891",
        "https://greasyfork.org/en/scripts/394039",
        "https://greasyfork.org/scripts/5-x",
    ]
    out, _, _, _ = _run(urls, handler)
    assert out[urls[0]][0].kind == SignalKind.STORE_REMOVED
    assert urls[1] not in out  # lives on SleazyFork
    assert urls[2] not in out  # merged into a fresh script
    assert out[urls[3]][0].kind == SignalKind.STORE_REMOVED


def test_apple_country_quirk_avoids_false_removal() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        cc = req.url.params["country"]
        ids = req.url.params["id"].split(",")
        results = []
        for i in ids:
            if i == "6737597349" and cc == "cn":  # absent in gb/us storefronts, present in cn
                results.append({"trackId": 6737597349, "currentVersionReleaseDate": _days_ago(4),
                                "trackViewUrl": "https://apps.apple.com/cn/app/x/id6737597349?uo=4"})
            if i == "1491684442":
                results.append({"trackId": 1491684442, "currentVersionReleaseDate": _days_ago(500)})
        return httpx.Response(200, json={"resultCount": len(results), "results": results})

    urls = [
        "https://apps.apple.com/gb/app/deepseek/id6737597349",
        "https://apps.apple.com/in/app/keypad/id1491684442",
        "https://apps.apple.com/app/id642211198",
    ]
    out, seen, _, _ = _run(urls, handler)
    assert urls[0] not in out
    assert out[urls[1]][0].severity == "critical" and out[urls[1]][0].kind == SignalKind.STORE_STALE
    assert out[urls[2]][0].kind == SignalKind.STORE_REMOVED
    # URL storefronts are queried first, then the global list.
    assert [r.url.params["country"] for r in seen][:2] == ["gb", "in"]


def test_apple_transient_failure_never_marks_removed() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.params["country"] == "de":
            return httpx.Response(503)
        return httpx.Response(200, json={"resultCount": 0, "results": []})

    out, _, _, _ = _run(["https://apps.apple.com/app/id642211198"], handler)
    assert out == {}


PLAY_OK = (
    '<html><body>org.example.app<div class="q078ud">Updated on</div><div class="xg1aie">Nov 18, 2024</div></body></html>'
)


def test_google_play_date_404_429_and_consent() -> None:
    calls: dict[str, int] = {}

    def handler(req: httpx.Request) -> httpx.Response:
        pkg = req.url.params["id"]
        calls[pkg] = calls.get(pkg, 0) + 1
        assert req.url.params["hl"] == "en"
        if pkg == "org.example.app":
            return httpx.Response(200, text=PLAY_OK)
        if pkg == "ru.sberbankmobile":
            return httpx.Response(404, text="<html>Not Found</html>")
        if pkg == "rate.limited":
            return httpx.Response(429, headers={"Retry-After": "1"})
        if pkg == "consent.redirect":
            return httpx.Response(302, headers={"Location": "https://consent.google.com/ml?continue=x"})
        return httpx.Response(500)

    base = "https://play.google.com/store/apps/details?id="
    urls = [base + p for p in ("org.example.app", "ru.sberbankmobile", "rate.limited", "consent.redirect")]
    out, _, _, _ = _run(urls, handler)
    assert out[urls[0]][0].detail == "last updated 2024-11-18 (Google Play)"
    assert out[urls[1]][0].kind == SignalKind.STORE_REMOVED
    assert calls["ru.sberbankmobile"] == 2  # 404 confirmed without gl before flagging
    assert urls[2] not in out and calls["rate.limited"] == 2  # one Retry-After retry, then no evidence
    assert urls[3] not in out


def test_chrome_web_store_empty_title_is_removed() -> None:
    live_id, gone_id = "ddkjiahejlhfcafbddmgiahcphecmpfh", "cjpalhdlnbpafiamejdnhcphjbkeiagm"

    def handler(req: httpx.Request) -> httpx.Response:
        path = req.url.path
        if path == f"/detail/{live_id}":
            return httpx.Response(301, headers={"Location": f"/detail/ublock-origin-lite/{live_id}?hl=en"})
        if path == f"/detail/ublock-origin-lite/{live_id}":
            return httpx.Response(
                200,
                text='<li class="MqICNe"><div class="QDHp8e">Updated</div><div>March 2, 2026</div></li>',
            )
        if path == f"/detail/{gone_id}":
            return httpx.Response(301, headers={"Location": f"/detail/empty-title/{gone_id}?hl=en"})
        if path == f"/detail/empty-title/{gone_id}":
            return httpx.Response(200, text="<title>Chrome Web Store</title>")
        return httpx.Response(500)

    urls = [
        f"https://chrome.google.com/webstore/detail/ublock-origin-lite/{live_id}",
        f"https://chromewebstore.google.com/detail/ubo/{gone_id}",
    ]
    out, _, _, _ = _run(urls, handler)
    assert out[urls[0]][0].detail == "last updated 2026-03-02 (Chrome Web Store)"
    assert out[urls[0]][0].severity == "warning"
    assert out[urls[1]][0].kind == SignalKind.STORE_REMOVED


def test_fdroid_page_dates_and_confirmed_removal() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/en/packages/org.old.app/":
            return httpx.Response(200, text="<p>Added on Nov 18, 2021</p><p>Added on Mar 02, 2021</p>")
        if req.url.path == "/en/packages/org.gone.app/":
            return httpx.Response(404)
        if req.url.path == "/api/v1/packages/org.gone.app":
            return httpx.Response(404)
        return httpx.Response(500)

    urls = ["https://f-droid.org/packages/org.old.app/", "https://f-droid.org/en/packages/org.gone.app/"]
    out, _, _, _ = _run(urls, handler)
    assert out[urls[0]][0].detail == "last updated 2021-11-18 (F-Droid)"
    assert out[urls[1]][0].kind == SignalKind.STORE_REMOVED


def test_aur_out_of_date_is_warning() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "type": "multiinfo",
                "resultcount": 1,
                "results": [{"Name": "foo", "LastModified": int((NOW - timedelta(days=20)).timestamp()),
                             "OutOfDate": int(datetime(2026, 10, 1, tzinfo=UTC).timestamp())}],
            },
        )

    urls = ["https://aur.archlinux.org/packages/foo", "https://aur.archlinux.org/packages/gone"]
    out, _, _, _ = _run(urls, handler)
    sig = out[urls[0]][0]
    assert sig.kind == SignalKind.STORE_STALE and sig.severity == "warning"
    assert sig.detail == "flagged out-of-date 2026-10-01 (AUR)"
    assert out[urls[1]][0].kind == SignalKind.STORE_REMOVED


def test_registry_apis() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        h, p = req.url.host, req.url.path
        if h == "pypi.org":
            return httpx.Response(200, json={"releases": {"0.25": [{"upload_time_iso_8601": "2020-12-20T13:03:50Z"}]}})
        if h == "registry.npmjs.org":
            return httpx.Response(404, json={"error": "Not found"})
        if h == "store.steampowered.com":
            return httpx.Response(200, json={req.url.params["appids"]: {"success": False}})
        if h == "microsoftedge.microsoft.com":
            return httpx.Response(200, json={"crxId": p.split("/")[-1], "lastUpdateDate": 1778695969.05})
        if h == "flathub.org":
            return httpx.Response(200, json={"releases": [{"timestamp": "1778112000"}], "is_eol": True})
        if h == "marketplace.visualstudio.com":
            return httpx.Response(200, json={"results": [{"extensions": []}]})
        return httpx.Response(500)

    urls = [
        "https://pypi.org/project/wenku/",
        "https://www.npmjs.com/package/gone-pkg",
        "https://store.steampowered.com/app/999999/X/",
        "https://microsoftedge.microsoft.com/addons/detail/x/jpdmlobalaahflgppobfnhbdobpfpoao",
        "https://flathub.org/apps/dev.geopjr.Collision",
        "https://marketplace.visualstudio.com/items?itemName=a.gone",
    ]
    out, _, _, _ = _run(urls, handler)
    assert out[urls[0]][0].detail == "last updated 2020-12-20 (PyPI)"
    assert out[urls[1]][0].kind == SignalKind.STORE_REMOVED
    assert out[urls[2]][0].kind == SignalKind.STORE_REMOVED
    assert out[urls[3]][0].detail == "last updated 2026-05-13 (Edge Add-ons)"
    assert out[urls[4]][0].detail == "last updated 2026-05-07; marked end-of-life (Flathub)"
    assert out[urls[4]][0].severity == "warning"
    assert out[urls[5]][0].kind == SignalKind.STORE_REMOVED


def test_openuserjs_updated_time() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, text='updated <time class="script-updated" datetime="2018-04-30T10:59:59.718Z" title="x">'
        )

    url = "https://openuserjs.org/scripts/anka-213/Webcomic_Reader"
    out, _, _, _ = _run([url], handler)
    assert out[url][0].detail == "last updated 2018-04-30 (OpenUserJS)"


# ------------------------------------------------------------------------------------------- run mechanics


def test_dedupe_and_cache_reuse() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"status": "public", "last_updated": _days_ago(400)})

    urls = [
        "https://addons.mozilla.org/en-US/firefox/addon/foo/",
        "https://addons.mozilla.org/firefox/addon/foo/",
        "https://addons.mozilla.org/addon/foo",
    ]
    out, seen, cache, _ = _run(urls, handler)
    assert len(seen) == 1 and set(out) == set(urls)
    assert cache.get(CACHE_NS, "amo:foo", ttl=timedelta(days=6))["status"] == "ok"

    out2, seen2, _, stats = _run(urls, handler, cache=cache)
    assert seen2 == [] and set(out2) == set(urls)
    assert stats["platforms"]["amo"]["cached"] == 1


def test_deadline_falls_back_to_any_age_cache() -> None:
    cache = SignalCache(None, now=NOW - timedelta(days=30))
    cache.put(CACHE_NS, "amo:foo", {"status": "removed", "reason": "API 404", "evidence": "x", "first_seen": "2026-10-26"})
    cache.now = NOW

    def handler(req: httpx.Request) -> httpx.Response:  # pragma: no cover - must not be called
        raise AssertionError("no requests after deadline")

    url = "https://addons.mozilla.org/firefox/addon/foo/"
    out, seen, _, stats = _run([url], handler, cache=cache, deadline=time.monotonic() - 1)
    assert seen == []
    assert out[url][0].kind == SignalKind.STORE_REMOVED
    assert stats["deadline_hit"] is True


def test_circuit_breaker_stops_platform() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(503)

    urls = [f"https://addons.mozilla.org/firefox/addon/a{i}/" for i in range(20)]
    out, seen, _, stats = _run(urls, handler)
    assert out == {}
    assert "amo" in stats["breaker_open"]
    assert len(seen) <= 2 * (sc.BREAKER_THRESHOLD + sc.DEFAULT_CONCURRENCY)


def test_network_errors_never_raise() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom", request=req)

    out, _, _, _ = _run(
        ["https://play.google.com/store/apps/details?id=a.b", "https://apps.apple.com/app/id123456"], handler
    )
    assert out == {}


def test_removed_first_seen_is_preserved() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"detail": "Not found."})

    url = "https://addons.mozilla.org/firefox/addon/gone/"
    old = SignalCache(None, now=NOW - timedelta(days=10))
    old.put(CACHE_NS, "amo:gone", {"status": "removed", "reason": "API 404", "first_seen": "2026-11-15T00:00:00+00:00"})
    old.now = NOW
    out, _, _, _ = _run([url], handler, cache=old)
    assert out[url][0].observed_at == datetime(2026, 11, 15, tzinfo=UTC)
