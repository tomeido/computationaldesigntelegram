import base64
import quopri
from dataclasses import FrozenInstanceError

import pytest

from compdesign_bot.mail_bounces import BounceNotice, classify_bounce, parse_bounces

MESSAGE_ID = "<batch-123@example.test>"


def recipient_record(address="reader@example.test", *, action="failed", status="5.1.1", extra=""):
    return (f"Final-Recipient: rfc822; {address}\r\nAction: {action}\r\nStatus: {status}\r\n"
            f"Diagnostic-Code: smtp; 550 {status} Synthetic delivery result\r\n{extra}")


def report(records=None, *, original=None, original_type="text/rfc822-headers", encoding=None,
           human="A synthetic delivery status notification.", report_type="delivery-status",
           per_message="Reporting-MTA: dns; relay.example.test", extra_parts=""):
    if records is None:
        records = [recipient_record()]
    if original is None:
        original = (f"Message-ID: {MESSAGE_ID}\r\nFrom: sender@example.test\r\n"
                    "To: reader@example.test\r\nSubject: Synthetic briefing\r\n\r\n")
    returned = original.encode("ascii")
    if encoding == "base64":
        returned = base64.encodebytes(returned)
    elif encoding == "quoted-printable":
        returned = quopri.encodestring(returned)
    transfer = f"Content-Transfer-Encoding: {encoding}\r\n" if encoding else ""
    status_body = "\r\n\r\n".join([per_message, *(r.rstrip() for r in records)])
    return (
        "From: mailer-daemon@relay.example.test\r\nTo: sender@example.test\r\n"
        "Message-ID: <dsn-not-the-original@relay.example.test>\r\n"
        "Subject: Delivery Status Notification (Failure)\r\nMIME-Version: 1.0\r\n"
        f'Content-Type: multipart/report; report-type="{report_type}"; boundary="dsn-boundary"\r\n\r\n'
        "--dsn-boundary\r\nContent-Type: text/plain; charset=utf-8\r\n\r\n"
        f"{human}\r\n--dsn-boundary\r\nContent-Type: message/delivery-status\r\n\r\n"
        f"{status_body}\r\n\r\n--dsn-boundary\r\nContent-Type: {original_type}\r\n"
        f"{transfer}\r\n"
    ).encode("ascii") + returned + f"\r\n{extra_parts}--dsn-boundary--\r\n".encode("ascii")


def test_gmail_headers_only_uses_returned_original_not_report_id():
    assert parse_bounces(report()) == [BounceNotice(
        MESSAGE_ID, "reader@example.test", "failed", "5.1.1", "invalid_mailbox", True)]


def test_attached_original_multipart_message_reads_inner_headers_only():
    original = (
        f"Message-ID: {MESSAGE_ID}\r\nMIME-Version: 1.0\r\n"
        'Content-Type: multipart/alternative; boundary="original-boundary"\r\n\r\n'
        "--original-boundary\r\nContent-Type: text/plain\r\n\r\n"
        "Synthetic briefing. Message-ID: <body-only@example.test>\r\n"
        "--original-boundary\r\nContent-Type: text/html\r\n\r\n<p>Synthetic briefing</p>\r\n"
        "--original-boundary--\r\n")
    notice, = parse_bounces(report(original=original, original_type="message/rfc822"))
    assert notice.message_id == MESSAGE_ID


@pytest.mark.parametrize("original_type", ["text/rfc822-headers", "message/rfc822"])
@pytest.mark.parametrize("encoding", ["base64", "quoted-printable"])
def test_encoded_returned_headers(original_type, encoding):
    notice, = parse_bounces(report(original_type=original_type, encoding=encoding))
    assert notice.message_id == MESSAGE_ID


