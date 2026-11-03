from email.message import EmailMessage

import pytest

from compdesign_bot import mail_unsubscribe as parser
from compdesign_bot.mail_unsubscribe import UnsubscribeRequest, parse_unsubscribe, reply_headers

RECIPIENT = "newsletter@example.org"
AUTH = "mx.google.com; dkim=pass header.i=@mit.edu; dmarc=pass header.from=mit.edu"
EXPECTED = UnsubscribeRequest("<request@mit.edu>", "lsass@mit.edu", ("<newsletter@example.org>",))


def reply(body="STOP", *, html=False, auth=AUTH, sender="Larry Sass <lsass@mit.edu>",
          recipient=RECIPIENT, subject="Re: Computational Design", extra=None):
    message = EmailMessage()
    message["From"] = sender
    message["To"] = recipient
    message["Subject"] = subject
    message["Message-ID"] = "<request@mit.edu>"
    message["In-Reply-To"] = "<newsletter@example.org>"
    if auth is not None:
        message["Authentication-Results"] = auth
    for name, value in (extra or {}).items():
        message[name] = value
    message.set_content(body, subtype="html" if html else "plain")
    return message.as_bytes()


def parse(raw):
    return parse_unsubscribe(raw, recipient=RECIPIENT)


@pytest.mark.parametrize("command", [
    "STOP", "stop!", "unsubscribe", "Unsubscribe me.", "Please unsubscribe me",
    "Please unsubscribe", "Unsubscribe please.", "Please unsubscribe me from your mailing list.",
    "Remove me from your mailing list.",
    "Please remove me from this email list. Thank you.", "Stop emailing me", "Please stop sending me emails.",
    "구독 해지", "구독해지", "구독 취소 요청합니다", "수신 거부", "수신거부 해주세요", "구독 해지 부탁드립니다",
    "메일 그만 보내주세요", "이메일 그만 보내 주세요.", "수신 거부합니다", "구독 해지해주세요",
])
def test_explicit_english_and_korean_commands(command):
    assert parse(reply(command)) == EXPECTED


def test_larry_stop_reply_with_signature_and_quoted_newsletter():
    raw = reply("STOP\n\nLarry Sass\nProfessor\nMIT\n\nOn Thu, Oct 8, Newsletter wrote:\n"
                "> Daily briefing\n> To unsubscribe, contact the administrator.\n")
    assert parse(raw) == EXPECTED


def test_header_only_candidate_does_not_claim_body_intent():
    raw = reply("Thanks, I want to keep receiving these!")
    assert reply_headers(raw, recipient=RECIPIENT) == EXPECTED
    assert parse(raw) is None


@pytest.mark.parametrize("body", [
    "I do not want to unsubscribe", "Do not unsubscribe me.", "Please don't unsubscribe me",
    "How do I unsubscribe?", "Do I reply STOP to unsubscribe?", "Please subscribe me.",
    "Here is an example:\nSTOP", "Thanks for this!\n\nUnsubscribe", "Stop sending spam to my colleague",
    "Do not\nunsubscribe", "Please unsubscribe my colleague other@mit.edu", "UNSUBSCRIBE?", "",
    "> STOP", "\n> Please unsubscribe me", "On Thu, Newsletter wrote:\nSTOP",
    "---------- Forwarded message ---------\nSTOP", "Begin forwarded message:\nSTOP",
    "From: Newsletter <newsletter@example.org>\nSTOP", "-- \nUnsubscribe", "Sent from my iPhone\nSTOP",
    "뉴스레터 잘 읽고 있습니다.\n구독 해지", "구독 해지하지 마세요", "수신 거부 방법을 알려주세요",
    "STOP\nDo not unsubscribe me", "unsubscribe\nI don't want to unsubscribe", "STOP\n구독 해지하지 마세요",
])
def test_no_substring_search_quoted_footer_or_negative_intent(body):
    assert parse(reply(body)) is None


