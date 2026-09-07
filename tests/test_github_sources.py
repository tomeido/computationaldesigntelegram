import asyncio
import fcntl
import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from compdesign_bot import github_sources
from compdesign_bot.github_sources import Repository, collect_releases, load_repositories

NOW = datetime(2026, 9, 7, 12, tzinfo=UTC)
REPO = Repository(
    "processing", "p5.js", "브라우저 생성예술 실험에 유용합니다.",
    "creative coding generative art", "https://p5js.org/reference/", "https://p5js.org/examples/",
)


def metadata(repo=REPO, **kwargs):
    return {"full_name": repo.full_name, "archived": False, "disabled": False, "private": False,
            "license": {"spdx_id": "LGPL-2.1"}, **kwargs}


def release(repo=REPO, **kwargs):
    return {
        "name": "v2.0.1", "tag_name": "v2.0.1", "draft": False, "prerelease": False,
        "html_url": f"{repo.url}/releases/tag/v2.0.1",
        "published_at": "2026-09-06T09:00:00Z", "created_at": "2020-01-01T00:00:00Z",
        "body": "## New features\n* Add shader support for rendering animated generative artwork.\n"
                "* Improve geometry export to preserve vertex colors by @author in https://github.com/x/y/pull/7\n"
                "**Full Changelog**: https://github.com/x/y/compare/v1...v2",
        **kwargs,
    }


def collect(tmp_path, handler, repositories=None, now=NOW):
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=True) as client:
            return await collect_releases(
                repositories if repositories is not None else [REPO], client,
                tmp_path / "github.json", now=now,
            )
    return asyncio.run(run())


def test_catalog_has_five_curated_official_projects():
    from pathlib import Path

    repositories = load_repositories(Path("config/repositories.json"))
    assert {repo.full_name for repo in repositories} == {
        "ArtBlocks/artblocks-contracts", "fxhash/onchfs", "compas-dev/compas",
        "processing/p5.js", "NVIDIA/warp",
    }
    assert all(repo.why and repo.topic and repo.docs_url for repo in repositories)


@pytest.mark.parametrize("field,value", [
    ("owner", "../evil"), ("owner", "foo?token=x"), ("name", "../evil"),
    ("name", "foo/bar"), ("name", "."), ("enabled", "false"),
    ("why", ""), ("topic", "x" * 401),
    ("docs_url", "https://user:secret@example.com/docs"),
    ("docs_url", "javascript:alert(1)"),
    ("example_url", "https://github.com/attacker/fake/tree/main/examples"),
])
def test_catalog_rejects_invalid_configuration(tmp_path, field, value):
    path = tmp_path / "repositories.json"
    path.write_text(json.dumps([{"owner": "processing", "name": "p5.js", "why": "useful", "topic": "art",
                                 field: value}]))
    with pytest.raises(ValueError):
        load_repositories(path)


def test_duplicate_and_oversized_catalog_rejected(tmp_path):
    path = tmp_path / "repositories.json"
    row = {"owner": "processing", "name": "p5.js", "why": "useful", "topic": "art"}
    path.write_text(json.dumps([row, {**row, "owner": "PROCESSING"}]))
    with pytest.raises(ValueError, match="duplicate"):
        load_repositories(path)
    path.write_text(" " * (github_sources.MAX_CONFIG_BYTES + 1))
    with pytest.raises(ValueError, match="large"):
        load_repositories(path)


def test_official_release_uses_actual_date_changelog_license_and_curated_links(tmp_path):
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(200, json=[release()] if request.url.path.endswith("/releases") else metadata())

    report = collect(tmp_path, handler)
    assert not report.errors
    article, = report.articles
    assert article.kind == "release"
    assert article.title == "processing/p5.js release: v2.0.1"
    assert article.published_at == datetime(2026, 9, 6, 9, tzinfo=UTC)
    assert article.license == "LGPL-2.1"
    assert article.curator_note == REPO.why
    assert article.topic_context == REPO.topic
    assert article.summary.startswith("Add shader support")
    assert "generative" in article.summary
    assert REPO.why not in article.summary and REPO.topic not in article.summary
    assert "Full Changelog" not in article.summary and "@author" not in article.summary
    assert [link.url for link in article.links] == [REPO.url, REPO.docs_url, REPO.example_url]
    assert len(requests) == 2
    assert all(request.url.host == "api.github.com" and request.method == "GET" for request in requests)
    assert all("authorization" not in request.headers for request in requests)
    assert str(requests[1].url).endswith("/releases?per_page=5")