def test_folded_mime_message_id_and_recipient_headers():
    raw = report(original=f"Message-ID:\r\n\t{MESSAGE_ID}\r\nSubject: Synthetic\r\n\r\n")
    raw = raw.replace(b'Content-Type: multipart/report; report-type="delivery-status";',
                      b'Content-Type: multipart/report;\r\n\treport-type="delivery-status";')
    raw = raw.replace(b"Final-Recipient: rfc822; reader@example.test", b"Final-Recipient: RFC822;\r\n\tReader@EXAMPLE.TEST")
    notice, = parse_bounces(raw)
    assert notice.recipient == "reader@example.test"
    assert notice.message_id == MESSAGE_ID


def test_multiple_recipients_do_not_use_headers_to_guess_recipient():
    notices = parse_bounces(report([
        recipient_record("first@example.test"),
        recipient_record("second@example.test", status="5.1.2"),
        recipient_record("third@example.test", action="delayed", status="4.4.1"),
        recipient_record("delivered@example.test", action="delivered", status="2.0.0"),
    ]))
    assert [(n.recipient, n.reason, n.permanent) for n in notices] == [
        ("first@example.test", "invalid_mailbox", True),
        ("second@example.test", "invalid_domain", True),
        ("third@example.test", "temporary_failure", False),
    ]


@pytest.mark.parametrize("status,reason", [
    ("5.1.1", "invalid_mailbox"), ("5.1.2", "invalid_domain"),
    ("5.1.3", "invalid_mailbox"), ("5.1.6", "invalid_mailbox"),
    ("5.2.1", "mailbox_disabled"),
])
def test_permanent_destination_reason_allowlist(status, reason):
    notice, = parse_bounces(report([recipient_record(status=status)]))
    assert notice.reason == reason
    assert notice.permanent is True


@pytest.mark.parametrize("action,status,reason", [
    ("delayed", "4.4.1", "temporary_failure"),
    ("delayed", "4.2.2", "temporary_failure"),
    ("failed", "5.2.2", "mailbox_full"),
    ("failed", "5.7.1", "recipient_rejected"),
    ("failed", "5.0.0", "recipient_rejected"),
    ("failed", "5.1.0", "recipient_rejected"),
    ("failed", "5.9.99", "recipient_rejected"),
])
def test_other_delivery_failures_are_holds_not_invalid_addresses(action, status, reason):
    notice, = parse_bounces(report([recipient_record(action=action, status=status)]))
    assert notice.reason == reason
    assert notice.permanent is False


def test_microsoft_550_541_access_denied_is_a_hold():
    record = recipient_record(status="5.4.1").replace(
        "Synthetic delivery result", "Recipient address rejected: Access denied")
    notice, = parse_bounces(report([record]))
    assert notice.reason == "recipient_rejected"
    assert notice.permanent is False


def test_conflicting_microsoft_smtp_diagnostic_cannot_mark_a_mailbox_invalid():
    record = recipient_record().replace("550 5.1.1 Synthetic delivery result", "550 5.4.1 Access denied")
    notice, = parse_bounces(report([record]))
    assert notice.status == "5.1.1"
    assert notice.reason == "recipient_rejected"
    assert notice.permanent is False


@pytest.mark.parametrize("action,status,expected", [
    ("failed", "5.1.1", ("invalid_mailbox", True)),
    ("FAILED", "5.1.2", ("invalid_domain", True)),
    ("failed", "5.1.3", ("invalid_mailbox", True)),
    ("failed", "5.1.6", ("invalid_mailbox", True)),
    ("failed", "5.2.1", ("mailbox_disabled", True)),
    ("failed", "5.2.2", ("mailbox_full", False)),
    ("failed", "5.4.1", ("recipient_rejected", False)),
    ("failed", "5.7.1", ("recipient_rejected", False)),
    ("delayed", "4.2.2", ("temporary_failure", False)),
    (" failed ", " 5.1.1 (unknown mailbox) ", ("invalid_mailbox", True)),
    ("failed", "4.4.1", None),
    ("delayed", "5.1.1", None),
    ("delivered", "2.0.0", None),
    ("failed", "5.1.01", None),
    (None, "5.1.1", None),
    ("failed", None, None),
])
def test_public_classifier_is_shared_with_synchronous_smtp(action, status, expected):
    assert classify_bounce(action, status) == expected


