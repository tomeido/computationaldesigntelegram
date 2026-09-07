from dataclasses import replace
from datetime import UTC, datetime

from compdesign_bot.models import Article, RankedArticle
from compdesign_bot.pipeline import varied_candidates
from compdesign_bot.storage import cache_key


def candidate(index, kind, priority=3):
    article = Article(
        f"Computational design {index}", f"https://example.com/{index}", "Example",
        "An original source excerpt.", datetime.now(UTC), kind=kind,
    )
    return RankedArticle(article, priority, 100 - index, "컴퓨테이셔널 디자인")


def test_kind_mix_preserves_hard_web3_priority_and_each_kinds_order():
    items = [candidate(0, "news", 1), candidate(1, "news", 1),
             candidate(2, "paper"), candidate(3, "paper"), candidate(4, "paper"),
             candidate(5, "showcase"), candidate(6, "funding")]
    result = varied_candidates(items)
    assert [item.article.kind for item in result] == [
        "news", "news", "paper", "showcase", "funding", "paper", "paper"
    ]
    assert result[:2] == items[:2]
    assert [item for item in result if item.article.kind == "paper"] == items[2:5]
    assert len(result) == len(set(result)) == len(items)


def test_reclassification_invalidates_excerpt_cache():
    article = candidate(0, "news").article
    assert cache_key(article, "translator") != cache_key(replace(article, kind="paper"), "translator")
