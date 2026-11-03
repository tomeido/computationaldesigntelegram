"""Mail reads are bounded, authenticated and replay safe without modifying flags."""

import imaplib
import re
import ssl
from dataclasses import asdict
from email import policy
from email.parser import BytesHeaderParser
from types import SimpleNamespace

import pytest

from compdesign_bot import mail_unsubscribe_sync as sync
from compdesign_bot.mailing import MailingStore

SINCE = "03-Oct-2026"
AUTH = "mx.google.com; dkim=pass header.i=@example.org; dmarc=pass header.from=example.org"
ORIGINAL = "<newsletter@example.org>"


def reply(*, body="STOP", sender="reader@example.org", recipient="newsletter@example.org",
          reference=ORIGINAL, message_id="<reply@example.org>", auth=AUTH, extra=""):
    auth_header = f"Authentication-Results: {auth}\r\n" if auth is not None else ""
    return (
        f"From: Reader <{sender}>\r\nTo: {recipient}\r\nMessage-ID: {message_id}\r\n"
        f"In-Reply-To: {reference}\r\nSubject: Re: Daily newsletter\r\n{auth_header}{extra}"
        f"Content-Type: text/plain; charset=utf-8\r\n\r\n{body}\r\n"
    ).encode()


def settings():
    return SimpleNamespace(
        mailing_unsubscribe_enabled=True, imap_host="imap.gmail.com", imap_port=993,
        smtp_username="account@example.org", smtp_from="newsletter@example.org",
        smtp_password="test-app-password", require_unsubscribes=lambda: None,
    )


def large_attachment_reply():
    header = reply().split(b"\r\n\r\n", 1)[0].replace(
        b"Content-Type: text/plain; charset=utf-8", b'Content-Type: multipart/mixed; boundary="large-reply"',
    )
    return (
        header + b"\r\n\r\n--large-reply\r\nContent-Type: text/plain\r\n\r\nSTOP\r\n"
        b"--large-reply\r\nContent-Type: application/octet-stream\r\n"
        b'Content-Disposition: attachment; filename="large.bin"\r\n\r\n' + b"x" * 600_000
        + b"\r\n--large-reply--\r\n"
    )


class Store:
    def __init__(self, outcomes=None):
        self.cursors = {}
        self.checkpoints = []
        self.applied = []
        self.events = set()
        self.outcomes = outcomes or {}

    def bounce_scan_since(self):
        return SINCE

    def unsubscribe_cursor(self, key):
        return self.cursors.get(key)

    def save_unsubscribe_cursor(self, key, validity, uid):
        assert re.fullmatch(r"[a-f0-9]{64}", key)
        self.cursors[key] = validity, uid
        self.checkpoints.append((key, validity, uid))

    def unsubscribe_reply_matches(self, request):
        return request.email == "reader@example.org" and ORIGINAL in request.references

    def apply_unsubscribe_reply(self, request):
        self.applied.append(request)
        if request.message_id in self.outcomes:
            return self.outcomes[request.message_id]
        identity = request.message_id, request.email
        if identity in self.events:
            return "duplicate"
        self.events.add(identity)
        return "applied"


