import asyncio
import json
from collections import Counter
from dataclasses import replace
from datetime import UTC, datetime
from types import SimpleNamespace

import httpx
import pytest

from compdesign_bot import pipeline
from compdesign_bot.config import Settings
from compdesign_bot.formatting import render_post
from compdesign_bot.models import Article, ArticleLink, RankedArticle, Summary
from compdesign_bot.storage import Store
from compdesign_bot.telegram import DeliveryUncertain, Telegram, TelegramError, message_payload


async def no_sleep(_seconds):
    pass


def test_source_button_uses_original_url_and_preserves_html_links():
    url = "https://example.com/article?a=1&b=2#details"
    article = replace(make_article(), url=url, links=(ArticleLink("코드", "https://github.com/a/b"),))
    post = render_post(RankedArticle(article, 1, 10, "디자인"), Summary("제목", ("핵심",), "의미", "근거"))
    calls = []

    def handler(request):
        calls.append(json.loads(request.content))
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 123}})

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await Telegram(client, "SECRET", "@channel").send(post)

    assert asyncio.run(run()) == 123
    payload = calls[0]
    assert payload["text"] == post
    assert payload["parse_mode"] == "HTML"
    assert payload["reply_markup"] == {"inline_keyboard": [[{"text": "원문 보기", "url": url}]]}


@pytest.mark.parametrize("text", [
    "안녕하세요",
    '<a href="https://example.com">다른 링크</a>',
    '&lt;a href="https://example.com"&gt;원문 보기&lt;/a&gt;',
    '<a href="javascript:alert(1)">원문 보기</a>',
    '<a href="https://user:pass@example.com">원문 보기</a>',
    '<a href="https://[invalid">원문 보기</a>',
])
def test_non_source_messages_have_no_source_button(text):
    assert "reply_markup" not in message_payload(42, text)


def test_rate_limit_retries_only_after_definite_rejection(monkeypatch):
    calls, delays = [], []

    async def sleep(delay):
        delays.append(delay)

    def handler(request):
        calls.append(json.loads(request.content))
        if len(calls) == 1:
            return httpx.Response(
                429, json={"ok": False, "error_code": 429, "parameters": {"retry_after": 2}}
            )
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 123}})

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await Telegram(client, "SECRET", "@channel").send("안녕하세요")

    monkeypatch.setattr(asyncio, "sleep", sleep)
    assert asyncio.run(run()) == 123
    assert calls[0] == calls[1]
    assert delays == [2]


@pytest.mark.parametrize("failure", ["timeout", "server_error", "missing_message_id", "missing_ok"])
def test_uncertain_delivery_is_never_automatically_retried(failure):
    calls = []

    def handler(request):
        calls.append(request)
        if failure == "timeout":
            raise httpx.ReadTimeout("secret request URL must not leak", request=request)
        if failure == "server_error":
            return httpx.Response(503, text="Unavailable")
        if failure == "missing_ok":
            return httpx.Response(200, json={"result": {"message_id": 123}})
        return httpx.Response(200, json={"ok": True, "result": {}})

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            await Telegram(client, "SECRET", "@channel").send("한국어 글")

    with pytest.raises(DeliveryUncertain) as error:
        asyncio.run(run())
    assert len(calls) == 1
    assert "SECRET" not in str(error.value)


def make_article(index=1):
    return Article(
        f"Web3 generative art research {index}",
        f"https://example.com/{index}",
        "Example",
        "The open-source tool generates on-chain generative art by combining seeded geometry "
        "with procedural shaders that render reproducible compositions in the browser.",
        datetime.now(UTC),
    )


def test_sent_pending_and_uncertain_records_survive_restart(tmp_path):
    path = tmp_path / "bot.sqlite3"
    store = Store(path)
    first, second, third = [make_article(index) for index in range(1, 4)]
    sent_id = store.reserve(first, "-1001")
    store.sent(sent_id, 55)
    store.release(sent_id)
    uncertain_id = store.reserve(second, "-1001")
    store.uncertain(uncertain_id)
    store.reserve(third, "-1001")
    store.close()
    reopened = Store(path)
    try:
        assert all(reopened.seen(item, "-1001") for item in (first, second, third))
        assert not reopened.seen(first, "-1002")
        assert reopened.status_counts() == {"sent": 1, "uncertain": 1, "pending": 1}
        reopened.release(uncertain_id)
        assert not reopened.seen(second, "-1001")
    finally:
        reopened.close()


