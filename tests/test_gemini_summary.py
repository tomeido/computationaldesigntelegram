import asyncio
import json
from datetime import UTC, datetime

import httpx
import pytest

from compdesign_bot.errors import SummaryError
from compdesign_bot.gemini_summary import (
    DEFAULT_GEMINI_MODEL,
    GeminiSummarizer,
    GeminiUnavailable,
    cache_namespace,
)
from compdesign_bot.local_summary import CACHE_NAMESPACE
from compdesign_bot.models import Article, RankedArticle

KEY = "test-private-key-not-for-logs"
TITLE = "Computational design research"
FACT = "The open-source toolkit creates generative art on the blockchain."


def item(title=TITLE, excerpt=FACT, *, kind="news"):
    return RankedArticle(
        Article(title, "https://example.com/research", "Research", excerpt, datetime.now(UTC), kind=kind),
        1,
        10,
        "Web3 × 디자인",
    )


def result(translations, *, finish_reason="STOP"):
    return {
        "candidates": [
            {
                "finishReason": finish_reason,
                "content": {"parts": [{"text": json.dumps({"translations": translations})}]},
            }
        ]
    }


def run_summary(handler, ranked=None, *, model=DEFAULT_GEMINI_MODEL):
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=True) as client:
            return await GeminiSummarizer(client, api_key=KEY, model=model).summarize(ranked or item())

    return asyncio.run(run())


def test_translates_only_selected_text_in_single_request_and_keeps_key_in_header():
    requests = []

    def handler(request):
        requests.append(request)
        assert request.url.host == "generativelanguage.googleapis.com"
        assert request.url.scheme == "https"
        assert not request.url.query
        assert KEY not in str(request.url)
        assert request.headers["x-goog-api-key"] == KEY
        payload = json.loads(request.content)
        assert KEY not in request.content.decode()
        assert "https://example.com/research" not in request.content.decode()
        assert "tools" not in payload
        instruction = payload["systemInstruction"]["parts"][0]["text"]
        assert "Keep monetary expressions verbatim" in instruction
        assert "Do not convert amounts, scale units or currencies" in instruction
        texts = json.loads(payload["contents"][0]["parts"][0]["text"])["texts"]
        assert texts == [TITLE, FACT]
        config = payload["generationConfig"]
        assert config["thinkingConfig"] == {"thinkingLevel": "MINIMAL"}
        assert config["maxOutputTokens"] == 1024
        assert config["responseJsonSchema"]["properties"]["translations"]["maxItems"] == 2
        return httpx.Response(
            200, json=result(["컴퓨테이셔널 디자인 연구", "오픈소스 도구로 블록체인 생성형 예술을 만듭니다."])
        )

    summary = run_summary(handler, item(excerpt=FACT + " Subscribe to our newsletter."))
    assert len(requests) == 1
    assert summary.title == "컴퓨테이셔널 디자인 연구"
    assert summary.bullets == ("오픈소스 도구로 블록체인 생성형 예술을 만듭니다.",)
    assert "Gemini" in summary.evidence


def test_korean_original_requires_no_api_request():
    def unexpected(_request):
        pytest.fail("Korean text must not be sent for translation")

    summary = run_summary(
        unexpected,
        item("블록체인 생성형 예술 연구", "오픈소스 도구로 온체인 생성형 예술을 만드는 방법을 소개합니다."),
    )
    assert summary.title == "블록체인 생성형 예술 연구"
    assert "한국어 원문" in summary.evidence
    assert "Gemini" not in summary.evidence


