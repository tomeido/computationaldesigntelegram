"""Reply opt-outs are tied to the original recipient and membership generation."""

import sqlite3
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from compdesign_bot import mailing
from compdesign_bot.config import Settings
from compdesign_bot.mail_unsubscribe import UnsubscribeRequest
from compdesign_bot.mailing import MailingStore

NOW = datetime(2026, 10, 4, 12, tzinfo=UTC)
ORIGINAL = "<newsletter@example.org>"


@pytest.fixture
def clock(monkeypatch):
    current = [NOW]

    class FrozenDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return current[0].astimezone(tz) if tz else current[0].replace(tzinfo=None)

    monkeypatch.setattr(mailing, "datetime", FrozenDateTime)
    return current


def request(**overrides):
    return replace(UnsubscribeRequest("<reply@example.org>", "reader@example.org", (ORIGINAL,)), **overrides)


def rows(store):
    return [dict(row) for row in store.db.execute("SELECT * FROM mailing_outbox ORDER BY id")]


def member(store):
    return dict(store.db.execute("SELECT * FROM mailing_subscribers WHERE email='reader@example.org'").fetchone())


def attempted(store, state="sent"):
    assert store.subscribe("reader@example.org", 11) == "subscribed"
    store.store_post("original", "Original title", "Original content")
    original_id = rows(store)[0]["id"]
    assert store.claim_batch([original_id], ORIGINAL)
    if state != "pending":
        store.finish(original_id, state)
    return original_id


@pytest.mark.parametrize("state", ["sent", "pending", "uncertain", "failed"])
def test_opt_out_suppresses_queue_preserves_sent_and_reserved_history_and_budget(tmp_path, clock, state):
    with MailingStore(tmp_path / "mail.db") as store:
        original_id = attempted(store, state)
        original = rows(store)[0]
        store.store_post("queued", "Queued", "Queued")
        store.store_post("failed", "Failed", "Failed")
        failed_id = rows(store)[-1]["id"]
        assert store.claim(failed_id)
        store.finish(failed_id, "failed", "smtp_rejected")
        before = store.delivery_status(Settings())
        assert store.unsubscribe_reply_matches(request())
        assert store.apply_unsubscribe_reply(request()) == "applied"
        if state != "failed":
            assert rows(store)[0] == original
        else:
            assert rows(store)[0]["status"] == "suppressed"
        assert all(row["status"] == "suppressed" for row in rows(store) if row["id"] != original_id)
        after = store.delivery_status(Settings())
        for key in ("sent_messages_last_24h", "reserved_messages_last_24h", "remaining_daily_messages", "last_sent_at"):
            assert after[key] == before[key]
        assert member(store)["active"] == 0
        assert store.db.execute("SELECT applied FROM mailing_unsubscribe_events").fetchone()[0] == 1


@pytest.mark.parametrize("unmatched", [
    request(email="other@example.org"), request(references=("<wrong@example.org>",)), request(references=()),
])
def test_reply_requires_exact_recipient_and_referenced_delivery(tmp_path, clock, unmatched):
    with MailingStore(tmp_path / "mail.db") as store:
        attempted(store)
        store.subscribe("other@example.org", 22)
        before = rows(store)
        assert not store.unsubscribe_reply_matches(unmatched)
        assert store.apply_unsubscribe_reply(unmatched) == "unmatched"
        assert member(store)["active"] == 1 and rows(store) == before
        assert store.db.execute("SELECT COUNT(*) FROM mailing_unsubscribe_events").fetchone()[0] == 0


def test_batch_message_id_without_delivery_attempt_does_not_prove_receipt(tmp_path, clock):
    with MailingStore(tmp_path / "mail.db") as store:
        attempted(store)
        with store.db:
            store.db.execute("UPDATE mailing_outbox SET attempted_at=NULL")
        assert not store.unsubscribe_reply_matches(request())
        assert store.apply_unsubscribe_reply(request()) == "unmatched"
        assert member(store)["active"] == 1


