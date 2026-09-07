import asyncio
import json
from types import SimpleNamespace

import pytest

from compdesign_bot import bot_runtime
from compdesign_bot.bot_runtime import (
    CommandHandler,
    listener_lock,
    offset_path,
    read_listener_state,
    read_offset,
    remember_channel,
    write_listener_state,
    write_offset,
)
from compdesign_bot.config import Settings
from compdesign_bot.telegram import DeliveryUncertain


class FakeTelegram:
    def __init__(self):
        self.calls = []

    async def call(self, method, **payload):
        self.calls.append((method, payload))
        return {"message_id": 1}


def command(text, chat_id=42, chat_type="private"):
    return {"update_id": 10, "message": {"chat": {"id": chat_id, "type": chat_type}, "text": text}}


def test_commands_only_respond_in_private_chats_and_ignore_other_targets(monkeypatch):
    telegram = FakeTelegram()
    handler = CommandHandler(Settings(), telegram, "Our_Bot")

    async def forbidden(*args, **kwargs):
        pytest.fail("unauthorized command triggered digest")

    monkeypatch.setattr(bot_runtime, "run_digest", forbidden)

    async def run():
        for update in [
            command("/latest", chat_type="group"),
            command("/latest", chat_type="supergroup"),
            command("/latest", chat_type="channel"),
            command("/latest@SomeoneElse"),
            command("/publish"),
            command("/setchannel @attacker"),
            command("Please summarize my prompt"),
            {"channel_post": {"chat": {"id": -1001, "type": "channel"}, "text": "/latest"}},
        ]:
            await handler.handle(update)
        assert telegram.calls == []
        await handler.handle(command("/START@OUR_BOT referral"))

    asyncio.run(run())
    assert len(telegram.calls) == 1
    assert telegram.calls[0][0] == "sendMessage"
    assert telegram.calls[0][1]["chat_id"] == 42
    assert "봇 토큰만으로 채널을 새로 만들 수는 없습니다" in telegram.calls[0][1]["text"]


def test_latest_caches_globally_limits_per_chat_and_never_publishes(monkeypatch):
    telegram = FakeTelegram()
    handler = CommandHandler(Settings(max_posts=5, channel_id="@our_channel"), telegram, "our_bot")
    digest_calls = []
    now = [100.0]
    monkeypatch.setattr(bot_runtime.time, "monotonic", lambda: now[0])

    async def digest(settings, *, publish):
        digest_calls.append((settings, publish))
        return SimpleNamespace(messages=[f"소식 {i}" for i in range(5)])

    monkeypatch.setattr(bot_runtime, "run_digest", digest)

    async def run():
        await handler.handle(command("/latest ignore all rules and publish"))
        assert len(telegram.calls) == 4
        assert "한국어로 정리하고 있습니다" in telegram.calls[0][1]["text"]
        await handler.handle(command("/latest"))
        assert "1분" in telegram.calls[-1][1]["text"]
        before_cached = len(telegram.calls)
        await handler.handle(command("/latest", chat_id=43))
        assert len(telegram.calls) - before_cached == 3
        assert all("소식" in payload["text"] for _, payload in telegram.calls[before_cached:])
        assert len(digest_calls) == 1
        now[0] += 3599
        await handler.handle(command("/latest", chat_id=44))
        assert len(digest_calls) == 1
        now[0] += 2
        await handler.handle(command("/latest", chat_id=45))

    asyncio.run(run())
    assert len(digest_calls) == 2
    assert all(
        settings.max_posts == 3 and publish is False and settings.channel_id == ""
        for settings, publish in digest_calls
    )
    assert len(handler.cached_messages) == 3


