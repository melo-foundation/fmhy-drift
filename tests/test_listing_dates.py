"""Tests for FMHY listing dates from GitHub GraphQL blame (MockTransport only)."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime

import httpx

from fmhy_drift import listing_dates
from fmhy_drift.listing_dates import fetch_listing_dates, map_hosts_to_dates
from fmhy_drift.signal_cache import SignalCache

NOW = datetime(2026, 11, 25, tzinfo=UTC)

VIDEO_MD = "\n".join(
    [
        "# Video",
        "* [Site A](https://www.site-a.com/) / [Mirror](https://site-a.com/mirror) - streaming",
        "* [Site B](https://site-b.net/), [GH](https://github.com/x/y)",
        "* [Site A again](https://sub.site-a.com/x)",
        "* [Pages](https://someone.github.io/tool)",
    ]
)
VIDEO_RANGES = [
    {
        "startingLine": 1,
        "endingLine": 1,
        "commit": {"committedDate": "2024-08-07T12:00:00Z"},
    },
    {
        "startingLine": 2,
        "endingLine": 2,
        "commit": {"committedDate": "2026-03-01T00:00:00Z"},
    },
    {
        "startingLine": 3,
        "endingLine": 3,
        "commit": {"committedDate": "2025-01-02T00:00:00Z"},
    },
    {
        "startingLine": 4,
        "endingLine": 5,
        "commit": {"committedDate": "2024-09-09T00:00:00Z"},
    },
]
NOTE_MD = "Note: use [Site B](https://site-b.net/) first"
NOTE_RANGES = [
    {
        "startingLine": 1,
        "endingLine": 1,
        "commit": {"committedDate": "2024-10-10T00:00:00Z"},
    }
]

TREE = {
    "tree": [
        {"path": "docs/video.md", "type": "blob", "sha": "blob-video-1", "size": 200},
        {"path": "docs/unsafe.md", "type": "blob", "sha": "blob-unsafe", "size": 10},
        {
            "path": "docs/posts/jan-2024.md",
            "type": "blob",
            "sha": "blob-post",
            "size": 10,
        },
        {
            "path": "docs/.vitepress/notes/site-b.md",
            "type": "blob",
            "sha": "blob-note",
            "size": 40,
        },
        {
            "path": "docs/.vitepress/config.mts",
            "type": "blob",
            "sha": "blob-cfg",
            "size": 10,
        },
        {"path": "README.md", "type": "blob", "sha": "blob-readme", "size": 10},
    ]
}
RAW = {"docs/video.md": VIDEO_MD, "docs/.vitepress/notes/site-b.md": NOTE_MD}
BLAME = {"docs/video.md": VIDEO_RANGES, "docs/.vitepress/notes/site-b.md": NOTE_RANGES}


def test_map_hosts_to_dates_takes_earliest_line_per_site() -> None:
    out = map_hosts_to_dates(VIDEO_MD, VIDEO_RANGES)
    assert (
        out["site-a.com"] == "2024-09-09T00:00:00Z"
    )  # sub.site-a.com line is older than the www line
    assert out["site-b.net"] == "2025-01-02T00:00:00Z"
    assert out["someone.github.io"] == "2024-09-09T00:00:00Z"
    assert "github.com" in out


class _Github:
    def __init__(
        self,
        *,
        fail_batches: bool = False,
        history: list[tuple[str, str, str, dict[str, str]]] | None = None,
        broken_raw: set[str] | None = None,
    ) -> None:
        self.requests: list[httpx.Request] = []
        self.graphql_calls = 0
        self.fail_batches = fail_batches
        # (repo, sha, committer date, {path: text}) per historical commit
        self.history = history or []
        self.broken_raw = broken_raw or set()

    def _history_response(self, url: httpx.URL) -> httpx.Response | None:
        parts = url.path.split("/")
        if len(parts) < 4 or parts[1] != "repos":
            return None
        repo = f"{parts[2]}/{parts[3]}"
        rest = "/".join(parts[4:])
        if rest == "commits" and "until" in url.params:
            until = url.params["until"]
            rows = sorted(
                (d, sha) for r, sha, d, _ in self.history if r == repo and d <= until
            )
            if not rows:
                return httpx.Response(200, json=[])
            d, sha = rows[-1]
            return httpx.Response(
                200, json=[{"sha": sha, "commit": {"committer": {"date": d}}}]
            )
        for r, sha, _d, files in self.history:
            if r != repo:
                continue
            if rest == f"git/trees/{sha}":
                if any(p.startswith("docs/") for p in files):
                    return httpx.Response(
                        200,
                        json={
                            "tree": [
                                {"path": "docs", "type": "tree", "sha": f"{sha}-docs"}
                            ]
                        },
                    )
                return httpx.Response(
                    200,
                    json={
                        "tree": [
                            {"path": p, "type": "blob", "sha": f"{sha}:{p}"}
                            for p in files
                        ]
                    },
                )
            if rest == f"git/trees/{sha}-docs":
                tree = [
                    {"path": p[len("docs/") :], "type": "blob", "sha": f"{sha}:{p}"}
                    for p in files
                    if p.startswith("docs/")
                ]
                return httpx.Response(200, json={"tree": tree})
        return None

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        url = request.url
        if url.host == "api.github.com" and url.path == "/repos/fmhy/edit/commits/main":
            return httpx.Response(
                200,
                json={
                    "sha": "c0ffee",
                    "commit": {
                        "tree": {"sha": "tree1"},
                        "committer": {"date": "2026-11-24T00:00:00Z"},
                    },
                },
            )
        if (
            url.host == "api.github.com"
            and url.path == "/repos/fmhy/edit/git/trees/tree1"
        ):
            return httpx.Response(200, json=TREE)
        if url.host == "api.github.com" and url.path == "/graphql":
            self.graphql_calls += 1
            body = json.loads(request.content)
            paths = [
                v for k, v in sorted(body["variables"].items()) if k.startswith("p")
            ]
            assert body["variables"]["oid"] == "c0ffee"
            if self.fail_batches and len(paths) > 1:
                return httpx.Response(502, text="timeout")
            obj = {f"f{i}": {"ranges": BLAME[p]} for i, p in enumerate(paths)}
            return httpx.Response(
                200,
                json={
                    "data": {
                        "rateLimit": {"cost": 1, "remaining": 4999},
                        "repository": {"object": obj},
                    }
                },
            )
        if url.host == "api.github.com":
            hist = self._history_response(url)
            if hist is not None:
                return hist
        if url.host == "raw.githubusercontent.com":
            if "/c0ffee/" in url.path:
                path = url.path.split("/c0ffee/", 1)[1]
                return httpx.Response(200, text=RAW[path])
            for r, sha, _d, files in self.history:
                prefix = f"/{r}/{sha}/"
                if url.path.startswith(prefix):
                    if sha in self.broken_raw:
                        return httpx.Response(500)
                    return httpx.Response(200, text=files[url.path[len(prefix) :]])
        return httpx.Response(404)


def test_fetch_listing_dates_blames_once_and_reuses_cache(monkeypatch) -> None:
    gh = _Github()
    cache = SignalCache(None, now=NOW)
    out = asyncio.run(
        fetch_listing_dates(
            github_token="t", cache=cache, now=NOW, transport=httpx.MockTransport(gh)
        )
    )
    assert out["site-a.com"] == datetime(2024, 9, 9, tzinfo=UTC)
    assert out["site-b.net"] == datetime(
        2024, 10, 10, tzinfo=UTC
    )  # note file is earlier than the page line
    assert gh.graphql_calls == 1  # both files aliased into one query
    fetched = {
        r.url.path for r in gh.requests if r.url.host == "raw.githubusercontent.com"
    }
    assert not any("unsafe" in p or "posts" in p or "README" in p for p in fetched)
    assert all(
        r.url.host in {"api.github.com", "raw.githubusercontent.com"}
        for r in gh.requests
    )

    gh2 = _Github()
    out2 = asyncio.run(
        fetch_listing_dates(
            github_token="t", cache=cache, now=NOW, transport=httpx.MockTransport(gh2)
        )
    )
    assert out2 == out
    assert gh2.graphql_calls == 0 and not any(
        r.url.host == "raw.githubusercontent.com" for r in gh2.requests
    )


def test_batch_failure_falls_back_to_single_file_queries(monkeypatch) -> None:
    async def no_sleep(_s: float) -> None:
        return None

    monkeypatch.setattr(listing_dates.asyncio, "sleep", no_sleep)
    gh = _Github(fail_batches=True)
    out = asyncio.run(
        fetch_listing_dates(
            github_token="t",
            cache=SignalCache(None, now=NOW),
            now=NOW,
            transport=httpx.MockTransport(gh),
        )
    )
    assert out["site-b.net"] == datetime(2024, 10, 10, tzinfo=UTC)
    assert gh.graphql_calls == listing_dates.MAX_ATTEMPTS + 2


def test_no_token_and_api_failure_never_raise() -> None:
    assert (
        asyncio.run(
            fetch_listing_dates(github_token=None, cache=SignalCache(None), now=NOW)
        )
        == {}
    )

    def down(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500)

    assert (
        asyncio.run(
            fetch_listing_dates(
                github_token="t",
                cache=SignalCache(None, now=NOW),
                now=NOW,
                transport=httpx.MockTransport(down),
            )
        )
        == {}
    )


HISTORY = [
    (
        "fmhy/archive",
        "old1",
        "2023-01-15T10:00:00Z",
        {
            "VideoPiracyGuide.md": "* [A](https://site-a.com/) / [Old](https://oldsite.com/)",
            "UnsafeSites.md": "* [B](https://site-b.net/) - malware",  # excluded: not a listing
            "README.md": "https://site-b.net/",
        },
    ),
    (
        "fmhy/edit",
        "mid1",
        "2025-03-10T10:00:00Z",
        {"docs/video.md": "* [A](https://www.site-a.com/)"},
    ),
    (
        "fmhy/edit",
        "new1",
        "2026-10-20T10:00:00Z",
        {
            "docs/video.md": "* [A](https://site-a.com/) * [B](https://site-b.net/)",
            "docs/posts/oct.md": "https://github.com/x",  # excluded
        },
    ),
]


def test_snapshot_dates_monthly_then_quarterly() -> None:
    dates = listing_dates.snapshot_dates(NOW)
    assert dates[0] == datetime(2026, 11, 1, tzinfo=UTC)
    assert dates[23] == datetime(2024, 12, 1, tzinfo=UTC)
    assert dates[24] == datetime(2024, 10, 1, tzinfo=UTC)
    assert all(d.month in (1, 4, 7, 10) for d in dates[24:])
    assert dates[-1] >= listing_dates.HISTORY_START


def test_compute_listed_since_restarts_after_gap_and_breaks_on_failure() -> None:
    d = [datetime(2022 + i, 1, 1, tzinfo=UTC) for i in range(4)]
    head = datetime(2026, 1, 1, tzinfo=UTC)
    snaps = [(d[0], {"a", "b"}), (d[1], {"a"}), (d[2], {"a", "b"}), (d[3], {"a", "b"})]
    blame = {"a": d[3], "b": d[3], "c": datetime(2025, 12, 1, tzinfo=UTC)}
    out = listing_dates.compute_listed_since(snaps, {"a", "b", "c"}, head, blame)
    assert out == {"a": d[0], "b": d[2], "c": datetime(2025, 12, 1, tzinfo=UTC)}
    failed = [(d[0], {"a"}), (d[1], None), (d[2], {"a"}), (d[3], {"a"})]
    assert listing_dates.compute_listed_since(failed, {"a"}, head, blame)["a"] == d[2]


def test_history_snapshots_across_repos_layouts_and_cache() -> None:
    gh = _Github(history=HISTORY)
    cache = SignalCache(None, now=NOW)
    out = asyncio.run(
        fetch_listing_dates(
            github_token="t", cache=cache, now=NOW, transport=httpx.MockTransport(gh)
        )
    )
    assert out["site-a.com"] == datetime(
        2023, 1, 15, 10, tzinfo=UTC
    )  # continuous since the archive repo
    assert out["site-b.net"] == datetime(
        2026, 10, 20, 10, tzinfo=UTC
    )  # absent in 2025-03; unsafe list ignored
    assert out["github.com"] == datetime(
        2025, 1, 2, tzinfo=UTC
    )  # head-only host: blame fallback
    assert "oldsite.com" not in out
    archive_calls = [r for r in gh.requests if "/fmhy/archive/" in r.url.path]
    assert archive_calls and all(
        "until" not in r.url.params or r.url.params["until"] < "2024-08-07T12:23:35Z"
        for r in archive_calls
    )
    assert not any("posts" in r.url.path or "README" in r.url.path for r in gh.requests)

    gh2 = _Github(history=HISTORY)
    out2 = asyncio.run(
        fetch_listing_dates(
            github_token="t", cache=cache, now=NOW, transport=httpx.MockTransport(gh2)
        )
    )
    assert out2 == out
    # Past calendar points and their snapshots are cached forever; only the current month's point is re-resolved.
    assert all(r.url.host == "api.github.com" for r in gh2.requests)
    assert not any(
        "/git/trees/old1" in r.url.path or "/git/trees/mid1" in r.url.path
        for r in gh2.requests
    )
    assert sum(1 for r in gh2.requests if "until" in r.url.params) <= 1


def test_failed_snapshot_breaks_runs_and_is_not_cached(monkeypatch) -> None:
    async def no_sleep(_s: float) -> None:
        return None

    monkeypatch.setattr(listing_dates.asyncio, "sleep", no_sleep)
    cache = SignalCache(None, now=NOW)
    gh = _Github(history=HISTORY, broken_raw={"mid1"})
    out = asyncio.run(
        fetch_listing_dates(
            github_token="t", cache=cache, now=NOW, transport=httpx.MockTransport(gh)
        )
    )
    assert out["site-a.com"] == datetime(
        2026, 10, 20, 10, tzinfo=UTC
    )  # later = conservative
    assert cache.get_any_age(listing_dates.SNAP_NS, "fmhy/edit@mid1") is None
    assert cache.get_any_age(listing_dates.SNAP_NS, "fmhy/archive@old1") is not None
