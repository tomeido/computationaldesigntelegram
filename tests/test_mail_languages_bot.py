import asyncio

import pytest

from compdesign_bot import bot_runtime
from compdesign_bot.bot_runtime import CommandHandler
from compdesign_bot.config import Settings
from compdesign_bot.mail_localization import LANGUAGE_NAMES
from compdesign_bot.mailing import MailingStore


class FakeTelegram:
    def __init__(self):
        self.calls = []

    async def call(self, method, **payload):
        self.calls.append((method, payload))
        return {"message_id": 1}


def incoming(text, user_id=42, chat_type="private"):
    return {"message": {
        "chat": {"id": user_id if chat_type == "private" else -1001, "type": chat_type},
        "from": {"id": user_id, "is_bot": False},
        "text": text,
    }}


def send(handler, text, **kwargs):
    asyncio.run(handler.handle(incoming(text, **kwargs)))


@pytest.fixture
def language_bot(tmp_path):
    settings = Settings(database_path=tmp_path / "bot.sqlite3", mailing_enabled=True)
    telegram = FakeTelegram()
    return settings, telegram, CommandHandler(settings, telegram, "our_bot")


def imported_and_owned(settings):
    with MailingStore(settings.database_path) as store:
        store.subscribe("imported@example.com", language="ja")
        store.subscribe("own@example.com", telegram_user_id=42, language="ko")
        return store.db.execute(
            "SELECT token FROM mailing_subscribers WHERE email='imported@example.com'"
        ).fetchone()[0]


def test_new_subscriptions_default_to_bilingual_and_rejoining_preserves_language(language_bot):
    settings, telegram, handler = language_bot
    send(handler, "/subscribe")
    assert "Send your own email" in telegram.calls[-1][1]["text"]
    send(handler, "own@example.com")
    assert "Email language" in telegram.calls[-1][1]["text"]
    assert "/language en" in telegram.calls[-1][1]["text"]
    with MailingStore(settings.database_path) as store:
        assert store.get_language(42) == "bilingual"
    send(handler, "/language ja")
    send(handler, "/unsubscribe")
    send(handler, "/subscribe own@example.com")
    with MailingStore(settings.database_path) as store:
        assert store.get_language(42) == "ja"
    assert "日本語" in telegram.calls[-1][1]["text"]


def test_language_menu_shows_current_and_supported_choices(language_bot):
    settings, telegram, handler = language_bot
    send(handler, "/language")
    assert "No active email subscription" in telegram.calls[-1][1]["text"]
    with MailingStore(settings.database_path) as store:
        store.subscribe("own@example.com", telegram_user_id=42, language="en")
    send(handler, "/language")
    reply = telegram.calls[-1][1]["text"]
    assert "Current email language: English" in reply
    for code in LANGUAGE_NAMES:
        assert f"/language {code}" in reply
    send(handler, "/help")
    assert "/language" in telegram.calls[-1][1]["text"]


@pytest.mark.parametrize("argument,expected", [
    ("ko", "ko"), ("en", "en"), ("ja", "ja"), ("zh", "zh"), ("de", "de"),
    ("fr", "fr"), ("es", "es"), ("pt", "pt"), ("bilingual", "bilingual"),
    ("English", "en"), ("한국어", "ko"), ("日本語", "ja"),
    ("fr-FR", "fr"), ("pt_BR", "pt"), ("한영", "bilingual"),
])
def test_owned_language_changes_accept_supported_codes_and_aliases_without_affecting_others(
    language_bot, argument, expected,
):
    settings, telegram, handler = language_bot
    with MailingStore(settings.database_path) as store:
        store.subscribe("own@example.com", telegram_user_id=42)
        store.subscribe("other@example.com", telegram_user_id=43, language="ja")
        store.subscribe("imported@example.com", language="de")
    send(handler, f"/language {argument}")
    assert "Email language set" in telegram.calls[-1][1]["text"]
    with MailingStore(settings.database_path) as store:
        assert store.get_language(42) == expected
        assert store.get_language(43) == "ja"
        assert store.status_counts() == {"active": 3, "unsubscribed": 0}
        assert store.db.execute(
            "SELECT language FROM mailing_subscribers WHERE email='imported@example.com'"
        ).fetchone()[0] == "de"


@pytest.mark.parametrize("argument", ["secret@example.com", "en secret@example.com", "xx", "<script>"])
def test_invalid_language_never_echoes_input_or_changes_subscription(language_bot, argument):
    settings, telegram, handler = language_bot
    with MailingStore(settings.database_path) as store:
        store.subscribe("own@example.com", telegram_user_id=42, language="ko")
    send(handler, f"/language {argument}")
    reply = telegram.calls[-1][1]["text"]
    assert "supported language code" in reply
    assert argument not in reply
    with MailingStore(settings.database_path) as store:
        assert store.get_language(42) == "ko"