@pytest.mark.parametrize("field", ["archived", "disabled", "private"])
def test_inactive_repositories_are_skipped_without_release_fetch(tmp_path, field):
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(200, json=metadata(**{field: True}))

    report = collect(tmp_path, handler)
    assert not report.articles and not report.errors
    assert len(requests) == 1


def test_disabled_catalog_does_not_make_requests_or_create_cache(tmp_path):
    report = collect(tmp_path, lambda request: pytest.fail("must not request"), [replace(REPO, enabled=False)])
    assert not report.articles and not report.errors
    assert not (tmp_path / "github.json").exists()


@pytest.mark.parametrize("changes", [
    {"draft": True}, {"prerelease": True}, {"published_at": None},
    {"name": "v1.11.14-rc.2", "prerelease": False},
    {"name": "New release", "tag_name": "v1.11.14-rc.2", "prerelease": False},
    {"published_at": "2026-09-07T12:00:00"}, {"published_at": "not a date"},
    {"published_at": "0001-01-01T00:00:00+01:00"},
    {"html_url": "https://github.com/other/repo/releases/tag/v2"},
    {"html_url": "https://evil.example/releases/tag/v2"},
    {"html_url": "https://github.com/processing/p5.js/releases/tag/"},
])
def test_unpublished_drafts_prereleases_and_untrusted_release_links_are_skipped(tmp_path, changes):
    report = collect(tmp_path, lambda request: httpx.Response(
        200, json=[release(**changes)] if request.url.path.endswith("/releases") else metadata(),
    ))
    assert not report.articles


def test_old_release_is_never_freshened_by_repository_activity(tmp_path):
    report = collect(tmp_path, lambda request: httpx.Response(
        200, json=[release(published_at="2020-01-01T00:00:00Z", created_at=NOW.isoformat())]
        if request.url.path.endswith("/releases") else metadata(pushed_at=NOW.isoformat()),
    ))
    assert report.articles[0].published_at.year == 2020


def test_code_identifiers_survive_markdown_cleanup_and_cache(tmp_path):
    def handler(request):
        return httpx.Response(200, json=[release(body="* Added `load_model()` to **load geometric models**.")]
                              if request.url.path.endswith("/releases") else metadata())

    first = collect(tmp_path, handler)
    cached = collect(tmp_path, handler, now=NOW + timedelta(hours=1))
    assert first.articles == cached.articles
    assert "load_model()" in cached.articles[0].summary


def test_changelog_omits_leading_commit_hash_but_preserves_versions_and_identifiers():
    text = github_sources._changelog(
        "* 4b5167e: Core v3.3: added per-project transfer hooks.\n"
        "* 0123456789abcdef0123456789abcdef01234567: Added `load_model()` support.\n"
        "* v1.4.0: Improved geometry export.\n"
        "* 123456: Preserved project identifier.\n"
        "* Added hash 4b5167e in provenance records."
    )
    assert text.startswith("Core v3.3: added per-project transfer hooks.")
    assert "0123456789abcdef0123456789abcdef01234567" not in text
    assert "load_model()" in text and "v1.4.0:" in text and "123456:" in text
    assert "hash 4b5167e" in text


@pytest.mark.parametrize("license_data", [None, {"spdx_id": "NOASSERTION"}, {"spdx_id": None}])
def test_unknown_license_not_claimed_as_open_source(tmp_path, license_data):
    report = collect(tmp_path, lambda request: httpx.Response(
        200, json=[release()] if request.url.path.endswith("/releases") else metadata(license=license_data),
    ))
    assert report.articles[0].license == ""


def test_success_cache_survives_client_restart_and_expires_after_six_hours(tmp_path):
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(200, json=[release()] if request.url.path.endswith("/releases") else metadata())

    first = collect(tmp_path, handler)
    cached = collect(tmp_path, handler, now=NOW + timedelta(hours=5))
    assert cached.articles == first.articles
    assert len(requests) == 2
    refreshed = collect(tmp_path, handler, now=NOW + timedelta(hours=6))
    assert refreshed.articles == first.articles
    assert len(requests) == 4


