import json
import re
from dataclasses import replace

import httpx
import pytest

from compdesign_bot.config import Settings
from compdesign_bot.mail_localization import (
    LANGUAGE_CODES,
    LANGUAGE_NAMES,
    MailLocalizationUnavailable,
    MailLocalizer,
    language_copy,
    normalize_language,
)


class MemoryStore:
    def __init__(self):
        self.cache = {}

    def get_localized_post(self, post_key, language, source_key):
        return self.cache.get((post_key, language, source_key))

    def cache_localized_post(self, post_key, language, source_key, title, html):
        self.cache[post_key, language, source_key] = (title, html)


def settings():
    return Settings(translation_provider="gemini", gemini_api_key="PRIVATE_API_KEY")


def response(translations):
    return httpx.Response(
        200,
        json={
            "candidates": [
                {
                    "finishReason": "STOP",
                    "content": {"parts": [{"text": json.dumps({"translations": translations})}]},
                }
            ]
        },
    )


def client_for(translate, requests):
    def handle(request):
        payload = json.loads(request.content)
        requests.append(payload)
        texts = json.loads(payload["contents"][0]["parts"][0]["text"])["texts"]
        return response(translate(texts))

    return httpx.Client(transport=httpx.MockTransport(handle))


@pytest.mark.parametrize(
    "value,expected",
    [
        ("ko-KR", "ko"),
        ("한국어", "ko"),
        ("English", "en"),
        (" en_US ", "en"),
        ("日本語", "ja"),
        ("zh-CN", "zh"),
        ("Deutsch", "de"),
        ("Français", "fr"),
        ("Español", "es"),
        ("pt-BR", "pt"),
        ("ko/en", "bilingual"),
    ],
)
def test_language_aliases(value, expected):
    assert normalize_language(value) == expected


@pytest.mark.parametrize("value", [None, "", "ru", "PRIVATE_UNSUPPORTED_LANGUAGE"])
def test_unsupported_language_does_not_echo_input(value):
    with pytest.raises(ValueError) as error:
        normalize_language(value)
    assert "PRIVATE_UNSUPPORTED_LANGUAGE" not in str(error.value)


def test_all_languages_have_native_labels_complete_copy_and_count_template():
    assert set(LANGUAGE_NAMES) == LANGUAGE_CODES
    for code in LANGUAGE_CODES:
        copy = language_copy(code)
        assert set(copy) == {
            "html_lang",
            "invite",
            "subscribe",
            "share_hint",
            "unsubscribe",
            "unsubscribe_hint",
            "preferences",
            "preferences_hint",
            "digest_subject",
        }
        assert "3" in copy["digest_subject"].format(count=3)
    assert (
        language_copy("ko")["unsubscribe_hint"]
        == "링크를 열고 봇의 안내에 따라 /unsubscribe를 보내면 해지됩니다."
    )
    assert "Unsubscribe" in language_copy("bilingual")["unsubscribe"]


def test_korean_passthrough_does_not_create_http_client_or_read_cache(monkeypatch):
    def forbidden(*_args, **_kwargs):
        pytest.fail("Korean content requires no API client or localization cache")

    store = MemoryStore()
    monkeypatch.setattr(store, "get_localized_post", forbidden)
    monkeypatch.setattr("compdesign_bot.mail_localization.httpx.Client", forbidden)
    localizer = MailLocalizer(Settings(), store)
    assert localizer.localize("post", "한국어 제목", "<b>한국어 본문</b>", "ko") == (
        "한국어 제목",
        "<b>한국어 본문</b>",
    )
    localizer.close()


def test_translates_real_content_preserves_links_numbers_code_and_escapes_output():
    requests = []

    def translate(texts):
        substitutions = {
            "새 도구": "New tool",
            "도구는 ": "The tool handles ",
            "개 메시를 처리합니다.": " meshes.",
            "원문 보기": '<img src="x" onerror="steal()">Read source',
        }
        results = []
        for text in texts:
            for original, translated in substitutions.items():
                text = text.replace(original, translated)
            results.append(text)
        return results

    store = MemoryStore()
    with client_for(translate, requests) as client:
        localizer = MailLocalizer(settings(), store, client=client)
        title, html = localizer.localize(
            "post",
            "새 도구",
            "<b>도구는 12개 메시를 처리합니다.</b>\n<code>wp.tile_load()</code>\n"
            '<a href="https://example.com/public?x=1&amp;y=2" onclick="steal()">원문 보기</a>',
            "en",
        )
    assert title == "New tool"
    assert "The tool handles 12 meshes." in html
    assert "<code>wp.tile_load()</code>" in html
    assert '<a href="https://example.com/public?x=1&amp;y=2">' in html
    assert "onclick" not in html
    assert '<img src="x"' not in html
    assert "&lt;img" in html
    user_payload = requests[0]["contents"]
    assert "example.com" not in str(user_payload)
    assert "wp.tile_load" not in str(user_payload)
    assert "PRIVATE_API_KEY" not in json.dumps(requests)
    assert "English" in requests[0]["systemInstruction"]["parts"][0]["text"]
    assert requests[0]["generationConfig"]["maxOutputTokens"] == 8192
    assert len(store.cache) == 1


