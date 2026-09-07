from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class Article:
    title: str
    url: str
    source: str
    summary: str
    published_at: datetime | None


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