class IMAP:
    def __init__(self, folders=None):
        self.folders = folders or [(b"INBOX", b"\\HasNoChildren", "123", {1: reply()})]
        self.current = None
        self.calls = []
        self.constructor = None
        self.full_fetch_failure = None
        self.header_fetch_failure = None
        self.missing_body_uid = None
        self.size_override = None
        self.prefix_uid_override = None
        self.prefix_offset = 0
        self.prefix_missing_byte = False
        self.search_override = None
        self.logged_out = False

    def factory(self, *args, **kwargs):
        self.constructor = args, kwargs
        return self

    def login(self, username, password):
        self.calls.append(("LOGIN",))
        assert (username, password) == ("account@example.org", "test-app-password")
        return "OK", [b"authenticated"]

    def list(self):
        self.calls.append(("LIST",))
        return "OK", [b"(" + flags + b') "/" "' + name + b'"' for name, flags, _, _ in self.folders]

    def select(self, mailbox, readonly=False):
        self.calls.append(("SELECT", mailbox, readonly))
        name = re.sub(rb"\\(.)", rb"\1", mailbox[1:-1])
        self.current = next(row for row in self.folders if row[0] == name)
        return "OK", [str(len(self.current[3])).encode()]

    def response(self, code):
        assert code == "UIDVALIDITY"
        return code, [self.current[2].encode()]

    def uid(self, command, *args):
        self.calls.append((command, *args))
        messages = self.current[3]
        if command == "SEARCH":
            if self.search_override is not None:
                uids = self.search_override
            elif args[1] == "UID":
                minimum = int(args[2].split(":", 1)[0])
                uids = [uid for uid in messages if uid >= minimum]
            else:
                assert args == (None, "SINCE", SINCE)
                uids = list(messages)
            return "OK", [b" ".join(str(uid).encode() for uid in uids)]
        assert command == "FETCH"
        uid_set, query = args
        uids = [int(uid) for uid in uid_set.split(",")]
        if query == "(UID RFC822.SIZE)":
            assert len(uids) == 1
            size = len(messages[uids[0]]) if self.size_override is None else self.size_override
            return "OK", [f"1 (UID {uids[0]} RFC822.SIZE {size})".encode()]
        headers = "HEADER.FIELDS" in query
        failure = self.header_fetch_failure if headers else self.full_fetch_failure
        if failure is not None and failure in uids:
            raise imaplib.IMAP4.error("private-app-password reader@example.org private body")
        result = []
        for uid in uids:
            if not headers and uid == self.missing_body_uid:
                continue
            raw = messages[uid]
            prefix = re.search(r"BODY\.PEEK\[\]<0\.([0-9]+)>", query)
            if prefix:
                raw = raw[:int(prefix[1])]
                if self.prefix_missing_byte:
                    raw = raw[:-1]
                response_uid = uid if self.prefix_uid_override is None else self.prefix_uid_override
                result.append((
                    f"1 (UID {response_uid} BODY[]<{self.prefix_offset}> {{{len(raw)}}}".encode(), raw,
                ))
                result.append(b")")
                continue
            if headers:
                names = set(re.search(r"HEADER.FIELDS \(([^)]+)\)", query)[1].lower().split())
                header = BytesHeaderParser(policy=policy.default).parsebytes(raw)
                raw = b"".join(
                    f"{name}: {value}\r\n".encode() for name, value in header.raw_items() if name.lower() in names
                ) + b"\r\n"
            result.append((f"1 (UID {uid} BODY[] {{{len(raw)}}}".encode(), raw))
            result.append(b")")
        return "OK", result

    def logout(self):
        self.logged_out = True
        return "BYE", [b"logged out"]


def run(client, store=None, config=None):
    return sync.sync_mail_unsubscribes(config or settings(), store or Store(), client_factory=client.factory)


def body_calls(client):
    return [call for call in client.calls if call[0] == "FETCH" and call[2].startswith("(UID BODY.PEEK[]")]


def test_disabled_does_not_read_configuration_store_or_mailbox():
    report = sync.sync_mail_unsubscribes(
        SimpleNamespace(mailing_unsubscribe_enabled=False), object(),
        client_factory=lambda *_a, **_k: pytest.fail("connected"),
    )
    assert asdict(report) == {
        "scanned": 0, "recognized": 0, "applied": 0, "duplicates": 0, "unmatched": 0, "stale": 0,
        "oversized": 0, "backlog": False,
    }


