"""Recipient failures preserve delivery history and never bypass membership consent."""

import re
import smtplib
import sqlite3
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from html import escape
from zipfile import ZipFile

import pytest

from compdesign_bot import mailing
from compdesign_bot.config import Settings
from compdesign_bot.mail_bounces import BounceNotice
from compdesign_bot.mailing import MailingStore, deliver_pending

NOW = datetime(2026, 10, 4, 12, tzinfo=UTC)
MESSAGE_ID = "<digest-one@example.com>"


@pytest.fixture
def clock(monkeypatch):
    current = [NOW]

    class FrozenDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return current[0].astimezone(tz) if tz else current[0].replace(tzinfo=None)

    monkeypatch.setattr(mailing, "datetime", FrozenDateTime)
    return current


@pytest.fixture(autouse=True)
def local_content_only(monkeypatch):
    class Localizer:
        def __init__(self, *_args):
            pass

        def localize(self, _key, title, html, _language):
            return title, html

        def close(self):
            pass

    monkeypatch.setattr(mailing, "MailLocalizer", Localizer)


def configured(**overrides):
    return replace(Settings(
        smtp_host="smtp.example.com", smtp_from="sender@example.com",
        bot_username="bounce_test_bot", telegram_invite_url="https://t.me/example_channel",
        mailing_enabled=True,
    ), **overrides)


def notice(**overrides):
    return replace(BounceNotice(
        message_id=MESSAGE_ID, recipient="reader@example.com", action="failed",
        status="5.1.1", reason="invalid_mailbox", permanent=True,
    ), **overrides)


def outbox(store, email="reader@example.com"):
    return [dict(row) for row in store.db.execute(
        "SELECT * FROM mailing_outbox WHERE email=? ORDER BY id", (email,),
    )]


def block(store, email="reader@example.com"):
    row = store.db.execute("SELECT * FROM mailing_delivery_blocks WHERE email=?", (email,)).fetchone()
    return dict(row) if row else None


def member(store, email="reader@example.com"):
    return dict(store.db.execute("SELECT * FROM mailing_subscribers WHERE email=?", (email,)).fetchone())


def accepted(store, *, email="reader@example.com", user_id=11, status="sent", posts=2):
    assert store.subscribe(email, user_id) == "subscribed"
    for index in range(posts):
        store.store_post(f"accepted-{index}", f"Public title {index}", f"Public content {index}")
    ids = [row["id"] for row in outbox(store, email)]
    assert store.claim_batch(ids, MESSAGE_ID)
    if status != "pending":
        store.finish_batch(ids, status)
    return ids


def add_legacy_queue(store, *, email="reader@example.com", prefix="legacy", posts=2):
    """A pre-feature restore/retry may leave queued rows beside a durable block."""
    with store.db:
        for index in range(posts):
            key = f"{prefix}-{index}"
            store.db.execute("INSERT INTO mailing_posts VALUES(?,?,?,?)", (key, key, key, NOW.isoformat()))
            store.db.execute(
                "INSERT INTO mailing_outbox(post_key,email,created_at) VALUES(?,?,?)",
                (key, email, NOW.isoformat()),
            )
    return [row["id"] for row in outbox(store, email) if row["post_key"].startswith(prefix)]


def workbook(path, emails):
    ns = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
    rel = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
    with ZipFile(path, "w") as archive:
        archive.writestr("xl/workbook.xml", f'<workbook xmlns="{ns}" xmlns:r="{rel}"><sheets>'
                         '<sheet name="연락처" sheetId="1" r:id="rId1"/></sheets></workbook>')
        archive.writestr("xl/_rels/workbook.xml.rels", '<Relationships><Relationship Id="rId1" '
                         'Target="worksheets/sheet1.xml"/></Relationships>')
        archive.writestr("xl/worksheets/sheet1.xml", f'<worksheet xmlns="{ns}"><sheetData>' + "".join(
            f'<row r="{index}"><c r="A{index}" t="inlineStr"><is><t>{escape(value)}</t></is></c></row>'
            for index, value in enumerate(["이메일", *emails], 1)
        ) + '</sheetData></worksheet>')
    return path


