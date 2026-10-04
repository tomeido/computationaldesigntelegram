import asyncio
import smtplib
import threading
from dataclasses import replace
from types import SimpleNamespace

import pytest

from compdesign_bot import cli, config, mail_bounce_sync, mail_delivery
from compdesign_bot.config import Settings
from compdesign_bot.mail_bounce_sync import BounceSyncReport, BounceSyncUnavailable
from compdesign_bot.mailing import MailingStore, deliver_pending


def configured(tmp_path):
    return Settings(
        database_path=tmp_path / "mail.sqlite3", mailing_bounce_enabled=True,
        smtp_host="smtp.gmail.com", smtp_from="sender@example.com",
        smtp_username="PRIVATE_USERNAME", smtp_password="PRIVATE_PASSWORD",
        imap_host="imap.gmail.com", bot_username="example_bot",
        telegram_invite_url="https://t.me/example_channel",
    )


def test_manual_scan_constructs_sqlite_connection_in_its_worker_and_does_not_send(
    tmp_path, monkeypatch, capsys,
):
    settings = configured(tmp_path)
    main_thread = threading.get_ident()

    def scan(current, mail):
        assert current is settings
        assert threading.get_ident() != main_thread
        # sqlite3 refuses this query if the connection belongs to another thread.
        assert mail.db.execute("SELECT COUNT(*) FROM mailing_subscribers").fetchone()[0] == 0
        return BounceSyncReport(scanned=3)

    monkeypatch.setattr(mail_bounce_sync, "sync_mail_bounces", scan)
    monkeypatch.setattr(mail_delivery, "deliver_pending", lambda *_a, **_k: pytest.fail("unexpected SMTP"))
    asyncio.run(cli.dispatch(SimpleNamespace(command="mail-sync-bounces"), settings))
    output = capsys.readouterr().out
    assert '"scanned": 3' in output
    assert "PRIVATE_" not in output


@pytest.mark.parametrize("result", [BounceSyncReport(backlog=True), BounceSyncReport(untrusted=1)])
def test_incomplete_bounce_review_blocks_smtp_and_preserves_queue(tmp_path, monkeypatch, result):
    settings = configured(tmp_path)
    with MailingStore(settings.database_path) as mail:
        mail.subscribe("reader@example.com")
        mail.store_post("one", "Title", "Body")
    monkeypatch.setattr(mail_delivery, "sync_mail_queue", lambda _s: None)
    monkeypatch.setattr(mail_delivery, "sync_mail_bounces", lambda _s, _m: result)
    monkeypatch.setattr(mail_delivery, "deliver_pending", lambda *_a, **_k: pytest.fail("unexpected SMTP"))
    with pytest.raises(BounceSyncUnavailable):
        mail_delivery.send_mail_queue(settings)
    with MailingStore(settings.database_path) as mail:
        assert mail.outbox_counts() == {"queued": 1}


def test_imap_failure_isolated_from_telegram_publication(tmp_path, monkeypatch, caplog):
    settings = replace(configured(tmp_path), mailing_enabled=True)
    monkeypatch.setattr(mail_delivery, "sync_mail_queue", lambda _s: None)

    def failure(_s, _m):
        raise BounceSyncUnavailable("반송 메일 조회를 완료하지 못했습니다.")

    monkeypatch.setattr(mail_delivery, "sync_mail_bounces", failure)
    monkeypatch.setattr(mail_delivery, "deliver_pending", lambda *_a, **_k: pytest.fail("unexpected SMTP"))
    assert asyncio.run(mail_delivery.auto_send_mail(settings)) is None
    assert "PRIVATE_" not in caplog.text


def test_gmail_scan_configuration_reuses_secret_credentials_without_exposing_them(tmp_path, monkeypatch):
    settings = configured(tmp_path)
    settings.require_bounces()
    assert "PRIVATE_" not in repr(settings)
    for invalid in (
        replace(settings, mailing_bounce_enabled=False),
        replace(settings, imap_host="imap.other.example"),
        replace(settings, smtp_password=""),
    ):
        with pytest.raises(ValueError) as error:
            invalid.require_bounces()
        assert "PRIVATE_" not in str(error.value)
    monkeypatch.setattr(config, "load_dotenv", lambda: None)
    monkeypatch.setenv("MAILING_BOUNCE_ENABLED", "true")
    monkeypatch.setenv("IMAP_HOST", "IMAP.GMAIL.COM")
    monkeypatch.setenv("IMAP_PORT", "993")
    parsed = Settings.from_env()
    assert parsed.mailing_bounce_enabled and parsed.imap_host == "imap.gmail.com" and parsed.imap_port == 993
    monkeypatch.setenv("IMAP_PORT", "0")
    with pytest.raises(ValueError, match="IMAP_PORT"):
        Settings.from_env()


def test_manual_resume_changes_only_future_delivery_without_sending(tmp_path, monkeypatch, capsys):
    settings = configured(tmp_path)
    with MailingStore(settings.database_path) as mail:
        mail.subscribe("reader@example.com")
        mail.db.execute(
            "INSERT INTO mailing_delivery_blocks VALUES(?,?,?,?,?,?)",
            ("reader@example.com", "held", "recipient_rejected", "5.4.1", None, "2026-10-04T00:00:00+00:00"),
        )
        mail.db.commit()
    monkeypatch.setattr(mail_delivery, "deliver_pending", lambda *_a, **_k: pytest.fail("unexpected SMTP"))
    asyncio.run(cli.dispatch(SimpleNamespace(command="mail-resume", email="reader@example.com"), settings))
    output = capsys.readouterr().out
    assert '"resumed_future_delivery": true' in output
    assert "reader@example.com" not in output
    with MailingStore(settings.database_path) as mail:
        assert mail.bounce_status()["held_subscribers"] == 0 and mail.outbox_counts() == {}


@pytest.mark.parametrize("response", [b"5.4.5 quota exceeded", b"daily user sending limit exceeded"])
def test_recipient_stage_account_limit_keeps_address_and_queue(tmp_path, monkeypatch, response):
    class Connection:
        def __init__(self, *_a, **_k):
            pass

        def login(self, *_a):
            pass

        def send_message(self, *_a, **_k):
            raise smtplib.SMTPRecipientsRefused({"reader@example.com": (550, response)})

        def quit(self):
            pass

    monkeypatch.setattr(smtplib, "SMTP_SSL", Connection)
    settings = replace(configured(tmp_path), smtp_security="ssl", smtp_port=465)
    with MailingStore(settings.database_path) as mail:
        mail.subscribe("reader@example.com")
        mail.store_post("one", "Title", "Body")
        result = deliver_pending(settings, mail)
        assert result.paused and result.failed == 0
        assert mail.status_counts()["active"] == 1
        assert mail.bounce_status()["event_count"] == 0
        assert mail.outbox_counts() == {"queued": 1}