def test_real_parser_uses_sender_alias_tls_readonly_peek_and_size_guard():
    client, store = IMAP(), Store()
    report = run(client, store)
    assert (report.scanned, report.recognized, report.applied) == (1, 1, 1)
    assert store.applied[0].email == "reader@example.org"
    args, kwargs = client.constructor
    assert args == ("imap.gmail.com", 993)
    assert isinstance(kwargs["ssl_context"], ssl.SSLContext)
    assert kwargs["ssl_context"].verify_mode == ssl.CERT_REQUIRED and kwargs["ssl_context"].check_hostname
    assert kwargs["timeout"] == 30
    assert ("SELECT", b'"INBOX"', True) in client.calls
    assert all("BODY.PEEK[" in call[2] or call[2] == "(UID RFC822.SIZE)"
               for call in client.calls if call[0] == "FETCH")
    assert {call[0] for call in client.calls} <= {"LOGIN", "LIST", "SELECT", "SEARCH", "FETCH"}
    assert list(store.cursors.values()) == [("123", 1)] and client.logged_out


def test_unrelated_untrusted_automated_and_nonmember_bodies_are_never_fetched():
    messages = {
        1: b"From: friend@example.org\r\n\r\nPrivate unrelated message",
        2: reply(auth=None),
        3: reply(reference="<personal@example.org>"),
        4: reply(sender="other@example.org"),
        5: reply(extra="Auto-Submitted: auto-replied\r\n"),
        6: reply(extra="Resent-Sender: someone@example.org\r\n"),
        7: reply(extra="X-Forwarded-To: newsletter@example.org\r\n"),
        8: reply(),
    }
    client = IMAP([(b"INBOX", b"\\HasNoChildren", "123", messages)])
    report = run(client)
    assert report.scanned == 8 and report.recognized == 1 and report.applied == 1 and report.unmatched == 2
    assert [call[1] for call in body_calls(client)] == ["8"]
    sizes = [call[1] for call in client.calls if call[0] == "FETCH" and "RFC822.SIZE" in call[2]]
    assert sizes == ["8"]


@pytest.mark.parametrize("auth", [
    None, "attacker.example; dkim=pass; dmarc=pass header.from=example.org",
    "mx.google.com; dkim=pass; dmarc=fail header.from=example.org",
    "mx.google.com; dkim=pass; dmarc=pass header.from=attacker.example",
    "mx.google.com; dkim=fail; dmarc=pass header.from=example.org",
])
def test_failed_or_forged_authentication_never_fetches_body_or_applies(auth):
    client = IMAP([(b"INBOX", b"\\HasNoChildren", "123", {1: reply(auth=auth)})])
    store = Store()
    assert run(client, store).applied == 0
    assert not store.applied and not body_calls(client)


@pytest.mark.parametrize("body", ["Thank you", "> STOP", "On Monday, Sender wrote:\r\nSTOP"])
def test_related_reply_without_authored_unsubscribe_command_advances_safely(body):
    store = Store()
    client = IMAP([(b"INBOX", b"\\HasNoChildren", "123", {1: reply(body=body)})])
    report = run(client, store)
    assert report.recognized == 0 and report.applied == 0 and not store.applied
    assert list(store.cursors.values()) == [("123", 1)]


def test_all_mail_and_junk_are_scanned_once_with_duplicate_reply_safe():
    client = IMAP([
        (b"INBOX", b"\\HasNoChildren", "100", {99: reply()}),
        (b"[Gmail]/All", b"\\HasNoChildren \\All", "200", {3: reply()}),
        (b"[Gmail]/Junk", b"\\HasNoChildren \\Junk", "300", {7: reply()}),
    ])
    report = run(client)
    assert (report.scanned, report.recognized, report.applied, report.duplicates) == (2, 2, 1, 1)
    assert [call[1] for call in client.calls if call[0] == "SELECT"] == [b'"[Gmail]/All"', b'"[Gmail]/Junk"']


