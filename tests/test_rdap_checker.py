"""Tests for registry RDAP re-registration / drop-catch / expiry signals (MockTransport only)."""

from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from fmhy_drift import rdap_checker
from fmhy_drift.models import SignalKind
from fmhy_drift.rdap_checker import (
    build_signals,
    check_rdap,
    dropcatch_family,
    parse_rdap_domain,
)
from fmhy_drift.signal_cache import SignalCache

NOW = datetime(2026, 11, 25, 12, 0, tzinfo=UTC)


@pytest.fixture(autouse=True)
def _no_pacing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(rdap_checker, "SERVER_INTERVAL", {})


def _registrar(name: str, iana: str | None, handle: str | None = None) -> dict:
    ent: dict = {
        "objectClassName": "entity",
        "handle": handle or iana or name,
        "roles": ["registrar"],
        "vcardArray": [
            "vcard",
            [["version", {}, "text", "4.0"], ["fn", {}, "text", name]],
        ],
    }
    if iana:
        ent["publicIds"] = [{"type": "IANA Registrar ID", "identifier": iana}]
    return ent


# Trimmed from live responses (2026-11-25); field order and spellings kept.
VERISIGN = {
    "objectClassName": "domain",
    "handle": "2799834520_DOMAIN_NET-VRSN",
    "ldhName": "FMHY.NET",
    "status": ["client transfer prohibited", "client update prohibited"],
    "entities": [_registrar("Tucows Domains Inc.", "69")],
    "events": [
        {"eventAction": "registration", "eventDate": "2023-07-21T01:46:58Z"},
        {"eventAction": "expiration", "eventDate": "2027-07-21T01:46:58Z"},
        {"eventAction": "last changed", "eventDate": "2026-07-16T20:43:58Z"},
        {
            "eventAction": "last update of RDAP database",
            "eventDate": "2026-11-25T06:10:32Z",
        },
    ],
}
VERISIGN_DROPCATCH = {
    "objectClassName": "domain",
    "ldhName": "TUNETANK.COM",
    "status": ["client transfer prohibited"],
    "entities": [_registrar("DomainLocal LLC", "1529")],
    "events": [
        {"eventAction": "registration", "eventDate": "2016-02-05T13:01:46Z"},
        {"eventAction": "expiration", "eventDate": "2032-02-05T13:01:46Z"},
    ],
}
PIR = {
    "objectClassName": "domain",
    "ldhName": "archive.org",
    "status": ["client transfer prohibited", "client update prohibited"],
    "entities": [_registrar("easyDNS Technologies Inc.", "469")],
    "events": [
        {"eventAction": "transfer", "eventDate": "2013-01-02T00:41:38.956Z"},
        {"eventAction": "expiration", "eventDate": "2030-12-13T05:00:00Z"},
        {"eventAction": "registration", "eventDate": "1995-12-14T05:00:00.956Z"},
        {"eventAction": "last changed", "eventDate": "2026-08-12T02:55:08.137Z"},
    ],
}
IDENTITY_DIGITAL = {
    "objectClassName": "domain",
    "ldhName": "itch.io",
    "status": ["client transfer prohibited"],
    "entities": [_registrar("1API GmbH", "1387")],
    "events": [
        {"eventAction": "expiration", "eventDate": "2030-10-27T22:55:37Z"},
        {"eventAction": "registration", "eventDate": "2011-10-27T23:55:37Z"},
        {"eventAction": "last changed", "eventDate": "2026-08-12T07:55:57.739Z"},
    ],
}
GOOGLE = {
    "objectClassName": "domain",
    "ldhName": "web.dev",
    "status": ["client delete prohibited", "server transfer prohibited"],
    "entities": [_registrar("MarkMonitor Inc.", "292")],
    "events": [
        {"eventAction": "registration", "eventDate": "2018-10-29T15:57:39.435Z"},
        {"eventAction": "expiration", "eventDate": "2027-10-29T15:57:39.435Z"},
    ],
}
CENTRALNIC = {
    "objectClassName": "domain",
    "ldhName": "abc.xyz",
    "status": ["client transfer prohibited"],
    "entities": [_registrar("MarkMonitor Inc.", "292")],
    "events": [
        {"eventAction": "registration", "eventDate": "2014-03-20T12:59:17.0Z"},
        {"eventAction": "expiration", "eventDate": "2027-03-20T23:59:59.0Z"},
        {"eventAction": "transfer", "eventDate": "2015-08-04T00:07:02.0Z"},
    ],
}
TONIC_EXPIRED = {
    "objectClassName": "domain",
    "ldhName": "rawfree.to",
    "entities": [_registrar("NETIM SARL", "1519", handle="1519")],
    "status": [
        "server update prohibited",
        "server hold",
        "server transfer prohibited",
        "pending delete",
        "redemption period",
    ],
    "events": [
        {
            "eventAction": "registration",
            "eventActor": "netim",
            "eventDate": "2025-10-24T00:25:25.732Z",
        },
        {"eventAction": "expiration", "eventDate": "2026-10-24T00:25:25.732Z"},
    ],
}
REGISTER_SI = {  # date-only events, no publicIds, handle is an opaque UUID
    "objectClassName": "domain",
    "ldhName": "nyaa.si",
    "status": ["client delete prohibited", "client update prohibited"],
    "entities": [
        {
            "objectClassName": "entity",
            "handle": "r-0d989a26-82a8-11ee-a1a5-0000c9cca346",
            "roles": ["registrar"],
            "vcardArray": [
                "vcard",
                [["version", {}, "text", "4.0"], ["fn", {}, "text", "SAAX d.o.o."]],
            ],
        }
    ],
    "events": [
        {"eventAction": "registration", "eventDate": "2017-05-09"},
        {"eventAction": "last changed", "eventDate": "2023-10-24"},
        {"eventAction": "expiration", "eventDate": "2028-05-09"},
    ],
}


