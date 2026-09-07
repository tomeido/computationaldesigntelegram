import asyncio
import json
from datetime import UTC, datetime
from html.parser import HTMLParser

import httpx
import pytest

from compdesign_bot.formatting import render_post
from compdesign_bot.models import Article, RankedArticle, Summary
from compdesign_bot.summarizer import Summarizer, SummaryError


def item(*, excerpt="Research excerpt.", url="https://example.com/article"):
    return RankedArticle(
        Article(
            "Computational design research", url, "Example", excerpt, datetime(2026, 9, 7, 16, tzinfo=UTC)
        ),
        1,
        10,
        "Web3 × 디자인",
    )


def completed(data):
    return {
        "status": "completed",
        "output": [
            {
                "type": "message",
                "content": [{"type": "output_text", "text": json.dumps(data, ensure_ascii=False)}],
            }
        ],
    }


def summarize(payload, *, excerpt="Research excerpt."):
    async def run():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(lambda _request: httpx.Response(200, json=payload))
        ) as client:
            return await Summarizer(client, "SECRET", "test-model").summarize(item(excerpt=excerpt))

    return asyncio.run(run())


def test_korean_summary_is_bounded_and_title_only_evidence_is_explicit():
    summary = summarize(
        completed(
            {
                "relevant": True,
                "title": "한국어 제목 " * 20,
                "bullets": ["연구 내용 " * 50],
                "why": "적용 방법 " * 30,
            }
        ),
        excerpt="",
    )
    assert len(summary.title) <= 65
    assert len(summary.bullets[0]) <= 120
    assert len(summary.why) <= 100
    assert "제목만 확인" in summary.evidence
    assert "RSS" not in summary.evidence


def test_irrelevant_content_is_not_published():
    assert (
        summarize(
            completed(
                {
                    "relevant": False,
                    "title": "관련 없음",
                    "bullets": ["관련 없는 내용입니다."],
                    "why": "대상이 아닙니다.",
                }
            )
        )
        is None
    )


@pytest.mark.parametrize(
    "payload",
    [
        [],
        {"status": "completed", "output": [None]},
        {"status": "completed", "output": [{"type": "message", "content": [None]}]},
        {"status": "incomplete", "output": []},
        {
            "status": "completed",
            "output": [
                {
                    "type": "message",
                    "content": [{"type": "refusal", "refusal": "Cannot summarize this material"}],
                }
            ],
        },
        completed(
            {"relevant": True, "title": "English only", "bullets": ["한국어 내용"], "why": "한국어 의미"}
        ),
        completed({"relevant": "true", "title": "한국어", "bullets": ["한국어"], "why": "한국어"}),
        completed({"relevant": True, "title": "한국어", "bullets": [], "why": "한국어"}),
    ],
)
def test_refusal_and_malformed_responses_fail_closed(payload):
    with pytest.raises(SummaryError):
        summarize(payload)


def test_telegram_html_escapes_all_article_and_summary_content():
    class Tags(HTMLParser):
        def __init__(self):
            super().__init__()
            self.tags = []

        def handle_starttag(self, tag, attrs):
            self.tags.append((tag, dict(attrs)))

    url = 'https://example.com/article?a="&b=<evil>'
    summary = Summary(
        "<script>한국어 제목</script>", ("<b>핵심</b> & 내용",), '중요한 <img src="x"> 의미', "<i>근거</i>"
    )
    post = render_post(item(url=url), summary)
    parsed = Tags()
    parsed.feed(post)
    assert [tag for tag, _attrs in parsed.tags] == ["b", "b", "a"]
    assert parsed.tags[-1][1]["href"] == url
    assert "2026.09.08" in post  # The UTC date crosses midnight in Seoul.
    assert "&lt;script&gt;" in post


@pytest.mark.parametrize(
    "url", ["javascript:alert(1)", "file:///etc/passwd", "https://user:pass@example.com/a"]
)
def test_unsafe_article_links_are_rejected(url):
    with pytest.raises(ValueError):
        render_post(item(url=url), Summary("제목", ("핵심",), "의미", "근거"))
