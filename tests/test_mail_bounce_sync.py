import imaplib
import re
import ssl
from dataclasses import asdict
from email import policy
from email.parser import BytesHeaderParser
from types import SimpleNamespace

import pytest

from compdesign_bot import mail_bounce_sync as sync

AUTH = "mx.google.com; dkim=pass header.i=@googlemail.com; dmarc=pass header.from=googlemail.com"
SINCE = "03-Oct-2026"


def dsn(*, auth=AUTH, message_id="<newsletter@example.org>", status="5.1.1", original_type="text/rfc822-headers",
        sender="mailer-daemon@googlemail.com", recipient="reader@example.org"):
    auth_header = f"Authentication-Results: {auth}\r\n" if auth is not None else ""
    return (
        f"From: Mail Delivery Subsystem <{sender}>\r\n"
        "To: sender@example.org\r\n"
        "Message-ID: <bounce@example.org>\r\n"
        f"{auth_header}"
        'Content-Type: multipart/report; report-type=delivery-status; boundary="test-dsn"\r\n'
        "MIME-Version: 1.0\r\n\r\n"
        "--test-dsn\r\nContent-Type: text/plain\r\n\r\nDelivery failed.\r\n"
        "--test-dsn\r\nContent-Type: message/delivery-status\r\n\r\n"
        "Reporting-MTA: dns; googlemail.com\r\n\r\n"
        f"Final-Recipient: rfc822; {recipient}\r\n"
        f"Action: failed\r\nStatus: {status}\r\n"
        "Diagnostic-Code: smtp; 550 mailbox rejected\r\n\r\n"
        f"--test-dsn\r\nContent-Type: {original_type}\r\n\r\n"
        f"Message-ID: {message_id}\r\nTo: {recipient}\r\n"
        "From: sender@example.org\r\n\r\n"
        "--test-dsn--\r\n"
    ).encode()


def settings(*, enabled=True):
    def require_bounces():
        assert enabled
    return SimpleNamespace(
        mailing_bounce_enabled=enabled, imap_host="imap.gmail.com", imap_port=993,
        smtp_username="sender@example.org", smtp_password="test-app-password", require_bounces=require_bounces,
    )


class Store:
    def __init__(self, *, outcomes=None):
        self.cursors = {}
        self.checkpoints = []
        self.applied = []
        self.events = set()
        self.outcomes = outcomes or {}
        self.released = 0

    def bounce_scan_since(self):
        return SINCE

    def bounce_cursor(self, key):
        return self.cursors.get(key)

    def save_bounce_cursor(self, key, validity, uid):
        assert re.fullmatch(r"[a-f0-9]{64}", key)
        self.cursors[key] = validity, uid
        self.checkpoints.append((key, validity, uid))

    def apply_bounce(self, notice):
        self.applied.append(notice)
        if notice.message_id in self.outcomes:
            return self.outcomes[notice.message_id]
        identity = notice.message_id, notice.recipient, notice.status
        if identity in self.events:
            return "duplicate"
        self.events.add(identity)
        return "applied"

    def bounce_matches(self, notice):
        return self.outcomes.get(notice.message_id) != "unmatched"

    def release_expired_bounce_holds(self):
        self.released += 1


class IMAP:
    def __init__(self, folders=None):
        self.folders = folders or [(b"INBOX", b"\\HasNoChildren", "123", {1: dsn()})]
        self.current = None
        self.calls = []
        self.constructor = None
        self.full_fetch_failure = None
        self.header_fetch_failure = None
        self.missing_body_uid = None
        self.search_override = None
        self.logged_out = False

    def factory(self, *args, **kwargs):
        self.constructor = args, kwargs
        return self

    def login(self, username, password):
        self.calls.append(("LOGIN",))
        assert (username, password) == ("sender@example.org", "test-app-password")
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
        headers = "HEADER.FIELDS" in query
        uids = [int(uid) for uid in uid_set.split(",")]
        failure = self.header_fetch_failure if headers else self.full_fetch_failure
        if failure is not None and failure in uids:
            raise imaplib.IMAP4.error("credentials and recipient must never reach errors")
        result = []
        for uid in uids:
            if not headers and uid == self.missing_body_uid:
                continue
            raw = messages[uid]
            if headers:
                header = BytesHeaderParser(policy=policy.default).parsebytes(raw)
                raw = b"".join(
                    f"{name}: {value}\r\n".encode() for name, value in header.raw_items()
                    if name.lower() in {"content-type", "authentication-results"}
                ) + b"\r\n"
            result.append((f"1 (UID {uid} BODY[] {{{len(raw)}}}".encode(), raw))
            result.append(b")")
        return "OK", result

    def logout(self):
        self.logged_out = True
        return "BYE", [b"logged out"]


def run(client, store=None, config=None):
    return sync.sync_mail_bounces(config or settings(), store or Store(), client_factory=client.factory)