def test_parse_registry_shapes() -> None:
    v = parse_rdap_domain(VERISIGN)
    assert v["registration"].startswith("2023-07-21")
    assert v["registrar"] == "Tucows Domains Inc." and v["registrar_id"] == "69"
    assert v["statuses"] == ["clienttransferprohibited", "clientupdateprohibited"]

    p = parse_rdap_domain(PIR)
    assert p["registration"].startswith("1995-12-14") and p["transfer"].startswith(
        "2013-01-02"
    )

    i = parse_rdap_domain(IDENTITY_DIGITAL)
    assert i["registration"].startswith("2011-10-27") and i["expiration"].startswith(
        "2030-10-27"
    )

    assert parse_rdap_domain(GOOGLE)["registrar_id"] == "292"
    assert parse_rdap_domain(CENTRALNIC)["transfer"].startswith("2015-08-04")

    s = parse_rdap_domain(REGISTER_SI)
    assert (
        s["registration"].startswith("2017-05-09")
        and s["registrar"] == "SAAX d.o.o."
        and s["registrar_id"] is None
    )

    t = parse_rdap_domain(TONIC_EXPIRED)
    assert {"serverhold", "pendingdelete", "redemptionperiod"} <= set(t["statuses"])


def test_reregistered_requires_registration_after_listing_plus_grace() -> None:
    data = parse_rdap_domain(VERISIGN)  # registered 2023-07-21
    listed_before = datetime(2023, 1, 1, tzinfo=UTC)
    sigs = build_signals("fmhy.net", data, checked_at=NOW, listed_since=listed_before)
    assert [s.kind for s in sigs] == [SignalKind.REREGISTERED]
    assert sigs[0].severity == "critical"
    assert sigs[0].detail == "registered 2023-07-21, on FMHY since 2023-01-01"
    assert sigs[0].evidence_url == "https://lookup.icann.org/en/lookup?name=fmhy.net"

    within_grace = datetime(2023, 7, 18, tzinfo=UTC)
    assert (
        build_signals("fmhy.net", data, checked_at=NOW, listed_since=within_grace) == []
    )
    assert (
        build_signals(
            "fmhy.net",
            data,
            checked_at=NOW,
            listed_since=datetime(2024, 8, 7, tzinfo=UTC),
        )
        == []
    )
    assert build_signals("fmhy.net", data, checked_at=NOW, listed_since=None) == []


