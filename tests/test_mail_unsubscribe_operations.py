import asyncio
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from types import SimpleNamespace

import pytest

from compdesign_bot import bot_runtime, cli, config, mail_delivery, mailing, pipeline
from compdesign_bot.config import Settings
from compdesign_bot.mail_bounce_sync import BounceSyncReport
from compdesign_bot.mail_unsubscribe import parse_unsubscribe
from compdesign_bot.mail_unsubscribe_sync import UnsubscribeSyncReport, UnsubscribeSyncUnavailable
from compdesign_bot.mailing import MailingStore


def configured(tmp_path):
    return Settings(
        database_path=tmp_path / "mail.sqlite3", mailing_enabled=True,
        mailing_unsubscribe_enabled=True, smtp_host="smtp.gmail.com",
        smtp_from="sender@example.com", smtp_username="PRIVATE_USERNAME",
        smtp_password="PRIVATE_PASSWORD", imap_host="imap.gmail.com",
        bot_username="example_bot", telegram_invite_url="https://t.me/example_channel",
    )


def forbid_smtp(monkeypatch):
    def forbidden(*_args, **_kwargs):
        pytest.fail("unexpected SMTP delivery")

    monkeypatch.setattr("smtplib.SMTP", forbidden)
    monkeypatch.setattr("smtplib.SMTP_SSL", forbidden)
    monkeypatch.setattr(mailing.SMTPMailer, "send", forbidden)


def test_reply_unsubscribe_configuration_is_opt_in_and_validates_without_secrets(tmp_path, monkeypatch):
    settings = configured(tmp_path)
    settings.require_unsubscribes()
    assert not Settings().mailing_unsubscribe_enabled
    assert "PRIVATE_" not in repr(settings)
    for overrides in (
        {"mailing_unsubscribe_enabled": False},
        {"imap_host": "imap.other.example"},
        {"smtp_username": ""},
        {"smtp_password": ""},
        {"smtp_from": ""},
        {"smtp_from": "PRIVATE_INVALID_ADDRESS"},
    ):
        with pytest.raises(ValueError) as raised:
            replace(settings, **overrides).require_unsubscribes()
        assert "PRIVATE_" not in str(raised.value)

    monkeypatch.setattr(config, "load_dotenv", lambda: None)
    monkeypatch.delenv("MAILING_UNSUBSCRIBE_ENABLED", raising=False)
    assert not Settings.from_env().mailing_unsubscribe_enabled
    monkeypatch.setenv("MAILING_UNSUBSCRIBE_ENABLED", "true")
    monkeypatch.setenv("IMAP_HOST", "IMAP.GMAIL.COM")
    parsed = Settings.from_env()
    assert parsed.mailing_unsubscribe_enabled and parsed.imap_host == "imap.gmail.com"
    monkeypatch.setenv("MAILING_UNSUBSCRIBE_ENABLED", "PRIVATE_INVALID_BOOLEAN")
    with pytest.raises(ValueError, match="MAILING_UNSUBSCRIBE_ENABLED") as raised:
        Settings.from_env()
    assert "PRIVATE_" not in str(raised.value)


def test_manual_unsubscribe_cli_scans_in_worker_and_never_sends(tmp_path, monkeypatch, capsys):
    settings = configured(tmp_path)
    main_thread = threading.get_ident()
    forbid_smtp(monkeypatch)

    def scan(current, mail):
        assert current is settings
        assert threading.get_ident() != main_thread
        assert mail.db.execute("SELECT COUNT(*) FROM mailing_subscribers").fetchone()[0] == 0
        return UnsubscribeSyncReport(scanned=4, applied=1)

    monkeypatch.setattr(mail_delivery, "sync_mail_unsubscribes", scan)
    monkeypatch.setattr(
        mail_delivery, "sync_mail_queue",
        lambda _settings: pytest.fail("manual unsubscribe synchronization must not queue mail"),
    )
    asyncio.run(cli.dispatch(SimpleNamespace(command="mail-sync-unsubscribes"), settings))
    output = capsys.readouterr().out
    report = json.loads(output)
    assert report["scanned"] == 4 and report["applied"] == 1
    assert "PRIVATE_" not in output


def test_manual_unsubscribe_disabled_fails_before_scanner_or_smtp(tmp_path, monkeypatch):
    forbid_smtp(monkeypatch)
    monkeypatch.setattr(
        mail_delivery, "sync_mail_unsubscribes",
        lambda *_args: pytest.fail("disabled scanner was called"),
    )
    with pytest.raises(ValueError, match="MAILING_UNSUBSCRIBE_ENABLED"):
        mail_delivery.sync_unsubscribe_requests(replace(configured(tmp_path), mailing_unsubscribe_enabled=False))


