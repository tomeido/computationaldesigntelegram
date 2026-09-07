import asyncio
import io
import json
import zipfile
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

import httpx
import pytest

from compdesign_bot.errors import SummaryError
from compdesign_bot.local_summary import (
    MODEL_FILES,
    MODEL_METADATA,
    LocalSummarizer,
    OfflineTranslator,
    _download_model,
    _terms_in_korean,
    check_model,
    extract_sentences,
    setup_model,
    validate_translation,
)
from compdesign_bot.models import Article, RankedArticle


def item(title="Computational design research", excerpt="", *, kind="news"):
    return RankedArticle(
        Article(title, "https://example.com/research", "Research", excerpt, datetime.now(UTC), kind=kind),
        1,
        10,
        "Web3 × 디자인",
    )


class FakeTranslator:
    def __init__(self, answers):
        self.answers = answers
        self.calls = []

    def translate(self, text):
        self.calls.append(text)
        return self.answers[text]


def test_offline_summary_translates_only_source_facts_and_makes_no_http_calls():
    title = "Computational design research"
    fact = "The open-source toolkit creates generative art on the blockchain."
    translator = FakeTranslator(
        {title: "컴퓨테이셔널 디자인 연구", fact: "도구가 블록체인 생성형 예술을 만듭니다."}
    )

    async def run():
        def unexpected_http(_request):
            pytest.fail("Offline summarization must never make HTTP calls")

        async with httpx.AsyncClient(transport=httpx.MockTransport(unexpected_http)) as client:
            return await LocalSummarizer(client, translator=translator).summarize(item(title, fact))

    summary = asyncio.run(run())
    assert summary.title == "컴퓨테이셔널 디자인 연구"
    assert summary.bullets == ("도구가 블록체인 생성형 예술을 만듭니다.",)
    assert translator.calls == [title, fact]
    assert "RSS 발췌·기계번역" in summary.evidence
    assert "원문" in summary.why


def test_korean_items_require_no_translation_model():
    title = "블록체인 생성형 예술 연구"
    fact = "오픈소스 도구로 온체인 생성형 예술을 만드는 방법을 소개합니다."
    translator = FakeTranslator({})
    summary = asyncio.run(LocalSummarizer(translator=translator).summarize(item(title, fact)))
    assert summary.title == title
    assert summary.bullets == (fact,)
    assert translator.calls == []
    assert "한국어 원문" in summary.evidence
    assert "기계번역" not in summary.evidence


def test_title_only_items_do_not_claim_to_have_read_the_article():
    title = "Generative art on the blockchain"
    translator = FakeTranslator({title: "블록체인 생성형 예술"})
    summary = asyncio.run(LocalSummarizer(translator=translator).summarize(item(title)))
    assert summary.bullets == (summary.title,)
    assert translator.calls == [title]
    assert "제목만 확인" in summary.evidence
    assert "RSS" not in summary.evidence


def test_extraction_prefers_relevant_sentences_without_reordering_or_rewriting():
    first = "The new open-source toolkit creates generative art on the blockchain."
    second = "Artists can implement smart contracts using the tutorial."
    excerpt = f"Subscribe to our newsletter. Today is a sunny day outside. {first} {second} Read more here."
    assert extract_sentences("A different title", excerpt) == [first, second]


def test_repeated_headlines_boilerplate_and_long_sentences_fall_back_to_title():
    title = "Computational design research"
    assert extract_sentences(title, f"{title}. Subscribe to our newsletter.") == []
    assert extract_sentences(title, "Long " * 300) == []


def test_repeated_facts_are_selected_once():
    sentence = "The toolkit creates generative art on the blockchain."
    assert extract_sentences("A title", f"{sentence} {sentence}") == [sentence]


def test_english_with_a_single_korean_tag_still_requires_translation():
    title = "Computational design tools and open-source generative art workflows 뉴스"
    translator = FakeTranslator({title: "컴퓨테이셔널 디자인 도구와 생성형 예술 작업 방식"})
    asyncio.run(LocalSummarizer(translator=translator).summarize(item(title)))
    assert translator.calls == [title]


