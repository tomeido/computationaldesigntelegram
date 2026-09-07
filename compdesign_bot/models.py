from dataclasses import dataclass
from datetime import datetime

ARTICLE_KINDS = frozenset({"news", "paper", "funding", "showcase", "release"})


@dataclass(frozen=True)
class ArticleLink:
    label: str
    url: str


@dataclass(frozen=True)
class Article:
    title: str
    url: str
    source: str
    summary: str
    published_at: datetime | None
    kind: str = "news"
    links: tuple[ArticleLink, ...] = ()
    license: str = ""
    topic_context: str = ""
    curator_note: str = ""


@dataclass(frozen=True)
class RankedArticle:
    article: Article
    priority: int
    score: float
    category: str


@dataclass(frozen=True)
class Summary:
    title: str
    bullets: tuple[str, ...]
    why: str
    evidence: str