def test_reply_suppresses_queued_mail_before_bounce_review_and_smtp(tmp_path, monkeypatch):
    settings = replace(configured(tmp_path), mailing_bounce_enabled=True)
    with MailingStore(settings.database_path) as mail:
        mail.subscribe("reader@example.com")
        mail.store_post("earlier", "Earlier", "Earlier body")
        original_id = mail.db.execute("SELECT id FROM mailing_outbox").fetchone()[0]
        assert mail.claim_batch([original_id], "<original@example.com>")
        mail.finish_batch([original_id], "sent")
        mail.store_post("next", "Next", "Next body")

    events = []

    def sync_unsubscribes(_settings, mail):
        events.append("unsubscribe")
        request = parse_unsubscribe(
            b"From: reader@example.com\r\nTo: sender@example.com\r\n"
            b"Message-ID: <stop-reply@example.com>\r\nIn-Reply-To: <original@example.com>\r\n"
            b"Authentication-Results: mx.google.com; dkim=pass header.i=@example.com; "
            b"dmarc=pass header.from=example.com\r\nContent-Type: text/plain\r\n\r\nSTOP\r\n",
            recipient=settings.smtp_from,
        )
        assert request is not None
        assert mail.apply_unsubscribe_reply(request) == "applied"
        return UnsubscribeSyncReport(scanned=1, applied=1)

    def sync_bounces(_settings, mail):
        events.append("bounce")
        assert mail.status_counts()["active"] == 0
        assert mail.outbox_counts() == {"sent": 1, "suppressed": 1}
        return BounceSyncReport()

    monkeypatch.setattr(mail_delivery, "sync_mail_unsubscribes", sync_unsubscribes)
    monkeypatch.setattr(mail_delivery, "sync_mail_bounces", sync_bounces)
    forbid_smtp(monkeypatch)
    report = mail_delivery.send_mail_queue(settings)
    assert report.sent == 0 and report.deferred == 0
    assert events == ["unsubscribe", "bounce"]
    with MailingStore(settings.database_path) as mail:
        assert mail.status_counts()["active"] == 0
        assert mail.outbox_counts() == {"sent": 1, "suppressed": 1}
        mail.store_post("future", "Future", "Future body")
        assert mail.outbox_counts() == {"sent": 1, "suppressed": 1}


@pytest.mark.parametrize("failure", [False, True], ids=["backlog", "imap-failure"])
def test_unfinished_unsubscribe_scan_blocks_smtp_without_losing_queued_mail(tmp_path, monkeypatch, failure):
    settings = replace(configured(tmp_path), mailing_bounce_enabled=True)
    with MailingStore(settings.database_path) as mail:
        mail.subscribe("reader@example.com")
        mail.store_post("one", "Title", "Body")

    def scan(_settings, _mail):
        if failure:
            raise UnsubscribeSyncUnavailable("해지 요청 확인을 완료하지 못했습니다.")
        return UnsubscribeSyncReport(backlog=True)

    monkeypatch.setattr(mail_delivery, "sync_mail_unsubscribes", scan)
    monkeypatch.setattr(
        mail_delivery, "sync_mail_bounces", lambda *_args: pytest.fail("unsubscribe review was bypassed"),
    )
    forbid_smtp(monkeypatch)
    with pytest.raises(UnsubscribeSyncUnavailable):
        mail_delivery.send_mail_queue(settings)
    with MailingStore(settings.database_path) as mail:
        assert mail.status_counts()["active"] == 1
        assert mail.outbox_counts() == {"queued": 1}


@pytest.mark.parametrize("failure", [False, True], ids=["backlog", "imap-failure"])
def test_unsubscribe_outage_keeps_successful_telegram_publication(tmp_path, monkeypatch, caplog, failure):
    settings = configured(tmp_path)
    published = pipeline.RunResult(posted=1, messages=["Telegram publication"])

    async def digest(_settings, *, publish, slot):
        assert publish
        return published

    def scan(_settings, _mail):
        if failure:
            raise UnsubscribeSyncUnavailable("해지 요청 확인을 완료하지 못했습니다.")
        return UnsubscribeSyncReport(backlog=True)

    monkeypatch.setattr(pipeline, "_run_digest", digest)
    monkeypatch.setattr(mail_delivery, "sync_mail_unsubscribes", scan)
    forbid_smtp(monkeypatch)
    returned = asyncio.run(pipeline.run_digest(settings, publish=True))
    assert returned is published and returned.posted == 1 and returned.mailed == 0
    assert "PRIVATE_" not in caplog.text