def test_english_only_model_output_fails_closed():
    translator = FakeTranslator({"Research": "Research"})
    with pytest.raises(SummaryError, match="한국어"):
        asyncio.run(LocalSummarizer(translator=translator).summarize(item("Research")))


def test_known_design_terminology_is_normalized_using_source_evidence():
    title = "Generative art on the blockchain"
    translator = FakeTranslator({title: "블록체인의 유전 예술"})
    summary = asyncio.run(LocalSummarizer(translator=translator).summarize(item(title)))
    assert summary.title == "블록체인의 생성형 예술"


def test_long_korean_translation_is_bounded():
    title = "한국어 제목 " * 100
    translator = FakeTranslator({})
    summary = asyncio.run(LocalSummarizer(translator=translator).summarize(item(title)))
    assert len(summary.title) <= 65
    assert len(summary.bullets[0]) <= 120
    assert summary.title.endswith("…")


def test_missing_model_has_actionable_error_and_never_downloads(tmp_path):
    with (
        patch("compdesign_bot.local_summary.importlib.util.find_spec", return_value=object()),
        patch("urllib.request.urlopen", side_effect=AssertionError("unexpected network")),
        pytest.raises(SummaryError, match="setup-translator"),
    ):
        OfflineTranslator(tmp_path).translate("Design research")


def test_missing_runtime_has_actionable_error(tmp_path):
    with (
        patch("compdesign_bot.local_summary.importlib.util.find_spec", return_value=None),
        pytest.raises(SummaryError, match="pip install"),
    ):
        check_model(tmp_path)


def write_model(path: Path, *, target="ko"):
    for name in MODEL_FILES:
        file = path / name
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_text("model data", encoding="utf-8")
    (path / "metadata.json").write_text(json.dumps({**MODEL_METADATA, "to_code": target}), encoding="utf-8")


def test_wrong_language_model_is_rejected(tmp_path):
    write_model(tmp_path, target="es")
    with (
        patch("compdesign_bot.local_summary.importlib.util.find_spec", return_value=object()),
        pytest.raises(SummaryError, match="영어→한국어"),
    ):
        check_model(tmp_path)


def test_setup_is_idempotent_for_existing_model_and_does_not_download(tmp_path):
    write_model(tmp_path)
    with (
        patch("compdesign_bot.local_summary.importlib.util.find_spec", return_value=object()),
        patch("urllib.request.urlopen", side_effect=AssertionError("unexpected download")),
    ):
        assert setup_model(tmp_path) == tmp_path


def test_setup_preserves_unrelated_existing_files(tmp_path):
    existing = tmp_path / "important.txt"
    existing.write_text("preserve this", encoding="utf-8")
    with pytest.raises(SummaryError, match="다른 파일"):
        setup_model(tmp_path)
    assert existing.read_text(encoding="utf-8") == "preserve this"


def test_download_rejects_changed_model_data(tmp_path):
    with (
        patch("urllib.request.urlopen", return_value=io.BytesIO(b"unexpected model content")),
        pytest.raises(SummaryError, match="체크섬"),
    ):
        _download_model(tmp_path / "download.argosmodel")


def test_setup_installs_only_model_data_and_ignores_arbitrary_archive_paths(tmp_path):
    def download(destination):
        with zipfile.ZipFile(destination, "w") as archive:
            for name in MODEL_FILES:
                data = json.dumps({"from_code": "en", "to_code": "ko"}) if name == "metadata.json" else "data"
                archive.writestr(f"translate-fairseq_m2m_100_418M/{name}", data)
            archive.writestr("../escaped.txt", "must not be written")

    target = tmp_path / "installed"
    with (
        patch("compdesign_bot.local_summary._download_model", side_effect=download),
        patch("compdesign_bot.local_summary.importlib.util.find_spec", return_value=object()),
    ):
        setup_model(target)
        check_model(target)
    assert sorted(str(path.relative_to(target)) for path in target.rglob("*") if path.is_file()) == sorted(
        MODEL_FILES
    )
    assert not (tmp_path / "escaped.txt").exists()


