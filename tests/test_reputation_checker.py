"""Unit tests for the passive reputation checker (GSB / URLhaus / ThreatFox / OpenPhish), MockTransport only."""

from __future__ import annotations

import asyncio
import io
import json
import zipfile
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from fmhy_drift import reputation_checker as rc
from fmhy_drift.models import SignalKind
from fmhy_drift.signal_cache import SignalCache

NOW = datetime(2026, 11, 25, 12, 0, tzinfo=UTC)
ALLOWED_HOSTS = {
    "safebrowsing.googleapis.com",
    "urlhaus-api.abuse.ch",
    "threatfox-api.abuse.ch",
    "openphish.com",
    "raw.githubusercontent.com",
}


# --- false-positive guards -------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("fmhy", "feed", "expected"),
    [
        # ordinary single-owner host: any feed URL on the same host flags it
        ("https://badsite.example/", "http://badsite.example/payload.exe", True),
        ("https://www.badsite.example/tools", "http://badsite.example/x/y.zip", True),
        ("https://badsite.example/", "http://cdn.badsite.example/x.zip", False),  # different host
        ("https://goodsite.example/", "http://goodsite.example.evil.test/", False),
        # Google Drive: unrelated uploaded files never flag the FMHY link
        ("https://drive.google.com/drive/folders/AAA", "https://drive.google.com/uc?id=ZZZ&export=download", False),
        ("https://drive.google.com/file/d/AAA/view", "https://drive.google.com/file/d/BBB/view", False),
        ("https://drive.google.com/file/d/AAA/view", "https://drive.google.com/file/d/AAA/view", True),
        ("https://drive.google.com/file/d/AAA", "https://drive.google.com/file/d/AAA/view?usp=sharing", True),
        ("https://drive.google.com/open?id=AAA", "https://drive.google.com/open?id=BBB", False),
        ("https://docs.google.com/spreadsheets/d/S1", "https://docs.google.com/forms/d/e/PHISH/viewform", False),
        # GitHub: only releases / files under the listed repo
        ("https://github.com/owner/repo", "https://github.com/other/malware/releases/download/v1/a.exe", False),
        ("https://github.com/owner/repo", "https://github.com/owner/repo2/raw/main/a.exe", False),
        ("https://github.com/owner/repo", "https://github.com/owner/repo/releases/download/v1/a.exe", True),
        ("https://github.com/", "https://github.com/x/y/raw/main/a.exe", False),
        ("https://raw.githubusercontent.com/o/r/main/x", "https://raw.githubusercontent.com/z/q/main/evil.ps1", False),
        # MEGA / MediaFire / Pastebin / cloud storage
        ("https://mega.nz/folder/ABC#key", "https://mega.nz/file/XYZ#k", False),
        ("https://www.mediafire.com/folder/abc/tools", "https://www.mediafire.com/file/zzz/crack.zip/file", False),
        ("https://pastebin.com/AbCd", "https://pastebin.com/raw/EvIl", False),
        ("https://mybucket.s3.amazonaws.com/index.html", "https://mybucket.s3.amazonaws.com/evil.exe", False),
        ("https://cdn.discordapp.com/attachments/1/2/a", "https://cdn.discordapp.com/attachments/9/9/x.exe", False),
        # PSL-private tenants (*.github.io, *.pages.dev): tenant root flags the tenant, other paths need a prefix
        ("https://someuser.github.io/", "https://otheruser.github.io/phish", False),
        ("https://someuser.github.io/tool/", "https://someuser.github.io/Netflix_gt", False),
        ("https://someuser.github.io/", "https://someuser.github.io/Netflix_gt", True),
        ("https://someuser.github.io/tool/", "https://someuser.github.io/tool/login.html", True),
        ("https://someuser.github.io/tool/", "https://someuser.github.io/", True),
        ("https://project.pages.dev/", "https://about-blank-b9l.pages.dev/", False),
    ],
)
def test_feed_entry_matches_guards_shared_platforms(fmhy: str, feed: str, expected: bool) -> None:
    assert rc.feed_entry_matches(fmhy, feed) is expected


