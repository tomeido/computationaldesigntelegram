"""Recognize authenticated, explicit unsubscribe replies without mailbox access.

The caller must match a reference and the exact sender to a previously attempted
newsletter delivery before applying the request. Subject/body Message-IDs and
addresses mentioned in the message never establish that association.
"""

import re
from dataclasses import dataclass
from email import policy
from email.errors import CloseBoundaryNotFoundDefect, MessageError
from email.message import Message
from email.parser import BytesHeaderParser, BytesParser
from html.parser import HTMLParser

from .mail_bounce_sync import _AUTH_METHOD, _DMARC_FROM, _auth_tokens, _domain
from .mail_bounces import _MESSAGE_ID, _header, _recipient

_MAX_MESSAGE = 512 * 1024
_MAX_HEADERS = 64 * 1024
_MAX_TEXT = 64 * 1024
_MAX_PARTS = 80
_MAX_DEPTH = 12
_AUTOMATED = {
    "list-id", "list-unsubscribe", "list-post", "mailing-list", "x-autoreply", "x-autorespond",
    "x-auto-response-suppress", "x-forwarded-to", "x-forwarded-for", "resent-from", "resent-to",
    "resent-date", "resent-sender",
}
_SUBJECT = re.compile(
    r"^(?:(?:re|aw|답장)\s*:\s*)*(?:fwd?\s*:|automatic reply\b|auto[ -]?reply\b|out of office\b|"
    r"자동\s*응답|부재중)", re.IGNORECASE,
)
_QUOTE_LINE = re.compile(
    r"^(?:[>|]|on\b.{0,500}\bwrote\s*:|.{0,300}님이\s*작성\s*:|"
    r"[-_]{2,}\s*(?:original|forwarded) message|begin forwarded message\s*:|"
    r"from\s*:|보낸\s*사람\s*:|sent from my\b)", re.IGNORECASE,
)
_COMMAND = re.compile(
    r"(?:stop|(?:please\s+)?unsubscribe(?:\s+me)?"
    r"(?:\s+from\s+(?:your|the|this)\s+(?:mailing|email)\s+list)?|"
    r"(?:please\s+)?remove\s+me\s+from\s+(?:your|the|this)\s+(?:mailing|email)\s+list|"
    r"(?:please\s+)?stop\s+(?:emailing\s+me|sending\s+me\s+(?:these\s+)?emails?)|"
    r"(?:구독\s*(?:해지|취소)|수신\s*거부)(?:\s*(?:신청|요청)(?:합니다|드립니다)?|"
    r"\s*(?:합니다|해\s*주세요|부탁(?:드립니다|합니다)))?|(?:이메일|메일)\s*그만\s*보내\s*주세요)"
    r"(?:[.!。！]*[,;]?\s+(?:please|thanks|thank\s+you|감사합니다))?[.!。！]*",
    re.IGNORECASE,
)
_GREETING = re.compile(r"(?:hi|hello|안녕하세요)[,!！.]?", re.IGNORECASE)
_NEGATIVE = re.compile(
    r"\b(?:do\s+not|don['’]t|never|not\s+to)\s+(?:want\s+to\s+)?(?:unsubscribe|remove\s+me)\b|"
    r"(?:해지|취소|거부)\s*하지\s*(?:마|말)", re.IGNORECASE,
)


@dataclass(frozen=True)
class UnsubscribeRequest:
    message_id: str
    email: str
    references: tuple[str, ...]


def _authenticated(message: Message, sender_domain: str) -> bool:
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
    domains = set()
    for result in results.split(";"):
        match = _AUTH_METHOD.match(result)
        if not match:
            continue
        method, outcome = match[1].lower(), match[2].lower()
        methods.setdefault(method, set()).add(outcome)
        if method == "dmarc":
            reported = _DMARC_FROM.findall(result)
            if len(reported) != 1:
                return False
            domains.add(_domain(reported[0]))
    return domains == {sender_domain} and methods.get("dmarc") == {"pass"} and (
        "pass" in methods.get("dkim", set()) or "pass" in methods.get("spf", set())
    )


