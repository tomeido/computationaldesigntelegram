from datetime import UTC, datetime

import pytest

from compdesign_bot.models import Article, ArticleLink, RankedArticle
from compdesign_bot.quality import assess_quality, select_quality, select_release_changes


def candidate(title, summary="", *, kind="news", **kwargs):
    return Article(
        title,
        "https://example.com/source",
        "Primary source",
        summary,
        datetime(2026, 9, 7, tzinfo=UTC),
        kind=kind,
        **kwargs,
    )


@pytest.mark.parametrize("kind", ["news", "paper", "funding", "showcase", "release"])
def test_headlines_alone_cannot_be_automatically_published(kind):
    title = "New computational design algorithm introduces neural network mesh generation"
    assert not assess_quality(candidate(title, kind=kind)).accepted
    assert not assess_quality(candidate(title, title, kind=kind)).accepted


@pytest.mark.parametrize(
    "title,abstract",
    [
        (
            "WildFab: Collision-free multi-axis printing for shapes in the wild",
            (
                "We introduce a framework that computes collision-free toolpaths for multi-axis 3D printing. "
                "The method evaluates candidate fabrication directions on diverse geometric models."
            ),
        ),
        (
            "LayoutShop: Content-Constrained Exploratory Design of Creative Article Layout",
            (
                "Our system uses two neural networks to optimize article layout under content constraints. "
                "The optimization generates multiple layouts while preserving typographic hierarchy."
            ),
        ),
        (
            "GradRig: Differentiable weights for skinned Gaussian deformation",
            (
                "The method computes skinning weights for editable mesh deformation and Gaussian splatting. "
                "Experiments compare the deformation accuracy across multiple articulated shapes."
            ),
        ),
    ],
)
def test_paper_abstracts_with_actual_methods_are_eligible_without_a_repository(title, abstract):
    decision = assess_quality(candidate(title, abstract, kind="paper"))
    assert decision.accepted
    assert "초록" in decision.reason


@pytest.mark.parametrize(
    "abstract",
    [
        "This exciting paper will transform the future of computational design for every creative professional.",
        "We present a new vision for the future of AI design and discuss opportunities for creators everywhere.",
        "Keywords: algorithm, neural network, mesh, fabrication, optimization, computational design, paper.",
    ],
)
def test_generic_paper_promises_or_keyword_lists_are_not_a_method(abstract):
    assert not assess_quality(candidate("Future computational design", abstract, kind="paper")).accepted


def test_paper_repository_improves_completeness_but_cannot_replace_an_abstract():
    link = ArticleLink("코드", "https://github.com/example/research")
    abstract = "Our method generates mesh geometry using a differentiable optimization algorithm."
    plain = candidate("Differentiable geometry", abstract, kind="paper")
    linked = candidate("Differentiable geometry", abstract, kind="paper", links=(link,))
    assert assess_quality(linked).score > assess_quality(plain).score
    assert not assess_quality(candidate("Differentiable geometry", kind="paper", links=(link,))).accepted


def test_codrops_style_implementation_excerpt_is_useful():
    item = candidate(
        "Building an Infinite Loom: Unravelling Images into Threads with Three.js",
        "Learn how to build an interactive image loom using Three.js and a custom shader. "
        "The shader transforms image pixels into animated ribbons with adjustable geometry.",
        kind="showcase",
    )
    assert assess_quality(item).accepted


def test_short_korean_technical_excerpt_is_not_measured_by_english_word_count():
    item = candidate(
        "위상 최적화로 제작하는 가벼운 격자 구조",
        "연구진은 위상 최적화 알고리즘을 사용해 삼차원 프린팅에 적합한 격자 구조를 생성하고 계산 시간을 줄였습니다.",
        kind="paper",
    )
    assert assess_quality(item).accepted


@pytest.mark.parametrize(
    "title,body",
    [
        (
            "Rhino computational design courses",
            (
                "Register for our Grasshopper workshop to learn parametric geometry "
                "and use computational design tools. Early bird tickets are available now."
            ),
        ),
        (
            "A revolutionary computational design platform",
            (
                "Unlock new possibilities and transform the future of "
                "your creative workflows with our groundbreaking platform for everyone."
            ),
        ),
        (
            "Weekly creative coding digest",
            (
                "This week we introduce a shader algorithm that generates interactive "
                "mesh geometry, along with many other links from across the internet."
            ),
        ),
    ],
)
def test_registration_marketing_and_digests_are_held_for_review(title, body):
    assert not assess_quality(candidate(title, body)).accepted