def test_big_shared_host_detection() -> None:
    for host in ["drive.google.com", "github.com", "mega.nz", "www.mediafire.com", "pastebin.com",
                 "mybucket.s3.amazonaws.com", "storage.googleapis.com", "objects.githubusercontent.com"]:
        assert rc.is_big_shared_host(host), host
    for host in ["badsite.example", "someuser.github.io", "fmhy.net"]:
        assert not rc.is_big_shared_host(host), host


# --- parsers ---------------------------------------------------------------------------------------------

URLHAUS_CSV = """################################################################
# abuse.ch URLhaus Database Dump (CSV - recent URLs)            #
# Last updated: 2026-11-25 11:55:02 (UTC)                       #
#                                                              #
# Terms Of Use: https://urlhaus.abuse.ch/api/                  #
################################################################
# id,dateadded,url,url_status,last_online,threat,tags,urlhaus_link,reporter
"3400001","2026-11-24 08:00:01","http://badsite.example/payload.exe","online","2026-11-25 10:00:00","malware_download","exe,Formbook","https://urlhaus.abuse.ch/url/3400001/","anonymous"
"3400002","2026-08-01 08:00:01","http://oldbad.example/x.bin","offline","2026-08-02 10:00:00","malware_download","elf","https://urlhaus.abuse.ch/url/3400002/","anonymous"
"3400003","2026-11-23 08:00:01","https://drive.google.com/uc?id=ZZZ&export=download","online","2026-11-25 10:00:00","malware_download","zip","https://urlhaus.abuse.ch/url/3400003/","anonymous"
"3400004","2026-11-20 08:00:01","http://recentoff.example/a.sh","offline","","malware_download","sh","https://urlhaus.abuse.ch/url/3400004/","anonymous"
"3400005","2026-11-24 09:00:00","https://github.com/owner/repo/releases/download/v1/a.exe","online","","malware_download","exe","https://urlhaus.abuse.ch/url/3400005/","x"
"""

THREATFOX_CSV = """################################################################
# ThreatFox IOCs: full data dump                                 #
################################################################
#
# "first_seen_utc","ioc_id","ioc_value","ioc_type","threat_type","fk_malware","malware_alias","malware_printable","last_seen_utc","confidence_level","reference","tags","anonymous","reporter"
"2026-11-10 10:00:00", "1500001", "c2bad.example", "domain", "botnet_cc", "win.lumma", "None", "Lumma Stealer", "2026-11-24 00:00:00", "100", "None", "Lumma", "0", "abuse_ch"
"2026-11-10 10:00:00", "1500002", "lowconf.example", "domain", "botnet_cc", "win.x", "None", "X", "", "25", "None", "", "0", "y"
"2026-11-10 10:00:00", "1500003", "1.2.3.4:443", "ip:port", "botnet_cc", "win.x", "None", "X", "", "100", "None", "", "0", "y"
"2026-11-10 10:00:00", "1500004", "drive.google.com", "domain", "payload_delivery", "win.x", "None", "X", "", "100", "None", "", "0", "y"
"""

OPENPHISH_FEED = """https://belepes-telekom-hu.vh22325.vh.net.pl/
http://dan-san2006.github.io/Netflix_gt
https://phishy.example/login
https://docs.google.com/forms/d/e/PHISH/viewform
"""


def test_parse_urlhaus_keeps_online_and_recent_only() -> None:
    entries = rc.parse_urlhaus_csv(URLHAUS_CSV, now=NOW)
    raw = {e.raw for e in entries}
    assert "http://badsite.example/payload.exe" in raw
    assert "http://recentoff.example/a.sh" in raw  # offline but added 5 days ago
    assert "http://oldbad.example/x.bin" not in raw  # offline and old
    bad = next(e for e in entries if e.raw == "http://badsite.example/payload.exe")
    assert bad.evidence_url == "https://urlhaus.abuse.ch/url/3400001/"
    assert bad.observed_at == datetime(2026, 11, 24, 8, 0, 1, tzinfo=UTC)
    assert "added 2026-11-24" in bad.detail