def test_dropcatch_registrar_severity_and_id_rules() -> None:
    assert (
        dropcatch_family("DomainLocal LLC", "1529", gtld=True) == "DropCatch/NameBright"
    )
    assert dropcatch_family("DropCatch.com 1234 LLC", None, gtld=False)
    assert dropcatch_family("Dynadot3 LLC", "1651", gtld=True) == "Dynadot shells"
    assert dropcatch_family("Dynadot Inc", "472", gtld=True) is None
    assert (
        dropcatch_family("Domain.com - Network Solutions, LLC", "886", gtld=True)
        is None
    )
    # ccTLD registrar numbering is registry-local: an ID alone must not match.
    assert dropcatch_family("Some Local Registrar", "1529", gtld=False) is None

    data = parse_rdap_domain(VERISIGN_DROPCATCH)  # registered 2016-02-05
    old_listing = build_signals(
        "tunetank.com",
        data,
        checked_at=NOW,
        listed_since=datetime(2024, 8, 7, tzinfo=UTC),
    )
    assert [(s.kind, s.severity) for s in old_listing] == [
        (SignalKind.DROPCATCH_REGISTRAR, "notice")
    ]
    assert (
        old_listing[0].detail
        == "registrar DomainLocal LLC (IANA 1529), registered 2016-02-05"
    )

    recent = {**data, "registration": "2026-11-14T12:33:46+00:00"}
    relisted = build_signals(
        "apkcombo.com",
        recent,
        checked_at=NOW,
        listed_since=datetime(2026, 11, 24, tzinfo=UTC),
    )
    assert [(s.kind, s.severity) for s in relisted] == [
        (SignalKind.DROPCATCH_REGISTRAR, "warning")
    ]

    unknown_listing = build_signals(
        "tunetank.com", data, checked_at=NOW, listed_since=None
    )
    assert [(s.kind, s.severity) for s in unknown_listing] == [
        (SignalKind.DROPCATCH_REGISTRAR, "warning")
    ]

    caught = build_signals(
        "tunetank.com",
        data,
        checked_at=NOW,
        listed_since=datetime(2015, 1, 1, tzinfo=UTC),
    )
    assert {(s.kind, s.severity) for s in caught} == {
        (SignalKind.REREGISTERED, "critical"),
        (SignalKind.DROPCATCH_REGISTRAR, "critical"),
    }


def test_expired_and_hold_statuses() -> None:
    data = parse_rdap_domain(TONIC_EXPIRED)
    data["url"] = "https://rdap.tonicregistry.to/rdap/domain/rawfree.to"
    sigs = build_signals(
        "rawfree.to",
        data,
        checked_at=NOW,
        listed_since=datetime(2026, 1, 1, tzinfo=UTC),
    )
    assert [s.kind for s in sigs] == [SignalKind.DOMAIN_EXPIRED]
    assert sigs[0].detail.startswith("expired 2026-10-24; status ")
    assert "server hold" in sigs[0].detail and "redemption period" in sigs[0].detail
    assert sigs[0].evidence_url == data["url"]  # ccTLD: ICANN lookup does not cover it

    # Expiry alone inside the registrar renewal grace window (or only after a stale fetch) is not reported.
    fresh = parse_rdap_domain(VERISIGN)
    fresh["expiration"] = "2026-11-20T00:00:00+00:00"
    assert build_signals("fmhy.net", fresh, checked_at=NOW, listed_since=None) == []
    fresh["expiration"] = "2026-09-20T00:00:00+00:00"
    assert (
        build_signals(
            "fmhy.net",
            fresh,
            checked_at=datetime(2026, 11, 1, tzinfo=UTC),
            listed_since=None,
        )
        == []
    )
    assert [
        s.kind
        for s in build_signals("fmhy.net", fresh, checked_at=NOW, listed_since=None)
    ] == [SignalKind.DOMAIN_EXPIRED]


BOOTSTRAP = {
    "services": [
        [["com", "net"], ["https://rdap.verisign.com/com/v1/"]],
        [["org"], ["https://rdap.publicinterestregistry.org/rdap/"]],
        [["xyz"], ["https://rdap.centralnic.com/xyz/"]],
    ]
}


def _make_handler(contacted: list[str], responses: dict[str, httpx.Response | dict]):
    def handler(request: httpx.Request) -> httpx.Response:
        contacted.append(str(request.url))
        url = str(request.url)
        if url == rdap_checker.BOOTSTRAP_URL:
            return httpx.Response(200, json=BOOTSTRAP)
        for suffix, resp in responses.items():
            if url.endswith(suffix):
                if callable(resp):
                    return resp(request)
                return (
                    resp
                    if isinstance(resp, httpx.Response)
                    else httpx.Response(200, json=resp)
                )
        return httpx.Response(404, json={"errorCode": 404})

    return handler


