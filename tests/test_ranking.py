import unittest
from datetime import UTC, datetime, timedelta

from compdesign_bot.models import Article
from compdesign_bot.ranking import rank_articles

NOW = datetime(2026, 9, 7, 12, tzinfo=UTC)


def article(title, *, summary="", url=None, age=timedelta(hours=1), published=None):
    return Article(
        title=title,
        url=url or "https://example.com/" + title.replace(" ", "-"),
        source="Example",
        summary=summary,
        published_at=published if published is not None else NOW - age,
    )


class RankingTests(unittest.TestCase):
    def test_web3_precedes_newer_ai_and_general_design(self):
        web3 = article("Onchain generative art algorithm tutorial", age=timedelta(days=6))
        ai = article("AI parametric design research", age=timedelta(minutes=1))
        general = article("Computational design open source tool release")
        ranked = rank_articles([general, ai, web3], now=NOW)
        self.assertEqual([item.article for item in ranked], [web3, ai, general])
        self.assertEqual([item.priority for item in ranked], [1, 2, 3])

    def test_web3_ai_overlap_gets_preference(self):
        both = article("AI onchain generative art tutorial")
        web3 = article("Onchain generative art tutorial")
        ranked = rank_articles([web3, both], now=NOW)
        self.assertEqual(ranked[0].article, both)

    def test_unrelated_finance_and_generic_design_are_excluded(self):
        titles = [
            "Bitcoin blockchain price reaches a new high",
            "AI design for a bank landing page",
            "NFT art auction raises millions",
            "Blockchain design patterns for developers",
            "Generative AI business investment report",
        ]
        self.assertEqual(rank_articles([article(title) for title in titles], now=NOW), [])

    def test_no_substring_traps(self):
        unrelated = [
            article("Grasshopper population study in Africa"),
            article("Processing daily news about code blocks"),
        ]
        self.assertEqual(rank_articles(unrelated, now=NOW), [])
        daily = article("Daily computational design digest: code blocks")
        self.assertEqual(rank_articles([daily], now=NOW)[0].priority, 3)

    def test_tools_need_meaningful_context(self):
        titles = ["Grasshopper parametric geometry tutorial", "p5.js shader art workshop"]
        self.assertEqual(len(rank_articles([article(title) for title in titles], now=NOW)), 2)

    def test_korean_synonyms_and_inflected_words(self):
        web3 = article("블록체인 기반 제너레이티브 아트를 만드는 오픈소스 도구")
        ai = article("인공지능으로 파라메트릭 설계를 자동화하는 연구")
        general = article("크리에이티브 코딩으로 구현한 기하 패턴")
        ranked = rank_articles([general, ai, web3], now=NOW)
        self.assertEqual([item.priority for item in ranked], [1, 2, 3])
        self.assertEqual(ranked[0].category, "Web3 × 디자인")

    def test_english_acronyms_with_korean_particles(self):
        item = article("Web3에서 AI로 구현하는 파라메트릭 디자인")
        ranked = rank_articles([item], now=NOW)
        self.assertEqual(ranked[0].priority, 1)
        self.assertGreater(ranked[0].score, 50)

    def test_dates_must_be_known_recent_and_plausible(self):
        unknown = Article("Unknown generative art", "https://example.com/unknown", "Example", "", None)
        stale = article("Stale generative art", age=timedelta(days=7, seconds=1))
        boundary = article("Boundary generative art", age=timedelta(days=7))
        future = article("Future generative art", age=-timedelta(hours=6, seconds=1))
        skew = article("Skewed generative art", age=-timedelta(hours=2))
        ranked = rank_articles([unknown, stale, boundary, future, skew], now=NOW)
        self.assertEqual({item.article.title for item in ranked}, {boundary.title, skew.title})
        self.assertEqual(rank_articles([boundary], now=NOW, max_age_days=2), [])

    def test_naive_dates_are_utc(self):
        item = article("Computational design paper", published=NOW.replace(tzinfo=None))
        self.assertEqual(len(rank_articles([item], now=NOW.replace(tzinfo=None))), 1)

    def test_tracking_urls_and_punctuation_titles_are_deduplicated(self):
        first = article("Generative art paper", url="http://www.example.com/a/?utm_source=rss&x=1#top")
        second = article("Generative art paper released", url="https://example.com/a?x=1&utm_medium=feed")
        third = article("GENERATIVE ART PAPER RELEASED!", url="https://another.example/paper")
        ranked = rank_articles([first, second, third], now=NOW)
        self.assertEqual(len(ranked), 1)

    def test_content_query_parameters_are_preserved(self):
        first = article("Generative art first paper", url="https://example.com/article?id=1")
        second = article("Generative art second paper", url="https://example.com/article?id=2")
        self.assertEqual(len(rank_articles([first, second], now=NOW)), 2)

    def test_promotional_and_market_only_articles_are_excluded(self):
        spam = [
            "NFT generative art airdrop: claim now",
            "Onchain generative art price prediction",
            "Art Blocks generative art floor prices surge",
            "온체인 생성형 아트 에어드롭 이벤트",
        ]
        self.assertEqual(rank_articles([article(title) for title in spam], now=NOW), [])

    def test_known_platform_needs_actual_art_context(self):
        art = article("Art Blocks generative collection uses new code-based artwork")
        irrelevant = article("Art Blocks company announces new office")
        ranked = rank_articles([irrelevant, art], now=NOW)
        self.assertEqual([item.article for item in ranked], [art])
        self.assertEqual(ranked[0].priority, 1)

    def test_practical_work_outweighs_small_recency_difference(self):
        research = article(
            "Computational design research paper and open source implementation", age=timedelta(days=3)
        )
        headline = article("Computational design is popular", age=timedelta(minutes=1))
        self.assertEqual(rank_articles([headline, research], now=NOW)[0].article, research)

    def test_order_is_independent_of_input_order(self):
        items = [article("Generative art beta"), article("Generative art alpha")]
        self.assertEqual(rank_articles(items, now=NOW), rank_articles(list(reversed(items)), now=NOW))

    def test_invalid_max_age(self):
        with self.assertRaises(ValueError):
            rank_articles([], now=NOW, max_age_days=-1)


if __name__ == "__main__":
    unittest.main()
