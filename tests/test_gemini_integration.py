import asyncio
from dataclasses import replace
from datetime import UTC, datetime
from types import SimpleNamespace

import httpx
import pytest

from compdesign_bot import config, local_summary, pipeline
from compdesign_bot.config import Settings
from compdesign_bot.errors import SummaryError
from compdesign_bot.gemini_summary import GeminiUnavailable
from compdesign_bot.models import Article, RankedArticle, Summary
from compdesign_bot.storage import Store

KEY = "private-gemini-key-for-tests"


def test_gemini_config_comes_from_environment_without_exposing_credentials(monkeypatch):
    monkeypatch.setattr(config, "load_dotenv", lambda: None)
    monkeypatch.setenv("TRANSLATION_PROVIDER", " Gemini ")
    monkeypatch.setenv("GEMINI_API_KEY", f" {KEY} ")
    monkeypatch.setenv("GEMINI_MODEL", " gemini-3.5-flash-lite ")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "private-telegram-token")

    settings = Settings.from_env()

    assert settings.translation_provider == "gemini"
    assert settings.gemini_api_key == KEY
    assert settings.gemini_model == "gemini-3.5-flash-lite"
    assert KEY not in repr(settings)
    assert "private-telegram-token" not in repr(settings)


def test_api_key_alone_does_not_implicitly_switch_provider(monkeypatch):
    monkeypatch.setattr(config, "load_dotenv", lambda: None)
    monkeypatch.delenv("TRANSLATION_PROVIDER", raising=False)
    monkeypatch.delenv("GEMINI_MODEL", raising=False)
    monkeypatch.setenv("GEMINI_API_KEY", KEY)

    settings = Settings.from_env()

    assert settings.translation_provider == "local"
    assert settings.gemini_model == "gemini-3.5-flash-lite"


def test_gemini_setup_does_not_require_installed_local_model(monkeypatch, tmp_path):
    def unexpected_check(_path):
        pytest.fail("Gemini must work without downloading a local translation model")

    monkeypatch.setattr(local_summary, "check_model", unexpected_check)
    settings = Settings(
        translation_provider="gemini",
        gemini_api_key=KEY,
        local_model_path=tmp_path / "missing-model",
    )
    settings.require_summary()


def test_missing_gemini_key_fails_with_actionable_setting_name():
    with pytest.raises(ValueError, match="GEMINI_API_KEY"):
        Settings(translation_provider="gemini").require_summary()


def test_local_provider_still_checks_local_model(monkeypatch, tmp_path):
    checked = []
    monkeypatch.setattr(local_summary, "check_model", checked.append)
    settings = Settings(local_model_path=tmp_path / "model", gemini_api_key=KEY)
    settings.require_summary()
    assert checked == [tmp_path / "model"]


@pytest.mark.parametrize(
    "setting,value",
    [
        ("translation_provider", KEY),
        ("gemini_model", f"gemini-model?key={KEY}"),
        ("gemini_model", f"gemini-model/../../{KEY}"),
        ("gemini_model", f"https://example.com/{KEY}"),
        ("gemini_model", ""),
    ],
)
def test_invalid_provider_or_model_is_rejected_without_echoing_input(setting, value):
    with pytest.raises(ValueError) as exc:
        Settings(**{setting: value})
    assert setting.upper() in str(exc.value)
    assert KEY not in str(exc.value)


def configure_digest(monkeypatch, tmp_path):
    articles = [
        Article(
            f"Computational design research {index}",
            f"https://example.com/research/{index}",
            "Research",
            f"An open-source generative design tool explores technique {index}.",
            datetime.now(UTC),
        )
        for index in range(1, 4)
    ]
    ranked = [RankedArticle(article, 1, 10, "Web3 × 디자인") for article in articles]
    calls = []
    failures = {}
    real_client = httpx.AsyncClient

    def unexpected_http(_request):
        pytest.fail("Config and pipeline integration must not use external services")

    async def collect(_sources, _client):
        return SimpleNamespace(articles=articles, errors=[])

    class FakeGemini:
        def __init__(self, client, *, api_key, model):
            assert isinstance(client, real_client)
            assert api_key == KEY
            self.model = model

        async def summarize(self, item):
            calls.append(("gemini", self.model, item.article.url))
            if item.article.url in failures:
                raise failures[item.article.url]
            return Summary(
                f"제미나이 디자인 연구 {item.article.title.rsplit(' ', 1)[-1]}",
                ("생성형 디자인 도구를 공개했습니다.",),
                "원문에서 세부 내용을 확인하세요.",
                f"Gemini {self.model}",
            )

    class FakeLocal:
        def __init__(self, *, model_path):
            pass

        async def summarize(self, item):
            calls.append(("local", None, item.article.url))
            return Summary(
                f"로컬 디자인 연구 {item.article.title.rsplit(' ', 1)[-1]}",
                ("생성형 디자인 도구를 공개했습니다.",),
                "원문에서 세부 내용을 확인하세요.",
                "로컬 기계번역",
            )

    monkeypatch.setattr(
        pipeline.httpx,
        "AsyncClient",
        lambda **kwargs: real_client(transport=httpx.MockTransport(unexpected_http), **kwargs),
    )
    monkeypatch.setattr(pipeline, "collect_articles", collect)
    monkeypatch.setattr(pipeline, "load_sources", lambda _path: [])
    monkeypatch.setattr(pipeline, "rank_articles", lambda _articles, **_kwargs: ranked)
    monkeypatch.setattr(pipeline, "GeminiSummarizer", FakeGemini)
    monkeypatch.setattr(pipeline, "LocalSummarizer", FakeLocal)
    settings = Settings(
        translation_provider="gemini",
        gemini_api_key=KEY,
        database_path=tmp_path / "digest.sqlite3",
    )
    return settings, articles, calls, failures