def test_expired_cache_is_not_served_as_fresh_when_fetch_fails(tmp_path):
    collect(tmp_path, lambda request: httpx.Response(
        200, json=[release()] if request.url.path.endswith("/releases") else metadata(),
    ))
    report = collect(tmp_path, lambda request: httpx.Response(503), now=NOW + timedelta(hours=6))
    assert report.errors and not report.articles


def test_rate_limit_cooldown_survives_restart_and_error_body_is_not_logged(tmp_path):
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(429, text="do not log this confidential body")

    report = collect(tmp_path, handler)
    assert "429" in report.errors[0] and "confidential" not in report.errors[0]
    collect(tmp_path, handler, now=NOW + timedelta(minutes=59))
    assert len(requests) == 1
    collect(tmp_path, handler, now=NOW + timedelta(hours=1))
    assert len(requests) == 2


def test_one_bad_repository_does_not_block_valid_source(tmp_path):
    bad = replace(REPO, owner="broken")

    def handler(request):
        if "/broken/" in request.url.path:
            return httpx.Response(200, json={"full_name": 42})
        return httpx.Response(200, json=[release()] if request.url.path.endswith("/releases") else metadata())

    report = collect(tmp_path, handler, [bad, REPO])
    assert len(report.articles) == 1 and len(report.errors) == 1


def test_redirects_not_followed_even_if_client_enables_them(tmp_path):
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(302, headers={"Location": "https://unexpected.example/"})

    report = collect(tmp_path, handler)
    assert report.errors and not report.articles
    assert len(requests) == 1 and requests[0].url.host == "api.github.com"


@pytest.mark.parametrize("response", [
    httpx.Response(200, content=b"not JSON"),
    httpx.Response(200, content=b" " * (github_sources.MAX_RESPONSE_BYTES + 1)),
])
def test_invalid_or_oversized_responses_are_isolated(tmp_path, response):
    report = collect(tmp_path, lambda request: response)
    assert report.errors and not report.articles


def test_corrupt_cache_recovers_and_prunes_unconfigured_sources(tmp_path):
    cache = tmp_path / "github.json"
    cache.write_text("invalid JSON")
    report = collect(tmp_path, lambda request: httpx.Response(
        200, json=[release()] if request.url.path.endswith("/releases") else metadata(),
    ))
    assert len(report.articles) == 1
    data = json.loads(cache.read_text())
    assert set(data["entries"]) == {"processing/p5.js"}
    assert not list(tmp_path.glob(".github.json.*"))


def test_concurrent_collector_does_not_duplicate_requests(tmp_path):
    lock_path = tmp_path / "github.json.lock"
    with lock_path.open("w") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        report = collect(tmp_path, lambda request: pytest.fail("must not request while locked"))
    assert report.errors and not report.articles


def test_network_error_does_not_leak_request_url_or_body(tmp_path):
    def handler(request):
        raise httpx.ConnectError("secret-password-query", request=request)

    report = collect(tmp_path, handler)
    assert "ConnectError" in report.errors[0] and "secret" not in report.errors[0]


def test_slow_stream_has_total_deadline(tmp_path, monkeypatch):
    class SlowBody(httpx.AsyncByteStream):
        async def __aiter__(self):
            while True:
                await asyncio.sleep(0.005)
                yield b" "

    monkeypatch.setattr(github_sources, "REQUEST_TIMEOUT_SECONDS", 0.02)
    report = collect(tmp_path, lambda request: httpx.Response(200, stream=SlowBody()))
    assert not report.articles and "timed out" in report.errors[0]


def test_network_concurrency_is_bounded_to_two(tmp_path):
    active = 0
    peak = 0
    repositories = [replace(REPO, owner=f"owner{i}") for i in range(5)]
    lookup = {repo.owner: repo for repo in repositories}

    async def handler(request):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.002)
        active -= 1
        repo = lookup[request.url.path.split("/")[2]]
        return httpx.Response(200, json=[release(repo)] if request.url.path.endswith("/releases")
                              else metadata(repo))

    report = collect(tmp_path, handler, repositories)
    assert not report.errors and len(report.articles) == 5
    assert peak == 2