@pytest.mark.parametrize("delivery_state", ["sent", "pending", "uncertain"])
@pytest.mark.parametrize("permanent", [True, False])
def test_bounce_preserves_original_digest_rows_and_message_budget(tmp_path, clock, delivery_state, permanent):
    with MailingStore(tmp_path / "mail.db") as store:
        ids = accepted(store, status=delivery_state)
        original = outbox(store)
        store.store_post("queued", "Queued post", "Queued content")
        store.store_post("failed", "Failed post", "Failed content")
        failed_id = outbox(store)[-1]["id"]
        assert store.claim(failed_id)
        store.finish(failed_id, "failed", "smtp_rejected")
        before = store.delivery_status(configured())
        event = notice() if permanent else notice(status="5.4.1", reason="recipient_rejected", permanent=False)
        assert store.apply_bounce(event) == "applied"
        assert [row for row in outbox(store) if row["id"] in ids] == original
        assert all(row["status"] == "suppressed" for row in outbox(store) if row["id"] not in ids)
        after = store.delivery_status(configured())
        for key in ("sent_messages_last_24h", "reserved_messages_last_24h", "remaining_daily_messages", "last_sent_at"):
            assert after[key] == before[key]
        assert after["pending_recipients"] == 0
        assert member(store)["active"] == (0 if permanent else 1)
        assert block(store)["state"] == ("bounced" if permanent else "held")
        assert store.bounce_status()["event_count"] == 1


@pytest.mark.parametrize("event", [
    notice(message_id="<different@example.com>"),
    notice(recipient="other@example.com"),
])
def test_bounce_requires_both_exact_message_id_and_envelope_recipient(tmp_path, clock, event):
    with MailingStore(tmp_path / "mail.db") as store:
        accepted(store)
        store.subscribe("other@example.com", 22)
        original = outbox(store)
        assert store.apply_bounce(event) == "unmatched"
        assert outbox(store) == original
        assert store.status_counts() == {"active": 2, "unsubscribed": 0}
        assert store.bounce_status()["eligible_subscribers"] == 2
        assert store.bounce_status()["event_count"] == 0


@pytest.mark.parametrize("state", ["queued", "failed", "suppressed"])
def test_unattempted_or_rejected_records_cannot_authenticate_a_dsn(tmp_path, clock, state):
    with MailingStore(tmp_path / "mail.db") as store:
        ids = accepted(store)
        with store.db:
            store.db.execute("UPDATE mailing_outbox SET status=? WHERE id IN (?,?)", (state, *ids))
        original = outbox(store)
        assert store.apply_bounce(notice()) == "unmatched"
        assert outbox(store) == original
        assert member(store)["active"] == 1
        assert block(store) is None


def test_duplicate_notice_remains_idempotent_after_restart(tmp_path, clock):
    path = tmp_path / "mail.db"
    with MailingStore(path) as store:
        accepted(store)
        assert store.apply_bounce(notice(permanent=False, status="5.7.1", reason="recipient_rejected")) == "applied"
        before = block(store)
    clock[0] += timedelta(hours=1)
    with MailingStore(path) as store:
        assert store.apply_bounce(notice(permanent=False, status="5.7.1", reason="recipient_rejected")) == "duplicate"
        assert block(store) == before
        assert store.bounce_status()["event_count"] == 1


def test_old_membership_notice_never_blocks_a_confirmed_owned_rejoin(tmp_path, clock):
    with MailingStore(tmp_path / "mail.db") as store:
        accepted(store)
        store.store_post("before-bounce", "Past post", "Past content")
        assert store.apply_bounce(notice()) == "applied"
        clock[0] += timedelta(hours=1)
        assert store.subscribe("reader@example.com", 11) == "subscribed"
        created_at = member(store)["created_at"]
        assert created_at == clock[0].isoformat()
        assert block(store) is None
        assert store.apply_bounce(notice()) == "duplicate"
        assert store.apply_bounce(notice(status="5.2.1", reason="mailbox_disabled")) == "stale"
        assert member(store)["active"] == 1 and block(store) is None
        assert store.bounce_status()["eligible_subscribers"] == 1
        assert store.bounce_status()["event_count"] == 2
        assert store.db.execute("SELECT applied FROM mailing_bounce_events WHERE status='5.2.1'").fetchone()[0] == 0
        assert not any(row["status"] == "queued" for row in outbox(store))
        store.store_post("after-rejoin", "Future post", "Future content")
        assert [row["post_key"] for row in outbox(store) if row["status"] == "queued"] == ["after-rejoin"]


