from dataclasses import replace
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


@pytest.mark.parametrize(
    "kind,label,tag", [("paper", "논문", "#논문"), ("funding", "투자·지원", "#투자지원"),
                       ("showcase", "작품·실험", "#작품실험")]
)
def test_content_kind_is_visible_without_replacing_topic(kind, label, tag):
    ranked = item()
    ranked = replace(ranked, article=replace(ranked.article, kind=kind))
    post = render_post(ranked, Summary("제목", ("핵심",), "의미", "근거"))
    assert f"{label} · Web3 × 디자인" in post
    assert tag in post


def test_arxiv_label_does_not_infer_peer_review_status():
    ranked = item(url="https://arxiv.org/abs/2604.24648")
    ranked = replace(ranked, article=replace(ranked.article, kind="paper"))
    post = render_post(ranked, Summary("제목", ("핵심",), "의미", "근거"))
    assert "arXiv 수록본" in post
    assert "게재 여부" in post
    assert "미심사" not in post
    assert "심사 완료" not in post
