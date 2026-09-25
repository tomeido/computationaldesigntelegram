"""Private mailing-list membership, XLSX imports, and durable SMTP delivery."""

import hashlib
import posixpath
import re
import secrets
import smtplib
import sqlite3
import ssl
from dataclasses import dataclass
from datetime import UTC, datetime
from email.message import EmailMessage
from email.utils import formatdate, parseaddr
from html import escape
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import unquote, urlsplit
from xml.etree import ElementTree as ET
from zipfile import BadZipFile, ZipFile

_XML = {"s": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
_REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
_EMAIL = re.compile(r"[^\s,;<>()\"=]+@[^\s,;<>()\"=]+")
_LOCAL = re.compile(r"[a-z0-9!#$%&'*+/=?^_`{|}~.-]+", re.IGNORECASE)


def normalize_email(value: str) -> str:
    """Normalize ordinary SMTP mailboxes; reject malformed and injected headers."""
    value = value.strip()
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise ValueError("올바른 이메일 주소를 입력하세요.")
    if value.lower().startswith("mailto:"):
        value = unquote(value[7:].split("?", 1)[0])
    if "<" in value:
        _, value = parseaddr(value)
    if value.count("@") != 1:
        raise ValueError("올바른 이메일 주소를 입력하세요.")
    local, domain = value.rsplit("@", 1)
    try:
        domain = domain.encode("idna").decode("ascii").lower()
    except UnicodeError:
        raise ValueError("올바른 이메일 주소를 입력하세요.") from None
    labels = domain.split(".")
    if (
        not _LOCAL.fullmatch(local) or len(local) > 64 or local.startswith(".")
        or local.endswith(".") or ".." in local or len(labels) < 2
        or any(not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label) for label in labels)
        or len(local) + len(domain) + 1 > 254
    ):
        raise ValueError("올바른 이메일 주소를 입력하세요.")
    return f"{local.casefold()}@{domain}"


validate_email = normalize_email


@dataclass
class ImportResult:
    imported: int = 0
    duplicates: int = 0
    invalid: int = 0
    suppressed: int = 0
    excluded: int = 0


@dataclass
class DeliveryReport:
    sent: int = 0
    failed: int = 0
    uncertain: int = 0
    skipped: int = 0


def _email_candidates(value: str) -> list[str]:
    value = re.sub(r"mailto:([^\s<>]+)", lambda match: unquote(match[1].split("?", 1)[0]),
                   value, flags=re.IGNORECASE)
    return [candidate.rstrip(".!?:") for candidate in _EMAIL.findall(value)]


def _sheet_rows(archive: ZipFile, path: str, strings: list[str]) -> list[dict[str, str]]:
    root = ET.fromstring(archive.read(path))
    links = {}
    relpath = posixpath.join(posixpath.dirname(path), "_rels", posixpath.basename(path) + ".rels")
    if relpath in archive.namelist():
        rels = {
            element.get("Id"): element.get("Target", "")
            for element in ET.fromstring(archive.read(relpath))
        }
        for link in root.findall("s:hyperlinks/s:hyperlink", _XML):
            target = rels.get(link.get(f"{{{_REL}}}id"), "")
            if target.lower().startswith("mailto:"):
                links[link.get("ref")] = unquote(target[7:].split("?", 1)[0])
    rows = []
    for row in root.findall("s:sheetData/s:row", _XML):
        cells = {}
        for cell in row.findall("s:c", _XML):
            reference = cell.get("r", "")
            column = re.sub(r"\d", "", reference)
            value = cell.find("s:v", _XML)
            text = value.text or "" if value is not None else ""
            if cell.get("t") == "s":
                try:
                    text = strings[int(text)]
                except (ValueError, IndexError):
                    raise ValueError("XLSX 공유 문자열이 손상되었습니다.") from None
            elif cell.get("t") == "inlineStr":
                text = "".join(element.text or "" for element in cell.findall("s:is//s:t", _XML))
            if reference in links:
                # The hyperlink is the mailbox when the visible cell is a name.
                text = text if "@" in text else links[reference]
            cells[column] = text
        rows.append(cells)
    return rows