def test_trailing_rfc_status_comments_do_not_become_diagnostic_or_address_data():
    raw = report().replace(b"Status: 5.1.1", b"Status: 5.1.1 (Synthetic (unknown mailbox))")
    notice, = parse_bounces(raw)
    assert notice.status == "5.1.1"
    assert notice.reason == "invalid_mailbox"


@pytest.mark.parametrize("original", [
    "Subject: No original identifier\r\n\r\nMessage-ID: <batch-123@example.test>\r\n",
    "Subject: No original identifier\r\n\r\n",
])
def test_body_footer_and_envelope_id_cannot_supply_original_id(original):
    raw = report(original=original, human=f"Message-ID: {MESSAGE_ID}",
                 per_message=f"Reporting-MTA: dns; relay.example.test\r\nOriginal-Envelope-ID: {MESSAGE_ID}")
    assert parse_bounces(raw) == []


def test_nested_attachment_message_id_cannot_supply_missing_original_header():
    original = (
        'Content-Type: multipart/mixed; boundary="nested"\r\n\r\n'
        "--nested\r\nContent-Type: message/rfc822\r\n\r\n"
        f"Message-ID: {MESSAGE_ID}\r\n\r\nSynthetic message\r\n--nested--\r\n")
    assert parse_bounces(report(original=original, original_type="message/rfc822")) == []


@pytest.mark.parametrize("recipient", [
    "not-an-address", "a@example.test, b@example.test", "Name <a@example.test>",
    "a@example.test; b@example.test", ".a@example.test", "a..b@example.test", "a@localhost",
])
def test_malformed_recipient_is_not_guessed(recipient):
    assert parse_bounces(report([recipient_record(recipient)])) == []


def test_non_rfc822_recipient_type_is_ignored():
    record = recipient_record().replace("rfc822;", "x400;")
    assert parse_bounces(report([record])) == []


@pytest.mark.parametrize("action,status", [
    ("delivered", "2.0.0"), ("relayed", "2.0.0"), ("expanded", "2.0.0"),
    ("delayed", "5.1.1"), ("failed", "4.4.1"), ("unknown", "5.1.1"),
    ("failed", "550"), ("failed", "5.01.1"), ("failed", "5.1.01"),
    ("failed", "5.1.1000"), ("failed", "5.1.1 garbage"), ("failed", "9.1.1"),
])
def test_successes_unknown_actions_or_malformed_status_are_ignored(action, status):
    assert parse_bounces(report([recipient_record(action=action, status=status)])) == []


@pytest.mark.parametrize("field", ["Final-Recipient", "Action", "Status"])
def test_missing_and_duplicate_required_fields_do_not_suppress(field):
    record = recipient_record()
    line = next(line for line in record.splitlines() if line.startswith(field + ":"))
    assert parse_bounces(report([record.replace(line + "\r\n", "")])) == []
    assert parse_bounces(report([record + line + "\r\n"])) == []


def test_duplicate_records_are_deduplicated_and_frozen():
    notices = parse_bounces(report([recipient_record(), recipient_record()]))
    assert len(notices) == 1
    with pytest.raises(FrozenInstanceError):
        notices[0].recipient = "other@example.test"


def test_conflicting_recipient_records_are_not_used_but_other_recipients_survive():
    notices = parse_bounces(report([
        recipient_record(), recipient_record(status="5.7.1"),
        recipient_record("other@example.test"),
    ]))
    assert [notice.recipient for notice in notices] == ["other@example.test"]


@pytest.mark.parametrize("action", ["delivered", "relayed", "expanded"])
def test_contradictory_success_and_failure_records_cannot_suppress_recipient(action):
    assert parse_bounces(report([
        recipient_record(), recipient_record(action=action, status="2.0.0"),
    ])) == []