def test_release_masks_code_before_translation_and_restores_exact_names_afterwards():
    title = "ArtBlocks/tool release 1.4.0"
    fact = "Added ITransferHook interface support for onchain transfer hooks and GPU mesh rendering."

    def handler(request):
        payload = json.loads(request.content)
        texts = json.loads(payload["contents"][0]["parts"][0]["text"])["texts"]
        assert "ITransferHook" not in " ".join(texts)
        assert "ArtBlocks/tool" not in " ".join(texts)
        assert texts[0] == "__CDREF_A__ release __CDREF_B__"
        assert "Copy every such token exactly once" in payload["systemInstruction"]["parts"][0]["text"]
        return httpx.Response(
            200,
            json=result(
                [
                    "__CDREF_A__ __CDREF_B__ 출시",
                    "__CDREF_A__ 인터페이스와 __CDREF_B__ 메시 렌더링을 온체인 전송 훅에 지원합니다.",
                ]
            ),
        )

    summary = run_summary(handler, item(title, fact, kind="release"))
    assert summary.title == "ArtBlocks/tool 1.4.0 출시"
    assert "ITransferHook" in summary.bullets[0]
    assert "GPU" in summary.bullets[0]
    assert "CDREF" not in str(summary)


def test_release_model_cannot_replace_an_api_with_a_different_name():
    title = "새로운 메시 도구"
    fact = "Added ITransferHook interface support for onchain transfer hooks and mesh rendering."
    with pytest.raises(SummaryError, match="식별자"):
        run_summary(
            lambda _: httpx.Response(200, json=result(["IWorkbook 인터페이스를 지원합니다."])),
            item(title, fact, kind="release"),
        )


def test_mixed_language_sends_only_foreign_text():
    def handler(request):
        payload = json.loads(request.content)
        assert json.loads(payload["contents"][0]["parts"][0]["text"])["texts"] == [FACT]
        return httpx.Response(200, json=result(["도구가 블록체인 생성형 예술을 만듭니다."]))

    summary = run_summary(handler, item("생성형 예술 연구", FACT))
    assert summary.title == "생성형 예술 연구"


def test_title_only_translates_once_and_marks_limited_evidence():
    def handler(request):
        payload = json.loads(request.content)
        assert json.loads(payload["contents"][0]["parts"][0]["text"])["texts"] == [TITLE]
        return httpx.Response(200, json=result(["컴퓨테이셔널 디자인 연구"]))

    summary = run_summary(handler, item(excerpt=""))
    assert summary.bullets == (summary.title,)
    assert "제목만 확인" in summary.evidence


@pytest.mark.parametrize(
    "translations", [[], ["한국어 하나"], ["하나", "둘", "셋"], [12, "문장"], ["", "문장"], "문장"]
)
def test_rejects_malformed_translation_array(translations):
    with pytest.raises(SummaryError, match="응답 형식"):
        run_summary(lambda _: httpx.Response(200, json=result(translations)))


@pytest.mark.parametrize("reason", ["MAX_TOKENS", "SAFETY", "RECITATION", "OTHER", None])
def test_rejects_incomplete_or_refused_output(reason):
    with pytest.raises(SummaryError, match="응답 형식"):
        run_summary(lambda _: httpx.Response(200, json=result(["연구", "문장"], finish_reason=reason)))


@pytest.mark.parametrize(
    "body",
    [
        None,
        {},
        [],
        {"candidates": [None]},
        {"candidates": []},
        {"candidates": [{"finishReason": "STOP", "content": {"parts": []}}]},
    ],
)
def test_rejects_malformed_provider_envelope(body):
    with pytest.raises(SummaryError, match="응답 형식"):
        run_summary(lambda _: httpx.Response(200, json=body))


@pytest.mark.parametrize(
    "translations",
    [
        ["Design research", "오픈소스 도구입니다."],
        ["컴퓨테이셔널 디자인 연구", "이 도구는 999개의 작품을 만듭니다."],
        ["컴퓨테이셔널 디자인 연구", "리뷰 보기를 눌러 주세요."],
    ],
)
def test_rejects_non_korean_and_detectable_fabrication(translations):
    with pytest.raises(SummaryError):
        run_summary(lambda _: httpx.Response(200, json=result(translations)))


@pytest.mark.parametrize("status", [301, 400, 401, 403, 404, 429, 500, 503])
def test_http_failure_stops_digest_without_retry_redirect_or_secret_leak(status):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(status, headers={"Location": "https://example.com/secret"}, text=KEY)

    with pytest.raises(GeminiUnavailable) as exc:
        run_summary(handler)
    assert len(calls) == 1
    assert str(status) in str(exc.value)
    assert KEY not in str(exc.value)