def test_disabled_mail_delivery_skips_unsubscribe_scanner(tmp_path, monkeypatch):
    settings = replace(configured(tmp_path), mailing_unsubscribe_enabled=False)
    monkeypatch.setattr(
        mail_delivery, "sync_mail_unsubscribes", lambda *_args: pytest.fail("disabled scanner was called"),
    )
    forbid_smtp(monkeypatch)
    assert mail_delivery.send_mail_queue(settings).sent == 0


def test_unsubscribe_watcher_scans_immediately_retries_safely_and_can_be_cancelled(tmp_path, monkeypatch, caplog):
    settings = configured(tmp_path)
    events = []
    main_thread = threading.get_ident()
    forbid_smtp(monkeypatch)

    def scan(current):
        assert current is settings
        assert threading.get_ident() != main_thread
        events.append("scan")
        if events.count("scan") == 1:
            raise RuntimeError("PRIVATE_PASSWORD reader@example.com")
        return UnsubscribeSyncReport(applied=1)

    async def pause(seconds):
        assert seconds == 60
        assert events[-1] == "scan"
        events.append("sleep")
        if events.count("sleep") == 2:
            raise asyncio.CancelledError

    monkeypatch.setattr(mail_delivery, "sync_unsubscribe_requests", scan)
    monkeypatch.setattr(mail_delivery.asyncio, "sleep", pause)
    with (
        caplog.at_level("INFO", logger="compdesign_bot.mail_delivery"),
        pytest.raises(asyncio.CancelledError),
    ):
        asyncio.run(mail_delivery.watch_unsubscribe_requests(settings))
    assert events == ["scan", "sleep", "scan", "sleep"]
    assert "다음 확인 주기" in caplog.text and "자동 해지 1명" in caplog.text
    assert "PRIVATE_" not in caplog.text and "reader@example.com" not in caplog.text


def test_disabled_unsubscribe_watcher_never_scans_or_waits(tmp_path, monkeypatch):
    monkeypatch.setattr(
        mail_delivery, "sync_unsubscribe_requests", lambda *_args: pytest.fail("disabled scanner was called"),
    )

    async def pause(_seconds):
        pytest.fail("disabled watcher must return immediately")

    monkeypatch.setattr(mail_delivery.asyncio, "sleep", pause)
    settings = replace(configured(tmp_path), mailing_unsubscribe_enabled=False)
    asyncio.run(mail_delivery.watch_unsubscribe_requests(settings))


@pytest.mark.parametrize("command,channel,expected", [
    ("run", "", ["listen", "unsubscribe"]),
    ("run", "@channel", ["listen", "schedule", "unsubscribe"]),
    ("listen", "", ["listen", "unsubscribe"]),
    ("listen", "@channel", ["listen", "unsubscribe"]),
])
def test_run_and_listen_watch_replies_independently_of_channel_and_mailing(
    tmp_path, monkeypatch, command, channel, expected,
):
    settings = replace(configured(tmp_path), bot_token="TEST", channel_id=channel, mailing_enabled=False)
    events = []

    async def listener(current):
        assert current is settings
        events.append("listen")

    async def schedule(current):
        assert current is settings
        events.append("schedule")

    async def watcher(current):
        assert current is settings
        events.append("unsubscribe")

    monkeypatch.setattr(bot_runtime, "listen", listener)
    monkeypatch.setattr(cli, "serve", schedule)
    monkeypatch.setattr(mail_delivery, "watch_unsubscribe_requests", watcher)
    asyncio.run(cli.dispatch(SimpleNamespace(command=command), settings))
    assert sorted(events) == expected


@pytest.mark.parametrize("command", ["run", "listen"])
def test_disabled_unsubscribe_configuration_does_not_start_watcher(tmp_path, monkeypatch, command):
    settings = replace(configured(tmp_path), bot_token="TEST", mailing_unsubscribe_enabled=False)
    events = []

    async def listener(_settings):
        events.append("listen")

    async def watcher(_settings):
        pytest.fail("disabled watcher was started")

    monkeypatch.setattr(bot_runtime, "listen", listener)
    monkeypatch.setattr(mail_delivery, "watch_unsubscribe_requests", watcher)
    asyncio.run(cli.dispatch(SimpleNamespace(command=command), settings))
    assert events == ["listen"]


