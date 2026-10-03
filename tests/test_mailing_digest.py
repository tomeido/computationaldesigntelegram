"""Recipient digests preserve delivery records and enforce message-level budgets."""

import smtplib
import sqlite3
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from email.message import EmailMessage

import pytest

from compdesign_bot.config import Settings
from compdesign_bot.mailing import (
    EmailContentError,
    EmailDeliveryError,
    EmailDeliveryPaused,
    EmailDeliveryUncertain,
    MailingStore,
    SMTPMailer,
    deliver_pending,
)


def configured(**overrides):
    return replace(
        Settings(
            smtp_host="smtp.example.com", smtp_from="sender@example.com",
            smtp_username="test-user", smtp_password="test-password",
            bot_username="digest_test_bot", telegram_invite_url="https://t.me/example_channel",
            mailing_enabled=True,
        ),
        **overrides,
    )


def seed(store, *, recipients=2, posts=2):
    for number in range(1, recipients + 1):
        store.subscribe(f"reader{number}@example.com", number)
    for number in range(1, posts + 1):
        store.store_post(
            f"post-{number:02}", f"Title {number:02}",
            f'<b>POST-{number:02}</b>\n<a href="https://example.com/{number}">Source {number}</a>',
        )


def ids_for(store, email):
    return [row[0] for row in store.db.execute(
        "SELECT id FROM mailing_outbox WHERE email=? ORDER BY id", (email,),
    )]


def body(message, kind="plain"):
    return message.get_body(preferencelist=(kind,)).get_content()


def test_two_posts_per_recipient_share_one_private_digest_and_delivery_record(tmp_path):
    settings = configured()
    messages = []
    with MailingStore(tmp_path / "mail.db") as store:
        seed(store)
        report = deliver_pending(settings, store, sender=messages.append)
        assert (report.sent, report.failed, report.uncertain, report.deferred) == (2, 0, 0, 0)
        assert len(messages) == 2
        assert store.outbox_counts() == {"sent": 4}
        assert len({str(message["Message-ID"]) for message in messages}) == 2
        assert len({str(message["List-Unsubscribe"]) for message in messages}) == 2
        for message in messages:
            recipient = str(message["To"])
            other = "reader2@example.com" if recipient == "reader1@example.com" else "reader1@example.com"
            assert message.get_all("To") == [recipient]
            assert "Cc" not in message and "Bcc" not in message
            assert other not in str(message)
            unsubscribe = str(message["List-Unsubscribe"])[1:-1]
            assert unsubscribe.startswith("https://t.me/digest_test_bot?start=unsubscribe_")
            for kind in ("plain", "html"):
                content = body(message, kind)
                assert content.index("POST-01") < content.index("POST-02")
                assert content.count("https://t.me/example_channel") == 1
                assert content.count("https://t.me/digest_test_bot?start=subscribe") == 1
                assert content.count(unsubscribe) == 1
            rows = store.db.execute(
                "SELECT batch_message_id,attempted_at,sent_at FROM mailing_outbox WHERE email=?",
                (recipient,),
            ).fetchall()
            assert {row["batch_message_id"] for row in rows} == {str(message["Message-ID"])}
            assert all(row["attempted_at"] and row["sent_at"] for row in rows)
        status = store.delivery_status(settings)
        assert status["pending_recipients"] == 0
        assert status["sent_messages_last_24h"] == 2
        assert status["remaining_daily_messages"] == 398
        assert status["last_sent_at"]
        assert (status["batch_limit"], status["daily_limit"]) == (200, 400)


def test_single_post_subject_and_no_repeat_survive_restart(tmp_path):
    path = tmp_path / "mail.db"
    settings = configured()
    messages = []
    with MailingStore(path) as store:
        seed(store, recipients=1, posts=1)
        assert deliver_pending(settings, store, sender=messages.append).sent == 1
        assert str(messages[0]["Subject"]) == "Title 01"
    with MailingStore(path) as store:
        assert deliver_pending(settings, store, sender=messages.append).sent == 0
        assert store.outbox_counts() == {"sent": 1}
    assert len(messages) == 1


