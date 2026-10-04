"""Read Gmail delivery reports without changing mailbox flags or message contents."""

import hashlib
import imaplib
import re
import ssl
from contextlib import suppress
from dataclasses import dataclass
from email import policy
from email.parser import BytesHeaderParser

from .mail_bounces import parse_bounces

_PER_FOLDER_LIMIT = 500
_HEADER_CHUNK = 50
_UID = re.compile(rb"(?:^|[\s(])UID\s+([0-9]+)(?=[\s)])", re.IGNORECASE)
_LIST = re.compile(rb'^\(([^)]*)\)\s+(?:"(?:\\.|[^"])*"|NIL)\s+(.+)$')
_DATE = re.compile(r"[0-9]{1,2}-(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)-[0-9]{4}")
_AUTH_METHOD = re.compile(r"^\s*(dkim|spf|dmarc)(?:/[0-9]+)?\s*=\s*([a-z]+)(?=\s|$)", re.IGNORECASE)
_DMARC_FROM = re.compile(r"(?:^|\s)header\.from\s*=\s*([a-z0-9.-]+)(?=\s|$)", re.IGNORECASE)
_MTA_LOCAL = re.compile(r"(?:postmaster|mailer-daemon|MicrosoftExchange[0-9a-f]{32})", re.IGNORECASE)
_CONSUMER_DOMAINS = {
    "gmail.com", "googlemail.com", "outlook.com", "hotmail.com", "live.com", "msn.com",
    "yahoo.com", "ymail.com", "rocketmail.com", "aol.com", "icloud.com", "me.com", "mac.com",
    "gmx.com", "gmx.net", "mail.com", "proton.me", "protonmail.com", "pm.me",
}


class BounceSyncUnavailable(RuntimeError):
    """The mailbox scan was incomplete; mail delivery must wait for another scan."""


@dataclass
class BounceSyncReport:
    scanned: int = 0
    recognized: int = 0
    applied: int = 0
    duplicates: int = 0
    unmatched: int = 0
    untrusted: int = 0
    stale: int = 0
    backlog: bool = False


def _ok(response) -> list:
    status, data = response
    if isinstance(status, bytes):
        status = status.decode("ascii")
    if status.upper() != "OK":
        raise ValueError("incomplete IMAP operation")
    return data or []


def _mailboxes(client) -> list[bytes]:
    all_mail = []
    junk = []
    for item in _ok(client.list()):
        if item is None or item == b"":
            continue
        literal = None
        if isinstance(item, tuple):
            item, literal = item
        if not isinstance(item, bytes):
            raise TypeError("invalid mailbox metadata")
        match = _LIST.fullmatch(item)
        if not match:
            raise ValueError("invalid mailbox metadata")
        flags = {flag.lower() for flag in match[1].split()}
        if b"\\noselect" in flags or not flags.intersection({b"\\all", b"\\junk"}):
            continue
        name = match[2]
        if literal is not None:
            if not isinstance(literal, bytes) or not re.fullmatch(rb"\{[0-9]+\}", name):
                raise ValueError("invalid mailbox metadata")
            name = literal
        elif name.startswith(b'"'):
            if not name.endswith(b'"'):
                raise ValueError("invalid mailbox metadata")
            name = re.sub(rb"\\(.)", rb"\1", name[1:-1])
        if not name or any(byte < 32 or byte == 127 for byte in name):
            raise ValueError("invalid mailbox metadata")
        if b"\\all" in flags:
            all_mail.append(name)
        if b"\\junk" in flags:
            junk.append(name)
    # All Mail contains Inbox. Looking at both would count every report twice.
    return list(dict.fromkeys((all_mail[:1] or [b"INBOX"]) + junk))


def _mailbox_argument(name: bytes) -> bytes:
    return b'"' + name.replace(b"\\", b"\\\\").replace(b'"', b'\\"') + b'"'


def _uidvalidity(client) -> str:
    _, values = client.response("UIDVALIDITY")
    if not values or not isinstance(values[0], bytes) or not values[0].isdigit():
        raise ValueError("missing UIDVALIDITY")
    number = int(values[0])
    if not 0 < number <= 4_294_967_295:
        raise ValueError("invalid UIDVALIDITY")
    return str(number)