def test_original_recipient_is_normalized_and_preferred_for_outbox_matching():
    record = recipient_record(extra="Original-Recipient: RFC822; Reader@EXAMPLE.TEST\r\n")
    notice, = parse_bounces(report([record]))
    assert notice.recipient == "reader@example.test"
    assert notice.permanent is True


def test_failed_forwarded_destination_does_not_prove_original_alias_invalid():
    record = recipient_record("forwarded@example.test", extra="Original-Recipient: rfc822; alias@example.test\r\n")
    notice, = parse_bounces(report([record]))
    assert notice.recipient == "alias@example.test"
    assert notice.reason == "recipient_rejected"
    assert notice.permanent is False


@pytest.mark.parametrize("original", ["Original-Recipient: x400; reader@example.test\r\n",
                                      "Original-Recipient: rfc822; bad-address\r\n"])
def test_invalid_original_recipient_never_falls_back_to_final(original):
    assert parse_bounces(report([recipient_record(extra=original)])) == []


@pytest.mark.parametrize("original", [
    "Message-ID: <one@example.test>\r\nMessage-ID: <two@example.test>\r\n\r\n",
    "Message-ID: no-brackets@example.test\r\n\r\n",
    "Message-ID: <one@example.test> <two@example.test>\r\n\r\n",
    "Message-ID: <invalid..id@example.test>\r\n\r\n",
])
def test_ambiguous_or_malformed_original_id_is_ignored(original):
    assert parse_bounces(report(original=original)) == []


def test_multiple_returned_original_parts_are_ambiguous():
    extra = ("--dsn-boundary\r\nContent-Type: text/rfc822-headers\r\n\r\n"
             "Message-ID: <another@example.test>\r\n\r\n")
    assert parse_bounces(report(extra_parts=extra)) == []


@pytest.mark.parametrize("parameter", [
    b'report-type="other"', b'report-type="delivery-status"', b'boundary="another"',
])
def test_duplicate_mime_report_parameters_are_not_silently_salvaged(parameter):
    raw = report().replace(b'boundary="dsn-boundary"', b'boundary="dsn-boundary"; ' + parameter, 1)
    assert parse_bounces(raw) == []


@pytest.mark.parametrize("raw", [b"", b"not MIME", b"\x00\xff\x01", None, "not bytes"])
def test_non_message_or_non_bytes_is_ignored(raw):
    assert parse_bounces(raw) == []


def test_non_dsn_sender_or_failure_subject_is_insufficient():
    raw = (f"From: mailer-daemon@example.test\r\nSubject: Delivery failure\r\n"
           f"Message-ID: {MESSAGE_ID}\r\nContent-Type: text/plain\r\n\r\n"
           f"Message-ID: {MESSAGE_ID}\r\n{recipient_record()}").encode()
    assert parse_bounces(raw) == []


@pytest.mark.parametrize("change", [
    lambda raw: raw.replace(b'report-type="delivery-status"', b'report-type="disposition-notification"'),
    lambda raw: raw.replace(b"Content-Type: message/delivery-status", b"Content-Type: text/plain"),
    lambda raw: raw.replace(b"--dsn-boundary--\r\n", b""),
    lambda raw: raw.replace(b"Reporting-MTA: dns; relay.example.test", b"Reporting-MTA: missing-type"),
    lambda raw: raw.replace(b"Reporting-MTA: dns; relay.example.test", b""),
])
def test_malformed_or_other_report_is_ignored(change):
    assert parse_bounces(change(report())) == []


def test_bad_encoded_returned_headers_are_ignored_without_raising():
    raw = report(encoding="base64").replace(base64.encodebytes(
        (f"Message-ID: {MESSAGE_ID}\r\nFrom: sender@example.test\r\n"
         "To: reader@example.test\r\nSubject: Synthetic briefing\r\n\r\n").encode()), b"not!valid!base64")
    assert parse_bounces(raw) == []
