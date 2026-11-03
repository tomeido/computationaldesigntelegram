import asyncio
from dataclasses import replace
from html import escape
from types import SimpleNamespace

import pytest

from compdesign_bot import bot_runtime, cli, config
from compdesign_bot.bot_runtime import CommandHandler
from compdesign_bot.config import Settings
from compdesign_bot.mailing import MailingStore
from compdesign_bot.superfan import SuperfanError, SuperfanStore
from compdesign_bot.telegram import DeliveryUncertain


class FakeTelegram:
    def __init__(self):
        self.client = object()
        self.calls = []
        self.fail_next_send = False

    async def call(self, method, **payload):
        self.calls.append((method, payload))
        if self.fail_next_send:
            self.fail_next_send = False
            raise DeliveryUncertain("SECRET delivery details")
        return {"message_id": len(self.calls)}


def update(text, user_id=42, chat_type="private"):
    return {
        "message": {
            "chat": {"id": user_id if chat_type == "private" else -1001, "type": chat_type},
            "from": {"id": user_id, "is_bot": False},
            "text": text,
        },
    }


def send(handler, text, **kwargs):
    asyncio.run(handler.handle(update(text, **kwargs)))


@pytest.fixture
def superfan_bot(tmp_path, monkeypatch):
    settings = Settings(
        database_path=tmp_path / "bot.sqlite3",
        superfan_enabled=True,
        superfan_cooldown_seconds=10,
        gemini_api_key="test-provider-key",
    )
    telegram = FakeTelegram()
    state = SimpleNamespace(
        settings=settings,
        telegram=telegram,
        constructors=[],
        requests=[],
        reply="오늘도 응원해!",
        error=None,
        now=[1000.0],
        store_path=settings.database_path.with_suffix(".superfan.sqlite3"),
    )

    class FakeResponder:
        def __init__(self, client, *, api_key, model):
            state.constructors.append((client, api_key, model))

        async def reply(self, text, history):
            state.requests.append((text, [dict(turn) for turn in history]))
            if state.error is not None:
                raise state.error
            return state.reply

    monkeypatch.setattr(bot_runtime, "SuperfanResponder", FakeResponder)
    monkeypatch.setattr(bot_runtime.time, "time", lambda: state.now[0])
    state.handler = CommandHandler(settings, telegram, "Our_Bot")
    return state


@pytest.mark.parametrize("activation", ["/superfan", "/superfan on", "/start superfan", "/SUPERFAN@OUR_BOT"])
def test_activation_is_explicit_and_welcome_does_not_call_model(superfan_bot, activation):
    bot = superfan_bot
    send(bot.handler, "오늘 뭐 해?")
    assert not bot.telegram.calls
    assert not bot.requests

    send(bot.handler, activation)
    assert len(bot.telegram.calls) == 1
    assert not bot.requests
    assert not bot.constructors
    with SuperfanStore(bot.store_path) as store:
        assert store.is_active("our_bot", 42)
        assert store.history("our_bot", 42) == []

    send(bot.handler, "오늘 뭐 해?")
    assert bot.requests == [("오늘 뭐 해?", [])]
    assert bot.constructors == [(bot.telegram.client, bot.settings.gemini_api_key, bot.settings.gemini_model)]
    assert bot.telegram.calls[-1][1]["chat_id"] == 42


@pytest.mark.parametrize("unavailable", [{"superfan_enabled": False}, {"gemini_api_key": ""}])
def test_unavailable_mode_neither_activates_nor_answers_free_text(superfan_bot, unavailable):
    bot = superfan_bot
    handler = CommandHandler(replace(bot.settings, **unavailable), bot.telegram, "our_bot")
    send(handler, "안녕")
    assert not bot.telegram.calls
    for command in ("/superfan", "/superfan on", "/start superfan"):
        before = len(bot.telegram.calls)
        send(handler, command)
        assert len(bot.telegram.calls) == before + 1
    assert not bot.requests
    assert not bot.constructors
    if bot.store_path.exists():
        with SuperfanStore(bot.store_path) as store:
            assert not store.is_active("our_bot", 42)


