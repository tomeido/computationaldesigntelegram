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


def item(title=TITLE, excerpt=FACT):
    return RankedArticle(
        Article(title, "https://example.com/research", "Research", excerpt, datetime.now(UTC)),
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