def test_parse_threatfox_filters_confidence_and_types() -> None:
    entries = rc.parse_threatfox_csv(THREATFOX_CSV, now=NOW)
    assert {e.raw for e in entries} == {"c2bad.example", "drive.google.com"}
    c2 = next(e for e in entries if e.raw == "c2bad.example")
    assert c2.evidence_url == "https://threatfox.abuse.ch/ioc/1500001/"
    assert "Lumma Stealer" in c2.detail and "first seen 2026-11-10" in c2.detail


# --- end-to-end with MockTransport -----------------------------------------------------------------------


def _zip(name: str, text: str) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(name, text)
    return buf.getvalue()


LISTED = [
    "https://badsite.example/",
    "https://drive.google.com/drive/folders/AAA",
    "https://github.com/owner/repo",
    "https://github.com/someone/else",
    "https://c2bad.example/download",
    "https://dan-san2006.github.io/",
    "https://phishy.example/",
    "https://docs.google.com/spreadsheets/d/S1",
    "https://gsb-flagged.example/page",
    "https://clean.example/",
    "https://recentoff.example/",
]


def _handler_factory(seen: list[httpx.Request], gsb_bodies: list[dict]):
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        host = request.url.host
        path = request.url.path
        if host == "safebrowsing.googleapis.com":
            assert request.url.params.get("key") == "gsb-key"
            body = json.loads(request.content)
            gsb_bodies.append(body)
            entries = [e["url"] for e in body["threatInfo"]["threatEntries"]]
            matches = []
            for url in entries:
                if url == "https://gsb-flagged.example/":
                    matches.append(
                        {
                            "threatType": "SOCIAL_ENGINEERING",
                            "platformType": "ANY_PLATFORM",
                            "threatEntryType": "URL",
                            "threat": {"url": url},
                            "cacheDuration": "300.000s",
                        }
                    )
            return httpx.Response(200, json={"matches": matches} if matches else {})
        if host == "urlhaus-api.abuse.ch":
            assert path == "/v2/files/exports/abuse-key/full.csv.zip"
            return httpx.Response(200, content=_zip("csv.txt", URLHAUS_CSV))
        if host == "threatfox-api.abuse.ch":
            if path.endswith("/full.csv.zip"):
                return httpx.Response(404, text="not found")
            assert path == "/v2/files/exports/abuse-key/recent.csv"
            return httpx.Response(200, text=THREATFOX_CSV)
        if host == "openphish.com":
            return httpx.Response(
                302, headers={"Location": "https://raw.githubusercontent.com/openphish/public_feed/refs/heads/main/feed.txt"}
            )
        if host == "raw.githubusercontent.com":
            return httpx.Response(200, text=OPENPHISH_FEED)
        raise AssertionError(f"unexpected request to {request.url}")

    return handler


