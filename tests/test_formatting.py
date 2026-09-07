from datetime import UTC, datetime
from html.parser import HTMLParser

import pytest

from compdesign_bot.formatting import render_post
from compdesign_bot.models import Article, RankedArticle, Summary


def item(*, excerpt="Research excerpt.", url="https://example.com/article"):
    return RankedArticle(
        Article(
            "Computational design research", url, "Example", excerpt, datetime(2026, 9, 7, 16, tzinfo=UTC)
        ),
        1,
        10,
        "Web3 × 디자인",
    )


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
