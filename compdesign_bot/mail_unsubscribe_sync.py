"""Read authenticated newsletter replies without changing mailbox flags or content."""

import hashlib
import imaplib
import re
import ssl
from contextlib import suppress
from dataclasses import dataclass

from .mail_bounce_sync import (
    _DATE,
    _UID,
    _fetch,
    _mailbox_argument,
    _mailboxes,
    _ok,
    _search,
    _uidvalidity,
)
from .mail_unsubscribe import parse_unsubscribe, reply_headers

_PER_FOLDER_LIMIT = 500
_HEADER_CHUNK = 50
_MAX_MESSAGE_BYTES = 512 * 1024
_SIZE = re.compile(rb"(?:^|[\s(])RFC822\.SIZE\s+([0-9]+)(?=[\s)])", re.IGNORECASE)
_PREFIX_BODY = re.compile(rb"(?:^|[\s(])BODY\[\]<0>(?=\s)", re.IGNORECASE)
_HEADER_FIELDS = (
    "FROM", "TO", "MESSAGE-ID", "IN-REPLY-TO", "REFERENCES", "AUTHENTICATION-RESULTS",
    "CONTENT-TYPE", "SUBJECT", "AUTO-SUBMITTED", "PRECEDENCE", "LIST-ID", "LIST-UNSUBSCRIBE",
    "X-AUTOREPLY", "X-AUTORESPOND", "X-AUTO-RESPONSE-SUPPRESS", "RESENT-FROM", "RESENT-TO",
    "RESENT-DATE", "CONTENT-DISPOSITION", "X-FORWARDED-TO", "X-FORWARDED-FOR", "LIST-POST",
    "MAILING-LIST", "RESENT-SENDER",
)


class UnsubscribeSyncUnavailable(RuntimeError):
    """The reply scan was incomplete; mail delivery must wait for another scan."""


@dataclass
class UnsubscribeSyncReport:
    scanned: int = 0
    recognized: int = 0
    applied: int = 0
    duplicates: int = 0
    unmatched: int = 0
    stale: int = 0
    oversized: int = 0
    backlog: bool = False


def _message_size(client, uid: int) -> int:
    """Check declared size before fetching the body of a related reply."""
    found = None
    for item in _ok(client.uid("FETCH", str(uid), "(UID RFC822.SIZE)")):
        if item is None or item in (b"", b")"):
            continue
        if not isinstance(item, bytes):
            raise TypeError("invalid message size response")
        uids = _UID.findall(item)
        sizes = _SIZE.findall(item)
        if len(uids) != 1 or int(uids[0]) != uid or len(sizes) != 1 or found is not None:
            raise ValueError("ambiguous message size response")
        found = int(sizes[0])
    if found is None or not 0 < found <= 4_294_967_295:
        raise ValueError("invalid or unavailable reply body size")
    return found


def _fetch_prefix(client, uid: int, size: int) -> bytes:
    """Read a bounded prefix; IMAP must identify the exact UID and byte offset."""
    found = None
    query = f"(UID BODY.PEEK[]<0.{_MAX_MESSAGE_BYTES}>)"
    for item in _ok(client.uid("FETCH", str(uid), query)):
        if not isinstance(item, tuple):
            continue
        if len(item) != 2 or not all(isinstance(value, bytes) for value in item):
            raise ValueError("invalid partial fetch response")
        metadata, raw = item
        uids = _UID.findall(metadata)
        if len(uids) != 1 or int(uids[0]) != uid or found is not None or not _PREFIX_BODY.search(metadata):
            raise ValueError("ambiguous partial fetch response")
        if len(raw) != min(size, _MAX_MESSAGE_BYTES):
            raise ValueError("incomplete partial fetch response")
        found = raw
    if found is None:
        raise ValueError("missing partial fetch response")
    return found


def sync_mail_unsubscribes(settings, store, *, client_factory=None) -> UnsubscribeSyncReport:
    """Scan bounded reply batches and checkpoint only completely processed chunks."""
    report = UnsubscribeSyncReport()
    if not settings.mailing_unsubscribe_enabled:
        return report
    client = None
    try:
        settings.require_unsubscribes()
        since = store.bounce_scan_since()
        if not isinstance(since, str) or not _DATE.fullmatch(since):
            raise ValueError("invalid unsubscribe scan date")
        factory = client_factory or imaplib.IMAP4_SSL
        client = factory(settings.imap_host, settings.imap_port, ssl_context=ssl.create_default_context(), timeout=30)
        _ok(client.login(settings.smtp_username, settings.smtp_password))
        for mailbox in _mailboxes(client):
            _ok(client.select(_mailbox_argument(mailbox), readonly=True))
            validity = _uidvalidity(client)
            key = hashlib.sha256(
                settings.imap_host.encode() + b"\0" + settings.smtp_username.encode() + b"\0" + mailbox
            ).hexdigest()
            previous = store.unsubscribe_cursor(key)
            cursor = int(previous[1]) if previous and previous[0] == validity else 0
            if cursor < 0:
                raise ValueError("invalid saved cursor")
            uids = _search(client, since, cursor)
            report.backlog |= len(uids) > _PER_FOLDER_LIMIT
            selected = uids[:_PER_FOLDER_LIMIT]
            for offset in range(0, len(selected), _HEADER_CHUNK):
                chunk = selected[offset:offset + _HEADER_CHUNK]
                headers = _fetch(client, chunk, headers=True, header_fields=_HEADER_FIELDS)
                report.scanned += len(chunk)
                for uid in chunk:
                    candidate = reply_headers(headers[uid], recipient=settings.smtp_from)
                    if candidate is None:
                        continue
                    if not store.unsubscribe_reply_matches(candidate):
                        report.unmatched += 1
                        continue
                    size = _message_size(client, uid)
                    # At most one matching reply body is held in memory at a time.
                    truncated = size > _MAX_MESSAGE_BYTES
                    raw = _fetch_prefix(client, uid, size) if truncated else _fetch(client, [uid], headers=False)[uid]
                    if len(raw) != min(size, _MAX_MESSAGE_BYTES):
                        raise ValueError("reply body changed or exceeded the scan limit")
                    request = parse_unsubscribe(raw, recipient=settings.smtp_from, truncated=truncated)
                    if request is None:
                        report.oversized += int(truncated)
                        continue
                    if request != candidate:
                        raise ValueError("reply headers changed during scan")
                    report.recognized += 1
                    outcome = store.apply_unsubscribe_reply(request)
                    if outcome == "applied":
                        report.applied += 1
                    elif outcome == "duplicate":
                        report.duplicates += 1
                    elif outcome == "unmatched":
                        report.unmatched += 1
                    elif outcome == "stale":
                        report.stale += 1
                    else:
                        raise ValueError("unknown unsubscribe outcome")
                cursor = chunk[-1]
                store.save_unsubscribe_cursor(key, validity, cursor)
            if not selected:
                store.save_unsubscribe_cursor(key, validity, cursor)
    except Exception:  # noqa: BLE001 - prevent credentials, addresses and message text escaping.
        raise UnsubscribeSyncUnavailable("구독 해지 메일 조회를 완료하지 못했습니다. 메일 발송을 보류합니다.") from None
    finally:
        if client is not None:
            with suppress(Exception):
                client.logout()
    return report