def test_transport_error_hides_request_details():
    def handler(request):
        raise httpx.ReadTimeout(KEY, request=request)

    with pytest.raises(GeminiUnavailable) as exc:
        run_summary(handler)
    assert KEY not in str(exc.value)
    assert exc.value.__suppress_context__


def test_model_id_cannot_change_endpoint_or_inject_key_query():
    with pytest.raises(GeminiUnavailable, match="모델 ID"):
        run_summary(lambda _: pytest.fail("Unexpected network"), model="gemini-a/../../x?key=secret")


def test_cache_is_separate_from_local_and_other_models():
    assert cache_namespace() != CACHE_NAMESPACE
    assert cache_namespace() != cache_namespace("gemini-2.5-flash-lite")


def test_long_foreign_title_is_rejected_before_network():
    with pytest.raises(SummaryError, match="너무 깁니다"):
        run_summary(lambda _: pytest.fail("Unexpected network"), item("Design " * 100))


def test_paper_sends_only_locally_selected_method_and_result_for_translation():
    title = "Parametric surface reconstruction"
    intro = "Computational design and generative algorithms are useful for creative geometry research."
    method = "We propose a differentiable renderer for parametric surfaces."
    result_sentence = "Experiments report 12% lower reconstruction error."
    requests = []

    def handler(request):
        requests.append(request)
        payload = json.loads(request.content)
        texts = json.loads(payload["contents"][0]["parts"][0]["text"])["texts"]
        assert texts == [title, method, result_sentence]
        assert "tools" not in payload
        return httpx.Response(
            200,
            json=result(
                [
                    "파라메트릭 곡면 복원",
                    "파라메트릭 곡면을 위한 미분 가능 렌더러를 제안합니다.",
                    "실험에서 복원 오차가 12% 낮게 나타났습니다.",
                ]
            ),
        )

    summary = run_summary(handler, item(title, f"{intro} {method} {result_sentence}", kind="paper"))
    assert len(requests) == 1
    assert len(summary.bullets) == 2
    assert "12%" in summary.bullets[1]


def test_paper_geometry_and_author_attribution_are_corrected_without_inventing_names():
    title = "Models in the Wild"
    fact = "We propose a computational framework for non-manifold and non-watertight meshes."

    def handler(request):
        payload = json.loads(request.content)
        instruction = payload["systemInstruction"]["parts"][0]["text"]
        assert "계산 프레임워크 for computational framework" in instruction
        assert "비다양체 for non-manifold" in instruction
        assert "authors' we as 연구진은" in instruction
        assert "Do not invent author names" in instruction
        return httpx.Response(
            200,
            json=result(
                [
                    "야생의 모델",
                    "우리는 비다중 및 방수가 되지 않는 메시를 위한 컴퓨테이셔널 디자인 프레임워크를 제안합니다.",
                ]
            ),
        )

    summary = run_summary(handler, item(title, fact, kind="paper"))
    assert summary.title == "실제 환경의 모델"
    assert summary.bullets == (
        "연구진은 비다양체 및 밀폐되지 않은 메시를 위한 계산 프레임워크를 제안합니다.",
    )


@pytest.mark.parametrize("kind", ["paper", "news", "funding", "showcase"])
def test_gemini_paper_bullets_allow_full_technical_sentence_without_widening_other_kinds(kind):
    fact = (
        "연구진은 웹 브라우저에서 기하학적 제약 조건과 복잡한 메시 구조를 직접 조작할 수 있도록 "
        "미분 가능한 렌더링 방식과 실시간 최적화 알고리즘을 결합한 프레임워크를 제안하며 "
        "별도 프로그램 설치 없이 WebGL 환경에서 결과를 확인할 수 있도록 구현했습니다."
    )
    assert 120 < len(fact) <= 160
    summary = run_summary(
        lambda _: pytest.fail("Korean source requires no translation"),
        item("기하학 프레임워크 연구", fact, kind=kind),
    )
    if kind == "paper":
        assert summary.bullets == (fact,)
    else:
        assert len(summary.bullets[0]) == 120
        assert summary.bullets[0].endswith("…")