@pytest.mark.parametrize("body", [
    "<div>STOP</div><div>Sent from my phone</div><blockquote>Original newsletter</blockquote>",
    "<p>Please <b>unsubscribe</b> me.</p><div class='gmail_quote'>Original newsletter</div>",
    "<div>구독&nbsp;해지</div><div id='divRplyFwdMsg'>From: Newsletter</div><div>Original newsletter</div>",
    "<p>STOP</p><div class='yahoo_quoted'>Original newsletter</div>",
    "<html><head><title>Example</title></head><body><p>STOP</p></body></html>",
])
def test_html_only_author_command_is_supported(body):
    assert parse(reply(body, html=True)) == EXPECTED


@pytest.mark.parametrize("body", [
    "<blockquote>STOP</blockquote>", "<div class='gmail_quote'><div>STOP</div></div>",
    "<div class='yahoo_quoted'>Please unsubscribe me</div>",
    "<div id='divRplyFwdMsg'>From: Newsletter</div><div>STOP</div>",
    "<div class='gmail_attr'>On Thu, Newsletter wrote:</div><div>STOP</div>",
    "<div class='OutlookMessageHeader'>From: Newsletter</div><p>STOP</p>",
    "<div class='gmail_signature'>unsubscribe</div>", "<div id='Signature'>STOP</div>",
    "<style>STOP</style>", "<div style='display: none'>STOP</div>", "<span hidden>STOP</span>",
    "<p>Thanks for the newsletter.</p><footer>Unsubscribe</footer>",
    "<p>Do not unsubscribe me.</p><blockquote>STOP</blockquote>", "<footer>unsubscribe</footer>",
])
def test_html_quote_signature_hidden_and_footer_commands_are_ignored(body):
    assert parse(reply(body, html=True)) is None


@pytest.mark.parametrize("auth", [
    None, "attacker.example; dkim=pass; dmarc=pass header.from=mit.edu",
    "mx.google.com.attacker.example; dkim=pass; dmarc=pass header.from=mit.edu",
    "mx.google.com; dkim=pass; dmarc=fail header.from=mit.edu",
    "mx.google.com; dkim=fail; spf=fail; dmarc=pass header.from=mit.edu",
    "mx.google.com; dkim=pass; dmarc=pass header.from=attacker.example",
    "mx.google.com; dkim=pass; dmarc=pass",
    "mx.google.com; dkim=passive; dmarc=pass header.from=mit.edu",
    "mx.google.com; dkim=fail (dkim=pass; dmarc=pass header.from=mit.edu); dmarc=fail",
    'mx.google.com; dkim=fail reason="dkim=pass; dmarc=pass header.from=mit.edu"; dmarc=fail',
    "mx.google.com; dkim=pass; dmarc=pass header.from=mit.edu; dmarc=fail header.from=mit.edu",
    "mx.google.com; dkim=pass; dmarc=pass header.from=mit.edu header.from=attacker.example",
    AUTH + " (unterminated",
])
def test_authentication_missing_forged_misaligned_or_ambiguous(auth):
    raw = reply(auth=auth)
    assert reply_headers(raw, recipient=RECIPIENT) is None
    assert parse(raw) is None


def test_only_first_authentication_results_header_is_trusted():
    raw = reply(auth="attacker.example; dkim=fail; dmarc=fail")
    raw = raw.replace(b"Content-Type:", f"Authentication-Results: {AUTH}\nContent-Type:".encode(), 1)
    assert parse(raw) is None


def test_spf_pass_with_aligned_dmarc_is_sufficient():
    assert parse(reply(auth="mx.google.com; spf=pass; dmarc=pass header.from=mit.edu")) == EXPECTED


@pytest.mark.parametrize("sender", [
    "lsass@mit.edu, other@mit.edu", "lsass@mit.edu,", "not-an-address", "a..b@mit.edu",
    "Larry <lsass@mit.edu> garbage", "group: lsass@mit.edu;",
])
def test_malformed_or_multiple_sender_is_rejected(sender):
    assert parse(reply(sender=sender)) is None


def test_duplicate_from_header_is_rejected():
    raw = reply().replace(b"To:", b"From: other@mit.edu\nTo:", 1)
    assert parse(raw) is None