def test_imported_token_language_change_survives_restart_and_replay_without_claiming_address(language_bot):
    settings, telegram, handler = language_bot
    token = imported_and_owned(settings)
    send(handler, f"/start language_{token}")
    assert "Current email language: 日本語" in telegram.calls[-1][1]["text"]
    send(CommandHandler(settings, telegram, "our_bot"), "/language en")
    send(CommandHandler(settings, telegram, "our_bot"), "/language en")
    with MailingStore(settings.database_path) as store:
        assert store.get_language_token(token) == "en"
        assert store.get_language(42) == "ko"
        assert store.get_pending_language(42)[0] == token
        row = store.db.execute(
            "SELECT telegram_user_id,active FROM mailing_subscribers WHERE email='imported@example.com'"
        ).fetchone()
        assert tuple(row) == (None, 1)
    assert all(token not in str(payload) for _, payload in telegram.calls)


def test_expired_language_token_never_changes_another_owned_address_after_restart(language_bot, monkeypatch):
    settings, telegram, handler = language_bot
    now = [100.0]
    monkeypatch.setattr(bot_runtime.time, "time", lambda: now[0])
    token = imported_and_owned(settings)
    send(handler, f"/start language_{token}")
    now[0] += 901
    send(CommandHandler(settings, telegram, "our_bot"), "/language en")
    assert "expired" in telegram.calls[-1][1]["text"]
    send(CommandHandler(settings, telegram, "our_bot"), "/language en")
    assert "expired" in telegram.calls[-1][1]["text"]
    with MailingStore(settings.database_path) as store:
        assert store.get_language_token(token) == "ja"
        assert store.get_language(42) == "ko"
        assert store.status_counts() == {"active": 2, "unsubscribed": 0}


def test_language_token_cannot_reactivate_unsubscribed_or_change_fallback_subscription(language_bot):
    settings, telegram, handler = language_bot
    token = imported_and_owned(settings)
    with MailingStore(settings.database_path) as store:
        store.unsubscribe_token(token)
    send(handler, f"/start language_{token}")
    send(handler, "/language en")
    assert "No active email subscription" in telegram.calls[-1][1]["text"]
    with MailingStore(settings.database_path) as store:
        assert store.get_language(42) == "ko"
        assert store.status_counts() == {"active": 1, "unsubscribed": 1}
        row = store.db.execute(
            "SELECT language,telegram_user_id FROM mailing_subscribers WHERE email='imported@example.com'"
        ).fetchone()
        assert tuple(row) == ("ja", None)


@pytest.mark.parametrize("command", ["/cancel", "/subscribe", "/subscribe own@example.com"])
def test_cancel_or_signup_clears_language_link_context(language_bot, command):
    settings, telegram, handler = language_bot
    token = imported_and_owned(settings)
    send(handler, f"/start language_{token}")
    restarted = CommandHandler(settings, telegram, "our_bot")
    send(restarted, command)
    send(restarted, "/language en")
    with MailingStore(settings.database_path) as store:
        assert store.get_pending_language(42) is None
        assert store.get_language(42) == "en"
        assert store.get_language_token(token) == "ja"


def test_new_link_clears_opposite_confirmation_and_email_entry(language_bot):
    settings, _, handler = language_bot
    token = imported_and_owned(settings)
    send(handler, "/subscribe")
    send(handler, f"/start unsubscribe_{token}")
    send(handler, f"/start language_{token}")
    with MailingStore(settings.database_path) as store:
        assert store.get_pending_unsubscribe(42) is None
        assert store.get_pending_language(42)[0] == token
        assert not store.awaiting_email(42)
    send(handler, f"/start unsubscribe_{token}")
    with MailingStore(settings.database_path) as store:
        assert store.get_pending_language(42) is None
        assert store.get_pending_unsubscribe(42)[0] == token


@pytest.mark.parametrize("chat_type", ["group", "supergroup"])
def test_group_language_commands_redirect_privately_without_echoing_addresses_or_tokens(language_bot, chat_type):
    settings, telegram, handler = language_bot
    token = "A" * 32
    for text in ("/language en secret@example.com", f"/start language_{token}"):
        send(handler, text, chat_type=chat_type)
    assert not settings.database_path.exists()
    assert len(telegram.calls) == 2
    for _, payload in telegram.calls:
        assert "개인 대화" in payload["text"]
        assert 'href="https://t.me/our_bot"' in payload["text"]
        assert "secret@example.com" not in str(payload)
        assert token not in str(payload)


def test_language_changes_require_verified_private_identity_and_valid_deep_link(language_bot):
    settings, telegram, handler = language_bot
    wrong_sender = incoming("/language en")
    wrong_sender["message"]["from"]["id"] = 43
    asyncio.run(handler.handle(wrong_sender))
    send(handler, "/language@another_bot en")
    send(handler, "/start language_secret@example.com")
    assert not settings.database_path.exists()
    assert len(telegram.calls) == 1
    assert "secret@example.com" not in telegram.calls[-1][1]["text"]