def test_disabled_never_accesses_configuration_mailbox_or_store():
    config = SimpleNamespace(mailing_bounce_enabled=False)
    report = sync.sync_mail_bounces(config, object(), client_factory=lambda *_a, **_k: pytest.fail("connected"))
    assert asdict(report) == {
        "scanned": 0, "recognized": 0, "applied": 0, "duplicates": 0,
        "unmatched": 0, "untrusted": 0, "stale": 0, "backlog": False,
    }


@pytest.mark.parametrize("original_type", ["text/rfc822-headers", "message/rfc822"])
def test_real_parser_is_used_with_tls_readonly_peek_and_safe_cursor(original_type):
    client = IMAP([(b"INBOX", b"\\HasNoChildren", "123", {12: dsn(original_type=original_type)})])
    store = Store()
    report = run(client, store)
    assert (report.scanned, report.recognized, report.applied) == (1, 1, 1)
    assert store.applied[0].message_id == "<newsletter@example.org>"
    assert store.applied[0].permanent is True
    args, kwargs = client.constructor
    assert args == ("imap.gmail.com", 993)
    assert isinstance(kwargs["ssl_context"], ssl.SSLContext)
    assert kwargs["ssl_context"].verify_mode == ssl.CERT_REQUIRED
    assert kwargs["ssl_context"].check_hostname and kwargs["timeout"] == 30
    assert ("SELECT", b'"INBOX"', True) in client.calls
    assert all("BODY.PEEK[" in call[2] for call in client.calls if call[0] == "FETCH")
    assert {call[0] for call in client.calls} <= {"LOGIN", "LIST", "SELECT", "SEARCH", "FETCH"}
    assert [value for value in store.cursors.values()] == [("123", 12)]
    assert store.released == 1 and client.logged_out


def test_localized_all_mail_and_junk_flags_skip_inbox_and_deduplicate_reports():
    client = IMAP([
        (b"INBOX", b"\\HasNoChildren", "100", {99: dsn()}),
        (b"[Gmail]/&yATMtLz0rQDVaA-", b"\\HasNoChildren \\All", "200", {3: dsn()}),
        (b"[Gmail]/&wqTUzA-", b"\\HasNoChildren \\Junk", "300", {7: dsn()}),
    ])
    store = Store()
    report = run(client, store)
    assert (report.scanned, report.recognized, report.applied, report.duplicates) == (2, 2, 1, 1)
    selected = [call[1] for call in client.calls if call[0] == "SELECT"]
    assert selected == [b'"[Gmail]/&yATMtLz0rQDVaA-"', b'"[Gmail]/&wqTUzA-"']
    assert len(store.cursors) == 2


def test_headers_first_and_only_report_bodies_are_downloaded():
    client = IMAP([(b"INBOX", b"\\HasNoChildren", "123", {
        1: b"Content-Type: text/plain\r\n\r\nNormal private email.",
        2: b"Content-Type: multipart/report; boundary=unknown\r\n\r\n--unknown--\r\n",
        3: dsn(),
    })])
    report = run(client)
    assert (report.scanned, report.recognized, report.applied) == (3, 1, 1)
    body_calls = [call for call in client.calls if call[0] == "FETCH" and call[2] == "(UID BODY.PEEK[])"]
    assert [call[1] for call in body_calls] == ["2", "3"]


@pytest.mark.parametrize("auth", [
    None, "attacker.example; dkim=pass; dmarc=pass",
    "mx.google.com.attacker.example; dkim=pass; dmarc=pass",
    '"attacker.example" mx.google.com; dkim=pass; dmarc=pass',
    "mx.google.com; dkim=fail; dmarc=pass", "mx.google.com; dkim=pass; dmarc=fail",
    "mx.google.com; dkim=passive; dmarc=pass",
    "mx.google.com; dkim=fail (dkim=pass; dmarc=pass); dmarc=fail",
    'mx.google.com; dkim=fail reason="dkim=pass; dmarc=pass"; dmarc=fail',
    "mx.google.com; dkim=pass; dmarc=pass; dmarc=fail",
    "mx.google.com; dkim=pass; dmarc=pass (unclosed",
])
def test_missing_failed_or_forged_auth_results_never_apply(auth):
    store = Store()
    report = run(IMAP([(b"INBOX", b"\\HasNoChildren", "123", {1: dsn(auth=auth)})]), store)
    assert (report.recognized, report.untrusted, report.applied) == (1, 1, 0)
    assert not store.applied


@pytest.mark.parametrize("auth", [
    AUTH, "mx.google.com; spf=pass smtp.mailfrom=googlemail.com; dmarc=pass header.from=googlemail.com",
])
def test_dkim_or_spf_and_dmarc_pass_allow_verified_notice(auth):
    assert run(IMAP([(b"INBOX", b"\\HasNoChildren", "123", {1: dsn(auth=auth)})])).applied == 1