def test_activated_session_and_history_survive_restart_and_case_changes(superfan_bot):
    bot = superfan_bot
    send(bot.handler, "/superfan")
    send(bot.handler, "오늘 발표했어")
    first_reply = bot.reply
    bot.now[0] += 10

    restarted = CommandHandler(bot.settings, bot.telegram, "OUR_BOT")
    send(restarted, "다음에는 뭘 해 볼까?")
    assert bot.requests[-1] == (
        "다음에는 뭘 해 볼까?",
        [{"role": "user", "text": "오늘 발표했어"}, {"role": "model", "text": first_reply}],
    )
    with SuperfanStore(bot.store_path) as store:
        assert len(store.history("our_bot", 42)) == 4


def test_stable_bot_identity_survives_rename_without_sharing_with_replacement_bot(superfan_bot):
    bot = superfan_bot
    original = CommandHandler(bot.settings, bot.telegram, "our_bot", bot_id=100)
    send(original, "/superfan")
    send(original, "내가 만든 도구 기억해 줘")
    bot.now[0] += 10
    renamed = CommandHandler(bot.settings, bot.telegram, "renamed_bot", bot_id=100)
    send(renamed, "무엇을 기억해?")
    assert bot.requests[-1][1][0]["text"] == "내가 만든 도구 기억해 줘"
    request_count = len(bot.requests)
    replacement = CommandHandler(bot.settings, bot.telegram, "our_bot", bot_id=101)
    send(replacement, "이전 기억을 보여 줘")
    assert len(bot.requests) == request_count
    send(replacement, "/superfan")
    send(replacement, "새로운 대화")
    assert bot.requests[-1] == ("새로운 대화", [])


def test_sessions_are_isolated_between_chats_and_bot_accounts(superfan_bot):
    bot = superfan_bot
    send(bot.handler, "/superfan")
    send(bot.handler, "나만의 비밀")
    other_bot = CommandHandler(bot.settings, bot.telegram, "different_bot")
    before = len(bot.telegram.calls)
    send(bot.handler, "다른 사람의 메시지", user_id=43)
    send(other_bot, "다른 봇에 보낸 메시지")
    assert len(bot.telegram.calls) == before

    send(bot.handler, "/superfan", user_id=43)
    send(bot.handler, "두 번째 사용자", user_id=43)
    send(other_bot, "/superfan")
    send(other_bot, "두 번째 봇")
    assert bot.requests == [("나만의 비밀", []), ("두 번째 사용자", []), ("두 번째 봇", [])]
    with SuperfanStore(bot.store_path) as store:
        assert store.history("our_bot", 42)[0]["text"] == "나만의 비밀"
        assert store.history("our_bot", 43)[0]["text"] == "두 번째 사용자"
        assert store.history("different_bot", 42)[0]["text"] == "두 번째 봇"


def test_status_and_unknown_arguments_never_activate_or_call_model(superfan_bot):
    bot = superfan_bot
    for command in ("/superfan status", "/superfan surprise", "/superfan on extra"):
        send(bot.handler, command)
        assert bot.telegram.calls[-1][1]["text"]
    assert "/superfan" in bot.telegram.calls[-1][1]["text"]
    send(bot.handler, "아직 켜지지 않았지?")
    assert len(bot.telegram.calls) == 3
    assert not bot.requests
    assert not bot.constructors
    if bot.store_path.exists():
        with SuperfanStore(bot.store_path) as store:
            assert not store.is_active("our_bot", 42)

    send(bot.handler, "/superfan")
    send(bot.handler, "/superfan status")
    assert not bot.requests


@pytest.mark.parametrize("invalid", ["group", "supergroup", "channel", "missing_sender", "wrong_sender", "bot"])
def test_unverified_and_nonprivate_updates_cannot_activate_or_use_session(superfan_bot, invalid):
    bot = superfan_bot
    send(bot.handler, "/superfan")
    before = len(bot.telegram.calls)
    for text in ("/superfan", "/start superfan", "내 메시지를 읽어 줘"):
        incoming = update(text, chat_type=invalid if invalid in {"group", "supergroup", "channel"} else "private")
        if invalid == "missing_sender":
            del incoming["message"]["from"]
        elif invalid == "wrong_sender":
            incoming["message"]["from"]["id"] = 43
        elif invalid == "bot":
            incoming["message"]["from"]["is_bot"] = True
        asyncio.run(bot.handler.handle(incoming))
    assert len(bot.telegram.calls) == before
    assert not bot.requests


