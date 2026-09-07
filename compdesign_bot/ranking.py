"""Conservative, deterministic selection of computational design news.

The priority is a hard ordering: even fresh general AI news cannot displace
relevant Web3 design work. Feed text is evidence, never an instruction.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import replace
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
    "computational fabrication",
    "digital fabrication",
    "parametric cad",
    "generative cad",
    "cad synthesis",
    "cad generation",
    "text to cad",
    "text to 3d",
    "image to 3d",
    "3d asset generation",
    "3d content generation",
    "shape generation",
    "geometric deep learning for design",
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
    "디지털 제작",
    "디지털 패브리케이션",
    "계산 제작",
    "형상 생성",
    "3d 에셋 생성",
)
# These methods also occur in unrelated science and computer vision. Require
# explicit geometry, fabrication, or creative modelling evidence alongside them.
_RESEARCH_METHODS = _terms(
    "inverse design",
    "inverse graphics",
    "topology optimization",
    "topology optimisation",
    "shape optimization",
    "shape optimisation",
    "differentiable rendering",
    "역설계",
    "위상 최적화",
    "형상 최적화",
    "미분 가능 렌더링",
)
_RESEARCH_CONTEXT = _terms(
    "geometry",
    "geometric",
    "shape",
    "shapes",
    "mesh",
    "meshes",
    "cad",
    "fabrication",
    "3d printing",
    "additive manufacturing",
    "architecture",
    "structural design",
    "material design",
    "truss",
    "lattice",
    "기하",
    "형상",
    "메시",
    "제작",
    "3d 프린팅",
    "적층 제조",
    "건축",
    "구조 설계",
)
_AI_3D_CREATION = _terms(
    "3d modeling",
    "3d modelling",
    "3d model generation",
    "3d asset creation",
    "3d content creation",
    "3d 모델링",
    "3d 모델 생성",
)
_ANIMATION_METHODS = _terms(
    "skinning",
    "rigging",
    "mesh deformation",
    "skinned gaussian",
    "skeletal animation",
    "스키닝",
    "리깅",
    "메시 변형",
)
_LAYOUT_METHODS = _terms(
    "layout optimization",
    "layout optimisation",
    "layout generation",
    "layout synthesis",
    "automated layout",
    "exploratory design",
    "레이아웃 최적화",
    "레이아웃 생성",
)
_LAYOUT_CONTEXT = _terms("layout", "layouts", "typography", "typographic", "레이아웃", "타이포그래피")
_COMPUTATIONAL_METHODS = _terms(
    "algorithm",
    "algorithms",
    "optimization",
    "optimisation",
    "neural network",
    "neural networks",
    "constraint solver",
    "알고리즘",
    "최적화",
    "신경망",
)
_GRAPHICS_API = _terms("webgpu", "webgl", "tsl")
_VISUAL_EXPERIMENT = _terms(
    "drawing",
    "lighting",
    "tubes",
    "shader",
    "rendering",
    "visual effects",
    "particles",
    "셰이더",
    "시각 효과",
)
_UNRELATED_RESEARCH = _terms(
    "drug discovery",
    "drug design",
    "drug molecules",
    "molecular design",
    "molecular generation",
    "molecule generation",
    "protein design",
    "신약 개발",
    "약물 설계",
    "분자 설계",
    "단백질 설계",
)
_DESIGN_TOOLS = _terms(
    "p5.js",
    "p5js",
    "touchdesigner",
    "openframeworks",
    "cables.gl",
    "three.js",
    "threejs",
    "babylon.js",
    "threlte",
)
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
_FUNDING_EVENTS = _terms(
    "funding round",
    "financing round",
    "seed funding",
    "seed investment",
    "venture funding",
    "series a funding",
    "series b funding",
    "series c funding",
    "series a round",
    "series b round",
    "series c round",
    "grant program",
    "grants program",
    "grant programme",
    "grant applications",
    "grant application",
    "grant funding",
    "research grant",
    "artist grant",
    "artist grants",
    "acquired by",
    "acquisition deal",
    "initial public offering",
    "ipo",
    "투자 유치",
    "투자유치",
    "시드 투자",
    "지원금",
    "연구비",
    "창작 지원",
    "기업공개",
)
_FUNDING_AMOUNT = re.compile(
    r"\b(?:raises?|raised|secures?|secured|receives?|received)\s+(?:an?\s+)?"
    r"(?:[$€£]\s*\d|\d[\d,.]*\s*(?:million|billion|usd|eur|krw)\b)",
    re.IGNORECASE,
)
_PAPER_TITLE = re.compile(
    r"^(?:(?:new\s+)?research paper|preprint|논문|프리프린트)\s*[:：]",
    re.IGNORECASE,
)
_SHOWCASE_TITLE = _terms(
    "creative project",
    "art project",
    "generative art project",
    "demo",
    "interactive artwork",
    "interactive installation",
    "playground",
    "데모",
    "인터랙티브 작품",
    "인터랙티브 설치",
    "실험작",
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


def _has_funding_evidence(body: str) -> bool:
    return bool(_FUNDING_EVENTS.search(body) or _FUNDING_AMOUNT.search(body))


def _infer_kind(article: Article, body: str, host: str) -> Article:
    """Keep source labels; infer a label only from explicit publication evidence."""
    if article.kind != "news":
        return article
    try:
        path = urlsplit(article.url).path
    except ValueError:
        path = ""
    paper_url = (
        (host == "arxiv.org" or host.endswith(".arxiv.org")) and path.startswith(("/abs/", "/pdf/", "/html/"))
    ) or (host in {"doi.org", "dx.doi.org", "dl.acm.org"} and path.startswith(("/10.", "/doi/10.")))
    if paper_url or _PAPER_TITLE.search(article.title):
        return replace(article, kind="paper")
    if _has_funding_evidence(body):
        return replace(article, kind="funding")
    if _SHOWCASE_TITLE.search(article.title):
        return replace(article, kind="showcase")
    return article


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
        if _UNRELATED_RESEARCH.search(body):
            continue
        # Discovery queries can match text that is absent from the supplied RSS
        # headline. A source hint alone cannot establish a funding event.
        if article.kind == "funding" and not _has_funding_evidence(body):
            continue
        try:
            host = (urlsplit(article.url).hostname or "").lower()
        except ValueError:
            host = ""
        article = _infer_kind(article, body, host)
        design_hits = len(_DESIGN.findall(body))
        research_design = bool(_RESEARCH_METHODS.search(body) and _RESEARCH_CONTEXT.search(body))
        animation_design = bool(_ANIMATION_METHODS.search(body) and _RESEARCH_CONTEXT.search(body))
        layout_research = bool(
            article.kind == "paper"
            and _LAYOUT_METHODS.search(body)
            and _LAYOUT_CONTEXT.search(body)
            and _COMPUTATIONAL_METHODS.search(body)
        )
        graphics_experiment = bool(_GRAPHICS_API.search(body) and _VISUAL_EXPERIMENT.search(body))
        ai_creation = bool(_AI.search(body) and _AI_3D_CREATION.search(body))
        platform_art = bool(_ART_PLATFORMS.search(body) and _GEN_ART_CONTEXT.search(body))
        tool_design = bool(
            _DESIGN_TOOLS.search(body) or (_AMBIGUOUS_TOOLS.search(body) and _TOOL_CONTEXT.search(body))
        )
        if not (
            design_hits
            or platform_art
            or tool_design
            or research_design
            or animation_design
            or layout_research
            or graphics_experiment
            or ai_creation
        ):
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