def test_check_reputation_end_to_end_and_never_contacts_targets() -> None:
    seen: list[httpx.Request] = []
    gsb_bodies: list[dict] = []
    cache = SignalCache(None, now=NOW)
    out = asyncio.run(
        rc.check_reputation(
            LISTED,
            cache=cache,
            now=NOW,
            transport=httpx.MockTransport(_handler_factory(seen, gsb_bodies)),
            gsb_api_key="gsb-key",
            abusech_auth_key="abuse-key",
        )
    )
    assert {r.url.host for r in seen} <= ALLOWED_HOSTS

    by_source = {url: {s.source for s in sigs} for url, sigs in out.items()}
    assert by_source == {
        "https://badsite.example/": {"urlhaus"},
        "https://github.com/owner/repo": {"urlhaus"},
        "https://recentoff.example/": {"urlhaus"},
        "https://c2bad.example/download": {"threatfox"},
        "https://dan-san2006.github.io/": {"openphish"},
        "https://phishy.example/": {"openphish"},
        "https://gsb-flagged.example/page": {"google_safe_browsing"},
    }
    for sigs in out.values():
        assert all(s.kind == SignalKind.MALICIOUS and s.severity == "critical" for s in sigs)
        assert all(s.evidence_url for s in sigs)
    gsb = out["https://gsb-flagged.example/page"][0]
    assert "SOCIAL_ENGINEERING" in gsb.detail and "origin" in gsb.detail
    assert gsb.evidence_url.startswith("https://transparencyreport.google.com/safe-browsing/search?url=")

    # GSB request shape: all four threat types, ANY_PLATFORM, exact URL + origin, no origin for shared platforms
    (body,) = gsb_bodies
    info = body["threatInfo"]
    assert set(info["threatTypes"]) == {"MALWARE", "SOCIAL_ENGINEERING", "UNWANTED_SOFTWARE",
                                         "POTENTIALLY_HARMFUL_APPLICATION"}
    assert info["platformTypes"] == ["ANY_PLATFORM"]
    sent = {e["url"] for e in info["threatEntries"]}
    assert "https://gsb-flagged.example/page" in sent and "https://gsb-flagged.example/" in sent
    assert "https://drive.google.com/" not in sent and "https://github.com/" not in sent

    # second run on the same day is served from cache without any request
    seen.clear()
    out2 = asyncio.run(
        rc.check_reputation(
            LISTED,
            cache=cache,
            now=NOW + timedelta(hours=2),
            transport=httpx.MockTransport(_handler_factory(seen, gsb_bodies)),
            gsb_api_key="gsb-key",
            abusech_auth_key="abuse-key",
        )
    )
    assert seen == []
    assert set(out2) == set(out)


def test_gsb_batches_500_entries() -> None:
    urls = [f"https://site{i}.example/p" for i in range(400)]  # 800 entries (URL + origin) -> 2 requests
    seen: list[httpx.Request] = []
    sizes: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        sizes.append(len(json.loads(request.content)["threatInfo"]["threatEntries"]))
        return httpx.Response(200, json={})

    out = asyncio.run(
        rc.check_reputation(
            urls, cache=SignalCache(None, now=NOW), now=NOW, transport=httpx.MockTransport(handler),
            gsb_api_key="k", use_openphish=False,
        )
    )
    assert out == {}
    assert sorted(sizes) == [300, 500]


def test_missing_keys_and_failures_fall_back_to_stale_cache(capsys) -> None:
    cache = SignalCache(None, now=NOW - timedelta(days=3))
    seen: list[httpx.Request] = []
    asyncio.run(
        rc.check_reputation(
            LISTED, cache=cache, now=NOW - timedelta(days=3),
            transport=httpx.MockTransport(_handler_factory(seen, [])), use_openphish=True,
        )
    )
    assert any(r.url.host == "raw.githubusercontent.com" for r in seen)
    capsys.readouterr()

    # Three days later the OpenPhish feed is down: stale positives are still reported, nothing raises.
    cache.now = NOW

    def broken(request: httpx.Request) -> httpx.Response:
        assert request.url.host in ALLOWED_HOSTS
        return httpx.Response(500, text="boom")

    out = asyncio.run(rc.check_reputation(LISTED, cache=cache, now=NOW, transport=httpx.MockTransport(broken)))
    assert set(out) == {"https://dan-san2006.github.io/", "https://phishy.example/"}
    line = capsys.readouterr().out
    assert "GOOGLE_SAFE_BROWSING_API_KEY" in line and "ABUSECH_AUTH_KEY" in line
    assert "openphish failed" in line


def test_abusech_401_does_not_raise() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host in {"urlhaus-api.abuse.ch", "threatfox-api.abuse.ch"}:
            return httpx.Response(401, json={"status": 401, "message": "Invalid auth token"})
        assert request.url.host in ALLOWED_HOSTS
        return httpx.Response(200, text="")

    out = asyncio.run(
        rc.check_reputation(
            LISTED, cache=SignalCache(None, now=NOW), now=NOW, transport=httpx.MockTransport(handler),
            abusech_auth_key="bad",
        )
    )
    assert out == {}