def test_batch_limit_defers_recipient_digests_and_preserves_every_post(tmp_path):
    settings = configured(mailing_batch_limit=1, mailing_daily_limit=10)
    messages = []
    with MailingStore(tmp_path / "mail.db") as store:
        seed(store, recipients=3, posts=2)
        first = deliver_pending(settings, store, sender=messages.append)
        assert (first.sent, first.deferred) == (1, 2)
        assert store.outbox_counts() == {"queued": 4, "sent": 2}
        second = deliver_pending(settings, store, sender=messages.append)
        assert (second.sent, second.deferred) == (1, 1)
        third = deliver_pending(settings, store, sender=messages.append)
        assert (third.sent, third.deferred) == (1, 0)
        assert store.outbox_counts() == {"sent": 6}
    assert len({str(message["To"]) for message in messages}) == 3


def test_daily_limit_survives_multiple_delivery_invocations(tmp_path):
    settings = configured(mailing_batch_limit=10, mailing_daily_limit=2)
    messages = []
    with MailingStore(tmp_path / "mail.db") as store:
        seed(store, recipients=3)
        first = deliver_pending(settings, store, sender=messages.append)
        second = deliver_pending(settings, store, sender=messages.append)
        assert (first.sent, first.deferred) == (2, 1)
        assert (second.sent, second.deferred) == (0, 1)
        assert len(messages) == 2
        assert store.outbox_counts() == {"queued": 2, "sent": 4}
        status = store.delivery_status(settings)
        assert status["sent_messages_last_24h"] == 2
        assert status["remaining_daily_messages"] == 0
        assert status["pending_recipients"] == 1


def test_many_posts_use_one_daily_message_and_old_usage_expires(tmp_path):
    settings = configured(mailing_batch_limit=10, mailing_daily_limit=1)
    messages = []
    with MailingStore(tmp_path / "mail.db") as store:
        seed(store, recipients=1, posts=10)
        assert deliver_pending(settings, store, sender=messages.append).sent == 1
        assert store.outbox_counts() == {"sent": 10}
        assert store.delivery_status(settings)["sent_messages_last_24h"] == 1
        store.store_post("new-post", "A later post", "NEW-POST")
        assert deliver_pending(settings, store, sender=messages.append).deferred == 1
        old = (datetime.now(UTC) - timedelta(hours=24, minutes=1)).isoformat()
        with store.db:
            store.db.execute(
                "UPDATE mailing_outbox SET attempted_at=?,sent_at=? WHERE status='sent'", (old, old),
            )
        assert store.delivery_status(settings)["remaining_daily_messages"] == 1
        assert deliver_pending(settings, store, sender=messages.append).sent == 1
        assert len(messages) == 2
        assert store.delivery_status(settings)["sent_messages_last_24h"] == 1


def test_uncertain_digest_reserves_one_daily_message_without_resending(tmp_path):
    settings = configured(mailing_daily_limit=1)
    calls = []

    def uncertain(message):
        calls.append(message)
        raise EmailDeliveryUncertain("response lost")

    with MailingStore(tmp_path / "mail.db") as store:
        seed(store, recipients=1)
        first = deliver_pending(settings, store, sender=uncertain)
        assert (first.sent, first.uncertain) == (0, 1)
        assert store.outbox_counts() == {"uncertain": 2}
        store.subscribe("reader2@example.com", 2)
        store.store_post("new-post", "A later post", "NEW-POST")
        second = deliver_pending(settings, store, sender=uncertain)
        assert (second.sent, second.uncertain, second.deferred) == (0, 0, 2)
        assert len(calls) == 1
        status = store.delivery_status(settings)
        assert status["sent_messages_last_24h"] == 0
        assert status["reserved_messages_last_24h"] == 1
        assert status["remaining_daily_messages"] == 0


def test_pending_digest_claim_reserves_budget_atomically_across_workers(tmp_path):
    path = tmp_path / "mail.db"
    settings = configured(mailing_daily_limit=1)
    with MailingStore(path) as first, MailingStore(path) as second:
        seed(first)
        first_ids = ids_for(first, "reader1@example.com")
        second_ids = ids_for(second, "reader2@example.com")
        assert first.claim_batch(first_ids, "<one@example.com>", daily_limit=1)
        assert not second.claim_batch(first_ids, "<duplicate@example.com>", daily_limit=1)
        assert not second.claim_batch(second_ids, "<two@example.com>", daily_limit=1)
        assert first.outbox_counts() == {"pending": 2, "queued": 2}
        status = first.delivery_status(settings)
        assert status["reserved_messages_last_24h"] == 1
        assert status["remaining_daily_messages"] == 0
        old = (datetime.now(UTC) - timedelta(hours=25)).isoformat()
        with first.db:
            first.db.execute("UPDATE mailing_outbox SET attempted_at=? WHERE status='pending'", (old,))
        assert second.claim_batch(second_ids, "<two@example.com>", daily_limit=1)
        assert first.outbox_counts() == {"pending": 4}


