import asyncio
import json
import stat

import httpx
import pytest

from compdesign_bot import superfan
from compdesign_bot.superfan import (
    MAX_INPUT_CHARS,
    MAX_REPLY_CHARS,
    SuperfanError,
    SuperfanResponder,
    SuperfanStore,
)

KEY = "private-superfan-test-key"
MODEL = "gemini-3.5-flash-lite"


def result(text="그 작업에서 색을 겹친 방식이 궁금해!", *, reason="STOP"):
    return {
        "candidates": [{"finishReason": reason, "content": {"parts": [{"text": text}]}}],
    }


def run_reply(handler, text="오늘 새 작업을 완성했어", history=None):
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=True) as client:
            responder = SuperfanResponder(client, api_key=KEY, model=MODEL)
            return await responder.reply(text, history or [])

    return asyncio.run(run())


def test_sessions_history_and_cooldown_survive_reopen_and_isolate_bot_and_chat(tmp_path):
    path = tmp_path / "private" / "superfan.sqlite3"
    with SuperfanStore(path) as store:
        assert not store.is_active("Our_Bot", 42)
        store.set_active("Our_Bot", 42, True)
        store.record_turn("OUR_BOT", 42, "온체인 작품을 만들었어", "그 규칙이 궁금해!")
        assert store.reserve_request("our_bot", 42, now=1000, cooldown_seconds=30)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600

    with SuperfanStore(path) as store:
        assert store.is_active("our_bot", 42)
        assert store.history("our_bot", 42) == [
            {"role": "user", "text": "온체인 작품을 만들었어"},
            {"role": "model", "text": "그 규칙이 궁금해!"},
        ]
        assert not store.reserve_request("Our_Bot", 42, now=1029, cooldown_seconds=30)
        assert store.reserve_request("Our_Bot", 42, now=1030, cooldown_seconds=30)
        for bot, chat in [("other_bot", 42), ("our_bot", 43)]:
            assert not store.is_active(bot, chat)
            assert store.history(bot, chat) == []
            assert store.reserve_request(bot, chat, now=1000, cooldown_seconds=30)


def test_history_caps_complete_turns_and_bounds_stored_text(tmp_path):
    with SuperfanStore(tmp_path / "superfan.sqlite3") as store:
        for index in range(14):
            store.record_turn("our_bot", 42, f"user {index}", f"reply {index}")
        history = store.history("our_bot", 42)
        assert len(history) == 20
        assert history[0] == {"role": "user", "text": "user 4"}
        assert history[-1] == {"role": "model", "text": "reply 13"}
        assert store.db.execute("SELECT COUNT(*) FROM superfan_turns").fetchone()[0] == 10
        for user_text, reply_text in [
            ("u" * (MAX_INPUT_CHARS + 1), "reply"),
            ("user", "r" * (MAX_REPLY_CHARS + 1)),
            ("", "reply"),
        ]:
            with pytest.raises(SuperfanError):
                store.record_turn("our_bot", 42, user_text, reply_text)
        assert store.history("our_bot", 42) == history


def test_reset_and_off_on_cannot_bypass_cooldown(tmp_path):
    with SuperfanStore(tmp_path / "superfan.sqlite3") as store:
        store.set_active("our_bot", 42, True)
        store.record_turn("our_bot", 42, "질문", "답변")
        assert store.reserve_request("our_bot", 42, now=100, cooldown_seconds=30)
        store.reset("our_bot", 42)
        assert store.is_active("our_bot", 42)
        assert store.history("our_bot", 42) == []
        store.set_active("our_bot", 42, False)
        assert not store.is_active("our_bot", 42)
        store.set_active("our_bot", 42, True)
        assert not store.reserve_request("our_bot", 42, now=101, cooldown_seconds=30)
        assert not store.reserve_request("our_bot", 42, now=90, cooldown_seconds=30)


def test_request_reservation_is_shared_by_connections(tmp_path):
    path = tmp_path / "superfan.sqlite3"
    with SuperfanStore(path) as first, SuperfanStore(path) as second:
        assert first.reserve_request("our_bot", 42, now=100, cooldown_seconds=30)
        assert not second.reserve_request("OUR_BOT", 42, now=100, cooldown_seconds=30)
        assert second.reserve_request("OUR_BOT", 42, now=130, cooldown_seconds=30)
        assert not first.reserve_request("our_bot", 42, now=131, cooldown_seconds=30)