def test_normalized_sender_preserves_exact_alias_and_service_must_be_in_to():
    assert parse(reply(sender="Larry <LSASS@MIT.EDU>")) == EXPECTED
    request = parse(reply(sender="lsass+work@mit.edu", recipient="Other <other@example.org>, " + RECIPIENT))
    assert request.email == "lsass+work@mit.edu"
    assert parse(reply(recipient="other@example.org", extra={"Cc": RECIPIENT})) is None


@pytest.mark.parametrize("name,value", [
    ("Auto-Submitted", "auto-replied"), ("Auto-Submitted", "auto-generated"),
    ("Precedence", "bulk"), ("Precedence", "list"), ("List-ID", "example.mit.edu"),
    ("List-Unsubscribe", "<mailto:remove@mit.edu>"), ("X-Autoreply", "yes"),
    ("X-Autorespond", "yes"), ("X-Auto-Response-Suppress", "All"),
    ("Resent-From", "lsass@mit.edu"), ("X-Forwarded-For", "other@mit.edu"),
])
def test_automatic_list_and_forwarded_messages_are_ignored(name, value):
    assert parse(reply(extra={name: value})) is None


@pytest.mark.parametrize("subject", ["Fwd: Newsletter", "FW: Newsletter", "Re: Fwd: Newsletter",
                                        "Automatic reply: Newsletter", "Out of office", "자동 응답: 뉴스레터"])
def test_forward_and_automatic_reply_subjects_are_ignored(subject):
    assert parse(reply(subject=subject)) is None


@pytest.mark.parametrize("name,value", [
    ("Message-ID", "<request@mit.edu> <other@mit.edu>"), ("Message-ID", "<request@mit.edu> garbage"),
    ("Message-ID", "<bad id@mit.edu>"), ("In-Reply-To", "<newsletter@example.org> trailing"),
    ("In-Reply-To", "newsletter@example.org"), ("References", "injected <newsletter@example.org>"),
])
def test_raw_reference_and_request_ids_are_strict(name, value):
    raw = reply()
    if name == "References":
        raw = raw.replace(b"Content-Type:", f"{name}: {value}\nContent-Type:".encode(), 1)
    else:
        lines = raw.splitlines(keepends=True)
        raw = b"".join(f"{name}: {value}\n".encode() if line.startswith(name.encode() + b":") else line
                       for line in lines)
    assert parse(raw) is None


def test_reference_ids_deduplicate_and_support_references_only_reply():
    raw = reply(extra={"References": "<first@example.org> <newsletter@example.org>"})
    assert parse(raw).references == ("<newsletter@example.org>", "<first@example.org>")
    raw = raw.replace(b"In-Reply-To: <newsletter@example.org>\n", b"")
    assert parse(raw).references == ("<first@example.org>", "<newsletter@example.org>")
    assert parse(reply().replace(b"In-Reply-To: <newsletter@example.org>\n", b"")) is None


def test_attachments_and_encapsulated_messages_are_never_author_text():
    from email import policy
    from email.parser import BytesParser

    message = BytesParser(policy=policy.default).parsebytes(reply("Thanks for this."))
    message.add_attachment("STOP", filename="request.txt")
    forwarded = EmailMessage()
    forwarded.set_content("STOP")
    message.add_attachment(forwarded)
    assert parse(message.as_bytes()) is None


def test_plaintext_alternative_wins_over_different_html_intent():
    from email import policy
    from email.parser import BytesParser

    message = BytesParser(policy=policy.default).parsebytes(reply("Do not unsubscribe me."))
    message.add_alternative("<p>STOP</p>", subtype="html")
    assert parse(message.as_bytes()) is None


def test_body_size_header_size_and_mime_depth_are_bounded():
    assert parse(reply() + b"x" * parser._MAX_MESSAGE) is None
    assert reply_headers(b"X-Long: " + b"x" * parser._MAX_HEADERS, recipient=RECIPIENT) is None
    assert parse(reply("STOP\n" + "x" * parser._MAX_TEXT)) is None
    from email import policy
    from email.parser import BytesParser

    message = BytesParser(policy=policy.default).parsebytes(reply())
    child = EmailMessage()
    child.set_content("STOP")
    for _ in range(parser._MAX_DEPTH + 2):
        wrapper = EmailMessage()
        wrapper.make_mixed()
        wrapper.attach(child)
        child = wrapper
    message.make_mixed()
    message.set_payload([child])
    assert parse(message.as_bytes()) is None


