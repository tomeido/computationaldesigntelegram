"""Parse structured RFC 3464 notices without treating them as authenticated.

Only returned MIME message headers identify the original message. Callers must
still match both its Message-ID and recipient against their delivery outbox.
``permanent`` means an explicitly reported invalid/disabled destination, not
merely the RFC's broader class-5 permanent delivery failure.
"""

from __future__ import annotations

import base64
import binascii
import quopri
import re
from dataclasses import dataclass
from email import policy
from email.errors import MessageError
from email.message import Message
from email.parser import BytesHeaderParser, BytesParser

_LOCAL = re.compile(r"[a-z0-9!#$%&'*+/=?^_`{|}~.-]+", re.IGNORECASE)
_LABEL = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?")
_ATOM = r"[a-zA-Z0-9!#$%&'*+/=?^_`{|}~-]+"
_MESSAGE_ID = re.compile(rf"<{_ATOM}(?:\.{_ATOM})*@{_ATOM}(?:\.{_ATOM})*>")
_NUMBER = r"(?:0|[1-9][0-9]{0,2})"
_STATUS = re.compile(rf"([245]\.{_NUMBER}\.{_NUMBER})(.*)")
_SMTP_STATUS = re.compile(
    rf"smtp\s*;\s*[245][0-9]{{2}}[ -]+([245]\.{_NUMBER}\.{_NUMBER})(?=\s|$)", re.IGNORECASE)
_PERMANENT = {
    "5.1.1": "invalid_mailbox",
    "5.1.2": "invalid_domain",
    "5.1.3": "invalid_mailbox",
    "5.1.6": "invalid_mailbox",
    "5.2.1": "mailbox_disabled",
}


@dataclass(frozen=True)
class BounceNotice:
    message_id: str
    recipient: str
    action: str
    status: str
    reason: str
    permanent: bool


def _header(message: Message, name: str) -> str | None:
    # HeaderRegistry may salvage a malformed Message-ID by dropping a second
    # ID or trailing garbage. Validate the complete original field instead.
    values = [value for key, value in message.raw_items() if key.casefold() == name.casefold()]
    if len(values) != 1:
        return None
    # The email parser recognizes continuation lines before this unfolding.
    value = " ".join(str(values[0]).splitlines()).strip()
    if any(ord(char) < 32 and char != "\t" or ord(char) == 127 for char in value):
        return None
    return value


def _valid_mime_type(message: Message) -> bool:
    values = message.get_all("Content-Type", [])
    return len(values) == 1 and not getattr(values[0], "defects", ())


def _recipient(value: str | None) -> str | None:
    if value is None:
        return None
    kind, separator, address = value.partition(";")
    if not separator or kind.strip().casefold() != "rfc822":
        return None
    address = address.strip()
    # Require one bare envelope address, not display names or a mailbox list.
    if address.count("@") != 1:
        return None
    local, domain = address.rsplit("@", 1)
    try:
        domain = domain.encode("idna").decode("ascii").lower()
    except UnicodeError:
        return None
    if (not _LOCAL.fullmatch(local) or len(local) > 64 or local.startswith(".")
            or local.endswith(".") or ".." in local or len(domain.split(".")) < 2
            or any(not _LABEL.fullmatch(label) for label in domain.split("."))
            or len(local) + len(domain) + 1 > 254):
        return None
    return f"{local.casefold()}@{domain}"


def _comments_only(value: str) -> bool:
    """Accept RFC trailing comments, including nested/escaped parentheses."""
    depth = 0
    escaped = False
    for char in value:
        if escaped:
            escaped = False
        elif depth and char == "\\":
            escaped = True
        elif char == "(":
            depth += 1
        elif char == ")" and depth:
            depth -= 1
        elif not depth and not char.isspace():
            return False
    return depth == 0 and not escaped


def _status(value: str | None) -> str | None:
    if value is None:
        return None
    match = _STATUS.fullmatch(value)
    if match is None or not _comments_only(match[2]):
        return None
    return match[1]


def classify_bounce(action: str, status: str) -> tuple[str, bool] | None:
    """Classify a sane DSN status without claiming every class-5 address invalid.

    SMTP recipient rejections can reuse this classifier with ``action=failed``.
    It provides no authentication or outbox matching and never implies a retry.
    """
    if not isinstance(action, str) or not isinstance(status, str):
        return None
    action = action.strip().casefold()
    code = _status(status.strip())
    if code is None:
        return None
    if action == "delayed" and code.startswith("4."):
        return "temporary_failure", False
    if action != "failed" or not code.startswith("5."):
        return None
    if reason := _PERMANENT.get(code):
        return reason, True
    return ("mailbox_full" if code == "5.2.2" else "recipient_rejected"), False