def test_same_post_language_cached_once_bilingual_uses_cached_english_and_content_change_invalidates():
    requests = []
    store = MemoryStore()
    with client_for(
        lambda texts: [text.replace("제목", "Title").replace("본문", "Body") for text in texts], requests
    ) as client:
        localizer = MailLocalizer(settings(), store, client=client)
        first = localizer.localize("post", "제목", "<b>본문</b>", "en")
        assert localizer.localize("post", "제목", "<b>본문</b>", "en") == first
        title, html = localizer.localize("post", "제목", "<b>본문</b>", "bilingual")
        assert title == "제목 / Title"
        assert "한국어 / Korean" in html and "<b>English</b>" in html
        assert "<b>본문</b>" in html and "<b>Body</b>" in html
        assert len(requests) == 1
        localizer.localize("post", "제목", "본문\nBody", "en")
        assert len(requests) == 2


@pytest.mark.parametrize(
    "code,native,expected_target",
    [
        ("ja", "新しいツール", "Japanese"),
        ("zh", "新工具", "Simplified Chinese"),
        ("de", "Neues Werkzeug", "German"),
        ("fr", "Nouvel outil", "French"),
        ("es", "Nueva herramienta", "Spanish"),
        ("pt", "Nova ferramenta", "Portuguese"),
    ],
)
def test_requested_target_language_is_used_for_actual_title_and_body(code, native, expected_target):
    requests = []
    with client_for(lambda texts: [native for _ in texts], requests) as client:
        title, html = MailLocalizer(settings(), MemoryStore(), client=client).localize(
            "post", "제목", "본문", code
        )
    assert title == native
    assert html == native
    assert expected_target in requests[0]["systemInstruction"]["parts"][0]["text"]


@pytest.mark.parametrize(
    "translate",
    [
        lambda texts: texts,
        lambda texts: ["" for _ in texts],
        lambda texts: ["English 999" for _ in texts],
        lambda texts: [
            re.sub(r"__CDREF_[A-Z]+__", "", text).replace("제목", "Title").replace("본문", "Body")
            for text in texts
        ],
    ],
)
def test_untranslated_empty_or_corrupted_numeric_content_fails_without_caching(translate):
    store = MemoryStore()
    with client_for(translate, []) as client, pytest.raises(MailLocalizationUnavailable):
        MailLocalizer(settings(), store, client=client).localize("post", "제목", "본문 12", "en")
    assert not store.cache


def test_malformed_provider_response_is_sanitized_and_never_cached():
    store = MemoryStore()
    with (
        httpx.Client(
            transport=httpx.MockTransport(
                lambda _request: httpx.Response(
                    200,
                    json={"error": "PRIVATE_PROVIDER_ERROR"},
                )
            )
        ) as client,
        pytest.raises(MailLocalizationUnavailable) as error,
    ):
        MailLocalizer(settings(), store, client=client).localize("post", "제목", "본문", "en")
    assert "PRIVATE_PROVIDER_ERROR" not in str(error.value)
    assert not store.cache


@pytest.mark.parametrize(
    "invalid_response",
    [
        httpx.Response(200, json={"error": "PRIVATE_PROVIDER_ERROR"}),
        response(["제목", "본문"]),
        response(["Title 999", "Body"]),
    ],
)
def test_failed_variant_is_attempted_once_per_batch_but_other_posts_can_translate(invalid_response):
    requests = []

    def handle(request):
        requests.append(request)
        return invalid_response if len(requests) == 1 else response(["Title", "Body"])

    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        localizer = MailLocalizer(settings(), MemoryStore(), client=client)
        for _ in range(2):
            with pytest.raises(MailLocalizationUnavailable):
                localizer.localize("bad-post", "제목", "본문", "en")
        assert len(requests) == 1
        assert localizer.localize("different-post", "제목", "본문", "en") == ("Title", "Body")
        assert len(requests) == 2


def test_provider_failure_stops_new_calls_but_cached_content_remains_available():
    requests = []

    def handle(request):
        requests.append(request)
        if len(requests) == 1:
            return response(["Title", "Body"])
        return httpx.Response(429, json={"error": "PRIVATE_PROVIDER_ERROR"})

    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        localizer = MailLocalizer(settings(), MemoryStore(), client=client)
        cached = localizer.localize("first", "제목", "본문", "en")
        for post_key in ("second", "third"):
            with pytest.raises(MailLocalizationUnavailable) as error:
                localizer.localize(post_key, "제목", "본문", "en")
            assert "PRIVATE_PROVIDER_ERROR" not in str(error.value)
        assert localizer.localize("first", "제목", "본문", "en") == cached
        assert len(requests) == 2


def test_local_provider_never_silently_switches_to_gemini():
    requests = []
    with client_for(lambda texts: ["English" for _ in texts], requests) as client:
        localizer = MailLocalizer(
            replace(settings(), translation_provider="local"), MemoryStore(), client=client
        )
        with pytest.raises(MailLocalizationUnavailable, match="Gemini 설정"):
            localizer.localize("post", "제목", "본문", "en")
    assert not requests


def test_source_urls_are_hidden_from_provider_and_restored_exactly():
    requests = []
    with client_for(
        lambda texts: [text.replace("제목", "Title").replace("원문", "Source") for text in texts], requests
    ) as client:
        title, html = MailLocalizer(settings(), MemoryStore(), client=client).localize(
            "post",
            "제목",
            "원문 https://example.com/2026?x=12",
            "en",
        )
    assert title == "Title"
    assert "https://example.com/2026?x=12" in html
    assert "https://example.com" not in str(requests[0]["contents"])