def test_claim_batch_rejects_partially_claimed_snapshot_without_partial_update(tmp_path):
    with MailingStore(tmp_path / "mail.db") as store:
        seed(store, recipients=1)
        ids = ids_for(store, "reader1@example.com")
        assert store.claim(ids[0])
        assert not store.claim_batch(ids, "<digest@example.com>")
        assert store.outbox_counts() == {"pending": 1, "queued": 1}
        remaining = store.db.execute(
            "SELECT attempts,attempted_at,batch_message_id FROM mailing_outbox WHERE id=?", (ids[1],),
        ).fetchone()
        assert remaining["attempts"] == 0
        assert not remaining["attempted_at"] and not remaining["batch_message_id"]


def test_claim_batch_rejects_mixed_recipients(tmp_path):
    with MailingStore(tmp_path / "mail.db") as store:
        seed(store, recipients=2, posts=1)
        ids = [row[0] for row in store.db.execute("SELECT id FROM mailing_outbox")]
        assert not store.claim_batch(ids, "<mixed@example.com>")
        assert store.outbox_counts() == {"queued": 2}


@pytest.mark.parametrize("resubscribe", [False, True])
def test_smtp_pause_preserves_unsubscribe_during_inflight_attempt(tmp_path, resubscribe):
    with MailingStore(tmp_path / "mail.db") as store:
        seed(store, recipients=1)

        def send(_message):
            store.unsubscribe_user(1)
            if resubscribe:
                store.subscribe("reader1@example.com", 1)
            raise EmailDeliveryPaused("quota exceeded")

        report = deliver_pending(configured(), store, sender=send)
        assert report.paused and report.deferred == 0
        assert store.outbox_counts() == {"suppressed": 2}
        messages = []
        assert deliver_pending(configured(), store, sender=messages.append).sent == 0
        assert not messages


def test_finish_batch_rolls_back_all_rows_when_recording_one_row_fails(tmp_path):
    with MailingStore(tmp_path / "mail.db") as store:
        seed(store, recipients=1)
        ids = ids_for(store, "reader1@example.com")
        assert store.claim_batch(ids, "<digest@example.com>")
        store.db.execute(f"""
            CREATE TRIGGER reject_second_finish BEFORE UPDATE OF status ON mailing_outbox
            WHEN NEW.id={ids[1]} AND NEW.status='sent'
            BEGIN SELECT RAISE(ABORT, 'test write failure'); END;
        """)
        with pytest.raises(sqlite3.IntegrityError):
            store.finish_batch(ids, "sent")
        assert store.outbox_counts() == {"pending": 2}
        assert store.db.execute(
            "SELECT COUNT(*) FROM mailing_outbox WHERE sent_at IS NOT NULL",
        ).fetchone()[0] == 0
        store.db.execute("DROP TRIGGER reject_second_finish")
        store.finish_batch(ids, "sent")
        assert store.outbox_counts() == {"sent": 2}


def test_unsubscribe_before_batch_claim_prevents_send(tmp_path):
    messages = []
    with MailingStore(tmp_path / "mail.db") as store:
        seed(store, recipients=1)
        ids = ids_for(store, "reader1@example.com")
        assert store.unsubscribe_user(1) == 1
        assert not store.claim_batch(ids, "<digest@example.com>")
        assert deliver_pending(configured(), store, sender=messages.append).sent == 0
        assert store.outbox_counts() == {"suppressed": 2}
    assert not messages


def test_unsubscribe_during_recipient_loop_invalidates_loaded_snapshot(tmp_path):
    messages = []
    with MailingStore(tmp_path / "mail.db") as store:
        seed(store)

        def send(message):
            messages.append(message)
            assert store.unsubscribe_user(2) == 1

        assert deliver_pending(configured(), store, sender=send).sent == 1
        assert len(messages) == 1
        assert str(messages[0]["To"]) == "reader1@example.com"
        assert store.outbox_counts() == {"sent": 2, "suppressed": 2}