def test_uidvalidity_reset_replays_safely_and_reversed_uid_range_is_filtered():
    store = Store()
    run(IMAP([(b"INBOX", b"\\HasNoChildren", "123", {8: reply()})]), store)
    client = IMAP([(b"INBOX", b"\\HasNoChildren", "123", {8: reply()})])
    client.search_override = [8]
    assert run(client, store).scanned == 0 and not body_calls(client)
    client = IMAP([(b"INBOX", b"\\HasNoChildren", "456", {1: reply()})])
    assert run(client, store).duplicates == 1
    assert ("SEARCH", None, "SINCE", SINCE) in client.calls
    assert list(store.cursors.values()) == [("456", 1)]


@pytest.mark.parametrize("failure", ["full_fetch_failure", "header_fetch_failure", "missing_body_uid"])
def test_incomplete_scan_preserves_cursor_and_sanitizes_exception(failure, capsys):
    store, client = Store(), IMAP()
    setattr(client, failure, 1)
    with pytest.raises(sync.UnsubscribeSyncUnavailable) as error:
        run(client, store)
    assert not store.cursors and not store.applied and client.logged_out
    assert "@" not in str(error.value) and "password" not in str(error.value) and "body" not in str(error.value)
    assert error.value.__suppress_context__ and capsys.readouterr() == ("", "")


@pytest.mark.parametrize("size", [0, 4_294_967_296, "invalid"])
def test_invalid_size_stops_scan_without_fetching_body(size):
    client, store = IMAP(), Store()
    client.size_override = size
    with pytest.raises(sync.UnsubscribeSyncUnavailable):
        run(client, store)
    assert not body_calls(client) and not store.cursors and not store.applied


def test_large_attachment_reply_applies_complete_stop_part_from_bounded_prefix():
    client = IMAP([(b"INBOX", b"\\HasNoChildren", "123", {1: large_attachment_reply()})])
    store = Store()
    report = run(client, store)
    assert report.applied == 1 and report.recognized == 1 and report.oversized == 0
    assert [call[2] for call in body_calls(client)] == ["(UID BODY.PEEK[]<0.524288>)"]
    assert list(store.cursors.values()) == [("123", 1)]


@pytest.mark.parametrize("body", ["Thank you\r\n", "STOP\r\n"])
def test_incomplete_large_authored_text_is_counted_and_checkpointed_without_blocking_later_replies(body):
    client = IMAP([(b"INBOX", b"\\HasNoChildren", "123", {
        1: reply(body=body + "x" * 600_000),
        2: reply(message_id="<later@example.org>"),
    })])
    store = Store()
    report = run(client, store)
    assert report.scanned == 2 and report.recognized == 1 and report.applied == 1
    assert report.oversized == 1 and not report.backlog
    assert [request.message_id for request in store.applied] == ["<later@example.org>"]
    assert [call[2] for call in body_calls(client)] == ["(UID BODY.PEEK[]<0.524288>)", "(UID BODY.PEEK[])"]
    assert list(store.cursors.values()) == [("123", 2)]


@pytest.mark.parametrize("failure,value", [
    ("prefix_uid_override", 999), ("prefix_offset", 1), ("prefix_missing_byte", True),
    ("missing_body_uid", 1), ("full_fetch_failure", 1),
])
def test_broken_prefix_transport_or_metadata_still_fails_closed(failure, value):
    client = IMAP([(b"INBOX", b"\\HasNoChildren", "123", {1: large_attachment_reply()})])
    setattr(client, failure, value)
    store = Store()
    with pytest.raises(sync.UnsubscribeSyncUnavailable):
        run(client, store)
    assert not store.cursors and not store.applied


def test_size_mismatch_stops_scan_without_advancing_cursor():
    client, store = IMAP(), Store()
    client.size_override = len(reply()) - 1
    with pytest.raises(sync.UnsubscribeSyncUnavailable):
        run(client, store)
    assert not store.cursors and not store.applied