@pytest.mark.parametrize(
    ("source", "translation"),
    [
        ("Rhinoceros Masterclass September 12 - 13", "사무국 02-583-3598 / FAX 02-525-3920"),
        (
            "Learn about the new computational design tools.",
            "웹 사이트는 쿠키를 사용하여 귀하의 경험을 향상시킵니다.",
        ),
        ("The source code is released.", "오픈소스가 공개되었습니다. " * 30),
        ("New generative art software release.", "새로운 예술 도구가 공개되었습니다. https://fake.example"),
        ("A new parametric design tutorial.", "파라메트릭 디자인은 매우 매우 매우 흥미롭습니다."),
    ],
)
def test_suspect_translation_additions_are_rejected(source, translation):
    with pytest.raises(SummaryError):
        validate_translation(source, translation)


def test_guard_allows_source_dates_and_english_month_to_digit_translation():
    validate_translation("September 25, 2026 - last 2 weeks", "2026년 9월 25일 - 마지막 2주")


def test_rejected_body_translation_falls_back_to_title_with_honest_evidence():
    title = "Computational design course"
    sentence = "The course covers Rhino and new parametric tools."
    translator = FakeTranslator(
        {title: "컴퓨테이셔널 디자인 강좌", sentence: "전화 02-123-4567로 문의하세요."}
    )
    summary = asyncio.run(LocalSummarizer(translator=translator).summarize(item(title, sentence)))
    assert summary.bullets == (summary.title,)
    assert "제목만 확인" in summary.evidence
    assert "02-123" not in str(summary)


def test_rejected_title_translation_prevents_entire_post():
    title = "New computational design course"
    translator = FakeTranslator({title: "컴퓨테이셔널 디자인 강좌 수강료는 500달러입니다."})
    with pytest.raises(SummaryError):
        asyncio.run(LocalSummarizer(translator=translator).summarize(item(title)))


def test_extraction_skips_malformed_schedule_tables_and_tool_lists():
    excerpt = (
        "Rhino Sep 12 | 07:00 PST | Grasshopper Oct 24 | 08:00 CET. "
        "If you're into Rhino, Grasshopper, Python, rhino3dm, Hops, SubD, and the Code Editor... "
        "These courses teach computational design using practical projects."
    )
    assert extract_sentences("Courses", excerpt) == [
        "These courses teach computational design using practical projects."
    ]


def test_paper_extraction_pairs_method_with_results_instead_of_generic_introduction():
    introduction = (
        "Computational design and generative geometry research use algorithms and machine learning."
    )
    method = "We propose a differentiable renderer for optimizing parametric surfaces."
    elaboration = "We introduce a new algorithm for computational geometry and generative design."
    result = "Experiments reduce reconstruction error by 12%, but thin structures remain a limitation."
    excerpt = f"{introduction} {method} {elaboration} {result}"
    selected = extract_sentences("Surface reconstruction", excerpt, kind="paper")
    assert result in selected
    assert introduction not in selected
    assert len(selected) == 2
    assert selected[0] in (method, elaboration)
    assert all(sentence in excerpt for sentence in selected)


def test_funding_extraction_keeps_announced_amount_and_application_terms_verbatim():
    introduction = "Generative design and AI research are transforming computational design workflows."
    announcement = "Mesh Lab secured a $2 million seed round for its parametric design tool."
    terms = "The grant accepts applications from open-source maintainers until September 30, 2026."
    excerpt = f"{introduction} {announcement} {terms}"
    assert extract_sentences("Mesh Lab announcement", excerpt, kind="funding") == [announcement, terms]


def test_showcase_extraction_keeps_how_it_works_and_demo_details():
    introduction = "Generative art and computational design inspire creative coding and AI research."
    technique = "The artwork uses a shader to map live weather readings onto moving geometric shapes."
    demo = "Visitors can try the interactive demo in a browser and download the source code."
    excerpt = f"{introduction} {technique} {demo}"
    assert extract_sentences("Weather artwork", excerpt, kind="showcase") == [technique, demo]