def test_smtp_pause_requeues_whole_digest_and_stops_remaining_recipients(tmp_path):
    messages = []
    settings = configured(mailing_daily_limit=10)

    def send(message):
        messages.append(message)
        if len(messages) == 2:
            raise EmailDeliveryPaused("temporary provider limit")

    with MailingStore(tmp_path / "mail.db") as store:
        seed(store, recipients=3)
        report = deliver_pending(settings, store, sender=send)
        assert (report.sent, report.failed, report.uncertain, report.deferred) == (1, 0, 0, 2)
        assert len(messages) == 2
        assert store.outbox_counts() == {"queued": 4, "sent": 2}
        assert store.db.execute(
            "SELECT COUNT(*) FROM mailing_outbox WHERE status='queued' AND error='smtp_paused'",
        ).fetchone()[0] == 2
        assert store.db.execute(
            "SELECT SUM(attempts) FROM mailing_outbox WHERE email='reader3@example.com'",
        ).fetchone()[0] == 0
        assert store.delivery_status(settings)["remaining_daily_messages"] == 9


def test_permanent_recipient_rejection_marks_whole_digest_and_continues(tmp_path):
    messages = []

    def send(message):
        messages.append(message)
        if str(message["To"]) == "reader2@example.com":
            raise EmailDeliveryError("recipient rejected")

    with MailingStore(tmp_path / "mail.db") as store:
        seed(store, recipients=3)
        report = deliver_pending(configured(), store, sender=send)
        assert (report.sent, report.failed, report.uncertain, report.deferred) == (2, 1, 0, 0)
        assert len(messages) == 3
        assert store.outbox_counts() == {"failed": 2, "sent": 4}
        assert deliver_pending(configured(), store, sender=send).sent == 0
        assert len(messages) == 3


def test_permanent_content_rejection_stops_before_other_recipients(tmp_path):
    messages = []

    def send(message):
        messages.append(message)
        raise EmailContentError("content rejected")

    with MailingStore(tmp_path / "mail.db") as store:
        seed(store, recipients=3)
        report = deliver_pending(configured(), store, sender=send)
        assert (report.sent, report.failed, report.deferred) == (0, 1, 2)
        assert len(messages) == 1
        assert store.outbox_counts() == {"failed": 2, "queued": 4}


@pytest.mark.parametrize("stage,code,response,expected", [
    ("data", 451, b"4.3.0 temporary service failure", EmailDeliveryPaused),
    ("data", 550, b"5.4.5 daily user sending limit exceeded", EmailDeliveryPaused),
    ("recipient", 450, b"4.2.0 mailbox temporarily unavailable", EmailDeliveryPaused),
    ("recipient", 550, b"5.1.1 unknown mailbox", EmailDeliveryError),
])
def test_smtp_explicit_response_distinguishes_provider_pause_from_bad_recipient(
    monkeypatch, stage, code, response, expected,
):
    class Connection:
        def __init__(self, *_args, **_kwargs):
            pass

        def login(self, *_args):
            pass

        def send_message(self, *_args, **_kwargs):
            if stage == "recipient":
                raise smtplib.SMTPRecipientsRefused({"reader@example.com": (code, response)})
            raise smtplib.SMTPDataError(code, response)

        def quit(self):
            pass

    monkeypatch.setattr(smtplib, "SMTP_SSL", Connection)
    message = EmailMessage()
    message["From"] = "sender@example.com"
    message["To"] = "reader@example.com"
    message.set_content("A local test message")
    with pytest.raises(expected) as error:
        SMTPMailer(configured(smtp_security="ssl", smtp_port=465)).send(message)
    if expected is EmailDeliveryError:
        assert not isinstance(error.value, EmailDeliveryPaused)