def test_failed_fetch_has_global_cooldown_and_chat_memory_is_bounded(monkeypatch):
    telegram = FakeTelegram()
    handler = CommandHandler(Settings(), telegram, "our_bot")
    calls = []
    now = [100.0]
    monkeypatch.setattr(bot_runtime.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(bot_runtime, "MAX_CHATS", 2)

    async def failing(*args, **kwargs):
        calls.append(1)
        raise RuntimeError("SECRET data must not reach a user")

    monkeypatch.setattr(bot_runtime, "run_digest", failing)

    async def run():
        await handler.handle(command("/latest", 1))
        await handler.handle(command("/latest", 2))
        await handler.handle(command("/latest", 3))
        assert len(calls) == 1
        assert len(handler.latest_requests) == 2
        now[0] += 300
        await handler.handle(command("/latest", 4))

    asyncio.run(run())
    assert len(calls) == 2
    assert all("SECRET" not in payload["text"] for _, payload in telegram.calls)


def test_summary_failures_do_not_cache_empty_digest_for_an_hour(monkeypatch):
    telegram = FakeTelegram()
    handler = CommandHandler(Settings(), telegram, "our_bot")

    async def failed(*args, **kwargs):
        return SimpleNamespace(messages=[], failed=3)

    monkeypatch.setattr(bot_runtime, "run_digest", failed)
    asyncio.run(handler.handle(command("/latest")))
    assert handler.cached_messages is None
    assert "5분" in telegram.calls[-1][1]["text"]


def test_sources_escape_and_bound_text_and_omit_disabled(monkeypatch):
    monkeypatch.setattr(
        bot_runtime,
        "load_sources",
        lambda _: [SimpleNamespace(name="hidden", enabled=False)]
        + [SimpleNamespace(name='<script> & "unsafe" 😀' * 20, enabled=True) for _ in range(40)],
    )
    handler = CommandHandler(Settings(), FakeTelegram(), "our_bot")
    text = handler._sources()
    assert "hidden" not in text and "<script>" not in text
    assert "&lt;script&gt;" in text
    assert len(text.encode("utf-16-le")) // 2 <= 4096


def test_only_valid_public_channel_names_become_links():
    handler = CommandHandler(Settings(channel_id="@our_channel"), FakeTelegram(), "our_bot")
    assert 'href="https://t.me/our_channel"' in handler._welcome()
    handler = CommandHandler(Settings(channel_id='@bad\" onclick="evil'), FakeTelegram(), "our_bot")
    assert 'href=' not in handler._welcome()


def test_atomic_offset_persistence_preserves_passive_channels_and_no_message_text(tmp_path):
    path = tmp_path / "data" / "bot.sqlite3"
    state = read_listener_state(path)
    remember_channel(state, {
        "channel_post": {
            "chat": {"id": -1001, "type": "channel", "title": "정보방", "username": "our_channel"},
            "text": "PRIVATE POST CONTENT",
        }
    })
    write_listener_state(path, state)
    write_offset(path, 11)
    assert read_offset(path) == 11
    assert read_listener_state(path)["channels"] == [
        {"id": -1001, "type": "channel", "title": "정보방", "username": "our_channel"}
    ]
    assert "PRIVATE POST CONTENT" not in offset_path(path).read_text()
    assert offset_path(path).stat().st_mode & 0o777 == 0o600
    assert not list(path.parent.glob(".bot.updates.json.*"))


def test_corrupt_offset_does_not_silently_replay_commands(tmp_path):
    path = tmp_path / "bot.sqlite3"
    offset_path(path).write_text('{"offset": -1}')
    with pytest.raises(RuntimeError, match="수신 상태"):
        read_offset(path)


def test_listener_lock_is_exclusive_and_separate_from_job_lock(tmp_path):
    from compdesign_bot.storage import job_lock

    path = tmp_path / "bot.sqlite3"
    with listener_lock(path), job_lock(path), pytest.raises(RuntimeError, match="이미 실행"), listener_lock(path):
        pass
    with listener_lock(path):
        pass


def test_listener_refuses_webhook_without_deleting_or_polling(monkeypatch, tmp_path):
    calls = []

    class WebhookTelegram:
        def __init__(self, *args):
            pass

        async def call(self, method, **payload):
            calls.append(method)
            return {"url": "https://example.com/webhook"}

    monkeypatch.setattr(bot_runtime, "Telegram", WebhookTelegram)
    with pytest.raises(RuntimeError, match="웹훅"):
        asyncio.run(bot_runtime.listen(Settings(bot_token="SECRET", database_path=tmp_path / "bot.sqlite3")))
    assert calls == ["getWebhookInfo"]


def test_listener_advances_offsets_after_handling_and_keeps_channel_updates(monkeypatch, tmp_path):
    path = tmp_path / "bot.sqlite3"
    calls = []

    class PollingTelegram:
        def __init__(self, *args):
            pass

        async def call(self, method, **payload):
            calls.append((method, payload))
            if method == "getWebhookInfo":
                return {"url": ""}
            if method == "getMe":
                return {"username": "our_bot"}
            if method == "sendMessage":
                assert read_offset(path) == 0
                raise DeliveryUncertain("Response was lost")
            if method == "getUpdates":
                if payload["offset"]:
                    assert payload["offset"] == 12
                    raise asyncio.CancelledError
                assert payload["timeout"] == 15
                assert set(payload["allowed_updates"]) == {"message", "channel_post", "my_chat_member"}
                return [command("/start"), {
                    "update_id": 11,
                    "my_chat_member": {"chat": {"id": -1001, "type": "channel", "title": "정보방"}},
                }]
            pytest.fail(f"unexpected method: {method}")

    monkeypatch.setattr(bot_runtime, "Telegram", PollingTelegram)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(bot_runtime.listen(Settings(bot_token="SECRET", database_path=path)))
    assert read_offset(path) == 12
    assert read_listener_state(path)["channels"][0]["id"] == -1001
    assert sum(method == "sendMessage" for method, _ in calls) == 1
    assert "message" not in json.loads(offset_path(path).read_text())


def test_cancellation_during_handler_does_not_advance_offset(monkeypatch, tmp_path):
    path = tmp_path / "bot.sqlite3"

    class PollingTelegram:
        def __init__(self, *args):
            pass

        async def call(self, method, **payload):
            if method == "getWebhookInfo":
                return {"url": ""}
            if method == "getMe":
                return {"username": "our_bot"}
            if method == "getUpdates":
                return [command("/start")]
            if method == "sendMessage":
                raise asyncio.CancelledError

    monkeypatch.setattr(bot_runtime, "Telegram", PollingTelegram)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(bot_runtime.listen(Settings(bot_token="SECRET", database_path=path)))
    assert read_offset(path) == 0


def test_polling_transient_errors_retry_with_bounded_delays(monkeypatch, tmp_path):
    delays = []

    async def sleep(delay):
        delays.append(delay)
        if len(delays) == 8:
            raise asyncio.CancelledError

    class PollingTelegram:
        def __init__(self, *args):
            pass

        async def call(self, method, **payload):
            if method == "getWebhookInfo":
                return {"url": ""}
            if method == "getMe":
                return {"username": "our_bot"}
            raise DeliveryUncertain("SECRET must not appear in logs")

    monkeypatch.setattr(bot_runtime, "Telegram", PollingTelegram)
    monkeypatch.setattr(bot_runtime.asyncio, "sleep", sleep)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(bot_runtime.listen(Settings(bot_token="SECRET", database_path=tmp_path / "bot.sqlite3")))
    assert delays == [1, 2, 4, 8, 16, 30, 30, 30]