def _search(client, since: str, cursor: int) -> list[int]:
    if cursor:
        if cursor >= 4_294_967_295:
            return []
        data = _ok(client.uid("SEARCH", None, "UID", f"{cursor + 1}:*"))
    else:
        data = _ok(client.uid("SEARCH", None, "SINCE", since))
    uids = set()
    for item in data:
        if item is None:
            continue
        if not isinstance(item, bytes):
            raise TypeError("invalid UID search response")
        for token in item.split():
            if not token.isdigit() or not 0 < int(token) <= 4_294_967_295:
                raise ValueError("invalid UID search response")
            uid = int(token)
            # Gmail may reverse a range whose start exceeds the maximum UID.
            if uid > cursor:
                uids.add(uid)
    return sorted(uids)


def _fetch(client, uids: list[int], *, headers: bool) -> dict[int, bytes]:
    query = "(UID BODY.PEEK[HEADER.FIELDS (CONTENT-TYPE AUTHENTICATION-RESULTS)])" if headers else "(UID BODY.PEEK[])"
    data = _ok(client.uid("FETCH", ",".join(str(uid) for uid in uids), query))
    found = {}
    for item in data:
        if not isinstance(item, tuple):
            continue
        if len(item) != 2 or not all(isinstance(value, bytes) for value in item):
            raise ValueError("invalid fetch response")
        metadata, body = item
        matches = _UID.findall(metadata)
        if len(matches) != 1:
            raise ValueError("missing fetch UID")
        uid = int(matches[0])
        if uid not in uids or uid in found:
            raise ValueError("ambiguous fetch UID")
        found[uid] = body
    if set(found) != set(uids):
        raise ValueError("incomplete fetch response")
    return found


def _report_header(raw: bytes) -> bool:
    message = BytesHeaderParser(policy=policy.default).parsebytes(raw)
    return len(message.get_all("Content-Type", [])) == 1 and message.get_content_type() == "multipart/report"


def _auth_tokens(value: str) -> str | None:
    """Discard comments and quoted properties before interpreting auth method tokens."""
    result = []
    comment_depth = 0
    quoted = False
    escaped = False
    for char in value:
        if escaped:
            escaped = False
            continue
        if char == "\\" and (quoted or comment_depth):
            escaped = True
        elif comment_depth:
            if char == "(":
                comment_depth += 1
            elif char == ")":
                comment_depth -= 1
        elif quoted:
            if char == '"':
                quoted = False
        elif char == "(":
            comment_depth = 1
            result.append(" ")
        elif char == '"':
            quoted = True
            result.append(" ")
        elif char == ")":
            return None
        else:
            result.append(char)
    if quoted or comment_depth or escaped:
        return None
    return "".join(result)


def _domain(value: str) -> str | None:
    try:
        value = value.encode("idna").decode("ascii").lower()
    except UnicodeError:
        return None
    if len(value) > 253 or len(value.split(".")) < 2 or any(
        not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label) for label in value.split(".")
    ):
        return None
    return value


def _trusted(raw: bytes, recipient: str) -> bool:
    message = BytesHeaderParser(policy=policy.default).parsebytes(raw)
    senders = message.get_all("From", [])
    if len(senders) != 1 or senders[0].defects or len(senders[0].addresses) != 1:
        return False
    sender = senders[0].addresses[0]
    sender_domain = _domain(sender.domain)
    recipient_domain = _domain(recipient.rsplit("@", 1)[-1])
    if sender_domain is None or recipient_domain is None:
        return False
    if sender_domain in {"gmail.com", "googlemail.com"}:
        if sender.username.casefold() != "mailer-daemon":
            return False
    else:
        if any(sender_domain == domain or sender_domain.endswith("." + domain) for domain in _CONSUMER_DOMAINS):
            return False
        if not _MTA_LOCAL.fullmatch(sender.username) or not (
            sender_domain == recipient_domain or sender_domain.endswith("." + recipient_domain)
        ):
            return False
    values = message.get_all("Authentication-Results", [])
    if not values:
        return False
    authority, separator, results = str(values[0]).partition(";")
    if not separator or authority.strip().lower() != "mx.google.com":
        return False
    results = _auth_tokens(results)
    if results is None:
        return False
    methods = {}
    dmarc_domains = set()
    for result in results.split(";"):
        match = _AUTH_METHOD.match(result)
        if match:
            methods.setdefault(match[1].lower(), set()).add(match[2].lower())
            if match[1].lower() == "dmarc":
                domains = _DMARC_FROM.findall(result)
                if len(domains) != 1:
                    return False
                dmarc_domains.add(_domain(domains[0]))
    # Conflicting DMARC outcomes are ambiguous even if one says "pass".
    return dmarc_domains == {sender_domain} and methods.get("dmarc") == {"pass"} and (
        "pass" in methods.get("dkim", set()) or "pass" in methods.get("spf", set())
    )