def test_processed_reply_is_idempotent_across_restart_and_rejoin(tmp_path, clock):
    path = tmp_path / "mail.db"
    with MailingStore(path) as store:
        attempted(store)
        assert store.apply_unsubscribe_reply(request()) == "applied"
    clock[0] += timedelta(hours=1)
    with MailingStore(path) as store:
        assert store.subscribe("reader@example.org", 11) == "subscribed"
        assert store.apply_unsubscribe_reply(request()) == "duplicate"
        assert member(store)["active"] == 1
        assert store.db.execute("SELECT COUNT(*) FROM mailing_unsubscribe_events").fetchone()[0] == 1


def test_unprocessed_old_reply_does_not_revoke_new_membership(tmp_path, clock):
    with MailingStore(tmp_path / "mail.db") as store:
        attempted(store)
        assert store.unsubscribe(11)
        clock[0] += timedelta(hours=1)
        assert store.subscribe("reader@example.org", 11) == "subscribed"
        store.store_post("new", "New", "New")
        before = rows(store)
        assert store.unsubscribe_reply_matches(request())
        assert store.apply_unsubscribe_reply(request()) == "stale"
        assert member(store)["active"] == 1 and rows(store) == before
        assert store.apply_unsubscribe_reply(request()) == "duplicate"


def test_latest_matching_attempt_allows_reply_from_current_membership(tmp_path, clock):
    with MailingStore(tmp_path / "mail.db") as store:
        attempted(store)
        store.unsubscribe(11)
        clock[0] += timedelta(hours=1)
        store.subscribe("reader@example.org", 11)
        store.store_post("new", "New", "New")
        new_id = rows(store)[-1]["id"]
        assert store.claim_batch([new_id], "<new@example.org>")
        store.finish(new_id, "sent")
        assert store.apply_unsubscribe_reply(request(references=(ORIGINAL, "<new@example.org>"))) == "applied"
        assert member(store)["active"] == 0


def test_inactive_subscriber_is_audited_once_without_other_mutations(tmp_path, clock):
    with MailingStore(tmp_path / "mail.db") as store:
        attempted(store)
        store.unsubscribe(11)
        before = member(store), rows(store)
        assert store.apply_unsubscribe_reply(request()) == "stale"
        assert store.apply_unsubscribe_reply(request()) == "duplicate"
        assert (member(store), rows(store)) == before
        assert store.db.execute("SELECT applied FROM mailing_unsubscribe_events").fetchone()[0] == 0


def test_suppression_failure_rolls_back_membership_event_and_entire_queue(tmp_path, clock):
    with MailingStore(tmp_path / "mail.db") as store:
        attempted(store)
        store.store_post("queued", "Queued", "Queued")
        store.db.executescript("""
            CREATE TRIGGER reject_suppression BEFORE UPDATE OF status ON mailing_outbox
            WHEN NEW.status='suppressed' BEGIN SELECT RAISE(ABORT,'test failure'); END;
        """)
        before = rows(store)
        with pytest.raises(sqlite3.IntegrityError):
            store.apply_unsubscribe_reply(request())
        assert member(store)["active"] == 1 and rows(store) == before
        assert store.db.execute("SELECT COUNT(*) FROM mailing_unsubscribe_events").fetchone()[0] == 0


def test_unsubscribe_cursor_is_independent_monotonic_and_resets_for_new_uidvalidity(tmp_path):
    with MailingStore(tmp_path / "mail.db") as store:
        store.save_bounce_cursor("same-folder", "123", 999)
        assert store.unsubscribe_cursor("same-folder") is None
        store.save_unsubscribe_cursor("same-folder", "123", 30)
        store.save_unsubscribe_cursor("same-folder", "123", 2)
        assert store.unsubscribe_cursor("same-folder") == ("123", 30)
        store.save_unsubscribe_cursor("same-folder", "456", 1)
        assert store.unsubscribe_cursor("same-folder") == ("456", 1)
        assert store.bounce_cursor("same-folder") == ("123", 999)