@pytest.mark.parametrize("status", ["pending", "uncertain", "failed"])
@pytest.mark.parametrize("retry", [False, True])
def test_resolving_one_digest_row_resolves_whole_group_only(tmp_path, status, retry):
    with MailingStore(tmp_path / "mail.db") as store:
        seed(store)
        ids = ids_for(store, "reader1@example.com")
        assert store.claim_batch(ids, "<digest@example.com>")
        if status != "pending":
            store.finish_batch(ids, status, "test_result")
        store.resolve(ids[0], retry=retry)
        expected = "queued" if retry else "sent"
        states = [row[0] for row in store.db.execute(
            "SELECT status FROM mailing_outbox WHERE email='reader1@example.com'",
        )]
        assert states == [expected, expected]
        assert store.db.execute(
            "SELECT COUNT(*) FROM mailing_outbox WHERE email='reader2@example.com' AND status='queued'",
        ).fetchone()[0] == 2
        if not retry:
            assert store.delivery_status(configured())["sent_messages_last_24h"] == 1


def test_legacy_outbox_schema_migrates_idempotently_without_losing_pending_mail(tmp_path):
    path = tmp_path / "mail.db"
    created_at = (datetime.now(UTC) - timedelta(hours=1)).isoformat()
    db = sqlite3.connect(path)
    db.executescript("""
        CREATE TABLE mailing_subscribers (
            email TEXT PRIMARY KEY, token TEXT NOT NULL UNIQUE, telegram_user_id INTEGER UNIQUE,
            active INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL
        );
        CREATE TABLE mailing_posts (
            post_key TEXT PRIMARY KEY, title TEXT NOT NULL, telegram_html TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE TABLE mailing_outbox (
            id INTEGER PRIMARY KEY, post_key TEXT NOT NULL, email TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'queued', attempts INTEGER NOT NULL DEFAULT 0,
            error TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL, UNIQUE(post_key,email)
        );
    """)
    db.execute("INSERT INTO mailing_subscribers VALUES(?,?,?,?,?)", (
        "reader1@example.com", "test-unsubscribe-token", 1, 1, created_at,
    ))
    db.execute("INSERT INTO mailing_posts VALUES(?,?,?,?)", ("legacy", "Legacy title", "LEGACY", created_at))
    db.execute("INSERT INTO mailing_outbox VALUES(?,?,?,?,?,?,?)", (
        77, "legacy", "reader1@example.com", "queued", 0, "", created_at,
    ))
    db.commit()
    db.close()
    messages = []
    with MailingStore(path) as store:
        columns = {row[1] for row in store.db.execute("PRAGMA table_info(mailing_outbox)")}
        assert {"attempted_at", "sent_at", "batch_message_id"} <= columns
        assert store.outbox_counts() == {"queued": 1}
        assert deliver_pending(configured(), store, sender=messages.append).sent == 1
    with MailingStore(path) as store:
        row = store.db.execute("SELECT * FROM mailing_outbox WHERE id=77").fetchone()
        assert row["post_key"] == "legacy" and row["status"] == "sent"
        assert row["attempted_at"] and row["sent_at"] and row["batch_message_id"]
        assert deliver_pending(configured(), store, sender=messages.append).sent == 0
    assert len(messages) == 1


def test_digest_post_limit_leaves_overflow_queued_for_next_batch(tmp_path):
    messages = []
    settings = configured(mailing_batch_limit=1)
    with MailingStore(tmp_path / "mail.db") as store:
        seed(store, recipients=1, posts=21)
        first = deliver_pending(settings, store, sender=messages.append)
        assert (first.sent, first.deferred) == (1, 1)
        assert store.outbox_counts() == {"queued": 1, "sent": 20}
        assert "POST-20" in body(messages[0]) and "POST-21" not in body(messages[0])
        second = deliver_pending(settings, store, sender=messages.append)
        assert (second.sent, second.deferred) == (1, 0)
        assert str(messages[1]["Subject"]) == "Title 21"
        assert store.outbox_counts() == {"sent": 21}


def test_digest_size_limit_leaves_large_second_post_queued(tmp_path):
    messages = []
    settings = configured(mailing_batch_limit=1)
    with MailingStore(tmp_path / "mail.db") as store:
        store.subscribe("reader1@example.com", 1)
        store.store_post("large-1", "First large post", "FIRST-LARGE " + "a" * 40000)
        store.store_post("large-2", "Second large post", "SECOND-LARGE " + "b" * 40000)
        report = deliver_pending(settings, store, sender=messages.append)
        assert (report.sent, report.deferred) == (1, 1)
        assert store.outbox_counts() == {"queued": 1, "sent": 1}
        assert "FIRST-LARGE" in body(messages[0]) and "SECOND-LARGE" not in body(messages[0])