def test_technical_workshop_recording_is_not_disqualified_by_a_registration_footer():
    item = candidate(
        "Grasshopper workshop recording: mesh relaxation",
        "The recording implements a mesh relaxation solver using geometric constraints and Python. "
        "Register for our next workshop to meet the authors.",
    )
    assert assess_quality(item).accepted


@pytest.mark.parametrize(
    "title,body",
    [
        (
            "Rhino Developer Meeting Barcelona - last 2 weeks to register",
            (
                "The developers will show how to build mesh geometry and use Python for procedural modeling. "
                "Grab your ticket: general admission costs 95 euros."
            ),
        ),
        (
            "More Three.js Speakers, More Ideas from Paris",
            (
                "New voices from the first Three.js Conference share what they're building, experimenting with, "
                "and bringing to Paris."
            ),
        ),
        (
            "Computational design workshop",
            "The workshop uses a mesh solver to create procedural geometry. The recording will be available soon.",
        ),
    ],
)
def test_upcoming_event_program_is_not_published_learning_material(title, body):
    assert not assess_quality(candidate(title, body)).accepted


def test_layoutshop_actual_method_verbs_are_understood():
    body = (
        "Our algorithm then determines the geometry of the extracted layout structures to frame the given "
        "article via an optimization approach. We then employ two neural networks for layout assessment, "
        "and the high-quality outputs are returned to users for selection."
    )
    assert assess_quality(candidate("LayoutShop", body, kind="paper")).accepted


@pytest.mark.parametrize(
    "body",
    [
        (
            "The Codrops mark, rebuilt as a draggable solid and printed in ASCII on the GPU, where every character "
            "cell searches 95 glyphs for its own."
        ),
        "How a meshline habit turned into a tube renderer, and the three ways I got the maths wrong along the way.",
        (
            "A hands-on exploration of how Three.js, shaders, and codec-inspired techniques can turn a scene transition "
            "into a convincing real-time datamosh effect."
        ),
        (
            "A playful Three.js experiment that turns the Eiffel Tower into a catapult, combining interactive physics "
            "with real-world 3D data from Cesium ion to launch yourself across Paris toward the Three.js conference."
        ),
        (
            "A mesmerizing Three.js experiment where glass, wandering cubes, caustics, refractions, and sound come "
            "together in a fluid, interactive scene."
        ),
    ],
)
def test_concrete_codrops_techniques_are_not_rejected_for_conversational_verbs(body):
    assert assess_quality(candidate("A creative coding experiment", body, kind="showcase")).accepted


def test_artblocks_transfer_hooks_release_has_actual_named_capabilities_and_changes():
    capability = (
        "4b5167e: Core v3.3: per-project transfer hooks on the V3 Engine (v3.3.0) and Engine Flex (v3.3.1) "
        "cores, with the ITransferHook interface, the AbstractTransferHook base, and the "
        "OwnerHistoryTransferHook reference implementation."
    )
    change = (
        "Adds the ProjectTransferHookUpdated and ProjectTransferHookLocked events and the "
        "FIELD_PROJECT_TRANSFER_HOOK / FIELD_PROJECT_TRANSFER_HOOK_LOCKED ProjectUpdated fields, "
        "which downstream indexers need."
    )
    item = candidate("ArtBlocks contracts 1.4.0", f"{capability} {change}", kind="release")
    assert assess_quality(item).accepted
    assert select_release_changes(item.summary) == [capability, change]


@pytest.mark.parametrize(
    "title,body",
    [
        (
            "Generative CAD startup raises $20 million",
            (
                "Mesh Studio raised $20 million in a Series A round led by "
                "Example Ventures to expand its parametric CAD engineering tools."
            ),
        ),
        (
            "Creative coding artist grants open",
            (
                "The artist grant program opens applications until October 1 "
                "for creators developing interactive artwork with code."
            ),
        ),
        (
            "Geometry tools acquisition",
            (
                "Example Tools acquired Mesh Studio to add its geometry processing "
                "and simulation tools to the engineering product suite."
            ),
        ),
    ],
)
def test_funding_requires_reported_event_and_concrete_details(title, body):
    assert assess_quality(candidate(title, body, kind="funding")).accepted


def test_funding_discovery_title_and_general_market_commentary_are_insufficient():
    title = "Generative CAD startup raises $20 million"
    assert not assess_quality(candidate(title, kind="funding")).accepted
    body = "Investment in computational design continues to grow as more investors explore creative tools."
    assert not assess_quality(candidate(title, body, kind="funding")).accepted