def test_digest_cache_is_reused_only_for_same_provider_and_model(monkeypatch, tmp_path):
    settings, _articles, calls, _failures = configure_digest(monkeypatch, tmp_path)
    local_settings = replace(settings, translation_provider="local")
    local = asyncio.run(pipeline.run_digest(local_settings))
    gemini = asyncio.run(pipeline.run_digest(settings))
    repeated = asyncio.run(pipeline.run_digest(settings))

    assert len(local.messages) == len(gemini.messages) == 3
    assert local.messages != gemini.messages
    assert repeated.messages == gemini.messages
    assert [provider for provider, _model, _url in calls] == ["local"] * 3 + ["gemini"] * 3

    changed_settings = replace(settings, gemini_model="gemini-2.5-flash-lite")
    changed = asyncio.run(pipeline.run_digest(changed_settings))
    assert len(changed.messages) == 3
    assert len(calls) == 9
    assert all(model == changed_settings.gemini_model for _provider, model, _url in calls[-3:])
    assert asyncio.run(pipeline.run_digest(local_settings)).messages == local.messages
    assert len(calls) == 9


def test_unavailable_gemini_stops_remaining_requests_and_is_not_cached(monkeypatch, tmp_path):
    settings, articles, calls, failures = configure_digest(monkeypatch, tmp_path)
    failures[articles[0].url] = GeminiUnavailable("Gemini 사용량 한도에 도달했습니다. (HTTP 429)")

    failed = asyncio.run(pipeline.run_digest(settings))

    assert failed.failed == 1
    assert failed.messages == []
    assert [url for _provider, _model, url in calls] == [articles[0].url]
    failures.clear()
    recovered = asyncio.run(pipeline.run_digest(settings))
    assert len(recovered.messages) == 3
    assert recovered.failed == 0
    assert [url for _provider, _model, url in calls[1:]] == [article.url for article in articles]


def test_gemini_failure_preserves_completed_messages_and_successful_cache(monkeypatch, tmp_path):
    settings, articles, calls, failures = configure_digest(monkeypatch, tmp_path)
    failures[articles[1].url] = GeminiUnavailable("Gemini 연결에 실패했습니다.")

    partial = asyncio.run(pipeline.run_digest(settings))

    assert partial.failed == 1
    assert len(partial.messages) == 1
    assert [url for _provider, _model, url in calls] == [article.url for article in articles[:2]]
    failures.clear()
    recovered = asyncio.run(pipeline.run_digest(settings))
    assert len(recovered.messages) == 3
    assert recovered.messages[0] == partial.messages[0]
    assert [url for _provider, _model, url in calls[2:]] == [article.url for article in articles[1:]]


def test_article_validation_failure_continues_other_articles_and_can_retry(monkeypatch, tmp_path):
    settings, articles, calls, failures = configure_digest(monkeypatch, tmp_path)
    failures[articles[0].url] = SummaryError("Gemini 번역 응답 형식이 올바르지 않습니다.")

    partial = asyncio.run(pipeline.run_digest(settings))

    assert partial.failed == 1
    assert len(partial.messages) == 2
    assert len(calls) == 3
    failures.clear()
    recovered = asyncio.run(pipeline.run_digest(settings))
    assert len(recovered.messages) == 3
    assert len(calls) == 4
    assert calls[-1][2] == articles[0].url


def test_switching_translation_provider_keeps_existing_delivery_history(monkeypatch, tmp_path):
    settings, articles, calls, _failures = configure_digest(monkeypatch, tmp_path)
    store = Store(settings.database_path)
    try:
        store.sent(store.reserve(articles[0], settings.channel_id), 123)
    finally:
        store.close()

    local = asyncio.run(pipeline.run_digest(replace(settings, translation_provider="local")))
    gemini = asyncio.run(pipeline.run_digest(settings))

    assert len(local.messages) == len(gemini.messages) == 2
    assert all(url != articles[0].url for _provider, _model, url in calls)
    store = Store(settings.database_path)
    try:
        assert store.status_counts() == {"sent": 1}
        assert store.seen(articles[0], settings.channel_id)
    finally:
        store.close()