def _original_id(part: Message) -> str | None:
    if not _valid_mime_type(part) or len(part.get_all("Content-Transfer-Encoding", [])) > 1:
        return None
    encoding = (_header(part, "Content-Transfer-Encoding") or "7bit").casefold()
    if encoding not in {"7bit", "8bit", "binary", "base64", "quoted-printable"}:
        return None
    if part.get_content_type() == "text/rfc822-headers":
        raw = part.get_payload(decode=True)
        if not isinstance(raw, bytes) or part.defects:
            return None
        original = BytesHeaderParser(policy=policy.default).parsebytes(raw)
    else:
        payload = part.get_payload()
        if not isinstance(payload, list) or len(payload) != 1 or not isinstance(payload[0], Message):
            return None
        original = payload[0]
        if encoding in {"base64", "quoted-printable"}:
            # stdlib parses encoded message/rfc822 bodies as one headerless
            # message; decode that MIME body before reading its actual headers.
            encoded = original.get_payload()
            if not isinstance(encoded, str):
                return None
            if encoding == "base64":
                if original.keys():
                    return None
                encoded_bytes = encoded.encode("ascii")
                raw = base64.b64decode(re.sub(rb"[ \t\r\n]", b"", encoded_bytes), validate=True)
            elif original.keys():
                # QP leaves most ASCII header lines looking like headers, so
                # stdlib may already have recognized them before CTE decoding.
                encoded_headers = "".join(f"{key}: {value}\r\n" for key, value in original.raw_items())
                raw = quopri.decodestring((encoded_headers + "\r\n").encode("ascii"))
            else:
                raw = quopri.decodestring(encoded.encode("ascii"))
            original = BytesHeaderParser(policy=policy.default).parsebytes(raw)
    value = _header(original, "Message-ID")
    return value if value is not None and _MESSAGE_ID.fullmatch(value) else None


def _record(record: Message, message_id: str) -> BounceNotice | None:
    if record.defects or len(record.get_all("Diagnostic-Code", [])) > 1:
        return None
    final = _recipient(_header(record, "Final-Recipient"))
    if final is None:
        return None
    original_values = record.get_all("Original-Recipient", [])
    original = _recipient(_header(record, "Original-Recipient")) if original_values else final
    if original is None:
        return None
    action = (_header(record, "Action") or "").casefold()
    status = _status(_header(record, "Status"))
    if status is None:
        return None
    classification = classify_bounce(action, status)
    if classification is None:
        return None
    reason, permanent = classification
    if action == "failed" and original != final:
        # A failed forwarding target does not prove the subscribed alias invalid.
        return BounceNotice(message_id, original, action, status, "recipient_rejected", False)
    diagnostic = _header(record, "Diagnostic-Code") or ""
    smtp_status = _SMTP_STATUS.match(diagnostic)
    if permanent and smtp_status is not None and smtp_status[1] != status:
        # A conflicting typed SMTP diagnostic (e.g. Microsoft access-denied)
        # cannot establish that the original destination is invalid.
        return BounceNotice(message_id, original, action, status, "recipient_rejected", False)
    return BounceNotice(message_id, original, action, status, reason, permanent)


def parse_bounces(raw: bytes) -> list[BounceNotice]:
    """Return unverified, deduplicated notices from a complete delivery report.

    Missing/ambiguous identities, malformed records, other recipient types and
    successful delivery reports are ignored. Subject, sender, Envelope-ID and
    arbitrary body/footer Message-IDs are never used as delivery evidence.
    """
    if not isinstance(raw, bytes) or not raw:
        return []
    try:
        report = BytesParser(policy=policy.default).parsebytes(raw)
        if (report.defects or not _valid_mime_type(report)
                or report.get_content_type() != "multipart/report"
                or str(report.get_param("report-type", "")).casefold() != "delivery-status"):
            return []
        parts = report.get_payload()
        if not isinstance(parts, list):
            return []
        delivery_parts = [p for p in parts if p.get_content_type() == "message/delivery-status"]
        originals = [p for p in parts if p.get_content_type() in {"message/rfc822", "text/rfc822-headers"}]
        if len(delivery_parts) != 1 or len(originals) != 1:
            return []
        message_id = _original_id(originals[0])
        delivery = delivery_parts[0]
        records = delivery.get_payload()
        if (message_id is None or delivery.defects or not _valid_mime_type(delivery)
                or len(delivery.get_all("Content-Transfer-Encoding", [])) > 1
                or (_header(delivery, "Content-Transfer-Encoding") or "7bit").casefold() != "7bit"
                or not isinstance(records, list) or len(records) < 2 or records[0].defects):
            return []
        reporting_mta = _header(records[0], "Reporting-MTA")
        if reporting_mta is None or not re.fullmatch(r"[a-zA-Z][a-zA-Z0-9-]*\s*;\s*\S.*", reporting_mta):
            return []
        notices: dict[str, BounceNotice] = {}
        conflicting: set[str] = set()
        for record in records[1:]:
            notice = _record(record, message_id)
            if notice is None:
                # A contradictory success record for the same recipient also
                # makes this report unsuitable for suppressing that address.
                action = (_header(record, "Action") or "").casefold()
                status = _status(_header(record, "Status"))
                final = _recipient(_header(record, "Final-Recipient"))
                if (not record.defects and action in {"delivered", "relayed", "expanded"}
                        and status is not None and status.startswith("2.") and final is not None):
                    original = (_recipient(_header(record, "Original-Recipient"))
                                if record.get_all("Original-Recipient", []) else final)
                    if original is not None:
                        conflicting.add(original)
                continue
            previous = notices.get(notice.recipient)
            if previous is not None and previous != notice:
                conflicting.add(notice.recipient)
            else:
                notices[notice.recipient] = notice
        return [notice for address, notice in notices.items() if address not in conflicting]
    except (MessageError, ValueError, TypeError, UnicodeError, RecursionError, binascii.Error):
        return []