def test_other_bot_targets_and_unknown_commands_are_not_sent_to_model(superfan_bot):
    bot = superfan_bot
    send(bot.handler, "/superfan")
    before = len(bot.telegram.calls)
    for text in ("/superfan@another_bot off", "/start@another_bot superfan", "/unknown", "/123"):
        send(bot.handler, text)
    assert len(bot.telegram.calls) == before
    assert not bot.requests
    with SuperfanStore(bot.store_path) as store:
        assert store.is_active("our_bot", 42)


def test_email_signup_consumes_pending_text_before_superfan(superfan_bot):
    bot = superfan_bot
    send(bot.handler, "/superfan")
    send(bot.handler, "/subscribe")
    restarted = CommandHandler(bot.settings, bot.telegram, "our_bot")
    send(restarted, "이메일 형식이 아님")
    send(restarted, " Person@Example.COM ")
    assert not bot.requests
    with MailingStore(bot.settings.database_path) as store:
        assert not store.awaiting_email(42)
        row = store.db.execute("SELECT email,telegram_user_id FROM mailing_subscribers").fetchone()
        assert tuple(row) == ("person@example.com", 42)
    with SuperfanStore(bot.store_path) as store:
        assert store.history("our_bot", 42) == []
    send(restarted, "이제 대화하자")
    assert bot.requests == [("이제 대화하자", [])]


def test_model_reply_is_escaped_for_telegram_and_history_keeps_plain_text(superfan_bot):
    bot = superfan_bot
    bot.reply = '<b>너 & 나</b> <a href="https://example.com">링크</a>'
    send(bot.handler, "/superfan")
    send(bot.handler, "<나> & 너")
    payload = bot.telegram.calls[-1][1]
    assert payload["parse_mode"] == "HTML"
    assert bot.reply not in payload["text"]
    assert payload["text"].endswith(escape(bot.reply))
    with SuperfanStore(bot.store_path) as store:
        assert store.history("our_bot", 42) == [
            {"role": "user", "text": "<나> & 너"},
            {"role": "model", "text": bot.reply},
        ]


def test_oversized_input_is_refused_without_provider_or_history(superfan_bot):
    bot = superfan_bot
    send(bot.handler, "/superfan")
    send(bot.handler, "가" * 2001)
    assert not bot.requests
    assert not bot.constructors
    assert bot.telegram.calls[-1][1]["text"]
    with SuperfanStore(bot.store_path) as store:
        assert store.history("our_bot", 42) == []
    send(bot.handler, "가" * 2000)
    assert bot.requests == [("가" * 2000, [])]


def test_provider_error_is_sanitized_not_remembered_and_still_rate_limited(superfan_bot, caplog):
    bot = superfan_bot
    bot.error = SuperfanError("SECRET provider token and private text")
    send(bot.handler, "/superfan")
    send(bot.handler, "응원해 줘")
    assert len(bot.requests) == 1
    assert "SECRET" not in str(bot.telegram.calls)
    assert "SECRET" not in caplog.text
    with SuperfanStore(bot.store_path) as store:
        assert store.history("our_bot", 42) == []
    restarted = CommandHandler(bot.settings, bot.telegram, "our_bot")
    send(restarted, "바로 다시 해 줘")
    assert len(bot.requests) == 1
    bot.error = None
    bot.now[0] += 10
    send(restarted, "이제 다시 해 줘")
    assert bot.requests[-1] == ("이제 다시 해 줘", [])


def test_uncertain_telegram_send_is_not_retried_or_recorded(superfan_bot):
    bot = superfan_bot
    send(bot.handler, "/superfan")
    before = len(bot.telegram.calls)
    bot.telegram.fail_next_send = True
    send(bot.handler, "기억에 남지 않을 메시지")
    assert len(bot.telegram.calls) == before + 1
    with SuperfanStore(bot.store_path) as store:
        assert store.history("our_bot", 42) == []
    bot.now[0] += 10
    send(CommandHandler(bot.settings, bot.telegram, "our_bot"), "전달 가능한 메시지")
    assert bot.requests[-1] == ("전달 가능한 메시지", [])