def test_malformed_input_does_not_raise():
    for raw in (b"", b"garbage", b"From: \xff\xfe\n\nSTOP", None, "STOP"):
        assert parse(raw) is None


def attachment_prefix(body="STOP", *, alternative=False):
    from email import policy
    from email.parser import BytesParser

    message = BytesParser(policy=policy.default).parsebytes(reply(body))
    if alternative:
        message.add_alternative(f"<p>{body}</p>", subtype="html")
    message.add_attachment(b"x" * parser._MAX_MESSAGE, maintype="application", subtype="octet-stream",
                           filename="large.bin")
    raw = message.as_bytes()
    assert len(raw) > parser._MAX_MESSAGE
    return raw[:parser._MAX_MESSAGE]


@pytest.mark.parametrize("alternative", [False, True])
def test_complete_authored_stop_before_truncated_attachment_is_recognized(alternative):
    prefix = attachment_prefix(alternative=alternative)
    assert parse_unsubscribe(prefix, recipient=RECIPIENT, truncated=True) == EXPECTED
    # Missing MIME close boundaries remain malformed for the ordinary API.
    assert parse(prefix) is None


def test_complete_normal_reply_before_truncated_attachment_has_no_unsubscribe_intent():
    prefix = attachment_prefix("Thanks, please keep sending these!")
    assert parse_unsubscribe(prefix, recipient=RECIPIENT, truncated=True) is None


@pytest.mark.parametrize("html", [False, True])
def test_truncated_singlepart_text_cannot_unsubscribe_even_when_it_starts_with_stop(html):
    raw = reply("<p>STOP</p>" if html else "STOP", html=html)
    assert parse_unsubscribe(raw, recipient=RECIPIENT, truncated=True) is None


def test_truncated_authored_mime_part_cannot_unsubscribe():
    prefix = attachment_prefix()
    stop_end = prefix.index(b"STOP\n") + len(b"STOP\n")
    assert parse_unsubscribe(prefix[:stop_end], recipient=RECIPIENT, truncated=True) is None


def test_incomplete_preferred_plaintext_does_not_fall_back_to_complete_html_command():
    from email import policy
    from email.parser import BytesParser

    message = BytesParser(policy=policy.default).parsebytes(reply("<p>STOP</p>", html=True))
    message.add_alternative("Do not unsubscribe me.", subtype="plain")
    raw = message.as_bytes()
    stop_at = raw.index(b"Do not unsubscribe me.") + len(b"Do not unsubscribe me.")
    assert parse_unsubscribe(raw[:stop_at], recipient=RECIPIENT, truncated=True) is None


def test_truncated_attachment_text_is_never_selected_as_author_command():
    from email import policy
    from email.parser import BytesParser

    message = BytesParser(policy=policy.default).parsebytes(reply("Thanks for the news."))
    message.add_attachment("STOP", filename="request.txt")
    message.add_attachment(b"x" * parser._MAX_MESSAGE, maintype="application", subtype="octet-stream",
                           filename="large.bin")
    prefix = message.as_bytes()[:parser._MAX_MESSAGE]
    assert parse_unsubscribe(prefix, recipient=RECIPIENT, truncated=True) is None


def test_other_mime_structural_defects_are_not_tolerated_for_truncated_messages():
    prefix = attachment_prefix()
    malformed = prefix.replace(b"Content-Type: text/plain; charset=\"utf-8\"\n", b"malformed header\n", 1)
    assert parse_unsubscribe(malformed, recipient=RECIPIENT, truncated=True) is None


def test_missing_close_boundary_in_a_complete_nested_part_is_rejected():
    prefix = attachment_prefix(alternative=True)
    from email import policy
    from email.parser import BytesParser

    message = BytesParser(policy=policy.default).parsebytes(prefix)
    inner_boundary = message.get_payload()[0].get_boundary().encode()
    malformed = prefix.replace(b"--" + inner_boundary + b"--\n", b"", 1)
    assert parse_unsubscribe(malformed, recipient=RECIPIENT, truncated=True) is None
