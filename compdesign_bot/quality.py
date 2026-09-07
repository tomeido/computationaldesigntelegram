"""Require concrete source evidence before automatic translation and posting.

These deterministic checks measure excerpt completeness and practical detail.
They do not verify scientific validity, reproduce code, or predict investment
returns. Rejected candidates remain available to an operator for review.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

from .models import Article, RankedArticle


@dataclass(frozen=True)
class QualityDecision:
    accepted: bool
    reason: str
    score: int


def _pattern(*terms: str) -> re.Pattern[str]:
    expressions = []
    for term in terms:
        escaped = re.escape(term).replace(r"\ ", r"[\s-]+")
        if term.isascii():
            escaped = rf"(?<![a-z0-9_]){escaped}(?![a-z0-9_])"
        expressions.append(escaped)
    return re.compile("|".join(expressions), re.IGNORECASE)


_TECHNIQUE = _pattern(
    "algorithm",
    "algorithms",
    "neural network",
    "neural networks",
    "diffusion",
    "optimization",
    "optimisation",
    "solver",
    "constraints",
    "constraint",
    "mesh",
    "meshes",
    "shader",
    "shaders",
    "geometry",
    "geometric",
    "rendering",
    "renderer",
    "toolpath",
    "toolpaths",
    "fabrication",
    "3d printing",
    "multi-axis",
    "simulation",
    "particles",
    "deformation",
    "skinning",
    "rigging",
    "gaussian",
    "splatting",
    "lattice",
    "topology",
    "parametric",
    "procedural",
    "spline",
    "splines",
    "vector",
    "vectors",
    "texture",
    "textures",
    "webgl",
    "webgpu",
    "gpu",
    "cad",
    "3d",
    "framebuffer",
    "ray tracing",
    "physics",
    "gradient",
    "gradients",
    "differentiable",
    "smart contract",
    "smart contracts",
    "onchain",
    "on-chain",
    "blockchain",
    "file storage",
    "typescript",
    "javascript",
    "python",
    "p5.js",
    "three.js",
    "tensors",
    "matrix",
    "canvas",
    "camera",
    "curve",
    "curves",
    "vertex",
    "vertices",
    "normals",
    "bvh",
    "collision",
    "autodiff",
    "automatic differentiation",
    "array",
    "arrays",
    "tile",
    "tiles",
    "minting",
    "token uri",
    "dependency registry",
    "solidity",
    "opengl",
    "transfer hook",
    "transfer hooks",
    "indexer",
    "indexers",
    "caustics",
    "refraction",
    "refractions",
    "알고리즘",
    "신경망",
    "최적화",
    "메시",
    "셰이더",
    "기하",
    "렌더링",
    "공구 경로",
    "툴패스",
    "제작",
    "프린팅",
    "시뮬레이션",
    "입자",
    "변형",
    "스키닝",
    "리깅",
    "가우시안",
    "격자",
    "위상",
    "파라메트릭",
    "프로시저럴",
    "텍스처",
    "미분",
    "스마트 컨트랙트",
    "온체인",
)
_ACTION = re.compile(
    r"\b(?:us(?:e|es|ed|ing)|introduc(?:e|es|ed|ing)|propos(?:e|es|ed|ing)|"
    r"present(?:s|ed|ing)?|generat(?:e|es|ed|ing)|comput(?:e|es|ed|ing)|"
    r"build(?:s|ing)?|built|creat(?:e|es|ed|ing)|implement(?:s|ed|ing)?|"
    r"transform(?:s|ed|ing)?|unravell?ing|optimiz(?:e|es|ed|ing)|optimis(?:e|es|ed|ing)|"
    r"learn(?:s|ed|ing)?|train(?:s|ed|ing)?|deform(?:s|ed|ing)?|support(?:s|ed|ing)?|"
    r"add(?:s|ed|ing)?|enabl(?:e|es|ed|ing)|improv(?:e|es|ed|ing)|"
    r"fix(?:es|ed|ing)?|correct(?:s|ed|ing)?|resolv(?:e|es|ed|ing)|"
    r"reduc(?:e|es|ed|ing)|accelerat(?:e|es|ed|ing)|render(?:s|ed|ing)?|"
    r"determin(?:e|es|ed|ing)|employ(?:s|ed|ing)?|combin(?:e|es|ed|ing)|"
    r"turn(?:s|ed|ing)?|rebuilt|rebuild(?:s|ing)?|print(?:s|ed|ing)?|search(?:es|ed|ing)?)\b"
    r"|\bcome(?:s)? together\b"
    r"|사용|활용|제안|생성|계산|구현|변환|최적화|학습|변형|지원|추가|개선|수정|줄이|줄였",
    re.IGNORECASE,
)
_RESULT = re.compile(
    r"\b(?:evaluat\w*|benchmark\w*|outperform\w*|experiments?|accuracy|collision[- ]free|"
    r"faster|speedup|reduc\w*|improv\w*|comparison|comparisons)\b"
    r"|평가|실험|성능|충돌 없는|충돌 방지|정확도|속도|비교",
    re.IGNORECASE,
)
_RESEARCH = re.compile(
    r"\b(?:we|our|this|the)\b.{0,70}\b(?:propos\w*|introduc\w*|present\w*|method|framework|"
    r"system|algorithm|approach|optim\w*|evaluat\w*|comput\w*|us(?:es|ing))\b"
    r"|논문|연구|제안|기법|방법론|프레임워크",
    re.IGNORECASE,
)
_DIGEST = re.compile(
    r"\b(?:daily|weekly|monthly)\b.{0,45}\b(?:digest|roundup|round-up|newsletter|links|news)\b"
    r"|\b(?:digest|roundup|round-up|newsletter)\b|주간.{0,20}(?:소식|뉴스|모음)|뉴스레터|뉴스 모음",
    re.IGNORECASE,
)
_EVENT = _pattern(
    "workshop",
    "conference",
    "summit",
    "meetup",
    "meeting",
    "speakers",
    "course",
    "courses",
    "training",
    "워크숍",
    "컨퍼런스",
    "강좌",
    "수강",
    "교육",
    "밋업",
    "행사",
)
_REGISTER = re.compile(
    r"\b(?:register|registration|tickets?|enroll\w*|sign up|join us|seats?|early bird)\b"
    r"|등록|신청|참가|수강|모집|얼리버드",
    re.IGNORECASE,
)
_PUBLISHED_LEARNING = re.compile(
    r"\b(?:recording|recorded session|video tutorial|tutorial recording)\b"
    r"|\b(?:tutorial|slides|video|source code)\b.{0,25}\b(?:available|published|released|download)\b"
    r"|공개된.{0,15}(?:강연|튜토리얼|녹화)|녹화 영상|강연 영상|녹화본",
    re.IGNORECASE,
)
_FUTURE_LEARNING = re.compile(
    r"\b(?:upcoming|future|planned|will|soon)\b.{0,35}\b(?:recording|tutorial|video|slides)\b"
    r"|\b(?:recording|tutorial|video|slides)\b.{0,25}\b(?:will|planned|soon)\b"
    r"|(?:녹화|영상|튜토리얼).{0,15}(?:예정|공개할)",
    re.IGNORECASE,
)
_FUNDING = re.compile(
    r"\b(?:rais(?:es|ed)|secur(?:es|ed)|receiv(?:es|ed))\s+(?:an?\s+)?"
    r"(?:[$€£]\s*\d|\d[\d,.]*\s*(?:million|billion|usd|eur|krw)\b)"
    r"|\b(?:announc\w*|clos\w*|complet\w*)\b.{0,65}\b(?:funding|financing|round|acquisition|offering)\b"
    r"|\b(?:acquir(?:ed|es)|acquired by)\b"
    r"|\b(?:grant|grants)\b.{0,65}\b(?:open|opens|opened|deadline|awarded|recipients)\b"
    r"|\b(?:open|opens|opened|awarded)\b.{0,45}\b(?:grant|grants)\b"
    r"|투자[ ]?유치|인수(?:했|한|를|하|합|했)|지원금.{0,35}(?:접수|신청|선정|지급|공모)|연구비.{0,25}(?:선정|지원)",
    re.IGNORECASE,
)
_FUNDING_DETAIL = re.compile(
    r"[$€£₩]\s*\d|\b\d[\d,.]*\s*(?:million|billion|usd|eur|krw)\b"
    r"|\b(?:led by|investors?|eligibility|eligible|deadline|until|applications?|recipients?|"
    r"series [abc]|seed round|acquir(?:ed|es))\b|억 원|억원|만 원|만원|투자사|주도|자격|마감|접수|선정|인수",
    re.IGNORECASE,
)
_NOISE_CHANGE = re.compile(
    r"^(?:(?:build|chore|ci|style|test|tests|deps|refactor)(?:\([^)]*\))?\s*[:：]|"
    r"(?:bump|update|upgrade|pin)\s+(?:dependencies|dependency|deps|version|actions?/|prettier|eslint|ruff|"
    r"typescript\s+(?:from|to)|node\s+(?:from|to))\b|"
    r"(?:new |first.time )?contributors?\b|(?:full |complete )?changelog\s*[:：]|"
    r"thanks?\b|release\s+v?\d|v?\d+\.\d+(?:\.\d+)?\b)"
    r"|\b(?:typos?|spelling|readme|dependabot|renovate|ci workflow|lint(?:ing)?|formatting)\b",
    re.IGNORECASE,
)


def _normalized(text: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", text).split()).strip()


def _substantive(text: str) -> bool:
    # A short headline or a list of topic tags is not enough source context.
    letters = len(re.findall(r"[A-Za-z가-힣]", text))
    words = len(re.findall(r"[A-Za-z][A-Za-z'-]*", text))
    hangul = len(re.findall(r"[가-힣]", text))
    return letters >= 38 and (words >= 8 or hangul >= 20)


def _pieces(text: str) -> list[str]:
    return [part.strip(" \t-*#•") for part in re.split(r"\n+|(?<=[.!?。])\s+", text) if part.strip()]


def _has_method(text: str) -> bool:
    return bool(_TECHNIQUE.search(text) and _ACTION.search(text))


def _has_release_change(text: str) -> bool:
    # Official changelogs also use noun phrases rather than "Added ...". A named
    # technical capability paired with an implementation/interface is evidence;
    # a version heading or repository description alone still cannot pass.
    nominal_capability = bool(
        _TECHNIQUE.search(text)
        and re.search(
            r"\b(?:reference implementation|interface|new API|new capability)\b", text, re.IGNORECASE
        )
    )
    return _has_method(text) or nominal_capability


def select_release_changes(excerpt: str) -> list[str]:
    """Return useful source changelog sentences, preserving numbers and API names.

    Both the publication gate and the translator use this selection so unrelated
    maintenance cannot become a release's published bullets. Long sentences are
    held for review rather than cut through a technical claim or code identifier.
    """
    return [
        piece
        for piece in _pieces(excerpt[:6000])
        if _substantive(piece)
        and len(piece) <= 320
        and not _NOISE_CHANGE.search(piece)
        and _has_release_change(piece)
    ]


def assess_quality(article: Article) -> QualityDecision:
    """Check evidence in supplied source text; never fetch or invent missing detail.

    Scores are 0–10 completeness signals for operator review, not star ratings.
    Static repository context, curator notes, and popularity never qualify a
    release with no useful changelog. Missing code does not disqualify a paper
    that reports an actual method or result.
    """
    title = _normalized(article.title)
    body = _normalized(article.summary)
    source_body = article.summary
    if not body or body.casefold().rstrip(".!。") == title.casefold().rstrip(".!。"):
        return QualityDecision(False, "본문 근거 없음: 제목만으로 자동 발행하지 않음", 0)
    # Discovery RSS often repeats a linked headline followed by the publisher's
    # name. That attribution is not an independent report of the headline claim.
    if title and body.casefold().startswith(title.casefold()):
        body = body[len(title) :].lstrip(" .!。:：|·–—-")
        source_body = body
    if not _substantive(body):
        return QualityDecision(False, "본문이 너무 짧아 방법·변경·발표 내용을 확인하기 어려움", 1)
    if _DIGEST.search(title):
        return QualityDecision(False, "모음·뉴스레터보다 개별 원문을 우선", 2)

    artifact_bonus = min(2, len(article.links))
    pieces = _pieces(source_body)
    detailed = [piece for piece in pieces if _substantive(piece)]
    methods = [piece for piece in detailed if _has_method(piece)]

    if article.kind == "release":
        changes = select_release_changes(source_body)
        if not changes:
            return QualityDecision(False, "릴리스에 사용자에게 유용한 기능·동작 변화 근거가 없음", 2)
        return QualityDecision(
            True,
            "실제 변경 기록에 제작·계산 작업의 기능 또는 수정 내용이 있음",
            min(10, 6 + min(2, len(changes)) + artifact_bonus),
        )

    if article.kind == "funding":
        if not _FUNDING.search(body) or not _FUNDING_DETAIL.search(body):
            return QualityDecision(False, "투자·인수·지원사업의 실제 발표와 구체적 조건이 본문에 부족함", 2)
        return QualityDecision(
            True, "본문에 자금조달·인수·지원사업의 발표 및 조건이 있음", min(10, 7 + artifact_bonus)
        )

    if article.kind == "paper":
        research = bool(_RESEARCH.search(body) or _RESULT.search(body))
        if not methods or not research:
            return QualityDecision(False, "논문 초록에 구체적 방법 또는 결과 근거가 부족함", 2)
        return QualityDecision(
            True,
            "초록에 구체적 계산·제작 방법 또는 평가 내용이 있음",
            min(10, 6 + int(bool(_RESULT.search(body))) + artifact_bonus),
        )

    # A future program can contain detailed technical talk descriptions. Only
    # already-published learning material makes an event notice actionable now.
    event_notice = bool(_EVENT.search(title))
    published_learning = bool(
        _PUBLISHED_LEARNING.search(f"{title} {body}") and not _FUTURE_LEARNING.search(f"{title} {body}")
    )
    if event_notice and not published_learning:
        return QualityDecision(False, "행사·강좌 안내에 이미 공개된 강연·튜토리얼 자료 근거가 없음", 2)
    if not methods:
        return QualityDecision(False, "제작 기법·실행 예제·구체적 개선 내용이 본문에 부족함", 2)
    return QualityDecision(
        True,
        "본문에 구체적 구현 방법 또는 도구 개선 내용이 있음",
        min(10, 6 + min(2, len(methods)) + artifact_bonus),
    )


def select_quality(
    ranked: list[RankedArticle],
) -> tuple[list[RankedArticle], list[tuple[RankedArticle, QualityDecision]]]:
    """Preserve topic ordering while separating candidates needing manual review."""
    accepted: list[RankedArticle] = []
    rejected: list[tuple[RankedArticle, QualityDecision]] = []
    for item in ranked:
        decision = assess_quality(item.article)
        if decision.accepted:
            accepted.append(item)
        else:
            rejected.append((item, decision))
    return accepted, rejected