def test_failed_later_chunk_keeps_only_completed_chunk_checkpoint():
    messages = {uid: b"Content-Type: text/plain\r\n\r\nNormal." for uid in range(1, 52)}
    messages[1] = reply()
    messages[51] = reply(message_id="<later@example.org>")
    client, store = IMAP([(b"INBOX", b"\\HasNoChildren", "123", messages)]), Store()
    client.full_fetch_failure = 51
    with pytest.raises(sync.UnsubscribeSyncUnavailable):
        run(client, store)
    assert list(store.cursors.values()) == [("123", 50)] and len(store.applied) == 1
    client.full_fetch_failure = None
    report = run(client, store)
    assert report.scanned == 1 and report.applied == 1
    assert list(store.cursors.values()) == [("123", 51)]


def test_bounded_scan_reports_backlog_and_continues_next_run():
    messages = {uid: b"Content-Type: text/plain\r\n\r\nNormal." for uid in range(1, 503)}
    messages[502] = reply()
    client, store = IMAP([(b"INBOX", b"\\HasNoChildren", "123", messages)]), Store()
    client.search_override = list(reversed(messages))
    report = run(client, store)
    assert report.scanned == 500 and report.backlog and report.applied == 0
    assert list(store.cursors.values()) == [("123", 500)]
    header_calls = [call for call in client.calls if call[0] == "FETCH" and "HEADER.FIELDS" in call[2]]
    assert len(header_calls) == 10 and all(len(call[1].split(",")) == 50 for call in header_calls)
    report = run(client, store)
    assert report.scanned == 2 and not report.backlog and report.applied == 1


@pytest.mark.parametrize("outcome,field", [("stale", "stale"), ("unmatched", "unmatched")])
def test_atomic_store_rechecks_are_reported(outcome, field):
    report = run(IMAP(), Store({"<reply@example.org>": outcome}))
    assert report.recognized == 1 and getattr(report, field) == 1 and report.applied == 0


def test_constructor_errors_and_logout_errors_do_not_expose_private_details(capsys):
    def fail(*_a, **_k):
        raise OSError("private-app-password reader@example.org private body")

    with pytest.raises(sync.UnsubscribeSyncUnavailable) as error:
        sync.sync_mail_unsubscribes(settings(), Store(), client_factory=fail)
    assert "@" not in str(error.value) and "password" not in str(error.value)
    client = IMAP()
    client.logout = fail
    assert run(client).applied == 1 and capsys.readouterr() == ("", "")


def test_scanner_applies_real_store_and_ignores_existing_bounce_cursor(tmp_path):
    with MailingStore(tmp_path / "mail.db") as store:
        store.subscribe("reader@example.org", 11)
        store.store_post("original", "Original", "Original")
        delivery = store.db.execute("SELECT id FROM mailing_outbox").fetchone()[0]
        assert store.claim_batch([delivery], ORIGINAL)
        store.finish(delivery, "sent")
        store.store_post("queued", "Queued", "Queued")
        store.bounce_scan_since = lambda: SINCE
        first_client = IMAP()
        report = run(first_client, store)
        assert report.applied == 1 and store.status_counts() == {"active": 0, "unsubscribed": 1}
        key = store.db.execute("SELECT mailbox_key FROM mailing_unsubscribe_cursors").fetchone()[0]
        store.save_bounce_cursor(key, "123", 999)
        with store.db:
            store.db.execute("DELETE FROM mailing_unsubscribe_cursors")
        report = run(IMAP(), store)
        assert report.scanned == 1 and report.duplicates == 1
        assert store.outbox_counts() == {"sent": 1, "suppressed": 1}
        assert store.bounce_cursor(key) == ("123", 999)


def test_sync_has_no_smtp_connection(monkeypatch):
    import smtplib

    def fail(*_a, **_k):
        pytest.fail("SMTP was accessed during unsubscribe sync")

    monkeypatch.setattr(smtplib, "SMTP", fail)
    monkeypatch.setattr(smtplib, "SMTP_SSL", fail)
    assert run(IMAP()).applied == 1