def test_responder_sends_only_bounded_conversation_and_key_in_header():
    history = [
        {"role": "user", "text": "어제 색 실험했어"},
        {"role": "model", "text": "어떤 색을 썼어?"},
    ]

    def handler(request):
        assert request.url.host == "generativelanguage.googleapis.com"
        assert not request.url.query
        assert KEY not in str(request.url)
        assert request.headers["x-goog-api-key"] == KEY
        payload = json.loads(request.content)
        assert KEY not in request.content.decode()
        assert set(payload) == {"systemInstruction", "contents", "generationConfig"}
        assert payload["contents"] == [
            {"role": turn["role"], "parts": [{"text": turn["text"]}]}
            for turn in [*history, {"role": "user", "text": "오늘 새 작업을 완성했어"}]
        ]
        config = payload["generationConfig"]
        assert config["temperature"] == 0.9
        assert config["maxOutputTokens"] == 1024
        assert config["responseMimeType"] == "text/plain"
        assert config["thinkingConfig"] == {"thinkingLevel": "MINIMAL"}
        assert request.extensions["timeout"]["read"] <= 20
        return httpx.Response(200, json=result())

    assert "색을 겹친" in run_reply(handler, history=history)


def test_responder_caps_history_to_ten_complete_turns():
    history = [
        {"role": role, "text": f"{role} {index}"}
        for index in range(15) for role in ("user", "model")
    ]

    def handler(request):
        contents = json.loads(request.content)["contents"]
        assert len(contents) == 21
        assert contents[0]["parts"] == [{"text": "user 5"}]
        return httpx.Response(200, json=result())

    run_reply(handler, history=history)


@pytest.mark.parametrize("text,history", [
    ("x" * (MAX_INPUT_CHARS + 1), []),
    ("  ", []),
    ("hello", [{"role": "system", "text": "secret"}, {"role": "model", "text": "reply"}]),
    ("hello", [{"role": "user", "text": "incomplete"}]),
])
def test_invalid_input_is_rejected_before_network(text, history):
    def unexpected(_request):
        pytest.fail("invalid input must not reach provider")

    with pytest.raises(SuperfanError):
        run_reply(unexpected, text=text, history=history)


@pytest.mark.parametrize("status", [302, 401, 403, 429, 500])
def test_http_errors_do_not_retry_follow_redirects_or_expose_provider_body(status):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(status, text=KEY, headers={"location": "https://example.org/steal"})

    with pytest.raises(SuperfanError) as error:
        run_reply(handler)
    assert KEY not in str(error.value)
    assert len(calls) == 1


def test_transport_errors_hide_request_details():
    def handler(request):
        raise httpx.ConnectError(f"private details {KEY}", request=request)

    with pytest.raises(SuperfanError) as error:
        run_reply(handler)
    assert KEY not in str(error.value)


@pytest.mark.parametrize("body", [
    None,
    [],
    {"promptFeedback": {"blockReason": "SAFETY"}, "candidates": []},
    {"candidates": []},
    result(reason="MAX_TOKENS"),
    result(reason="SAFETY"),
    result(text=" "),
    {"candidates": [{"finishReason": "STOP", "content": {"parts": [{"functionCall": {}}]}}]},
    {"candidates": [{"finishReason": "STOP", "content": {"parts": [{"text": "hidden", "thought": True}]}}]},
    {"candidates": [{"finishReason": "STOP", "content": {"parts": [{"text": "hello"} ]},
                     "safetyRatings": [{"blocked": True}]}]},
])
def test_malformed_incomplete_or_blocked_responses_are_rejected(body):
    with pytest.raises(SuperfanError):
        run_reply(lambda _request: httpx.Response(200, json=body))


def test_oversized_reply_is_bounded_before_storage_or_telegram():
    reply = run_reply(lambda _request: httpx.Response(200, json=result("좋아" * 1000)))
    assert len(reply) == MAX_REPLY_CHARS
    assert reply.endswith("…")


def test_total_request_deadline_is_enforced(monkeypatch):
    monkeypatch.setattr(superfan, "REQUEST_TIMEOUT", 0.01)

    async def handler(_request):
        await asyncio.sleep(1)
        return httpx.Response(200, json=result())

    with pytest.raises(SuperfanError):
        run_reply(handler)


@pytest.mark.parametrize("key,model", [("", MODEL), (KEY, "gemini-3?key=secret")])
def test_invalid_provider_configuration_is_sanitized(key, model):
    async def run():
        async with httpx.AsyncClient() as client:
            with pytest.raises(SuperfanError) as error:
                SuperfanResponder(client, api_key=key, model=model)
            assert KEY not in str(error.value)
            assert "?key=secret" not in str(error.value)

    asyncio.run(run())