def test_canonical_url_prevents_repost_after_headline_changes(tmp_path):
    store = Store(tmp_path / "bot.sqlite3")
    old = Article(
        "Generative art original title",
        "http://www.example.com/article/?utm_source=rss",
        "Example",
        "",
        datetime.now(UTC),
    )
    updated = Article(
        "Updated computational design headline",
        "https://example.com/article#top",
        "Example",
        "",
        datetime.now(UTC),
    )
    try:
        store.sent(store.reserve(old, "channel"), 44)
        assert store.seen(updated, "channel")
    finally:
        store.close()


def configure_pipeline(monkeypatch, tmp_path, send_handler, *, max_posts=5):
    real_client = httpx.AsyncClient

    def handler(request):
        method = request.url.path.rsplit("/", 1)[-1]
        payload = json.loads(request.content)
        results = {
            "getMe": {"id": 10, "username": "test_bot"},
            "getChat": {"id": -100123, "title": "테스트", "type": "channel"},
            "getChatMember": {"status": "administrator", "can_post_messages": True},
        }
        if method in results:
            return httpx.Response(200, json={"ok": True, "result": results[method]})
        if method == "sendMessage":
            return send_handler(request, payload)
        raise AssertionError(f"Unexpected external service: {request.url.host}")

    class LocalSummary:
        def __init__(self, **kwargs):
            pass

        async def summarize(self, item):
            index = item.article.title.rsplit(" ", 1)[-1]
            return Summary(
                f"생성 예술 연구 {index}",
                ("디자인 도구를 공개했습니다.",),
                "코딩으로 작품을 구현합니다.",
                "발췌·기계번역",
            )

    async def collect(_sources, _client):
        return SimpleNamespace(articles=[make_article(index) for index in range(1, 4)], errors=[])

    monkeypatch.setattr(
        pipeline.httpx,
        "AsyncClient",
        lambda **kwargs: real_client(transport=httpx.MockTransport(handler), **kwargs),
    )
    monkeypatch.setattr(pipeline, "collect_articles", collect)
    monkeypatch.setattr(pipeline, "load_sources", lambda _path: [])
    monkeypatch.setattr(pipeline, "LocalSummarizer", LocalSummary)
    monkeypatch.setattr(asyncio, "sleep", no_sleep)
    return Settings(
        bot_token="SECRET",
        channel_id="@test",
        database_path=tmp_path / "pipeline.sqlite3",
        max_posts=max_posts,
    )


def test_partial_failure_retry_does_not_repeat_successful_posts(monkeypatch, tmp_path):
    attempts, delivered = [], []
    fail = True

    def send(_request, payload):
        attempts.append(payload["text"])
        if fail and len(attempts) == 2:
            return httpx.Response(400, json={"ok": False, "error_code": 400})
        delivered.append(payload["text"])
        return httpx.Response(200, json={"ok": True, "result": {"message_id": len(delivered)}})

    settings = configure_pipeline(monkeypatch, tmp_path, send)
    with pytest.raises(TelegramError):
        asyncio.run(pipeline.run_digest(settings, publish=True))
    fail = False
    asyncio.run(pipeline.run_digest(settings, publish=True))
    assert len(delivered) == 3
    assert set(Counter(delivered).values()) == {1}
    assert attempts.count(delivered[0]) == 1