def test_unsubscribed_membership_stays_inactive_when_a_notice_arrives(tmp_path, clock):
    with MailingStore(tmp_path / "mail.db") as store:
        accepted(store)
        assert store.unsubscribe_user(11) == 1
        before = member(store)
        assert store.apply_bounce(notice()) == "stale"
        assert member(store) == before
        assert block(store) is None


@pytest.mark.parametrize("wrong_owner", [None, 22])
def test_permanent_bounce_cannot_be_cleared_by_another_or_unbound_owner(tmp_path, clock, wrong_owner):
    with MailingStore(tmp_path / "mail.db") as store:
        accepted(store, user_id=11 if wrong_owner == 22 else None)
        assert store.apply_bounce(notice()) == "applied"
        assert store.subscribe("reader@example.com", wrong_owner) == "email_in_use"
        assert member(store)["active"] == 0 and block(store)["state"] == "bounced"
        assert not store.resume_bounce_hold("reader@example.com")


@pytest.mark.parametrize("status", ["5.4.1", "5.7.1"])
def test_routing_and_policy_failures_hold_indefinitely_without_unsubscribing(tmp_path, clock, status):
    with MailingStore(tmp_path / "mail.db") as store:
        accepted(store)
        assert store.apply_bounce(notice(status=status, reason="recipient_rejected", permanent=False)) == "applied"
        assert member(store)["active"] == 1
        assert block(store)["hold_until"] is None
        clock[0] += timedelta(days=365)
        assert store.release_expired_bounce_holds() == 0
        assert block(store)["state"] == "held"
        assert store.bounce_status()["held_subscribers"] == 1


def test_delayed_notice_holds_for_48_hours_then_allows_only_future_posts(tmp_path, clock):
    with MailingStore(tmp_path / "mail.db") as store:
        accepted(store)
        store.store_post("old-backlog", "Old backlog", "Old content")
        assert store.apply_bounce(notice(action="delayed", status="4.4.1", reason="temporary_failure", permanent=False)) == "applied"
        assert datetime.fromisoformat(block(store)["hold_until"]) == NOW + timedelta(hours=48)
        clock[0] = NOW + timedelta(hours=48) - timedelta(microseconds=1)
        assert store.release_expired_bounce_holds() == 0
        store.store_post("during-hold", "While held", "Held content")
        assert not any(row["post_key"] == "during-hold" for row in outbox(store))
        clock[0] += timedelta(microseconds=1)
        assert store.release_expired_bounce_holds() == 1
        assert member(store)["active"] == 1 and block(store) is None
        assert not any(row["status"] == "queued" for row in outbox(store))
        store.store_post("future", "Future post", "Future content")
        assert [row["post_key"] for row in outbox(store) if row["status"] == "queued"] == ["future"]


def test_delayed_notice_does_not_weaken_an_existing_indefinite_hold(tmp_path, clock):
    with MailingStore(tmp_path / "mail.db") as store:
        accepted(store)
        assert store.apply_bounce(notice(status="5.4.1", reason="recipient_rejected", permanent=False)) == "applied"
        assert store.apply_bounce(notice(action="delayed", status="4.4.1", reason="temporary_failure", permanent=False)) == "applied"
        assert block(store)["hold_until"] is None
        clock[0] += timedelta(days=30)
        assert store.release_expired_bounce_holds() == 0


def test_permanent_bounce_never_expires_or_downgrades_to_a_temporary_hold(tmp_path, clock):
    with MailingStore(tmp_path / "mail.db") as store:
        accepted(store)
        assert store.apply_bounce(notice()) == "applied"
        original = block(store)
        assert store.apply_bounce(notice(action="delayed", status="4.4.1", reason="temporary_failure", permanent=False)) == "stale"
        clock[0] += timedelta(days=365)
        assert store.release_expired_bounce_holds() == 0
        assert not store.resume_bounce_hold("reader@example.com")
        assert block(store) == original and member(store)["active"] == 0


def test_resume_hold_preserves_membership_and_never_requeues_old_backlog(tmp_path, clock):
    with MailingStore(tmp_path / "mail.db") as store:
        accepted(store)
        store.store_post("before-hold", "Backlog", "Content")
        original_membership = member(store)
        assert store.apply_bounce(notice(status="5.7.1", reason="recipient_rejected", permanent=False)) == "applied"
        store.store_post("during-hold", "Held post", "Content")
        assert store.resume_bounce_hold("Reader@EXAMPLE.com")
        assert not store.resume_bounce_hold("reader@example.com")
        assert member(store) == original_membership
        assert not any(row["status"] == "queued" for row in outbox(store))
        store.store_post("after-resume", "Future post", "Content")
        assert [row["post_key"] for row in outbox(store) if row["status"] == "queued"] == ["after-resume"]


