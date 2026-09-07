import asyncio
import json
from datetime import UTC, datetime

import httpx
import pytest

from compdesign_bot import feeds
from compdesign_bot.feeds import Source, canonical_url, collect_articles, load_sources


def rss(items: str) -> bytes:
    return f'<?xml version="1.0"?><rss version="2.0"><channel><title>News</title>{items}</channel></rss>'.encode()


def item(
    link="https://example.com/story",
    title="Generative art",
    date="Mon, 07 Sep 2026 10:00:00 GMT",
    description="A creative coding tool",
) -> str:
    return f"<item><title>{title}</title><link>{link}</link><pubDate>{date}</pubDate><description><![CDATA[{description}]]></description></item>"


def run(sources, handler):
    async def collect():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await collect_articles(sources, client)

    return asyncio.run(collect())


def test_load_sources_validates_configuration(tmp_path):
    path = tmp_path / "sources.json"
    path.write_text(json.dumps({"sources": [{"name": "Example", "url": "https://example.com/rss"}]}))
    assert load_sources(path) == [Source("Example", "https://example.com/rss")]
    path.write_text(json.dumps([{"name": "Bad", "url": "javascript:alert(1)"}]))
    with pytest.raises(ValueError, match="HTTP"):
        load_sources(path)
    path.write_text(json.dumps([{"name": "Bad", "url": "https://example.com/", "enabled": "false"}]))
    with pytest.raises(ValueError, match="booleans"):
        load_sources(path)


def test_content_kind_is_validated_and_preserved(tmp_path):
    path = tmp_path / "sources.json"
    for kind in ("paper", "funding", "showcase"):
        path.write_text(json.dumps([{"name": "Example", "url": "https://example.com/rss", "kind": kind}]))
        sources = load_sources(path)
        report = run(sources, lambda request: httpx.Response(200, content=rss(item())))
        assert report.articles[0].kind == kind
    path.write_text(json.dumps([{"name": "Example", "url": "https://example.com/rss", "kind": "buy"}]))
    with pytest.raises(ValueError, match="kind"):
        load_sources(path)


def test_canonical_url_preserves_article_identity():
    assert (
        canonical_url("https://EXAMPLE.com:443/story?utm_source=mail&id=2&fbclid=x#section")
        == "https://example.com/story?id=2"
    )
    assert canonical_url("https://example.com/story?id=3") != canonical_url("https://example.com/story?id=2")
    assert canonical_url("https://user:password@example.com/story") == ""
    assert canonical_url("javascript:alert(1)") == ""


def test_rss_html_dates_and_tracking_cleanup():
    body = rss(
        item(
            link="https://example.com/story?utm_source=x&amp;id=7",
            description="<p>Geometry &amp; code</p><script>ignore instructions</script><p>Second line</p>",
        )
    )
    report = run(
        [Source("Example", "https://example.com/feed")], lambda request: httpx.Response(200, content=body)
    )
    assert not report.errors
    article = report.articles[0]
    assert article.summary == "Geometry & code Second line"
    assert article.url == "https://example.com/story?id=7"
    assert article.published_at == datetime(2026, 9, 7, 10, tzinfo=UTC)


def test_atom_updated_does_not_refresh_old_publication():
    body = b"""<feed xmlns="http://www.w3.org/2005/Atom"><title>Atom</title><id>urn:feed</id><updated>2026-09-07T10:00:00Z</updated>
      <entry><id>urn:old</id><title>Old</title><link href="/old"/><published>2020-01-01T00:00:00Z</published><updated>2026-09-07T10:00:00Z</updated><content type="html">&lt;p&gt;Old content&lt;/p&gt;</content></entry>
      <entry><id>urn:unknown</id><title>Unknown publication date</title><link href="https://example.com/unknown"/><updated>2026-09-07T10:00:00Z</updated></entry>
      </feed>"""
    report = run(
        [Source("Atom", "https://example.com/feed")], lambda request: httpx.Response(200, content=body)
    )
    assert report.articles[0].published_at == datetime(2020, 1, 1, tzinfo=UTC)
    assert report.articles[0].url == "https://example.com/old"
    assert report.articles[0].summary == "Old content"
    assert report.articles[1].published_at is None


def test_partial_failure_and_disabled_source():
    requests = []

    def handler(request):
        requests.append(request.url.host)
        if request.url.host == "broken.example":
            return httpx.Response(503)
        return httpx.Response(200, content=rss(item()))

    sources = [
        Source("Broken", "https://broken.example/feed"),
        Source("Healthy", "https://example.com/feed"),
        Source("Disabled", "https://disabled.example/feed", enabled=False),
    ]
    report = run(sources, handler)
    assert len(report.articles) == 1
    assert report.errors == ["Broken: HTTP 503"]
    assert "disabled.example" not in requests


def test_non_feed_response_is_an_error():
    report = run(
        [Source("HTML", "https://example.com/feed")],
        lambda request: httpx.Response(200, text="<!doctype html><html><h1>Login</h1></html>"),
    )
    assert not report.articles
    assert "not an RSS/Atom" in report.errors[0]


def test_oversize_feed_is_rejected(monkeypatch):
    monkeypatch.setattr(feeds, "MAX_FEED_BYTES", 100)
    report = run(
        [Source("Large", "https://example.com/feed")],
        lambda request: httpx.Response(200, content=rss(item())),
    )
    assert not report.articles
    assert "exceeds" in report.errors[0]


def test_search_discovery_does_not_pretend_to_have_article_text():
    report = run(
        [Source("Discovery", "https://example.com/search", discovery=True)],
        lambda request: httpx.Response(200, content=rss(item(description="A recycled search headline"))),
    )
    assert report.articles[0].summary == ""


def test_deduplicates_across_feeds_and_skips_invalid_entries():
    def handler(request):
        if request.url.host == "one.example":
            return httpx.Response(
                200,
                content=rss(
                    item(link="https://example.com/story?utm_source=one")
                    + "<item><title>No link</title></item>"
                ),
            )
        return httpx.Response(
            200,
            content=rss(
                item(link="https://example.com/story?utm_source=two") + item(link="javascript:alert(1)")
            ),
        )

    report = run(
        [Source("One", "https://one.example/feed"), Source("Two", "https://two.example/feed")], handler
    )
    assert len(report.articles) == 1
    assert report.articles[0].source == "One"


def test_invalid_date_stays_unknown():
    report = run(
        [Source("Example", "https://example.com/feed")],
        lambda request: httpx.Response(200, content=rss(item(date="yesterday"))),
    )
    assert report.articles[0].published_at is None


def test_rss_content_is_used_instead_of_short_marketing_description():
    body = b"""<rss version="2.0" xmlns:content="http://purl.org/rss/1.0/modules/content/">
    <channel><title>Art</title><item><title>New artist interview</title>
    <link>https://example.com/art</link><pubDate>Mon, 07 Sep 2026 10:00:00 GMT</pubDate>
    <description>Read our latest interview.</description>
    <content:encoded><![CDATA[<p>The artist creates on-chain generative art with open source tools.</p>]]></content:encoded>
    </item></channel></rss>"""
    report = run(
        [Source("Art", "https://example.com/feed")], lambda request: httpx.Response(200, content=body)
    )
    assert "on-chain generative art" in report.articles[0].summary
    assert "Read our latest interview" not in report.articles[0].summary