def test_check_rdap_end_to_end_never_contacts_listed_hosts() -> None:
    contacted: list[str] = []
    responses = {
        "/com/v1/domain/hijacked.com": {**VERISIGN, "ldhName": "HIJACKED.COM"},
        "/rdap/domain/archive.org": PIR,
        "/rdap/domain/itch.io": IDENTITY_DIGITAL,
        "/xyz/domain/abc.xyz": CENTRALNIC,
        # rdap.org discovery for a TLD missing from the bootstrap
        "rdap.org/domain/nyaa.si": httpx.Response(
            302, headers={"location": "https://rdap.register.si/domain/nyaa.si"}
        ),
        "rdap.register.si/domain/nyaa.si": REGISTER_SI,
        "rdap.org/domain/example.zz": httpx.Response(404),
    }
    cache = SignalCache(None, now=NOW)
    urls = [
        "https://hijacked.com/",
        "https://www.hijacked.com/page",
        "https://sub.hijacked.com/x",
        "https://archive.org/details/x",
        "https://itch.io/",
        "https://abc.xyz/",
        "https://nyaa.si/",
        "https://example.zz/",
        "https://someone.github.io/tool",  # shared platform: skipped
        "http://127.0.0.1/",  # IP: skipped
        "http://abcdefghijklmnop.onion/",  # non-public: skipped
        "https://gone.com/",  # 404 from authoritative registry
    ]
    listed = {
        "hijacked.com": datetime(2023, 1, 1, tzinfo=UTC),
        "archive.org": datetime(2024, 8, 7, tzinfo=UTC),
    }
    out = asyncio.run(
        check_rdap(
            urls,
            cache=cache,
            now=NOW,
            listed_since=listed,
            transport=httpx.MockTransport(_make_handler(contacted, responses)),
        )
    )
    hosts = {httpx.URL(u).host for u in contacted}
    listed_hosts = {httpx.URL(u).host for u in urls}
    assert not (hosts & listed_hosts), hosts & listed_hosts
    assert not any("github.io" in u for u in contacted)
    # .com lookups for 3 hosts of one registrable domain collapse to one query
    assert sum(1 for u in contacted if "hijacked.com" in u) == 1

    assert {s.kind for s in out["https://hijacked.com/"]} == {SignalKind.REREGISTERED}
    assert out["https://sub.hijacked.com/x"] == out["https://hijacked.com/"]
    assert "https://archive.org/details/x" not in out
    assert "https://itch.io/" not in out and "https://nyaa.si/" not in out
    gone = out["https://gone.com/"]
    assert [s.kind for s in gone] == [
        SignalKind.DOMAIN_EXPIRED
    ] and "not registered" in gone[0].detail
    assert cache.get_any_age("rdap_meta", "tld:si")[0] == {
        "base": "https://rdap.register.si/"
    }
    assert cache.get_any_age("rdap_meta", "tld:zz")[0] == {"base": None}

    # Second run: everything fresh in cache -> only no-rdap TLDs are skipped, no registry traffic.
    contacted.clear()
    out2 = asyncio.run(
        check_rdap(
            urls,
            cache=cache,
            now=NOW + timedelta(days=1),
            listed_since=listed,
            transport=httpx.MockTransport(_make_handler(contacted, responses)),
        )
    )
    assert contacted == []
    assert out2.keys() == out.keys()


def test_deadline_uses_stale_cache_and_registration_change_across_runs() -> None:
    cache = SignalCache(None, now=NOW - timedelta(days=60))
    contacted: list[str] = []
    old = {
        **VERISIGN,
        "events": [
            {"eventAction": "registration", "eventDate": "2012-01-01T00:00:00Z"}
        ],
    }
    asyncio.run(
        check_rdap(
            ["https://x.com/"],
            cache=cache,
            now=cache.now,
            listed_since={},
            transport=httpx.MockTransport(
                _make_handler(contacted, {"/domain/x.com": old})
            ),
        )
    )
    assert len(contacted) == 2  # bootstrap + x.com

    # Deadline already passed: no RDAP query, but the stale cached result still feeds signals.
    cache.now = NOW
    contacted.clear()
    expired_deadline = time.monotonic() - 1
    out = asyncio.run(
        check_rdap(
            ["https://x.com/"],
            cache=cache,
            now=NOW,
            listed_since={},
            deadline=expired_deadline,
            transport=httpx.MockTransport(_make_handler(contacted, {})),
        )
    )
    assert not any("/domain/" in u for u in contacted)
    assert out == {}  # nothing wrong with the stale data

    # Domain was deleted and registered again between runs: registration moved forward.
    new = {
        **VERISIGN,
        "events": [
            {"eventAction": "registration", "eventDate": "2026-09-01T00:00:00Z"}
        ],
    }
    out = asyncio.run(
        check_rdap(
            ["https://x.com/"],
            cache=cache,
            now=NOW,
            listed_since={},
            transport=httpx.MockTransport(
                _make_handler(contacted, {"/domain/x.com": new})
            ),
        )
    )
    sigs = out["https://x.com/"]
    assert [s.kind for s in sigs] == [SignalKind.REREGISTERED]
    assert sigs[0].detail == "registered 2026-09-01, previously registered 2012-01-01"