def _xlsx_addresses(path: Path, *, sheet_name: str | None = None, include_review: bool = False):
    """Read email columns, preferring a workbook's explicit delivery review sheet."""
    try:
        with ZipFile(path) as archive:
            if sum(entry.file_size for entry in archive.infolist()) > 40_000_000:
                raise ValueError("XLSX 압축 해제 크기는 40MB 이하여야 합니다.")
            strings = []
            if "xl/sharedStrings.xml" in archive.namelist():
                strings = [
                    "".join(t.text or "" for t in item.findall(".//s:t", _XML))
                    for item in ET.fromstring(archive.read("xl/sharedStrings.xml")).findall("s:si", _XML)
                ]
            relationships = {
                item.get("Id"): item.get("Target", "")
                for item in ET.fromstring(archive.read("xl/_rels/workbook.xml.rels"))
            }
            sheets = ET.fromstring(archive.read("xl/workbook.xml")).findall("s:sheets/s:sheet", _XML)
            names = {sheet.get("name") for sheet in sheets}
            preferred = sheet_name or ("발송 점검" if "발송 점검" in names else "연락처" if "연락처" in names else None)
            if preferred and preferred not in names:
                raise ValueError("요청한 XLSX 시트를 찾을 수 없습니다.")
            for sheet in sheets:
                if preferred and sheet.get("name") != preferred:
                    continue
                target = relationships.get(sheet.get(f"{{{_REL}}}id"), "")
                sheetpath = target.lstrip("/") if target.startswith("/") else posixpath.normpath("xl/" + target)
                rows = _sheet_rows(archive, sheetpath, strings)
                email_columns = None
                control_columns = []
                first_data = 0
                for index, row in enumerate(rows[:20]):
                    matches = [
                        column for column, value in row.items()
                        if "@" not in value and (
                            value.strip().lower() in {"email", "e-mail", "email address", "이메일", "대표 이메일",
                                                      "메일", "메일 주소", "이메일 주소", "기존·추가 주소"}
                        )
                    ]
                    if matches:
                        email_columns = matches
                        control_columns = [
                            column for column, value in row.items()
                            if value.strip().lower() in {"사용 판단", "발송 여부", "수신 여부", "상태", "status", "send"}
                        ]
                        first_data = index + 1
                        break
                for row in rows[first_data:]:
                    values = [row.get(column, "") for column in email_columns] if email_columns else row.values()
                    controls = {row.get(column, "").strip().lower() for column in control_columns}
                    excluded = bool(controls & {
                        "제외", "수신거부", "수신 거부", "발송 제외", "발송 금지", "아니오", "no", "false",
                        "0", "exclude", "excluded", "unsubscribed", "do not send",
                    }) or (not include_review and "추가 확인" in controls)
                    for value in values:
                        for candidate in _email_candidates(value):
                            yield candidate, excluded
    except (BadZipFile, KeyError, ET.ParseError):
        raise ValueError("올바른 XLSX 파일이 아닙니다.") from None