def test_resume_and_expiry_do_not_reactivate_an_unsubscribed_held_member(tmp_path, clock):
    with MailingStore(tmp_path / "mail.db") as store:
        accepted(store)
        assert store.apply_bounce(notice(action="delayed", status="4.4.1", reason="temporary_failure", permanent=False)) == "applied"
        store.unsubscribe_user(11)
        assert not store.resume_bounce_hold("reader@example.com")
        clock[0] += timedelta(hours=49)
        store.release_expired_bounce_holds()
        assert member(store)["active"] == 0
        store.store_post("future", "Future post", "Content")
        assert not any(row["post_key"] == "future" for row in outbox(store))


@pytest.mark.parametrize("permanent", [True, False])
def test_block_prevents_new_queue_and_independent_atomic_claims(tmp_path, clock, permanent):
    path = tmp_path / "mail.db"
    with MailingStore(path) as store, MailingStore(path) as other_worker:
        accepted(store)
        event = notice() if permanent else notice(status="5.7.1", reason="recipient_rejected", permanent=False)
        assert other_worker.apply_bounce(event) == "applied"
        store.store_post("after-block", "Future post", "Content")
        assert not any(row["post_key"] == "after-block" for row in outbox(store))
        ids = add_legacy_queue(store)
        before = outbox(store)
        assert not store.claim(ids[0])
        assert not store.claim_batch(ids, "<blocked-retry@example.com>")
        assert outbox(store) == before
        assert store.delivery_status(configured())["pending_recipients"] == 0


def test_operator_retry_cannot_requeue_an_uncertain_blocked_delivery(tmp_path, clock):
    with MailingStore(tmp_path / "mail.db") as store:
        ids = accepted(store, status="uncertain")
        assert store.apply_bounce(notice(status="5.7.1", reason="recipient_rejected", permanent=False)) == "applied"
        store.resolve(ids[0], retry=True)
        assert all(row["status"] == "suppressed" for row in outbox(store))
        assert block(store)["state"] == "held"


def test_xlsx_import_preserves_bounced_and_held_records(tmp_path, clock):
    with MailingStore(tmp_path / "mail.db") as store:
        accepted(store)
        assert store.apply_bounce(notice()) == "applied"
        store.subscribe("held@example.com", 22)
        store.store_post("held-match", "Held title", "Content")
        held_id = outbox(store, "held@example.com")[0]["id"]
        assert store.claim_batch([held_id], "<held@example.com>")
        store.finish(held_id, "sent")
        assert store.apply_bounce(notice(message_id="<held@example.com>", recipient="held@example.com", status="5.7.1", reason="recipient_rejected", permanent=False)) == "applied"
        before = [dict(row) for row in store.db.execute("SELECT * FROM mailing_delivery_blocks ORDER BY email")]
        result = store.import_xlsx(workbook(tmp_path / "contacts.xlsx", ["reader@example.com", "held@example.com", "healthy@example.com"]))
        assert (result.imported, result.duplicates, result.suppressed) == (1, 1, 1)
        assert [dict(row) for row in store.db.execute("SELECT * FROM mailing_delivery_blocks ORDER BY email")] == before
        assert member(store)["active"] == 0 and member(store, "held@example.com")["active"] == 1
        assert store.bounce_status()["eligible_subscribers"] == 1


def test_apply_bounce_rolls_back_event_block_membership_and_queue_together(tmp_path, clock):
    with MailingStore(tmp_path / "mail.db") as store:
        accepted(store)
        store.store_post("backlog", "Backlog", "Content")
        before = outbox(store)
        store.db.execute("CREATE TRIGGER fail_bounce_queue BEFORE UPDATE OF status ON mailing_outbox "
                         "WHEN NEW.status='suppressed' BEGIN SELECT RAISE(ABORT,'fixture failure'); END")
        with pytest.raises(sqlite3.IntegrityError, match="fixture failure"):
            store.apply_bounce(notice())
        assert outbox(store) == before
        assert member(store)["active"] == 1 and block(store) is None
        assert store.bounce_status()["event_count"] == 0