def test_retry_after_and_circuit_breaker(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = {"n": 0}

    def flaky(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(429, headers={"Retry-After": "0"})
        return httpx.Response(200, json=PIR)

    contacted: list[str] = []
    cache = SignalCache(None, now=NOW)
    asyncio.run(
        check_rdap(
            ["https://archive.org/"],
            cache=cache,
            now=NOW,
            listed_since={},
            transport=httpx.MockTransport(
                _make_handler(contacted, {"/domain/archive.org": flaky})
            ),
        )
    )
    assert calls["n"] == 2
    assert (
        rdap_checker.LAST_RUN_STATS["servers"]["rdap.publicinterestregistry.org"][
            "rate_limited"
        ]
        == 1
    )
    assert cache.get_any_age("rdap", "archive.org")[0]["state"] == "ok"

    # A registry that refuses everything trips its breaker; remaining names are skipped without requests.
    monkeypatch.setitem(rdap_checker.SERVER_CONCURRENCY, "rdap.centralnic.com", 1)
    contacted.clear()
    urls = [f"https://site{i}.xyz/" for i in range(20)]
    asyncio.run(
        check_rdap(
            urls,
            cache=SignalCache(None, now=NOW),
            now=NOW,
            listed_since={},
            transport=httpx.MockTransport(
                _make_handler(contacted, {".xyz": httpx.Response(403)})
            ),
        )
    )
    xyz_calls = [u for u in contacted if ".xyz" in u]
    assert len(xyz_calls) == rdap_checker.BREAKER_THRESHOLD
    assert rdap_checker.LAST_RUN_STATS["servers"]["rdap.centralnic.com"]["breaker_open"]
    assert (
        rdap_checker.LAST_RUN_STATS["stats"]["breaker_left"]
        == 20 - rdap_checker.BREAKER_THRESHOLD
    )


def test_network_failure_never_raises() -> None:
    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down", request=request)

    out = asyncio.run(
        check_rdap(
            ["https://a.com/"],
            cache=SignalCache(None, now=NOW),
            now=NOW,
            listed_since={},
            transport=httpx.MockTransport(boom),
        )
    )
    assert out == {}


def test_quota_retry_after_blocks_server_across_runs() -> None:
    contacted: list[str] = []
    cache = SignalCache(None, now=NOW)
    quota = httpx.Response(429, headers={"Retry-After": "85695"})
    urls = [f"https://s{i}.io/" for i in range(5)]
    h = _make_handler(contacted, {".io": quota})
    asyncio.run(
        check_rdap(
            urls,
            cache=cache,
            now=NOW,
            listed_since={},
            transport=httpx.MockTransport(h),
        )
    )
    assert (
        sum(1 for u in contacted if ".io" in u)
        <= rdap_checker.DEFAULT_SERVER_CONCURRENCY + 1
    )
    blk = cache.get_any_age("rdap_meta", "block:rdap.identitydigital.services")[0]
    assert blk["until"].startswith("2026-11-26")

    contacted.clear()
    asyncio.run(
        check_rdap(
            urls,
            cache=cache,
            now=NOW + timedelta(hours=2),
            listed_since={},
            transport=httpx.MockTransport(h),
        )
    )
    assert not any(".io" in u for u in contacted)
    assert rdap_checker.LAST_RUN_STATS["stats"]["quota_blocked"] == [
        "rdap.identitydigital.services"
    ]


def test_per_run_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(rdap_checker.SERVER_RUN_CAP, "rdap.verisign.com", 3)
    contacted: list[str] = []
    urls = [f"https://s{i}.com/" for i in range(10)]
    asyncio.run(
        check_rdap(
            urls,
            cache=SignalCache(None, now=NOW),
            now=NOW,
            listed_since={},
            transport=httpx.MockTransport(_make_handler(contacted, {".com": VERISIGN})),
        )
    )
    assert sum(1 for u in contacted if "/domain/" in u) == 3
    assert rdap_checker.LAST_RUN_STATS["stats"]["cap_left"] == 7


def test_no_false_positives_for_unsold_zones_and_unreliable_tlds() -> None:
    nf = {
        "schema": 1,
        "state": "notfound",
        "authoritative": True,
        "url": "https://rdap.nominet.uk/uk/domain/ed.ac.uk",
    }
    assert build_signals("ed.ac.uk", nf, checked_at=NOW, listed_since=None) == []
    assert build_signals("gone.co.uk", nf, checked_at=NOW, listed_since=None) != []

    to = parse_rdap_domain({**TONIC_EXPIRED, "status": ["active"]})
    to["expiration"] = "2030-01-01T00:00:00+00:00"
    assert (
        build_signals(
            "rawfree.to",
            to,
            checked_at=NOW,
            listed_since=datetime(2024, 8, 7, tzinfo=UTC),
        )
        == []
    )


def test_hold_on_unexpired_domain_is_held_not_expired() -> None:
    data = {
        "schema": 1,
        "state": "ok",
        "registration": "2026-03-31T12:38:15+00:00",
        "expiration": "2028-03-31T12:38:15+00:00",
        "last_changed": "2026-11-12T14:13:10+00:00",
        "statuses": ["clienthold", "clienttransferprohibited"],
        "registrar": "Tonic",
        "url": "https://rdap.tonicregistry.to/rdap/domain/voir-anime.to",
    }
    sigs = build_signals("voir-anime.to", data, checked_at=NOW, listed_since=None)
    assert [s.kind for s in sigs] == [SignalKind.DOMAIN_HELD]
    assert sigs[0].detail == "status client hold since 2026-11-12; paid until 2028-03-31"
    assert sigs[0].observed_at == datetime(2026, 11, 12, 14, 13, 10, tzinfo=UTC)


def test_redemption_with_hold_is_expired_with_expiry_date() -> None:
    data = {
        "schema": 1,
        "state": "ok",
        "registration": "2025-10-24T00:25:25+00:00",
        "expiration": "2026-10-24T00:25:25+00:00",
        "last_changed": "2026-11-23T22:47:42+00:00",
        "statuses": ["pendingdelete", "redemptionperiod", "serverhold"],
        "url": "https://rdap.tonicregistry.to/rdap/domain/rawfree.to",
    }
    sigs = build_signals("rawfree.to", data, checked_at=NOW, listed_since=None)
    assert [s.kind for s in sigs] == [SignalKind.DOMAIN_EXPIRED]
    assert sigs[0].detail.startswith("expired 2026-10-24; status pending delete, redemption period, server hold")
    assert sigs[0].observed_at == datetime(2026, 10, 24, 0, 25, 25, tzinfo=UTC)


def test_deleted_domain_keeps_last_known_expiry(tmp_path) -> None:
    dom = "gone.com"
    first = SignalCache(tmp_path / "c.json", now=NOW - timedelta(days=60))
    first.put(rdap_checker.CACHE_NS, dom, {
        "schema": 1,
        "state": "ok",
        "expiration": "2026-08-01T00:00:00+00:00",
        "statuses": ["redemptionperiod"],
    })
    first.save()
    second = SignalCache(tmp_path / "c.json", now=NOW - timedelta(days=7))
    nf = {"schema": 1, "state": "notfound", "authoritative": True}
    rdap_checker._carry_last_registered(second, dom, nf)
    second.put(rdap_checker.CACHE_NS, dom, nf)
    second.save()
    third = SignalCache(tmp_path / "c.json", now=NOW)
    nf2 = {"schema": 1, "state": "notfound", "authoritative": True}
    rdap_checker._carry_last_registered(third, dom, nf2)

    assert nf2["last_expiration"] == "2026-08-01T00:00:00+00:00"
    sigs = build_signals(dom, nf2, checked_at=NOW, listed_since=None)
    assert [s.kind for s in sigs] == [SignalKind.DOMAIN_EXPIRED]
    d0 = (NOW - timedelta(days=60)).date().isoformat()
    d1 = (NOW - timedelta(days=7)).date().isoformat()
    assert sigs[0].detail == (
        f"not registered (registry RDAP 404); expired 2026-08-01; deleted between {d0} and {d1}"
    )