class MailingStore:
    def __init__(self, path: Path):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, timeout=10)
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS mailing_subscribers (
                email TEXT PRIMARY KEY, token TEXT NOT NULL UNIQUE,
                telegram_user_id INTEGER UNIQUE, active INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS mailing_awaiting (user_id INTEGER PRIMARY KEY);
            CREATE TABLE IF NOT EXISTS mailing_posts (
                post_key TEXT PRIMARY KEY, title TEXT NOT NULL, telegram_html TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS mailing_outbox (
                id INTEGER PRIMARY KEY, post_key TEXT NOT NULL, email TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'queued', attempts INTEGER NOT NULL DEFAULT 0,
                error TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL,
                UNIQUE(post_key, email)
            );
        """)

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.close()

    def close(self):
        self.db.close()

    def subscribe(self, email: str, telegram_user_id: int | None = None) -> str:
        email = normalize_email(email)
        if telegram_user_id is not None and (isinstance(telegram_user_id, bool) or telegram_user_id <= 0):
            raise ValueError("개인 텔레그램 사용자 ID가 필요합니다.")
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            current = self.db.execute("SELECT * FROM mailing_subscribers WHERE email=?", (email,)).fetchone()
            if current and current["telegram_user_id"] not in (None, telegram_user_id):
                return "email_in_use"
            if current and not current["active"] and current["telegram_user_id"] is None:
                # Imported/previously detached mailboxes require verified ownership to reactivate.
                return "email_in_use"
            if current and current["active"]:
                # Knowing an imported mailbox is not proof of ownership.
                return "already_subscribed"
            if telegram_user_id is not None:
                self.db.execute(
                    "UPDATE mailing_outbox SET status='suppressed' WHERE status IN ('queued','failed') "
                    "AND email IN (SELECT email FROM mailing_subscribers WHERE telegram_user_id=? AND email!=?)",
                    (telegram_user_id, email),
                )
                self.db.execute(
                    "UPDATE mailing_subscribers SET active=0, telegram_user_id=NULL "
                    "WHERE telegram_user_id=? AND email!=?", (telegram_user_id, email),
                )
            if current:
                self.db.execute(
                    "UPDATE mailing_subscribers SET active=1, telegram_user_id=?, created_at=? WHERE email=?",
                    (telegram_user_id, datetime.now(UTC).isoformat(), email),
                )
            else:
                self.db.execute(
                    "INSERT INTO mailing_subscribers(email,token,telegram_user_id,created_at) VALUES(?,?,?,?)",
                    (email, secrets.token_urlsafe(24), telegram_user_id, datetime.now(UTC).isoformat()),
                )
        return "subscribed"

    def unsubscribe_user(self, user_id: int) -> int:
        with self.db:
            self.db.execute(
                "UPDATE mailing_outbox SET status='suppressed' WHERE status IN ('queued','failed') "
                "AND email IN (SELECT email FROM mailing_subscribers WHERE telegram_user_id=?)", (user_id,),
            )
            count = self.db.execute(
                "UPDATE mailing_subscribers SET active=0 WHERE telegram_user_id=? AND active=1", (user_id,),
            ).rowcount
            self.db.execute("DELETE FROM mailing_awaiting WHERE user_id=?", (user_id,))
        return count

    def unsubscribe(self, telegram_user_id: int) -> bool:
        return bool(self.unsubscribe_user(telegram_user_id))

    def unsubscribe_token(self, token: str) -> bool:
        with self.db:
            self.db.execute(
                "UPDATE mailing_outbox SET status='suppressed' WHERE status IN ('queued','failed') "
                "AND email IN (SELECT email FROM mailing_subscribers WHERE token=?)", (token,),
            )
            return bool(self.db.execute(
                "UPDATE mailing_subscribers SET active=0 WHERE token=?", (token,),
            ).rowcount)

    def set_awaiting_email(self, user_id: int, awaiting: bool):
        with self.db:
            if awaiting:
                self.db.execute("INSERT OR IGNORE INTO mailing_awaiting VALUES(?)", (user_id,))
            else:
                self.db.execute("DELETE FROM mailing_awaiting WHERE user_id=?", (user_id,))

    def awaiting_email(self, user_id: int) -> bool:
        return self.db.execute("SELECT 1 FROM mailing_awaiting WHERE user_id=?", (user_id,)).fetchone() is not None

    def import_xlsx(self, path: Path, *, sheet_name: str | None = None, include_review: bool = False) -> ImportResult:
        result = ImportResult()
        # Parse before mutating so malformed later sheets cannot leave a partial import.
        addresses = list(_xlsx_addresses(Path(path), sheet_name=sheet_name, include_review=include_review))
        with self.db:
            for candidate, excluded in addresses:
                if excluded:
                    result.excluded += 1
                    continue
                try:
                    email = normalize_email(candidate)
                except ValueError:
                    result.invalid += 1
                    continue
                row = self.db.execute("SELECT active FROM mailing_subscribers WHERE email=?", (email,)).fetchone()
                if row:
                    if row["active"]:
                        result.duplicates += 1
                    else:
                        result.suppressed += 1
                    continue
                self.db.execute(
                    "INSERT INTO mailing_subscribers(email,token,created_at) VALUES(?,?,?)",
                    (email, secrets.token_urlsafe(24), datetime.now(UTC).isoformat()),
                )
                result.imported += 1
        return result

    def status_counts(self) -> dict[str, int]:
        counts = dict(self.db.execute("SELECT active,COUNT(*) FROM mailing_subscribers GROUP BY active"))
        return {"active": counts.get(1, 0), "unsubscribed": counts.get(0, 0)}

    def outbox_counts(self) -> dict[str, int]:
        return dict(self.db.execute("SELECT status,COUNT(*) FROM mailing_outbox GROUP BY status"))

    def store_post(self, post_key: str, title: str, telegram_html: str, *, published_at: str | None = None):
        published_at = published_at or datetime.now(UTC).isoformat()
        with self.db:
            added = self.db.execute(
                "INSERT OR IGNORE INTO mailing_posts VALUES(?,?,?,?)",
                (str(post_key), title, telegram_html, published_at),
            ).rowcount
            if added:
                self.db.execute(
                    "INSERT INTO mailing_outbox(post_key,email,created_at) "
                    "SELECT ?,email,? FROM mailing_subscribers WHERE active=1 AND created_at<=?",
                    (str(post_key), datetime.now(UTC).isoformat(), published_at),
                )

    def unresolved(self) -> list[dict]:
        # Never expose addresses or bearer unsubscribe tokens in operational output.
        return [dict(row) for row in self.db.execute(
            "SELECT id,post_key,status,attempts,error FROM mailing_outbox "
            "WHERE status IN ('pending','uncertain','failed') ORDER BY id"
        )]

    def resolve(self, delivery_id: int, *, retry: bool):
        with self.db:
            updated = self.db.execute(
                "UPDATE mailing_outbox SET status=?,error='' "
                "WHERE id=? AND status IN ('pending','uncertain','failed')",
                ("queued" if retry else "sent", delivery_id),
            ).rowcount
            if not updated:
                raise ValueError("해당 ID의 미확인 이메일 전송 기록이 없습니다.")

    def claim(self, delivery_id: int) -> bool:
        with self.db:
            return bool(self.db.execute(
                "UPDATE mailing_outbox SET status='pending',attempts=attempts+1,error='' "
                "WHERE id=? AND status='queued' "
                "AND EXISTS (SELECT 1 FROM mailing_subscribers s WHERE s.email=mailing_outbox.email AND s.active=1)",
                (delivery_id,),
            ).rowcount)

    def finish(self, delivery_id: int, status: str, error: str = ""):
        if status not in {"sent", "failed", "uncertain", "suppressed"}:
            raise ValueError("올바르지 않은 이메일 전송 상태입니다.")
        with self.db:
            self.db.execute("UPDATE mailing_outbox SET status=?,error=? WHERE id=?", (status, error, delivery_id))


class _PostContent(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.html = []
        self.plain = []
        self.href = ""

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            href = dict(attrs).get("href", "")
            try:
                parts = urlsplit(href)
                valid = parts.scheme in {"http", "https"} and parts.hostname and not parts.username
            except ValueError:
                valid = False
            self.href = href if valid else ""
            if self.href:
                self.html.append(f'<a href="{escape(self.href, quote=True)}">')
        elif tag in {"b", "strong", "i", "em", "u", "s", "code", "pre"}:
            self.html.append(f"<{tag}>")
        elif tag == "br":
            self.html.append("<br>")
            self.plain.append("\n")

    def handle_endtag(self, tag):
        if tag == "a" and self.href:
            self.html.append("</a>")
            self.plain.append(f" ({self.href})")
            self.href = ""
        elif tag in {"b", "strong", "i", "em", "u", "s", "code", "pre"}:
            self.html.append(f"</{tag}>")

    def handle_data(self, data):
        self.html.append(escape(data).replace("\n", "<br>\n"))
        self.plain.append(data)


def build_email(*, sender: str, recipient: str, title: str, telegram_html: str,
                invite_url: str, unsubscribe_url: str, message_id: str = "") -> EmailMessage:
    normalize_email(sender)
    recipient = normalize_email(recipient)
    content = _PostContent()
    content.feed(telegram_html)
    plain = "".join(content.plain)
    html = "".join(content.html)
    if invite_url:
        plain += f"\n\n텔레그램 방 참여: {invite_url}"
        html += f'<br><br><a href="{escape(invite_url, quote=True)}">텔레그램 방 참여</a>'
    plain += f"\n\n메일 수신 해지: {unsubscribe_url}\n링크를 열고 봇의 안내에 따라 /unsubscribe를 보내면 해지됩니다."
    html += (f'<br><br><a href="{escape(unsubscribe_url, quote=True)}">메일 수신 해지</a>'
             '<br>링크를 열고 봇의 안내에 따라 /unsubscribe를 보내면 해지됩니다.')
    message = EmailMessage()
    message["From"] = sender
    message["To"] = recipient
    message["Subject"] = " ".join(title.splitlines())[:200]
    message["Date"] = formatdate(localtime=False)
    if message_id:
        message["Message-ID"] = message_id
    # A bot confirmation flow is not an RFC 8058 one-click unsubscribe endpoint.
    message["List-Unsubscribe"] = f"<{unsubscribe_url}>"
    message.set_content(plain)
    message.add_alternative(
        '<!doctype html><html lang="ko"><body style="font-family:sans-serif;line-height:1.7">'
        + html + "</body></html>", subtype="html",
    )
    return message


class EmailDeliveryError(RuntimeError):
    """The SMTP server definitely did not accept this message."""


class EmailDeliveryUncertain(RuntimeError):
    """The SMTP server may have accepted this message; require operator resolution."""


class EmailConnectionError(EmailDeliveryError):
    """A batch-wide SMTP configuration or connection failure; stop this batch."""


class SMTPMailer:
    def __init__(self, settings):
        self.settings = settings

    def send(self, message: EmailMessage):
        settings = self.settings
        connection = None
        sending = False
        try:
            context = ssl.create_default_context()
            if settings.smtp_security == "ssl":
                connection = smtplib.SMTP_SSL(settings.smtp_host, settings.smtp_port, timeout=30, context=context)
            elif settings.smtp_security == "starttls":
                connection = smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=30)
                connection.ehlo()
                connection.starttls(context=context)
                connection.ehlo()
            else:
                raise EmailDeliveryError("SMTP 보안 설정이 올바르지 않습니다.")
            if settings.smtp_username:
                connection.login(settings.smtp_username, settings.smtp_password)
            sending = True
            refused = connection.send_message(
                message, from_addr=normalize_email(str(message["From"])), to_addrs=[str(message["To"])],
            )
            if refused:
                raise EmailDeliveryError("SMTP 서버가 수신자를 거절했습니다.")
        except (smtplib.SMTPRecipientsRefused, smtplib.SMTPSenderRefused, smtplib.SMTPDataError):
            raise EmailDeliveryError("SMTP 서버가 이메일을 거절했습니다.") from None
        except (smtplib.SMTPException, OSError):
            if sending:
                raise EmailDeliveryUncertain("SMTP 전송 결과를 확인할 수 없습니다.") from None
            raise EmailConnectionError("SMTP 연결 또는 인증에 실패했습니다.") from None
        finally:
            if connection:
                try:
                    connection.quit()
                except (smtplib.SMTPException, OSError):
                    connection.close()


def deliver_pending(settings, store: MailingStore, *, bot_username: str = "", sender=None) -> DeliveryReport:
    """Attempt each queued recipient once. Interrupted/uncertain attempts stay reserved."""
    username = (bot_username or settings.bot_username).lstrip("@")
    if not re.fullmatch(r"[A-Za-z0-9_]{5,32}", username):
        raise ValueError("수신 해지 링크에 사용할 텔레그램 봇 사용자명이 필요합니다.")
    settings.require_mail()
    send = sender or SMTPMailer(settings).send
    report = DeliveryReport()
    pending = store.db.execute(
        "SELECT o.id,o.post_key,o.email,s.token,s.active,p.title,p.telegram_html "
        "FROM mailing_outbox o JOIN mailing_subscribers s ON s.email=o.email "
        "JOIN mailing_posts p ON p.post_key=o.post_key WHERE o.status='queued' ORDER BY o.id"
    ).fetchall()
    for row in pending:
        if not row["active"]:
            store.finish(row["id"], "suppressed")
            report.skipped += 1
            continue
        unsubscribe_url = f"https://t.me/{username}?start=unsubscribe_{row['token']}"
        digest = hashlib.sha256(f"{row['post_key']}\0{row['email']}".encode()).hexdigest()
        domain = normalize_email(settings.smtp_from).rsplit("@", 1)[1]
        message = build_email(
            sender=settings.smtp_from, recipient=row["email"], title=row["title"],
            telegram_html=row["telegram_html"], invite_url=settings.telegram_invite_url,
            unsubscribe_url=unsubscribe_url, message_id=f"<{digest}@{domain}>",
        )
        if not store.claim(row["id"]):
            report.skipped += 1
            continue
        try:
            send(message)
        except EmailConnectionError:
            store.finish(row["id"], "failed", "smtp_connection")
            report.failed += 1
            break
        except EmailDeliveryError:
            store.finish(row["id"], "failed", "smtp_rejected")
            report.failed += 1
        except (EmailDeliveryUncertain, OSError, smtplib.SMTPException):
            # A disconnect during DATA may happen after the server accepted the message.
            store.finish(row["id"], "uncertain", "delivery_uncertain")
            report.uncertain += 1
        else:
            store.finish(row["id"], "sent")
            report.sent += 1
    return report