def _identities(message: Message, recipient: str) -> UnsubscribeRequest | None:
    if message.defects:
        return None
    service = _recipient("rfc822; " + recipient)
    senders = message.get_all("From", [])
    targets = message.get_all("To", [])
    if (service is None or len(senders) != 1 or senders[0].defects or len(senders[0].addresses) != 1
            or any(group.display_name is not None for group in senders[0].groups)
            or (_header(message, "From") or "").endswith(",")
            or len(targets) != 1 or targets[0].defects):
        return None
    sender = _recipient("rfc822; " + senders[0].addresses[0].addr_spec)
    recipients = {_recipient("rfc822; " + address.addr_spec) for address in targets[0].addresses}
    if sender is None or None in recipients or service not in recipients:
        return None
    if any(name.lower() in _AUTOMATED for name in message):
        return None
    auto = message.get_all("Auto-Submitted", [])
    precedence = message.get_all("Precedence", [])
    if (auto and (len(auto) != 1 or str(auto[0]).strip().lower() != "no")
            or len(precedence) > 1 or precedence and str(precedence[0]).strip().lower() in {
                "bulk", "list", "junk", "auto_reply", "auto-reply",
            }):
        return None
    subjects = message.get_all("Subject", [])
    if len(subjects) > 1 or subjects and _SUBJECT.match(str(subjects[0]).strip()):
        return None
    if (len(message.get_all("Content-Type", [])) > 1
            or message.get_content_type() in {"multipart/report", "message/rfc822", "message/delivery-status"}
            or message.get_content_disposition() == "attachment"):
        return None
    message_id = _header(message, "Message-ID")
    if message_id is None or len(message_id) > 998 or not _MESSAGE_ID.fullmatch(message_id):
        return None
    references = []
    for name in ("In-Reply-To", "References"):
        if not message.get_all(name, []):
            continue
        value = _header(message, name)
        if value is None or len(value) > 8192:
            return None
        ids = _MESSAGE_ID.findall(value)
        if not ids or len(ids) > 50 or _MESSAGE_ID.sub("", value).strip():
            return None
        references.extend(ids)
    if not references or not _authenticated(message, sender.rsplit("@", 1)[1]):
        return None
    return UnsubscribeRequest(message_id, sender, tuple(dict.fromkeys(references)))


def reply_headers(raw: bytes, *, recipient: str) -> UnsubscribeRequest | None:
    """Return a trusted reply candidate; this does not inspect unsubscribe intent."""
    if not isinstance(raw, bytes) or not raw or not isinstance(recipient, str):
        return None
    boundaries = [at for at in (raw.find(b"\r\n\r\n"), raw.find(b"\n\n")) if at >= 0]
    headers = raw[:min(boundaries)] if boundaries else raw
    if len(headers) > _MAX_HEADERS:
        return None
    try:
        return _identities(BytesHeaderParser(policy=policy.default).parsebytes(headers), recipient)
    except (MessageError, ValueError, TypeError, UnicodeError, RecursionError, AttributeError):
        return None


