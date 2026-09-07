"""Conservative, deterministic selection of computational design news.

The priority is a hard ordering: even fresh general AI news cannot displace
relevant Web3 design work. Feed text is evidence, never an instruction.
"""

from __future__ import annotations

import re
import unicodedata
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from .models import Article, RankedArticle


def _terms(*phrases: str) -> re.Pattern[str]:
    """English words need boundaries; Korean terms may take grammatical suffixes."""
    patterns = []
    for phrase in phrases:
        escaped = re.escape(phrase).replace(r"\ ", r"[\s-]+")
        if phrase.isascii():
            escaped = rf"(?<![a-z0-9_]){escaped}(?![a-z0-9_])"
        patterns.append(escaped)
    return re.compile("|".join(patterns), re.IGNORECASE)


_DESIGN = _terms(
    "computational design",
    "computational art",
    "computational architecture",
    "algorithmic design",
    "algorithmic art",
    "algorithmic architecture",
    "generative art",
    "generative design",
    "generative architecture",
    "generative logo",
    "generative typography",
    "algorithmic typography",
    "parametric design",
    "parametric modeling",
    "parametric modelling",
    "parametric architecture",
    "procedural modeling",
    "procedural modelling",
    "procedural geometry",
    "procedural design",
    "procedural art",
    "creative coding",
    "geometry nodes",
    "code art",
    "code-based art",
    "plotter art",
    "컴퓨테이셔널 디자인",
    "컴퓨테이셔널 아트",
    "컴퓨테이셔널 건축",
    "계산적 디자인",
    "계산적 설계",
    "계산 설계",
    "계산 디자인",
    "알고리즘 디자인",
    "알고리즘 아트",
    "알고리즘 예술",
    "알고리즘 기반 설계",
    "알고리즘 기반 예술",
    "생성형 디자인",
    "생성 디자인",
    "생성형 설계",
    "생성형 아트",
    "생성형 예술",
    "생성 예술",
    "제너레이티브 아트",
    "제너레이티브 디자인",
    "제너러티브 아트",
    "파라메트릭 디자인",
    "파라메트릭 설계",
    "파라메트릭 모델링",
    "파라메트릭 건축",
    "매개변수 설계",
    "매개변수 디자인",
    "매개변수 모델링",
    "프로시저럴 모델링",
    "프로시저럴 디자인",
    "절차적 모델링",
    "절차적 생성",
    "절차적 디자인",
    "크리에이티브 코딩",
    "코딩 아트",
    "지오메트리 노드",
)
_DESIGN_TOOLS = _terms("p5.js", "p5js", "touchdesigner", "openframeworks", "cables.gl")
_AMBIGUOUS_TOOLS = _terms("grasshopper", "houdini", "processing", "rhino", "glsl")
_TOOL_CONTEXT = _terms(
    "3d",
    "geometry",
    "modeling",
    "modelling",
    "parametric",
    "procedural",
    "shader",
    "node-based",
    "visual programming",
    "rhino 3d",
    "rhino3d",
    "시각 프로그래밍",
    "기하",
    "모델링",
    "파라메트릭",
    "노드 기반",
    "셰이더",
)
_ART_PLATFORMS = _terms("art blocks", "artblocks", "fxhash", "아트블록스", "아트 블록스")
_GEN_ART_CONTEXT = _terms(
    "generative collection",
    "generative artwork",
    "generative artist",
    "algorithmic collection",
    "code-based artwork",
    "generative minting",
    "제너레이티브 작품",
    "생성형 작품",
    "생성형 컬렉션",
    "알고리즘 작품",
)
_WEB3 = _terms(
    "web3",
    "web 3",
    "web3.0",
    "blockchain",
    "onchain",
    "on-chain",
    "on chain",
    "ethereum",
    "tezos",
    "solana",
    "nft",
    "nfts",
    "smart contract",
    "smart contracts",
    "decentralized",
    "decentralised",
    "웹3",
    "웹 3",
    "블록체인",
    "온체인",
    "이더리움",
    "테조스",
    "솔라나",
    "스마트 컨트랙트",
    "스마트 계약",
    "탈중앙",
    "대체불가능토큰",
    "대체 불가능 토큰",
)
_AI = _terms(
    "ai",
    "artificial intelligence",
    "machine learning",
    "deep learning",
    "diffusion model",
    "diffusion models",
    "neural network",
    "neural networks",
    "llm",
    "llms",
    "stable diffusion",
    "comfyui",
    "인공지능",
    "인공 지능",
    "머신러닝",
    "머신 러닝",
    "딥러닝",
    "딥 러닝",
    "확산 모델",
    "신경망",
)
_SPAM = _terms(
    "airdrop",
    "airdrops",
    "giveaway",
    "giveaways",
    "price prediction",
    "price predictions",
    "price target",
    "price targets",
    "100x",
    "1000x",
    "에어드롭",
    "에어 드롭",
    "가격 예측",
    "가격 전망",
    "목표가",
    "무료 증정",
    "수익 보장",
    "급등 코인",
)
_TRADING = _terms(
    "floor price",
    "floor prices",
    "trading volume",
    "price analysis",
    "technical analysis",
    "market cap",
    "bull run",
    "buy now",
    "sell signal",
    "token price",
    "token prices",
    "바닥가",
    "최저가",
    "거래량",
    "시세",
    "매수",
    "매도",
    "투자 수익",
    "급등",
    "급락",
)
_USEFUL = _terms(
    "tutorial",
    "tutorials",
    "research",
    "paper",
    "papers",
    "open source",
    "open-source",
    "repository",
    "github",
    "implementation",
    "algorithm",
    "algorithms",
    "source code",
    "documentation",
    "developer",
    "developers",
    "sdk",
    "release",
    "released",
    "tool",
    "tools",
    "workflow",
    "workflows",
    "workshop",
    "workshops",
    "튜토리얼",
    "연구",
    "논문",
    "오픈소스",
    "오픈 소스",
    "소스 코드",
    "구현",
    "알고리즘",
    "개발자",
    "워크플로",
    "워크숍",
    "도구",
    "출시",
    "공개",
)
_TRACKING_PARAMS = {"fbclid", "gclid", "dclid", "mc_cid", "mc_eid", "igshid"}
_PRIMARY_DOMAINS = {
    "artblocks.io",
    "fxhash.xyz",
    "arxiv.org",
    "github.com",
    "p5js.org",
    "processing.org",
    "derivative.ca",
    "mcneel.com",
    "grasshopper3d.com",
    "blender.org",
    "sidefx.com",
    "ethereum.org",
    "tezos.com",
}


