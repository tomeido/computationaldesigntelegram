import asyncio
from dataclasses import replace

import pytest

from compdesign_bot import bot_runtime
from compdesign_bot.bot_runtime import CommandHandler
from compdesign_bot.config import Settings
from compdesign_bot.mailing import MailingStore
from compdesign_bot.telegram import channel_invite_url, mailing_signup_url, message_payload


class FakeTelegram:
    def __init__(self):
        self.calls = []

    async def call(self, method, **payload):
        self.calls.append((method, payload))
        return {"message_id": 1}


def update(text, user_id=42, chat_type="private"):
    return {
        "message": {
            "chat": {"id": user_id if chat_type == "private" else -1001, "type": chat_type},
            "from": {"id": user_id, "is_bot": False},
            "text": text,
        },
    }


@pytest.fixture
def setup_bot(tmp_path):
    settings = Settings(database_path=tmp_path / "bot.sqlite3", mailing_enabled=True)
    telegram = FakeTelegram()
    return settings, telegram, CommandHandler(settings, telegram, "our_bot")


def send(handler, text, **kwargs):
    asyncio.run(handler.handle(update(text, **kwargs)))


def test_signup_requires_intent_and_followup_survives_restart(setup_bot):
    settings, telegram, handler = setup_bot
    send(handler, "person@example.com")
    assert not telegram.calls
    assert not settings.database_path.exists()

    send(handler, "/start subscribe")
    assert "본인의 이메일" in telegram.calls[-1][1]["text"]
    restarted = CommandHandler(settings, telegram, "our_bot")
    send(restarted, "not-an-email")
    with MailingStore(settings.database_path) as store:
        assert store.awaiting_email(42)
        assert store.status_counts()["active"] == 0
    send(restarted, " Person@Example.COM ")
    with MailingStore(settings.database_path) as store:
        assert not store.awaiting_email(42)
        row = store.db.execute("SELECT email,telegram_user_id FROM mailing_subscribers").fetchone()
        assert tuple(row) == ("person@example.com", 42)
    assert "가입했습니다" in telegram.calls[-1][1]["text"]
    before = len(telegram.calls)
    send(restarted, "another@example.com")
    assert len(telegram.calls) == before


def test_direct_signup_and_cancel_never_send_confirmation_email(setup_bot):
    settings, telegram, handler = setup_bot
    send(handler, "/subscribe Person@Example.com")
    assert [method for method, _ in telegram.calls] == ["sendMessage"]
    with MailingStore(settings.database_path) as store:
        assert store.status_counts()["active"] == 1
    send(handler, "/subscribe")
    send(handler, "/cancel")
    send(handler, "another@example.com")
    with MailingStore(settings.database_path) as store:
        assert not store.awaiting_email(42)
        assert store.status_counts()["active"] == 1


@pytest.mark.parametrize("enabled,host,sender,awaiting_setup", [
    (True, "", "sender@example.com", True),
    (True, "smtp.example.com", "", True),
    (False, "smtp.example.com", "sender@example.com", True),
    (True, "smtp.example.com", "sender@example.com", False),
])
def test_signup_explains_when_delivery_is_waiting_for_smtp(setup_bot, enabled, host, sender, awaiting_setup):
    settings, telegram, _ = setup_bot
    handler = CommandHandler(
        replace(settings, mailing_enabled=enabled, smtp_host=host, smtp_from=sender), telegram, "our_bot",
    )
    send(handler, "/subscribe person@example.com")
    assert ("메일 발송 준비가 완료되면" in telegram.calls[-1][1]["text"]) is awaiting_setup
    with MailingStore(settings.database_path) as store:
        assert store.status_counts()["active"] == 1


@pytest.mark.parametrize("chat_type", ["group", "supergroup"])
def test_group_commands_redirect_privately_without_storing_or_echoing_address(setup_bot, chat_type):
    settings, telegram, handler = setup_bot
    for text in ("/subscribe private@example.com", "/unsubscribe private@example.com", "private@example.com"):
        send(handler, text, chat_type=chat_type)
    assert len(telegram.calls) == 2
    assert not settings.database_path.exists()
    for _, payload in telegram.calls:
        assert payload["chat_id"] == -1001
        assert "private@example.com" not in payload["text"]
        assert "https://t.me/our_bot" in payload["text"]
        assert "개인 대화" in payload["text"]