def test_delivery_sends_only_healthy_recipient_even_with_legacy_blocked_queue(tmp_path, clock):
    messages = []
    with MailingStore(tmp_path / "mail.db") as store:
        accepted(store)
        assert store.apply_bounce(notice(status="5.7.1", reason="recipient_rejected", permanent=False)) == "applied"
        store.subscribe("bad@example.com", 22)
        store.store_post("bad-match", "Bad match", "Content")
        bad_id = outbox(store, "bad@example.com")[0]["id"]
        assert store.claim_batch([bad_id], "<bad@example.com>")
        store.finish(bad_id, "sent")
        assert store.apply_bounce(notice(message_id="<bad@example.com>", recipient="bad@example.com")) == "applied"
        store.subscribe("healthy@example.com", 33)
        store.store_post("future", "Future post", "Content")
        add_legacy_queue(store, prefix="legacy-held")
        add_legacy_queue(store, email="bad@example.com", prefix="legacy-bad")
        report = deliver_pending(configured(), store, sender=messages.append)
        assert (report.sent, report.failed, report.deferred) == (1, 0, 0)
        assert [str(message["To"]) for message in messages] == ["healthy@example.com"]
        assert all(row["attempts"] == 0 for email in ("reader@example.com", "bad@example.com")
                   for row in outbox(store, email) if row["post_key"].startswith("legacy"))
        assert store.bounce_status() == {
            "eligible_subscribers": 1, "held_subscribers": 1, "bounced_subscribers": 1,
            "event_count": 2, "reasons": {"recipient_rejected": 1, "invalid_mailbox": 1},
        }


def test_cursor_is_durable_monotonic_and_resets_only_for_new_uidvalidity(tmp_path, clock):
    path = tmp_path / "mail.db"
    with MailingStore(path) as store:
        assert store.bounce_cursor("sender/inbox") is None
        store.save_bounce_cursor("sender/inbox", "111", 10)
        store.save_bounce_cursor("sender/inbox", "111", 8)
        assert store.bounce_cursor("sender/inbox") == ("111", 10)
        store.save_bounce_cursor("sender/inbox", "111", 12)
        store.save_bounce_cursor("other/inbox", "222", 5)
    with MailingStore(path) as store:
        assert store.bounce_cursor("sender/inbox") == ("111", 12)
        assert store.bounce_cursor("other/inbox") == ("222", 5)
        store.save_bounce_cursor("sender/inbox", "333", 1)
        assert store.bounce_cursor("sender/inbox") == ("333", 1)


def test_scan_since_includes_the_first_attempt_day_in_imap_date_format(tmp_path, clock):
    with MailingStore(tmp_path / "mail.db") as store:
        assert re.fullmatch(r"\d{2}-[A-Z][a-z]{2}-\d{4}", store.bounce_scan_since())
        accepted(store)
        oldest = NOW - timedelta(days=75)
        with store.db:
            store.db.execute("UPDATE mailing_outbox SET attempted_at=?,sent_at=?", (oldest.isoformat(), oldest.isoformat()))
        parsed = datetime.strptime(store.bounce_scan_since(), "%d-%b-%Y").replace(tzinfo=UTC).date()
        assert parsed <= oldest.date()


