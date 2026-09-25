import asyncio
from dataclasses import replace
from datetime import UTC, datetime

import pytest

from compdesign_bot import mail_delivery, pipeline
from compdesign_bot.config import Settings
from compdesign_bot.mailing import DeliveryReport, MailingStore
from compdesign_bot.models import Article
from compdesign_bot.storage import Store


def configured(tmp_path):
    return Settings(
        database_path=tmp_path / "bot.sqlite3", mailing_enabled=True,
        smtp_host="smtp.example.com", smtp_from="sender@example.com",
        telegram_invite_url="https://t.me/example_channel", bot_username="example_bot",
    )


def archive(settings, *, sent=True):
    store = Store(settings.database_path)
    try:
        article = Article("A Web3 post", "https://example.com/post", "Example", "", datetime.now(UTC))
        key = store.reserve(article, "channel", post_html='<b>새 소식</b>\n<a href="https://example.com">원문 보기</a>')
        if sent:
            store.sent(key, 123)
        return key
    finally:
        store.close()


def test_recovery_queues_only_successful_posts_once_for_existing_subscribers(tmp_path):
    settings = configured(tmp_path)
    with MailingStore(settings.database_path) as mail:
        mail.subscribe("first@example.com", 1)
    key = archive(settings, sent=False)
    with MailingStore(settings.database_path) as mail:
        mail.subscribe("afterpublication@example.com", 3)
    assert mail_delivery.sync_mail_queue(settings)["outbox"] == {}
    store = Store(settings.database_path)
    store.sent(key, 123)
    store.close()
    assert mail_delivery.sync_mail_queue(settings)["outbox"] == {"queued": 1}
    with MailingStore(settings.database_path) as mail:
        mail.subscribe("later@example.com", 2)
    assert mail_delivery.sync_mail_queue(settings)["outbox"] == {"queued": 1}


def test_missing_smtp_preserves_post_and_queue(tmp_path):
    settings = replace(configured(tmp_path), smtp_host="")
    with MailingStore(settings.database_path) as mail:
        mail.subscribe("reader@example.com", 1)
    archive(settings)
    assert asyncio.run(mail_delivery.auto_send_mail(settings)) is None
    with MailingStore(settings.database_path) as mail:
        assert mail.outbox_counts() == {"queued": 1}
    with pytest.raises(ValueError, match="SMTP_HOST"):
        mail_delivery.send_mail_queue(settings)


def test_send_queue_does_not_republish_telegram(tmp_path, monkeypatch):
    settings = configured(tmp_path)
    archive(settings)
    calls = []

    def send(current, store, *, bot_username):
        calls.append((current, bot_username))
        return DeliveryReport(sent=1)

    monkeypatch.setattr(mail_delivery, "deliver_pending", send)
    assert mail_delivery.send_mail_queue(settings).sent == 1
    assert calls == [(settings, "example_bot")]


def test_smtp_credentials_are_not_in_repr_and_configuration_rejects_insecure_mode(tmp_path):
    settings = replace(configured(tmp_path), smtp_username="hidden-user", smtp_password="hidden-password")
    assert "hidden-user" not in repr(settings)
    assert "hidden-password" not in repr(settings)
    with pytest.raises(ValueError, match="SMTP_SECURITY"):
        replace(settings, smtp_security="none")
    with pytest.raises(ValueError, match="SMTP_USERNAME"):
        replace(settings, smtp_password="").require_mail()


@pytest.mark.parametrize("url", ["http://t.me/channel", "https://evil.example/channel", "https://t.me@evil.example/x"])
def test_invite_configuration_requires_telegram_url(url):
    with pytest.raises(ValueError, match="TELEGRAM_INVITE_URL"):
        Settings(telegram_invite_url=url)


@pytest.mark.parametrize("digest_fails", [False, True])
def test_preview_never_calls_automatic_mail_delivery(tmp_path, monkeypatch, digest_fails):
    settings = configured(tmp_path)
    result = pipeline.RunResult(messages=["preview"])

    async def digest(current, *, publish, slot):
        assert current is settings
        assert publish is False
        assert slot == "preview-slot"
        if digest_fails:
            raise RuntimeError("collection failed")
        return result

    async def forbidden(_settings):
        pytest.fail("a private latest request or preview must never queue or send email")

    monkeypatch.setattr(pipeline, "_run_digest", digest)
    monkeypatch.setattr(mail_delivery, "auto_send_mail", forbidden)
    if digest_fails:
        with pytest.raises(RuntimeError, match="collection failed"):
            asyncio.run(pipeline.run_digest(settings, publish=False, slot="preview-slot"))
    else:
        assert asyncio.run(pipeline.run_digest(settings, publish=False, slot="preview-slot")) is result


@pytest.mark.parametrize("mail_report", [None, DeliveryReport(sent=4, failed=2, uncertain=1)])
def test_publication_drains_mail_queue_and_reports_delivery_results(tmp_path, monkeypatch, mail_report):
    settings = configured(tmp_path)
    events = []
    result = pipeline.RunResult(posted=2, messages=["first", "second"])

    async def digest(current, *, publish, slot):
        assert current is settings
        assert publish is True
        assert slot == "scheduled-slot"
        events.append("telegram")
        return result

    async def mail(current):
        assert current is settings
        events.append("mail")
        return mail_report

    monkeypatch.setattr(pipeline, "_run_digest", digest)
    monkeypatch.setattr(mail_delivery, "auto_send_mail", mail)
    returned = asyncio.run(pipeline.run_digest(settings, publish=True, slot="scheduled-slot"))
    assert returned is result
    assert events == ["telegram", "mail"]
    assert result.posted == 2
    assert result.mailed == (4 if mail_report else 0)
    assert result.mail_failed == (3 if mail_report else 0)


def test_publication_error_still_drains_successful_post_queue_before_reraising(tmp_path, monkeypatch):
    settings = configured(tmp_path)
    events = []
    failure = RuntimeError("second Telegram post was rejected")

    async def digest(_settings, *, publish, slot):
        assert publish is True
        events.append("telegram-error")
        raise failure

    async def mail(current):
        assert current is settings
        events.append("mail")
        return DeliveryReport(sent=1)

    monkeypatch.setattr(pipeline, "_run_digest", digest)
    monkeypatch.setattr(mail_delivery, "auto_send_mail", mail)
    with pytest.raises(RuntimeError) as raised:
        asyncio.run(pipeline.run_digest(settings, publish=True))
    assert raised.value is failure
    assert events == ["telegram-error", "mail"]