class _AuthoredHTML(HTMLParser):
    """Extract visible author text while dropping reply quotes and signatures."""

    _BLOCKS = frozenset({"p", "div", "br", "li", "tr", "h1", "h2", "h3", "hr", "table", "section"})
    _VOID = frozenset({"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "wbr"})

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack = []
        self.output = []
        self.stopped = False

    def handle_starttag(self, tag, attrs):
        attributes = {key.lower(): (value or "").lower() for key, value in attrs}
        markers = " ".join(attributes.get(key, "") for key in ("class", "id", "data-smartmail"))
        # Outlook puts the quoted body *after* this header div, outside it.
        if any(marker in markers for marker in ("divrplyfwdmsg", "outlookmessageheader", "gmail_attr")):
            self.stopped = True
        hidden = bool(self.stack and self.stack[-1][1]) or tag in {
            "blockquote", "script", "style", "head", "template", "noscript", "footer",
        } or any(marker in markers for marker in (
            "gmail_quote", "yahoo_quoted", "moz-cite-prefix", "gmail_signature", "moz-signature",
        )) or attributes.get("id") == "signature" or "hidden" in attributes or bool(
            re.search(r"(?:display\s*:\s*none|visibility\s*:\s*hidden)", attributes.get("style", ""))
        )
        if not self.stopped and not hidden and tag in self._BLOCKS:
            self.output.append("\n")
        if tag not in self._VOID:
            self.stack.append((tag, hidden))
            if len(self.stack) > 100:
                raise ValueError("excessive HTML nesting")

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in self._VOID:
            self.handle_endtag(tag)

    def handle_endtag(self, tag):
        if not self.stopped and not (self.stack and self.stack[-1][1]) and tag in self._BLOCKS:
            self.output.append("\n")
        for index in range(len(self.stack) - 1, -1, -1):
            if self.stack[index][0] == tag:
                del self.stack[index:]
                break

    def handle_data(self, data):
        if not self.stopped and not (self.stack and self.stack[-1][1]):
            self.output.append(data)


def _body(message: Message, *, truncated: bool = False) -> tuple[str, str] | None:
    # A following sibling boundary proves that the preceding part is complete,
    # even when the mailbox prefix ends in a later, large attachment. Never
    # infer that the final body part in a truncated container is complete.
    pending = [(message, 0, not truncated)]
    completeness = {}
    count = 0
    while pending:
        part, depth, complete = pending.pop()
        completeness[id(part)] = complete
        count += 1
        malformed = any(
            not (not complete and part.get_content_maintype() == "multipart"
                 and isinstance(defect, CloseBoundaryNotFoundDefect))
            for defect in part.defects
        )
        if count > _MAX_PARTS or depth > _MAX_DEPTH or malformed:
            raise ValueError("invalid MIME structure")
        for name in ("Content-Type", "Content-Disposition", "Content-Transfer-Encoding"):
            values = part.get_all(name, [])
            if len(values) > 1 or values and getattr(values[0], "defects", ()):
                raise ValueError("ambiguous MIME header")
        if part.get_content_maintype() == "multipart":
            payload = part.get_payload()
            if not isinstance(payload, list):
                raise ValueError("invalid multipart payload")
            pending.extend((child, depth + 1, complete or index < len(payload) - 1)
                           for index, child in enumerate(payload))

    def select(part):
        if part.get_content_disposition() == "attachment" or part.get_filename():
            return None
        kind = part.get_content_type()
        if kind in {"multipart/mixed", "multipart/related", "multipart/alternative"}:
            children = part.get_payload()
            candidates = [select(child) for child in children]
            candidates = [candidate for candidate in candidates if candidate is not None]
            if kind == "multipart/alternative":
                return next((candidate for candidate in candidates if candidate[0] == "text/plain"),
                            candidates[0] if candidates else None)
            return candidates[0] if candidates else None
        if kind not in {"text/plain", "text/html"}:
            return None
        if not completeness[id(part)]:
            # Keep this part in normal body selection so an incomplete preferred
            # plaintext alternative cannot fall back to contradictory HTML.
            return kind, None
        if (_header(part, "Content-Transfer-Encoding") or "7bit").lower() not in {
            "7bit", "8bit", "binary", "base64", "quoted-printable",
        }:
            return None
        payload = part.get_payload(decode=True)
        if not isinstance(payload, bytes) or len(payload) > _MAX_TEXT or part.defects:
            return None
        charset = part.get_content_charset() or "utf-8"
        text = payload.decode(charset, errors="strict")
        return (kind, text) if text.strip() else None

    selected = select(message)
    return selected if selected is not None and selected[1] is not None else None


def _explicit_command(body: str) -> bool:
    authored = []
    for line in body.splitlines():
        line = line.strip().replace("\u00a0", " ")
        if not line:
            continue
        if _QUOTE_LINE.match(line) or line == "--":
            break
        authored.append(line)
    if authored and _GREETING.fullmatch(authored[0]):
        authored.pop(0)
    if not authored or _NEGATIVE.search(" ".join(authored)):
        return False
    return len(authored[0]) <= 250 and _COMMAND.fullmatch(authored[0]) is not None


def parse_unsubscribe(raw: bytes, *, recipient: str, truncated: bool = False) -> UnsubscribeRequest | None:
    """Recognize a command only in complete authored text, including MIME prefixes."""
    if not isinstance(raw, bytes) or len(raw) > _MAX_MESSAGE:
        return None
    candidate = reply_headers(raw, recipient=recipient)
    if candidate is None:
        return None
    try:
        body = _body(BytesParser(policy=policy.default).parsebytes(raw), truncated=truncated)
        if body is None:
            return None
        kind, text = body
        if kind == "text/html":
            parser = _AuthoredHTML()
            parser.feed(text)
            parser.close()
            text = "".join(parser.output)
        return candidate if _explicit_command(text) else None
    except (MessageError, ValueError, TypeError, UnicodeError, RecursionError, LookupError):
        return None