def _text(value: str) -> str:
    return unicodedata.normalize("NFKC", value).casefold()


def _url_key(url: str) -> str:
    try:
        parts = urlsplit(url)
        hostname = (parts.hostname or "").lower().removeprefix("www.")
        port = parts.port
        netloc = hostname + (f":{port}" if port and port not in (80, 443) else "")
        query = sorted(
            (key, value)
            for key, value in parse_qsl(parts.query, keep_blank_values=True)
            if not key.lower().startswith("utm_") and key.lower() not in _TRACKING_PARAMS
        )
        return urlunsplit(("https", netloc, parts.path.rstrip("/"), urlencode(query), ""))
    except ValueError:
        return url.strip()


def _title_key(title: str) -> str:
    return re.sub(r"[\W_]+", " ", _text(title)).strip()


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def rank_articles(
    articles: list[Article], *, now: datetime | None = None, max_age_days: int = 7
) -> list[RankedArticle]:
    """Filter, rank, and deduplicate items using only their explicit feed evidence.

    Naive datetimes are interpreted as UTC. Unknown publication dates are excluded
    because their freshness cannot be established. Six hours of clock skew is
    tolerated without granting future-dated items extra recency credit.
    """
    if max_age_days < 0:
        raise ValueError("max_age_days must be nonnegative")
    current = _utc(now or datetime.now(UTC))
    ranked: list[RankedArticle] = []
    for article in articles:
        if article.published_at is None or not article.title.strip() or not article.url.strip():
            continue
        age = current - _utc(article.published_at)
        if age > timedelta(days=max_age_days) or age < -timedelta(hours=6):
            continue
        body = _text(f"{article.title}\n{article.summary}")
        design_hits = len(_DESIGN.findall(body))
        platform_art = bool(_ART_PLATFORMS.search(body) and _GEN_ART_CONTEXT.search(body))
        tool_design = bool(
            _DESIGN_TOOLS.search(body) or (_AMBIGUOUS_TOOLS.search(body) and _TOOL_CONTEXT.search(body))
        )
        if not (design_hits or platform_art or tool_design):
            continue
        if _SPAM.search(body):
            continue
        useful_hits = len(_USEFUL.findall(body))
        if _TRADING.search(body) and not useful_hits:
            continue
        web3 = bool(_WEB3.search(body) or _ART_PLATFORMS.search(body))
        ai = bool(_AI.search(body))
        if web3:
            priority, category = 1, "Web3 × 디자인"
        elif ai:
            priority, category = 2, "AI × 디자인"
        else:
            priority, category = 3, "컴퓨테이셔널 디자인"
        recency = max(0.0, 1.0 - max(0.0, age.total_seconds()) / (max(1, max_age_days) * 86400))
        try:
            host = (urlsplit(article.url).hostname or "").lower()
        except ValueError:
            host = ""
        primary = any(host == domain or host.endswith("." + domain) for domain in _PRIMARY_DOMAINS)
        score = (
            min(design_hits, 4) * 3
            + min(useful_hits, 4) * 4
            + (50 if web3 and ai else 0)
            + (3 if tool_design else 0)
            + (3 if primary else 0)
            + recency * 6
        )
        ranked.append(
            RankedArticle(article=article, priority=priority, score=round(score, 4), category=category)
        )

    ranked.sort(
        key=lambda item: (
            item.priority,
            -item.score,
            _title_key(item.article.title),
            _url_key(item.article.url),
        )
    )
    # A title duplicate may bridge two URL duplicates. Build complete groups first
    # so a lower-scoring bridge cannot leave two versions of one story selected.
    parents = list(range(len(ranked)))

    def group(index: int) -> int:
        while parents[index] != index:
            parents[index] = parents[parents[index]]
            index = parents[index]
        return index

    seen_keys: dict[tuple[str, str], int] = {}
    for index, item in enumerate(ranked):
        for key in (("url", _url_key(item.article.url)), ("title", _title_key(item.article.title))):
            if key in seen_keys:
                first, second = group(index), group(seen_keys[key])
                parents[max(first, second)] = min(first, second)
            else:
                seen_keys[key] = index
    return [item for index, item in enumerate(ranked) if group(index) == index]
