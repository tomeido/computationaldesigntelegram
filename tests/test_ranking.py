import unittest
from datetime import UTC, datetime, timedelta

from compdesign_bot.models import Article
from compdesign_bot.ranking import rank_articles

NOW = datetime(2026, 9, 7, 12, tzinfo=UTC)


def article(title, *, summary="", url=None, age=timedelta(hours=1), published=None, kind="news"):
    return Article(
        title=title,
        url=url or "https://example.com/" + title.replace(" ", "-"),
        source="Example",
        summary=summary,
        published_at=published if published is not None else NOW - age,
        kind=kind,
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
        self.assertLess(ranked[0].score, 25)

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

    def test_geometry_research_and_cad_papers_are_relevant(self):
        papers = [
            article(
                "Inverse design of printable lattice structures",
                summary="The paper optimizes geometry for additive manufacturing.",
                url="https://arxiv.org/abs/2609.00001",
            ),
            article(
                "Topology optimization for truss fabrication",
                url="https://arxiv.org/pdf/2609.00002",
            ),
            article(
                "Differentiable rendering for editable mesh geometry",
                url="https://arxiv.org/html/2609.00003v1",
            ),
            article(
                "LLM CAD synthesis from text instructions",
                url="https://dl.acm.org/doi/10.1145/123456",
            ),
        ]
        ranked = rank_articles(papers, now=NOW)
        self.assertEqual(len(ranked), 4)
        self.assertTrue(all(item.article.kind == "paper" for item in ranked))
        self.assertEqual(ranked[0].priority, 2)

    def test_research_topics_need_design_context(self):
        unrelated = [
            article("Differentiable rendering improves object detection", kind="paper"),
            article("Inverse design for drug discovery", kind="paper"),
            article("Generative design of drug molecules with AI", kind="paper"),
            article("AI protein design with a generative design algorithm", kind="paper"),
            article("Topology optimization of network packet routing", kind="paper"),
        ]
        self.assertEqual(rank_articles(unrelated, now=NOW), [])

    def test_design_funding_and_support_are_selected(self):
        titles = [
            "Generative CAD startup raises $20 million",
            "AI 3D modeling platform secures €4 million",
            "Creative coding artist grants program opens applications",
            "온체인 생성형 아트 창작 지원금 신청 접수",
        ]
        ranked = rank_articles([article(title) for title in titles], now=NOW)
        self.assertEqual(len(ranked), 4)
        self.assertTrue(all(item.article.kind == "funding" for item in ranked))
        self.assertEqual([item.priority for item in ranked], [1, 2, 3, 3])

    def test_generic_funding_and_crypto_prices_are_rejected(self):
        unrelated = [
            article("AI assistant startup raises $100 million", kind="funding"),
            article("Blockchain exchange announces a seed funding round", kind="funding"),
            article("Art Blocks generative art token price reaches new high", kind="funding"),
            article("NFT investment firm receives $10 million", kind="funding"),
        ]
        self.assertEqual(rank_articles(unrelated, now=NOW), [])

    def test_programming_raise_and_license_grants_are_not_funding(self):
        item = article(
            "Creative coding tutorial raises exceptions for malformed shaders",
            summary="The open source license grants permission to use the code.",
        )
        ranked = rank_articles([item], now=NOW)
        self.assertEqual(ranked[0].article.kind, "news")

    def test_funding_source_hint_requires_actual_funding_evidence(self):
        unsupported = article(
            "Generative Design Optimizes Liquid-Cooling Channels For 2.5D And 3D Packages",
            kind="funding",
        )
        generic = article("Generative design investment trends", kind="funding")
        funded = article("Generative CAD startup raises $20 million", kind="funding")
        supported = article("Creative coding artist grant applications open", kind="funding")
        ipo = article("AI 3D modeling startup announces initial public offering", kind="funding")
        ranked = rank_articles([unsupported, generic, funded, supported, ipo], now=NOW)
        self.assertEqual({item.article for item in ranked}, {funded, supported, ipo})

    def test_showcase_requires_explicit_creative_project_or_demo(self):
        demos = [
            article("A p5.js interactive artwork turns wind into geometry"),
            article("Generative art project creates a playable landscape"),
            article("파라메트릭 디자인 인터랙티브 작품 공개"),
        ]
        ranked = rank_articles(demos, now=NOW)
        self.assertEqual(len(ranked), 3)
        self.assertTrue(all(item.article.kind == "showcase" for item in ranked))

    def test_source_kind_is_preserved_but_does_not_override_relevance(self):
        supplied = article(
            "Computational design grant recipients present an interactive artwork",
            kind="showcase",
        )
        irrelevant = article("A gardening paper about feeding plants", kind="paper")
        ranked = rank_articles([supplied, irrelevant], now=NOW)
        self.assertEqual([item.article for item in ranked], [supplied])

    def test_kind_does_not_change_hard_topic_priorities(self):
        web3 = article("Onchain generative art demo", age=timedelta(days=6))
        ai = article("AI generative CAD seed funding round")
        paper = article(
            "Topology optimization for lattice geometry",
            age=timedelta(minutes=1),
            url="https://arxiv.org/abs/2609.01234",
        )
        ranked = rank_articles([paper, ai, web3], now=NOW)
        self.assertEqual([item.priority for item in ranked], [1, 2, 3])
        self.assertEqual([item.article.kind for item in ranked], ["showcase", "funding", "paper"])

    def test_paper_mentions_and_lookalike_domains_do_not_imply_papers(self):
        items = [
            article(
                "Computational design tool release",
                summary="The team cites a recent research paper in its documentation.",
            ),
            article("Generative art news", url="https://notarxiv.org/abs/2609.12345"),
            article("Creative coding news", url="https://arxiv.org/news"),
        ]
        ranked = rank_articles(items, now=NOW)
        self.assertEqual(len(ranked), 3)
        self.assertTrue(all(item.article.kind == "news" for item in ranked))

    def test_explicit_paper_heading_is_classified(self):
        item = article("논문: CAD synthesis with diffusion models")
        ranked = rank_articles([item], now=NOW)
        self.assertEqual(ranked[0].article.kind, "paper")

    def test_layout_and_animation_research_have_creation_context(self):
        papers = [
            article(
                "LayoutShop: Content-Constrained Exploratory Design of Creative Article Layout",
                summary="The algorithm uses two neural networks to optimize geometry and layout.",
                kind="paper",
            ),
            article(
                "GradRig: Differentiable Weights for Skinned Gaussian Splat Deformation",
                summary="Skinning deforms a 3D shape, with support for meshes and a WebGL viewer.",
                kind="paper",
            ),
        ]
        ranked = rank_articles(papers, now=NOW)
        self.assertEqual(len(ranked), 2)
        self.assertTrue(all(item.article.kind == "paper" for item in ranked))

    def test_creative_graphics_demos_are_selected(self):
        demos = [
            article(
                "Drawing With Light: An Exploration of Lit GPU Tubes with TSL and WebGPU",
                kind="showcase",
            ),
            article(
                "Building a Real-Time 3D Face Mask with MediaPipe, Threlte and Three.js",
                kind="showcase",
            ),
            article(
                "Building an Infinite Loom: Unravelling Images into Threads with Three.js",
                kind="showcase",
            ),
        ]
        ranked = rank_articles(demos, now=NOW)
        self.assertEqual(len(ranked), 3)
        self.assertTrue(all(item.article.kind == "showcase" for item in ranked))

    def test_general_layout_marketing_and_gpu_benchmarks_are_excluded(self):
        unrelated = [
            article(
                "Exploratory design of a new article layout",
                summary="Our marketing team selected a fresh layout for the campaign.",
            ),
            article("WebGPU compute benchmark makes matrix multiplication faster", kind="paper"),
            article("An AI rigging investigation of an election result", kind="paper"),
        ]
        self.assertEqual(rank_articles(unrelated, now=NOW), [])

    def test_incidental_nft_biography_does_not_promote_ai_design_to_web3(self):
        item = article(
            "Serpentine fellow investigates AI and computational design",
            summary=(
                "The research uses neural networks to explore algorithmic design systems. "
                "The fellow previously worked at an NFT marketplace and a blockchain startup."
            ),
        )
        ranked = rank_articles([item], now=NOW)
        self.assertEqual(ranked[0].priority, 2)

    def test_a_source_sentence_can_establish_actual_web3_design_connection(self):
        item = article(
            "A new way to preserve artworks",
            summary=(
                "The project stores generative art code and its assets onchain for reproducible rendering. "
                "Its JavaScript repository includes an implementation and a demo."
            ),
        )
        self.assertEqual(rank_articles([item], now=NOW)[0].priority, 1)

    def test_practical_web3_implementation_can_outweigh_generic_ai_keyword(self):
        implementation = article(
            "Onchain generative art algorithm and open source implementation",
            summary="The repository includes documentation and a tutorial for creative coding.",
            age=timedelta(days=3),
        )
        generic_ai = article("AI onchain generative art", age=timedelta(minutes=1))
        ranked = rank_articles([generic_ai, implementation], now=NOW)
        self.assertEqual(ranked[0].article, implementation)
        self.assertEqual([item.priority for item in ranked], [1, 1])

    def test_curated_repository_context_establishes_release_topic(self):
        release = Article(
            "fxhash/onchfs · v0.3.0", "https://github.com/fxhash/onchfs/releases/tag/v0.3.0",
            "GitHub · fxhash/onchfs", "Adds a resolver for loading file storage assets from a local gateway.",
            NOW, kind="release", topic_context="Web3 onchain generative art preservation",
        )
        ranked = rank_articles([release], now=NOW)
        self.assertEqual(ranked[0].priority, 1)
        self.assertEqual(ranked[0].article.kind, "release")

    def test_news_cannot_claim_a_topic_through_release_metadata(self):
        unrelated = Article(
            "New office in Berlin", "https://example.com/office", "Example",
            "The company opens a new workspace in Berlin.", NOW,
            topic_context="Web3 onchain generative art preservation",
        )
        self.assertEqual(rank_articles([unrelated], now=NOW), [])


if __name__ == "__main__":
    unittest.main()
