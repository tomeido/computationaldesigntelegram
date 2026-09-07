from html import escape
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from .models import RankedArticle, Summary
from .resources import public_resource_url

CHANNEL_DESCRIPTION = (
    "Web3·블록체인과 컴퓨테이셔널 디자인의 접점을 가장 먼저 전합니다. "
    "온체인 생성 예술, AI 디자인 도구, 파라메트릭 디자인, 크리에이티브 코딩의 "
    "논문·투자 및 지원 소식·흥미로운 제작 사례의 핵심을 한국어 발췌·번역과 원문 링크로 제공합니다."
)

KIND_LABELS = {
    "news": "소식", "paper": "논문", "funding": "투자·지원", "showcase": "작품·실험",
    "release": "GitHub 업데이트",
}


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
    kind = article.kind
    evidence = summary.evidence
    label = KIND_LABELS.get(kind, KIND_LABELS["news"])
    heading = item.category if kind == "news" else f"{label} · {item.category}"
    lines = [f"<b>{escape(heading)}</b>", f"<b>{escape(summary.title)}</b>", ""]
    lines.extend(f"• {escape(b)}" for b in summary.bullets)
    if article.curator_note:
        lines.extend(["", f"활용: {escape(article.curator_note[:180])}"])
    if kind == "paper":
        host = (parts.hostname or "").lower()
        if host == "arxiv.org" or host.endswith(".arxiv.org"):
            note = "arXiv 수록본 · 학술대회·저널 게재 여부와 검증 범위는 원문에서 확인하세요."
            evidence = evidence.replace("RSS 발췌", "논문 초록 발췌")
        else:
            note = "연구 소개 · 실험 조건과 한계는 원문에서 확인하세요."
        tag += " #논문"
    elif kind == "funding":
        note = (
            "투자·지원 소식 · 발표 내용과 조건은 원문에서 확인하세요."
            if article.summary
            else "검색 제목만 확인 · 투자·지원 내용과 조건은 원문에서 확인하세요."
        )
        tag += " #투자지원"
    elif kind == "showcase":
        note = summary.why
        tag += " #작품실험"
    elif kind == "release":
        note = "공식 릴리스 안내 · 코드 실행과 환경 호환성은 직접 확인하세요."
        evidence = evidence.replace("RSS 발췌", "공식 릴리스 발췌")
        tag += " #GitHub #오픈소스"
    else:
        note = summary.why
    resources = []
    seen = {article.url}
    for link in article.links[:6]:
        url = public_resource_url(link.url)
        if not url or url in seen:
            continue
        seen.add(url)
        resources.append(f'<a href="{escape(url, quote=True)}">{escape(link.label[:40])}</a>')
        if len(resources) == 3:
            break
    if resources:
        lines.extend(["", " · ".join(resources)])
    if article.license:
        lines.append(f"저장소 라이선스 표기: {escape(article.license[:60])}")
    elif kind == "release":
        lines.append("저장소 라이선스 표기: 확인 필요")
    lines.extend(
        [
            "",
            f"💡 {escape(note)}",
            "",
            f'<a href="{escape(article.url, quote=True)}">원문 보기</a> · {escape(article.source[:80])}',
            f"{date} · {escape(evidence)}",
            tag,
        ]
    )
    post = "\n".join(lines)
    # Conservative cap, including markup and surrogate pairs, below Telegram's parsed-text limit.
    if len(post.encode("utf-16-le")) // 2 > 4000:
        raise ValueError("텔레그램 메시지 길이 제한을 초과했습니다.")
    return post