def test_legacy_database_migration_preserves_identity_and_existing_delivery_records(tmp_path, clock):
    path = tmp_path / "mail.db"
    created = (NOW - timedelta(days=1)).isoformat()
    with sqlite3.connect(path) as db:
        db.executescript("""
            CREATE TABLE mailing_subscribers (
                email TEXT PRIMARY KEY, token TEXT NOT NULL UNIQUE, telegram_user_id INTEGER UNIQUE,
                active INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL
            );
            CREATE TABLE mailing_posts (post_key TEXT PRIMARY KEY,title TEXT NOT NULL,telegram_html TEXT NOT NULL,created_at TEXT NOT NULL);
            CREATE TABLE mailing_outbox (
                id INTEGER PRIMARY KEY,post_key TEXT NOT NULL,email TEXT NOT NULL,status TEXT NOT NULL DEFAULT 'queued',
                attempts INTEGER NOT NULL DEFAULT 0,error TEXT NOT NULL DEFAULT '',created_at TEXT NOT NULL,UNIQUE(post_key,email)
            );
        """)
        db.execute("INSERT INTO mailing_subscribers VALUES(?,?,?,?,?)", ("reader@example.com", "fixture-token", 11, 1, created))
        db.execute("INSERT INTO mailing_posts VALUES(?,?,?,?)", ("old-post", "Old title", "Old content", created))
        db.execute("INSERT INTO mailing_outbox VALUES(?,?,?,?,?,?,?)", (1, "old-post", "reader@example.com", "sent", 1, "", created))
    for _ in range(2):
        with MailingStore(path) as store:
            assert member(store)["token"] == "fixture-token" and member(store)["active"] == 1
            assert member(store)["created_at"] == created and member(store)["language"] == "ko"
            row = outbox(store)[0]
            assert (row["id"], row["status"], row["attempts"], row["created_at"]) == (1, "sent", 1, created)
            assert row["batch_message_id"] is None
            assert store.bounce_status() == {"eligible_subscribers": 1, "held_subscribers": 0, "bounced_subscribers": 0, "event_count": 0, "reasons": {}}


def install_smtp(monkeypatch, *, refusal=None, failure=None):
    calls = []

    class SMTP:
        def __init__(self, *_args, **_kwargs):
            pass

        def ehlo(self):
            pass

        def starttls(self, **_kwargs):
            pass

        def login(self, *_args):
            if failure == "auth":
                raise smtplib.SMTPAuthenticationError(535, b"5.7.8 authentication rejected")

        def send_message(self, message, **_kwargs):
            email = str(message["To"])
            calls.append(email)
            if failure == "sender":
                raise smtplib.SMTPSenderRefused(550, b"5.7.1 sender rejected", "sender@example.com")
            if failure == "data":
                raise smtplib.SMTPDataError(550, b"5.7.1 content policy rejection")
            return {email: (550, refusal)} if refusal and email == "bad@example.com" else {}

        def quit(self):
            pass

    monkeypatch.setattr(mailing.smtplib, "SMTP", SMTP)
    return calls


@pytest.mark.parametrize("status,permanent", [("5.1.1", True), ("5.4.1", False)])
def test_smtp_recipient_failure_blocks_remaining_digests_but_healthy_recipients_continue(tmp_path, clock, monkeypatch, status, permanent):
    calls = install_smtp(monkeypatch, refusal=f"{status} recipient rejected".encode())
    with MailingStore(tmp_path / "mail.db") as store:
        store.subscribe("bad@example.com", 11)
        store.subscribe("healthy@example.com", 22)
        for index in range(21):
            store.store_post(f"post-{index}", f"Title {index}", "Public content")
        report = deliver_pending(configured(), store)
        assert (report.sent, report.failed, report.deferred) == (2, 1, 0)
        assert calls == ["bad@example.com", "healthy@example.com", "healthy@example.com"]
        assert member(store, "bad@example.com")["active"] == (0 if permanent else 1)
        assert block(store, "bad@example.com")["state"] == ("bounced" if permanent else "held")
        bad_rows = outbox(store, "bad@example.com")
        assert sum(row["status"] == "failed" for row in bad_rows) == 20
        assert sum(row["status"] == "suppressed" for row in bad_rows) == 1
        assert store.delivery_status(configured())["sent_messages_last_24h"] == 2
        store.store_post("future", "Future title", "Content")
        assert deliver_pending(configured(), store).sent == 1
        assert calls.count("bad@example.com") == 1


@pytest.mark.parametrize("failure", ["data", "sender", "auth"])
def test_batch_wide_content_sender_and_auth_errors_never_disable_recipients(tmp_path, clock, monkeypatch, failure):
    install_smtp(monkeypatch, failure=failure)
    with MailingStore(tmp_path / "mail.db") as store:
        store.subscribe("bad@example.com", 11)
        store.subscribe("healthy@example.com", 22)
        store.store_post("post", "Public title", "Public content")
        settings = configured(smtp_username="fixture-user", smtp_password="fixture-password")
        report = deliver_pending(settings, store)
        assert (report.sent, report.failed, report.deferred) == (0, 1, 1)
        assert store.status_counts() == {"active": 2, "unsubscribed": 0}
        assert store.bounce_status() == {"eligible_subscribers": 2, "held_subscribers": 0, "bounced_subscribers": 0, "event_count": 0, "reasons": {}}