@pytest.mark.parametrize("enabled", [False, True])
@pytest.mark.parametrize("language,labels", [
    ("ko", ("구독 해지라고 답장",)),
    ("en", ("reply to this email with STOP",)),
    ("ja", ("STOP と返信",)),
    ("zh", ("回复此邮件",)),
    ("de", ("Antworten Sie auf diese E-Mail mit STOP",)),
    ("fr", ("répondez à cet e-mail avec STOP",)),
    ("es", ("responde a este correo con STOP",)),
    ("pt", ("responda a este e-mail com STOP",)),
    ("bilingual", ("구독 해지라고 답장", "reply to this email with STOP")),
])
def test_reply_unsubscribe_footer_is_localized_and_only_advertised_when_enabled(language, labels, enabled):
    unsubscribe_url = "https://t.me/example_bot?start=unsubscribe_PRIVATE_TOKEN"
    message = mailing.build_email(
        sender="sender@example.com", recipient="reader@example.com", title="Newsletter",
        telegram_html="Public newsletter body", invite_url="https://t.me/example_channel",
        unsubscribe_url=unsubscribe_url, language=language, reply_unsubscribe_enabled=enabled,
    )
    for kind in ("plain", "html"):
        content = message.get_body(preferencelist=(kind,)).get_content()
        assert unsubscribe_url in content
        assert ("STOP" in content) is enabled
        for label in labels:
            assert (label in content) is enabled
    assert message.get("Reply-To") == ("sender@example.com" if enabled else None)
    assert str(message["List-Unsubscribe"]) == f"<{unsubscribe_url}>"
    assert "List-Unsubscribe-Post" not in message


def test_default_email_footer_does_not_advertise_disabled_reply_unsubscribe():
    message = mailing.build_email(
        sender="sender@example.com", recipient="reader@example.com", title="Newsletter",
        telegram_html="Public newsletter body", invite_url="",
        unsubscribe_url="https://t.me/example_bot?start=unsubscribe_PRIVATE_TOKEN",
    )
    assert "Reply-To" not in message
    for kind in ("plain", "html"):
        assert "STOP" not in message.get_body(preferencelist=(kind,)).get_content()


@pytest.mark.parametrize("enabled", [False, True])
def test_durable_delivery_uses_reply_unsubscribe_setting_in_generated_mail(tmp_path, enabled):
    settings = replace(configured(tmp_path), mailing_unsubscribe_enabled=enabled)
    messages = []
    with MailingStore(settings.database_path) as mail:
        mail.subscribe("reader@example.com", language="ko")
        mail.store_post("one", "제목", "한국어 본문")
        report = mailing.deliver_pending(settings, mail, sender=messages.append)
        assert report.sent == 1 and mail.outbox_counts() == {"sent": 1}
    assert len(messages) == 1
    message = messages[0]
    assert message.get("Reply-To") == (settings.smtp_from if enabled else None)
    assert ("STOP" in message.get_body(preferencelist=("plain",)).get_content()) is enabled
    assert "List-Unsubscribe-Post" not in message


def test_automatic_delivery_waits_for_in_progress_reply_scan_instead_of_skipping_mail(tmp_path, monkeypatch):
    settings = configured(tmp_path)
    with MailingStore(settings.database_path) as mail:
        mail.subscribe("reader@example.com", language="ko")
        mail.store_post("one", "제목", "한국어 본문")
    scan_started = threading.Event()
    release_scan = threading.Event()
    events = []

    def scan(_settings, _mail):
        events.append("scan")
        if not scan_started.is_set():
            scan_started.set()
            assert release_scan.wait(timeout=5), "test did not release background scan"
        return UnsubscribeSyncReport()

    def send(_mailer, message):
        assert release_scan.is_set()
        assert str(message["To"]) == "reader@example.com"
        events.append("smtp")

    monkeypatch.setattr(mail_delivery, "sync_mail_unsubscribes", scan)
    monkeypatch.setattr(mailing.SMTPMailer, "send", send)
    with ThreadPoolExecutor(max_workers=2) as workers:
        background = workers.submit(mail_delivery.sync_unsubscribe_requests, settings)
        try:
            assert scan_started.wait(timeout=5), "background scanner did not start"
            automatic = workers.submit(asyncio.run, mail_delivery.auto_send_mail(settings))
            with pytest.raises(TimeoutError):
                automatic.result(timeout=0.1)
            assert events == ["scan"]
        finally:
            release_scan.set()
        assert background.result(timeout=5).scanned == 0
        assert automatic.result(timeout=5).sent == 1
    assert events == ["scan", "scan", "smtp"]
    with MailingStore(settings.database_path) as mail:
        assert mail.outbox_counts() == {"sent": 1}
