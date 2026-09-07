from html import escape
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from .models import RankedArticle, Summary

CHANNEL_DESCRIPTION = (
    "Web3·블록체인과 컴퓨테이셔널 디자인의 접점을 가장 먼저 전합니다. "
    "온체인 생성 예술, AI 디자인 도구, 파라메트릭 디자인, 크리에이티브 코딩의 "
    "핵심을 한국어 요약과 원문 링크로 제공합니다."
)


def render_post(item: RankedArticle, summary: Summary, timezone: str = "Asia/Seoul") -> str:
    article = item.article
    parts = urlsplit(article.url)
    if parts.scheme not in ("https", "http") or not parts.hostname or parts.username:
        raise ValueError("기사 URL이 올바르지 않습니다.")
    if len(article.url) > 1800:
        raise ValueError("기사 URL이 너무 깁니다.")
    tag = {1: "#Web3 #생성예술", 2: "#AI #컴퓨테이셔널디자인", 3: "#컴퓨테이셔널디자인"}[item.priority]
    date = (
        article.published_at.astimezone(ZoneInfo(timezone)).strftime("%Y.%m.%d")
        if article.published_at
        else "날짜 미확인"
    )
    lines = [f"<b>{escape(item.category)}</b>", f"<b>{escape(summary.title)}</b>", ""]
    lines.extend(f"• {escape(b)}" for b in summary.bullets)
    lines.extend(
        [
            "",
            f"💡 {escape(summary.why)}",
            "",
            f'<a href="{escape(article.url, quote=True)}">원문 보기</a> · {escape(article.source[:80])}',
            f"{date} · {escape(summary.evidence)}",
            tag,
        ]
    )
    post = "\n".join(lines)
    # Conservative cap, including markup and surrogate pairs, below Telegram's parsed-text limit.
    if len(post.encode("utf-16-le")) // 2 > 4000:
        raise ValueError("텔레그램 메시지 길이 제한을 초과했습니다.")
    return post