def sync_mail_bounces(settings, store, *, client_factory=None) -> BounceSyncReport:
    """Read bounded Gmail DSN batches; checkpoint only fully processed UID chunks."""
    report = BounceSyncReport()
    if not settings.mailing_bounce_enabled:
        return report
    client = None
    try:
        settings.require_bounces()
        since = store.bounce_scan_since()
        if not isinstance(since, str) or not _DATE.fullmatch(since):
            raise ValueError("invalid bounce scan date")
        factory = client_factory or imaplib.IMAP4_SSL
        client = factory(settings.imap_host, settings.imap_port, ssl_context=ssl.create_default_context(), timeout=30)
        _ok(client.login(settings.smtp_username, settings.smtp_password))
        for mailbox in _mailboxes(client):
            _ok(client.select(_mailbox_argument(mailbox), readonly=True))
            validity = _uidvalidity(client)
            key = hashlib.sha256(
                settings.imap_host.encode() + b"\0" + settings.smtp_username.encode() + b"\0" + mailbox
            ).hexdigest()
            previous = store.bounce_cursor(key)
            cursor = int(previous[1]) if previous and previous[0] == validity else 0
            if cursor < 0:
                raise ValueError("invalid saved cursor")
            uids = _search(client, since, cursor)
            report.backlog |= len(uids) > _PER_FOLDER_LIMIT
            selected = uids[:_PER_FOLDER_LIMIT]
            for offset in range(0, len(selected), _HEADER_CHUNK):
                chunk = selected[offset:offset + _HEADER_CHUNK]
                headers = _fetch(client, chunk, headers=True)
                report.scanned += len(chunk)
                candidates = [uid for uid in chunk if _report_header(headers[uid])]
                for uid in candidates:
                    # One full body at a time bounds memory even for large attachments.
                    raw = _fetch(client, [uid], headers=False)[uid]
                    notices = parse_bounces(raw)
                    report.recognized += len(notices)
                    matched = []
                    for notice in notices:
                        if store.bounce_matches(notice):
                            matched.append(notice)
                        else:
                            report.unmatched += 1
                    for notice in matched:
                        if not _trusted(raw, notice.recipient):
                            report.untrusted += 1
                            continue
                        outcome = store.apply_bounce(notice)
                        if outcome == "applied":
                            report.applied += 1
                        elif outcome == "duplicate":
                            report.duplicates += 1
                        elif outcome == "unmatched":
                            report.unmatched += 1
                        elif outcome == "stale":
                            report.stale += 1
                        else:
                            raise ValueError("unknown bounce outcome")
                cursor = chunk[-1]
                store.save_bounce_cursor(key, validity, cursor)
            if not selected:
                store.save_bounce_cursor(key, validity, cursor)
        store.release_expired_bounce_holds()
    except Exception:  # noqa: BLE001 - this privacy boundary must hide arbitrary server/store details.
        # Server/library exceptions may contain credentials, addresses or MIME data.
        raise BounceSyncUnavailable("반송 메일 조회를 완료하지 못했습니다. 메일 발송을 보류합니다.") from None
    finally:
        if client is not None:
            # Logout failure must not expose server text or undo verified results.
            with suppress(Exception):
                client.logout()
    return report