@pytest.mark.parametrize("slot", [None, "2026-09-07T18:00:00+09:00"])
def test_uncertain_post_is_skipped_on_next_run(monkeypatch, tmp_path, slot):
    attempts = []

    def send(request, payload):
        attempts.append(payload["text"])
        if len(attempts) == 1:
            raise httpx.ReadTimeout("response lost", request=request)
        return httpx.Response(200, json={"ok": True, "result": {"message_id": len(attempts)}})

    settings = configure_pipeline(monkeypatch, tmp_path, send, max_posts=2 if slot else 5)
    with pytest.raises(RuntimeError, match="발행 결과 불명"):
        asyncio.run(pipeline.run_digest(settings, publish=True, slot=slot))
    asyncio.run(pipeline.run_digest(settings, publish=True, slot=slot))
    assert len(attempts) == (2 if slot else 3)
    assert set(Counter(attempts).values()) == {1}
    store = Store(settings.database_path)
    try:
        assert store.status_counts() == {"sent": 1 if slot else 2, "uncertain": 1}
    finally:
        store.close()


def test_partial_failure_cannot_exceed_scheduled_slot_budget(monkeypatch, tmp_path):
    attempts, delivered = [], []

    def send(_request, payload):
        attempts.append(payload["text"])
        if len(attempts) == 2:
            return httpx.Response(400, json={"ok": False, "error_code": 400})
        delivered.append(payload["text"])
        return httpx.Response(200, json={"ok": True, "result": {"message_id": len(delivered)}})

    settings = configure_pipeline(monkeypatch, tmp_path, send, max_posts=2)
    slot = "2026-09-07T18:00:00+09:00"
    with pytest.raises(TelegramError):
        asyncio.run(pipeline.run_digest(settings, publish=True, slot=slot))
    asyncio.run(pipeline.run_digest(settings, publish=True, slot=slot))
    assert len(delivered) <= settings.max_posts
    assert set(Counter(delivered).values()) == {1}


def test_preview_does_not_send_or_mark_articles_and_reuses_summaries(monkeypatch, tmp_path):
    delivered = []

    def send(_request, payload):
        delivered.append(payload["text"])
        return httpx.Response(200, json={"ok": True, "result": {"message_id": len(delivered)}})

    settings = configure_pipeline(monkeypatch, tmp_path, send)
    preview = asyncio.run(pipeline.run_digest(settings))
    assert len(preview.messages) == 3
    assert delivered == []
    store = Store(settings.database_path)
    assert store.status_counts() == {}
    store.close()
    published = asyncio.run(pipeline.run_digest(settings, publish=True))
    assert published.posted == 3
    after = asyncio.run(pipeline.run_digest(settings))
    assert after.messages == []
    assert len(delivered) == 3


def test_only_articles_with_substantive_source_evidence_are_translated_and_sent(monkeypatch, tmp_path):
    translated, delivered = [], []
    articles = [
        replace(make_article(1), summary=""),
        replace(
            make_article(2),
            summary="An exciting generative art announcement for artists and developers. "
            "More details about this creative project will be available soon.",
        ),
        make_article(3),
    ]

    def send(_request, payload):
        delivered.append(payload["text"])
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 456}})

    async def collect(_sources, _client):
        return SimpleNamespace(articles=articles, errors=[])

    class RecordingSummary:
        def __init__(self, **_kwargs):
            pass

        async def summarize(self, item):
            translated.append(item.article.url)
            return Summary(
                "재현 가능한 온체인 생성예술 도구",
                ("시드 기반 기하와 프로시저럴 셰이더를 사용합니다.",),
                "브라우저에서 생성예술을 구현하는 예제입니다.",
                "본문 발췌·기계번역",
            )

    settings = configure_pipeline(monkeypatch, tmp_path, send)
    monkeypatch.setattr(pipeline, "collect_articles", collect)
    monkeypatch.setattr(pipeline, "LocalSummarizer", RecordingSummary)

    result = asyncio.run(pipeline.run_digest(settings, publish=True))

    assert result.quality_rejected == 2
    assert result.posted == 1
    assert translated == [articles[2].url]
    assert delivered == result.messages
    assert articles[2].url in delivered[0]
    store = Store(settings.database_path)
    try:
        assert store.status_counts() == {"sent": 1}
        assert not store.seen(articles[0], "-100123")
        assert not store.seen(articles[1], "-100123")
        assert store.seen(articles[2], "-100123")
    finally:
        store.close()