def test_first_auth_results_wins_even_if_second_header_claims_pass():
    raw = dsn(auth="attacker.example; dkim=fail; dmarc=fail")
    raw = raw.replace(b"Content-Type: multipart/report", f"Authentication-Results: {AUTH}\r\nContent-Type: multipart/report".encode(), 1)
    report = run(IMAP([(b"INBOX", b"\\HasNoChildren", "123", {1: raw})]))
    assert report.untrusted == 1 and report.applied == 0


@pytest.mark.parametrize("sender", ["postmaster@psu.edu", "mailer-daemon@m365.psu.edu",
                                    "MicrosoftExchange329e71ec88ae4615bbc36ab6ce41109e@m365.psu.edu"])
def test_authenticated_recipient_domain_mta_and_child_mta_are_trusted(sender):
    domain = sender.rsplit("@", 1)[1]
    raw = dsn(sender=sender, recipient="reader@psu.edu",
              auth=f"mx.google.com; dkim=pass header.i=@{domain}; dmarc=pass header.from={domain}")
    report = run(IMAP([(b"INBOX", b"\\HasNoChildren", "123", {1: raw})]))
    assert report.applied == 1 and report.untrusted == 0


@pytest.mark.parametrize("sender,recipient,auth_domain", [
    ("postmaster@attacker.example", "reader@psu.edu", "attacker.example"),
    ("postmaster@evilpsu.edu", "reader@psu.edu", "evilpsu.edu"),
    ("postmaster@m365.psu.edu.attacker.example", "reader@psu.edu", "m365.psu.edu.attacker.example"),
    ("ordinary-user@psu.edu", "reader@psu.edu", "psu.edu"),
    ("random-user@gmail.com", "reader@gmail.com", "gmail.com"),
    ("postmaster@outlook.com", "reader@outlook.com", "outlook.com"),
    ("postmaster@psu.edu", "reader@psu.edu", "attacker.example"),
])
def test_valid_authentication_does_not_trust_unrelated_or_consumer_senders(sender, recipient, auth_domain):
    raw = dsn(sender=sender, recipient=recipient,
              auth=f"mx.google.com; dkim=pass; dmarc=pass header.from={auth_domain}")
    report = run(IMAP([(b"INBOX", b"\\HasNoChildren", "123", {1: raw})]))
    assert report.applied == 0 and report.untrusted == 1


def test_recipient_domain_authentication_is_checked_separately_for_each_notice():
    raw = dsn(sender="postmaster@psu.edu", recipient="reader@psu.edu",
              auth="mx.google.com; spf=pass; dmarc=pass header.from=psu.edu")
    raw = raw.replace(b"--test-dsn\r\nContent-Type: text/rfc822-headers", (
        b"Final-Recipient: rfc822; other@example.org\r\nAction: failed\r\nStatus: 5.1.1\r\n\r\n"
        b"--test-dsn\r\nContent-Type: text/rfc822-headers"
    ))
    store = Store()
    report = run(IMAP([(b"INBOX", b"\\HasNoChildren", "123", {1: raw})]), store)
    assert (report.recognized, report.applied, report.untrusted) == (2, 1, 1)
    assert [notice.recipient for notice in store.applied] == ["reader@psu.edu"]


def test_next_scan_filters_reversed_uid_range_and_does_not_refetch_processed_mail():
    store = Store()
    client = IMAP([(b"INBOX", b"\\HasNoChildren", "123", {7: dsn()})])
    run(client, store)
    client.calls.clear()
    client.search_override = [7]
    report = run(client, store)
    assert ("SEARCH", None, "UID", "8:*") in client.calls
    assert report.scanned == 0 and not any(call[0] == "FETCH" for call in client.calls)


def test_uidvalidity_change_restarts_date_scan_and_remains_idempotent():
    store = Store()
    run(IMAP([(b"INBOX", b"\\HasNoChildren", "123", {8: dsn()})]), store)
    client = IMAP([(b"INBOX", b"\\HasNoChildren", "456", {1: dsn()})])
    report = run(client, store)
    assert ("SEARCH", None, "SINCE", SINCE) in client.calls
    assert report.duplicates == 1 and list(store.cursors.values()) == [("456", 1)]


@pytest.mark.parametrize("missing", [False, True])
def test_failed_full_body_never_advances_cursor_or_releases_holds(missing):
    store = Store()
    client = IMAP()
    if missing:
        client.missing_body_uid = 1
    else:
        client.full_fetch_failure = 1
    with pytest.raises(sync.BounceSyncUnavailable) as error:
        run(client, store)
    assert not store.cursors and not store.applied and not store.released
    assert "credentials" not in str(error.value) and "recipient" not in str(error.value)
    assert error.value.__suppress_context__ and client.logged_out