def test_extraction_prefers_self_contained_sentences_over_missing_pronoun_context():
    dangling = "His research combines generative art and computational design on the blockchain."
    fact = "The Mesh toolkit creates parametric surfaces from hand-drawn curves."
    detail = "A public tutorial explains the implementation with working examples."
    assert extract_sentences("Mesh toolkit", f"{dangling} {fact} {detail}") == [fact, detail]


def test_paper_extraction_does_not_invent_missing_results_or_rewrite_pronouns():
    method = "We propose a method for creating procedural geometry from sketches."
    assert extract_sentences("Sketch geometry", method, kind="paper") == [method]


def test_local_summarizer_uses_paper_selection_before_translation():
    title = "Parametric surface reconstruction"
    intro = "Computational design and AI algorithms support generative research."
    method = "We propose a differentiable renderer for parametric surfaces."
    result = "Experiments report 12% lower reconstruction error."
    translator = FakeTranslator(
        {
            title: "파라메트릭 곡면 복원",
            method: "파라메트릭 곡면을 위한 미분 가능 렌더러를 제안합니다.",
            result: "실험에서 복원 오차가 12% 낮게 나타났습니다.",
        }
    )
    summary = asyncio.run(
        LocalSummarizer(translator=translator).summarize(
            item(title, f"{intro} {method} {result}", kind="paper")
        )
    )
    assert translator.calls == [title, method, result]
    assert summary.bullets == (translator.answers[method], translator.answers[result])


@pytest.mark.parametrize(
    ("source", "translation", "expected"),
    [
        ("Models in the Wild", "야생의 모델", "실제 환경의 모델"),
        ("non-manifold geometry", "비다중 기하", "비다양체 기하"),
        ("non-watertight meshes", "방수가 되지 않는 메시", "밀폐되지 않은 메시"),
        ("a computational framework", "컴퓨테이셔널 디자인 프레임워크", "계산 프레임워크"),
    ],
)
def test_geometry_terms_are_corrected_only_with_corresponding_source_term(source, translation, expected):
    assert _terms_in_korean(source, translation) == expected
    assert _terms_in_korean("An unrelated source sentence", translation) == translation


def test_authorial_pronouns_are_attributed_only_in_papers_and_when_present_in_source():
    assert _terms_in_korean("We propose a method.", "우리는 방법을 제안합니다.", kind="paper") == (
        "연구진은 방법을 제안합니다."
    )
    assert _terms_in_korean("Our method improves accuracy.", "우리의 방법은 정확도를 높입니다.", kind="paper") == (
        "연구진의 방법은 정확도를 높입니다."
    )
    assert _terms_in_korean("We opened the studio.", "우리는 스튜디오를 열었습니다.") == (
        "우리는 스튜디오를 열었습니다."
    )
    assert _terms_in_korean("An unrelated source.", "우리는 방법을 제안합니다.", kind="paper") == (
        "우리는 방법을 제안합니다."
    )


@pytest.mark.parametrize("kind", ["paper", "news", "funding", "showcase"])
def test_local_paper_bullets_allow_full_technical_sentence_without_widening_other_kinds(kind):
    fact = (
        "연구진은 웹 브라우저에서 기하학적 제약 조건과 복잡한 메시 구조를 직접 조작할 수 있도록 "
        "미분 가능한 렌더링 방식과 실시간 최적화 알고리즘을 결합한 프레임워크를 제안하며 "
        "별도 프로그램 설치 없이 WebGL 환경에서 결과를 확인할 수 있도록 구현했습니다."
    )
    assert 120 < len(fact) <= 160
    summary = asyncio.run(
        LocalSummarizer(translator=FakeTranslator({})).summarize(item("기하학 프레임워크 연구", fact, kind=kind))
    )
    if kind == "paper":
        assert summary.bullets == (fact,)
    else:
        assert len(summary.bullets[0]) == 120
        assert summary.bullets[0].endswith("…")