@pytest.mark.parametrize("kind", ["news", "paper", "funding"])
def test_discovery_excerpt_repeating_headline_and_publisher_is_still_title_only(kind):
    title = "Generative CAD startup raises $20 million to build parametric geometry tools"
    body = title + " - Computational Design Industry News"
    assert not assess_quality(candidate(title, body, kind=kind)).accepted


def test_grant_application_discussion_is_not_an_announced_support_program():
    item = candidate(
        "Creative coding grant application guide",
        "The report discusses how artist grant applications can help creators finance their work "
        "and gives general advice about eligibility and proposals.",
        kind="funding",
    )
    assert not assess_quality(item).accepted


@pytest.mark.parametrize(
    "changes",
    [
        (
            "## Changes\n- chore: update dependencies for the geometry package\n- ci: add Python matrix tests\n"
            "- Fix README typo describing the new shader algorithm\n- Full Changelog: v1.2.2...v1.2.3"
        ),
        "v1.2.3\nThanks to all contributors for supporting the Python computational design community.",
        (
            "- Bump dependencies to fix shader package tests\n- New contributors: example\n"
            "- style: improve formatting of the mesh implementation"
        ),
    ],
)
def test_repository_context_and_notes_cannot_promote_release_maintenance_noise(changes):
    item = candidate(
        "compas-dev/compas · 2.15.0",
        changes,
        kind="release",
        topic_context="Computational design framework for mesh geometry and fabrication",
        curator_note="파이썬으로 기하 모델을 만들 수 있습니다.",
        links=(ArticleLink("코드", "https://github.com/compas-dev/compas"),),
        license="MIT",
    )
    assert not assess_quality(item).accepted


@pytest.mark.parametrize(
    "changes",
    [
        "- Added support for differentiable mesh queries and faster BVH construction on the GPU.",
        "- Fixed incorrect mesh normals returned by the ray intersection algorithm on degenerate geometry.",
        "- docs: Add an example rendering interactive shader particles using a WebGL framebuffer.",
        "- Enable storing a generative artwork's JavaScript dependencies using onchain file storage.",
    ],
)
def test_release_features_reproducible_examples_and_user_visible_fixes_pass(changes):
    assert assess_quality(candidate("Repository · 1.2.3", changes, kind="release")).accepted


def test_useful_release_change_survives_surrounding_maintenance_noise():
    item = candidate(
        "processing/p5.js · 2.4.1",
        "## Changes\n- chore: bump dependencies\n"
        "- Fix a WebGL framebuffer crash when creating a texture on Safari browsers.\n"
        "- Thanks to all contributors\n- Full Changelog: v2.4.0...v2.4.1",
        kind="release",
    )
    assert assess_quality(item).accepted


def test_release_selection_preserves_numbers_and_api_identifiers_in_source_order():
    first = "Added wp.tile_load support for 3D arrays with 16-bit elements on the GPU."
    second = "Fix p5.Framebuffer texture dimensions when the pixel density is set to 2."
    excerpt = f"chore: update Python dependencies for geometry tools.\n- {first}\n- {second}"
    assert select_release_changes(excerpt) == [first, second]


def test_release_gate_holds_unextractable_long_changes_for_review():
    change = (
        "Added support for new procedural geometry rendering and shader effects using "
        + "a configurable parametric particle simulation framework with " * 6
        + "explicit constraints."
    )
    assert len(change) > 320
    assert select_release_changes(change) == []
    assert not assess_quality(candidate("Repository · 1.2.3", change, kind="release")).accepted


def test_quality_selection_keeps_hard_topic_order_and_rejection_reason():
    web3 = RankedArticle(
        candidate(
            "Onchain generative art renderer",
            "The renderer uses onchain file storage to load generative art code and its texture assets.",
        ),
        1,
        10.0,
        "Web3 × 디자인",
    )
    thin = RankedArticle(candidate("AI computational design will change everything"), 2, 30.0, "AI × 디자인")
    general = RankedArticle(
        candidate(
            "Mesh geometry tool",
            "The implementation generates procedural mesh geometry using a constraint solver in Python.",
        ),
        3,
        12.0,
        "컴퓨테이셔널 디자인",
    )
    accepted, rejected = select_quality([web3, thin, general])
    assert accepted == [web3, general]
    assert rejected[0][0] == thin
    assert "제목" in rejected[0][1].reason