def test_header_fetch_failure_stops_scan_before_full_body_and_cursor():
    store = Store()
    client = IMAP()
    client.header_fetch_failure = 1
    with pytest.raises(sync.BounceSyncUnavailable):
        run(client, store)
    assert not store.cursors and not store.applied and not store.released
    assert not any(call[0] == "FETCH" and call[2] == "(UID BODY.PEEK[])" for call in client.calls)


def test_empty_initial_scan_remains_date_bounded_next_time():
    store = Store()
    client = IMAP([(b"INBOX", b"\\HasNoChildren", "123", {})])
    first = run(client, store)
    assert first.scanned == 0 and list(store.cursors.values()) == [("123", 0)]
    client.calls.clear()
    assert run(client, store).scanned == 0
    assert ("SEARCH", None, "SINCE", SINCE) in client.calls


def test_logout_failure_does_not_expose_server_details_or_undo_verified_scan(capsys):
    client = IMAP()
    def failed_logout():
        raise imaplib.IMAP4.error("test-app-password recipient@example.org")
    client.logout = failed_logout
    assert run(client).applied == 1
    assert capsys.readouterr() == ("", "")


def test_failed_later_chunk_keeps_only_fully_processed_cursor_and_events():
    messages = {uid: b"Content-Type: text/plain\r\n\r\nNormal." for uid in range(1, 52)}
    messages[1] = dsn()
    messages[51] = dsn(message_id="<later@example.org>")
    client = IMAP([(b"INBOX", b"\\HasNoChildren", "123", messages)])
    client.full_fetch_failure = 51
    store = Store()
    with pytest.raises(sync.BounceSyncUnavailable):
        run(client, store)
    assert list(store.cursors.values()) == [("123", 50)]
    assert len(store.applied) == 1 and not store.released
    client.full_fetch_failure = None
    report = run(client, store)
    assert report.scanned == 1 and report.applied == 1
    assert list(store.cursors.values()) == [("123", 51)]


def test_bounded_scan_in_order_exposes_backlog_and_continues_next_run():
    messages = {uid: b"Content-Type: text/plain\r\n\r\nNormal." for uid in range(1, 503)}
    messages[502] = dsn()
    client = IMAP([(b"INBOX", b"\\HasNoChildren", "123", messages)])
    client.search_override = list(reversed(messages))
    store = Store()
    first = run(client, store)
    assert first.scanned == 500 and first.backlog and first.recognized == 0
    assert list(store.cursors.values()) == [("123", 500)]
    headers = [call for call in client.calls if call[0] == "FETCH" and "HEADER.FIELDS" in call[2]]
    assert len(headers) == 10 and all(len(call[1].split(",")) == 50 for call in headers)
    second = run(client, store)
    assert second.scanned == 2 and not second.backlog and second.applied == 1
    assert list(store.cursors.values()) == [("123", 502)]


def test_unmatched_and_stale_are_counted_separately():
    store = Store(outcomes={"<unknown@example.org>": "unmatched", "<old@example.org>": "stale"})
    client = IMAP([(b"INBOX", b"\\HasNoChildren", "123", {
        1: dsn(message_id="<unknown@example.org>"), 2: dsn(message_id="<old@example.org>"),
    })])
    report = run(client, store)
    assert (report.recognized, report.unmatched, report.stale, report.applied) == (2, 1, 1, 0)


def test_untrusted_unrelated_personal_bounce_does_not_pause_newsletter():
    store = Store(outcomes={"<personal@example.org>": "unmatched"})
    client = IMAP([(b"INBOX", b"\\HasNoChildren", "123", {
        1: dsn(message_id="<personal@example.org>", auth=None),
    })])
    report = run(client, store)
    assert (report.recognized, report.unmatched, report.untrusted, report.applied) == (1, 1, 0, 0)
    assert not store.applied and list(store.cursors.values()) == [("123", 1)]


def test_constructor_failure_is_sanitized_without_printing_credentials(capsys):
    def fail(*_args, **_kwargs):
        raise OSError("private-app-password signed-url recipient@example.org")
    with pytest.raises(sync.BounceSyncUnavailable) as error:
        sync.sync_mail_bounces(settings(), Store(), client_factory=fail)
    assert "private-app-password" not in str(error.value)
    assert "signed-url" not in str(error.value) and "@" not in str(error.value)
    assert capsys.readouterr() == ("", "")


def test_sync_has_no_smtp_dependency_or_smtp_connection(monkeypatch):
    import smtplib
    def fail(*_args, **_kwargs):
        pytest.fail("SMTP was accessed during bounce synchronization")
    monkeypatch.setattr(smtplib, "SMTP", fail)
    monkeypatch.setattr(smtplib, "SMTP_SSL", fail)
    assert run(IMAP()).applied == 1