def test_existing_imported_or_other_users_address_cannot_be_claimed(setup_bot):
    settings, _, handler = setup_bot
    with MailingStore(settings.database_path) as store:
        store.subscribe("imported@example.com")
        store.subscribe("other@example.com", telegram_user_id=43)
    send(handler, "/subscribe imported@example.com")
    send(handler, "/unsubscribe")
    send(handler, "/subscribe other@example.com")
    send(handler, "/unsubscribe other@example.com")
    with MailingStore(settings.database_path) as store:
        rows = store.db.execute(
            "SELECT email,telegram_user_id,active FROM mailing_subscribers ORDER BY email"
        ).fetchall()
        assert [tuple(row) for row in rows] == [
            ("imported@example.com", None, 1), ("other@example.com", 43, 1),
        ]
    send(handler, "/subscribe own@example.com")
    send(handler, "/unsubscribe")
    with MailingStore(settings.database_path) as store:
        assert store.status_counts() == {"active": 2, "unsubscribed": 1}


def test_imported_mail_unsubscribe_link_requires_confirmation_and_supports_cancel(setup_bot):
    settings, telegram, handler = setup_bot
    with MailingStore(settings.database_path) as store:
        store.subscribe("imported@example.com")
        token = store.db.execute("SELECT token FROM mailing_subscribers").fetchone()[0]
    send(handler, f"/start unsubscribe_{token}")
    assert "/unsubscribe" in telegram.calls[-1][1]["text"]
    with MailingStore(settings.database_path) as store:
        assert store.status_counts()["active"] == 1
    send(handler, "/cancel")
    send(handler, "/unsubscribe")
    with MailingStore(settings.database_path) as store:
        assert store.status_counts()["active"] == 1
    send(handler, f"/start unsubscribe_{token}")
    send(handler, "/unsubscribe")
    with MailingStore(settings.database_path) as store:
        assert store.status_counts() == {"active": 0, "unsubscribed": 1}
    assert all(token not in payload["text"] for _, payload in telegram.calls)


def test_expired_unsubscribe_confirmation_does_not_change_own_or_imported_subscription(setup_bot, monkeypatch):
    settings, telegram, handler = setup_bot
    now = [100.0]
    monkeypatch.setattr(bot_runtime.time, "monotonic", lambda: now[0])
    with MailingStore(settings.database_path) as store:
        store.subscribe("imported@example.com")
        token = store.db.execute("SELECT token FROM mailing_subscribers").fetchone()[0]
    send(handler, "/subscribe own@example.com")
    send(handler, f"/start unsubscribe_{token}")
    now[0] += 901
    send(handler, "/unsubscribe")
    assert "만료" in telegram.calls[-1][1]["text"]
    with MailingStore(settings.database_path) as store:
        assert store.status_counts()["active"] == 2


def test_malformed_identity_other_bot_target_and_channel_messages_cannot_subscribe(setup_bot):
    settings, telegram, handler = setup_bot
    missing_sender = update("/subscribe person@example.com")
    del missing_sender["message"]["from"]
    wrong_sender = update("/subscribe person@example.com")
    wrong_sender["message"]["from"]["id"] = 43
    bot_sender = update("/subscribe person@example.com")
    bot_sender["message"]["from"]["is_bot"] = True
    for incoming in (missing_sender, wrong_sender, bot_sender):
        asyncio.run(handler.handle(incoming))
    send(handler, "/subscribe@another_bot person@example.com")
    send(handler, "/subscribe person@example.com", chat_type="channel")
    assert not telegram.calls
    assert not settings.database_path.exists()


def test_invite_and_post_buttons_preserve_original_article(setup_bot):
    settings, telegram, _ = setup_bot
    settings = replace(settings, channel_id="-1001", telegram_invite_url="https://t.me/+INVITE")
    handler = CommandHandler(settings, telegram, "our_bot")
    send(handler, "/invite")
    assert 'href="https://t.me/+INVITE"' in telegram.calls[-1][1]["text"]
    post = '<a href="https://example.com/?a=1&amp;b=2">원문 보기</a>'
    payload = message_payload(
        42, post, invite_url=handler.invite_url, subscribe_url=handler.subscribe_url,
    )
    assert payload["text"] == post
    assert payload["reply_markup"]["inline_keyboard"] == [
        [{"text": "원문 보기", "url": "https://example.com/?a=1&b=2"}],
        [
            {"text": "텔레그램 방 참여", "url": "https://t.me/+INVITE"},
            {"text": "메일링 가입", "url": "https://t.me/our_bot?start=subscribe"},
        ],
    ]
    assert channel_invite_url("@our_channel") == "https://t.me/our_channel"
    assert channel_invite_url("-1001", "https://evil.example/invite") == ""
    assert mailing_signup_url('bad\" username') == ""


def test_invalid_email_does_not_expose_input_or_exception_details(setup_bot):
    settings, telegram, handler = setup_bot
    send(handler, "/subscribe person@example.com\nBcc: secret@example.com")
    with MailingStore(settings.database_path) as store:
        assert store.status_counts()["active"] == 0
    assert "secret" not in telegram.calls[-1][1]["text"]