@pytest.mark.parametrize("control", ["reset", "off"])
def test_reset_and_off_clear_history_but_preserve_cooldown_across_restart(superfan_bot, control):
    bot = superfan_bot
    send(bot.handler, "/superfan")
    send(bot.handler, "기존 대화")
    send(bot.handler, f"/superfan {control}")
    with SuperfanStore(bot.store_path) as store:
        assert store.history("our_bot", 42) == []
        assert store.is_active("our_bot", 42) is (control == "reset")

    restarted = CommandHandler(bot.settings, bot.telegram, "our_bot")
    if control == "off":
        before = len(bot.telegram.calls)
        send(restarted, "꺼진 동안 무시")
        assert len(bot.telegram.calls) == before
        send(restarted, "/superfan on")
    bot.now[0] += 5
    send(restarted, "아직 너무 빠른 요청")
    assert len(bot.requests) == 1
    bot.now[0] += 5
    send(restarted, "새 대화")
    assert bot.requests[-1] == ("새 대화", [])


@pytest.mark.parametrize("control", ["reset", "off"])
def test_user_can_clear_existing_history_when_feature_is_disabled(superfan_bot, control):
    bot = superfan_bot
    send(bot.handler, "/superfan")
    send(bot.handler, "삭제할 대화")
    disabled = CommandHandler(replace(bot.settings, superfan_enabled=False), bot.telegram, "our_bot")
    before = len(bot.telegram.calls)
    send(disabled, "비활성화 상태 메시지")
    assert len(bot.telegram.calls) == before
    send(disabled, f"/superfan {control}")
    assert len(bot.telegram.calls) == before + 1
    with SuperfanStore(bot.store_path) as store:
        assert store.history("our_bot", 42) == []
        assert store.is_active("our_bot", 42) is (control == "reset")
    assert len(bot.requests) == 1


def test_superfan_environment_defaults_and_explicit_configuration(monkeypatch):
    monkeypatch.setattr(config, "load_dotenv", lambda: None)
    monkeypatch.delenv("SUPERFAN_ENABLED", raising=False)
    monkeypatch.delenv("SUPERFAN_COOLDOWN_SECONDS", raising=False)
    defaults = Settings.from_env()
    assert defaults.superfan_enabled is False
    assert defaults.superfan_cooldown_seconds == 10

    monkeypatch.setenv("SUPERFAN_ENABLED", "true")
    monkeypatch.setenv("SUPERFAN_COOLDOWN_SECONDS", "35")
    configured = Settings.from_env()
    assert configured.superfan_enabled is True
    assert configured.superfan_cooldown_seconds == 35


@pytest.mark.parametrize("value", ["0", "3601", "1.5", "invalid"])
def test_invalid_environment_cooldown_is_rejected(monkeypatch, value):
    monkeypatch.setattr(config, "load_dotenv", lambda: None)
    monkeypatch.setenv("SUPERFAN_COOLDOWN_SECONDS", value)
    with pytest.raises(ValueError, match="SUPERFAN_COOLDOWN_SECONDS"):
        Settings.from_env()


@pytest.mark.parametrize("value", [0, 3601, 1.5, True])
def test_invalid_direct_cooldown_is_rejected(value):
    with pytest.raises(ValueError, match="SUPERFAN_COOLDOWN_SECONDS"):
        Settings(superfan_cooldown_seconds=value)


def test_invalid_environment_enable_flag_is_rejected(monkeypatch):
    monkeypatch.setattr(config, "load_dotenv", lambda: None)
    monkeypatch.setenv("SUPERFAN_ENABLED", "invalid")
    with pytest.raises(ValueError, match="SUPERFAN_ENABLED"):
        Settings.from_env()


@pytest.mark.parametrize("enabled", [True, False])
def test_bot_configuration_advertises_superfan_only_when_enabled(monkeypatch, enabled):
    calls = []

    class ConfiguredTelegram:
        def __init__(self, *_args):
            pass

        async def call(self, method, **payload):
            calls.append((method, payload))
            return {"username": "our_bot"} if method == "getMe" else True

    monkeypatch.setattr(cli, "Telegram", ConfiguredTelegram)
    asyncio.run(cli.configure_bot(Settings(bot_token="test", superfan_enabled=enabled)))
    menus = [payload for method, payload in calls if method == "setMyCommands"]
    assert len(menus) == 2
    for menu in menus:
        commands = {item["command"] for item in menu["commands"]}
        assert ("superfan" in commands) is enabled
        assert {"start", "latest", "subscribe", "language", "help"} <= commands
        assert menu["scope"] == {"type": "all_private_chats"}
    descriptions = [payload["description"] for method, payload in calls if method == "setMyDescription"]
    assert all(("/superfan" in text) is enabled for text in descriptions)
