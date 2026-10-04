"""Private mailing-list membership, XLSX imports, and durable SMTP delivery."""

import hashlib
import posixpath
import re
import secrets
import smtplib
import sqlite3
import ssl
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from email.message import EmailMessage
from email.utils import formatdate, parseaddr
from html import escape
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import unquote, urlsplit
from xml.etree import ElementTree as ET
from zipfile import BadZipFile, ZipFile

from .errors import SummaryError
from .mail_localization import MailLocalizationUnavailable, MailLocalizer, language_copy, normalize_language

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
    deferred: int = 0
    paused: bool = False
    localization_failed: int = 0


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
                language_columns = []
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
                        language_columns = [
                            column for column, value in row.items()
                            if value.strip().lower() in {
                                "수신 언어", "메일 언어", "language", "language code", "email language",
                            }
                        ]
                        first_data = index + 1
                        break
                for row in rows[first_data:]:
                    values = [row.get(column, "") for column in email_columns] if email_columns else row.values()
                    controls = {row.get(column, "").strip().lower() for column in control_columns}
                    language = next((row.get(column, "").strip() for column in language_columns
                                     if row.get(column, "").strip()), "ko")
                    excluded = bool(controls & {
                        "제외", "수신거부", "수신 거부", "발송 제외", "발송 금지", "아니오", "no", "false",
                        "0", "exclude", "excluded", "unsubscribed", "do not send",
                    }) or (not include_review and "추가 확인" in controls)
                    for value in values:
                        for candidate in _email_candidates(value):
                            yield candidate, excluded, language
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
            CREATE TABLE IF NOT EXISTS mailing_unsubscribe_confirmations (
                user_id INTEGER PRIMARY KEY, token TEXT NOT NULL, created_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS mailing_language_confirmations (
                user_id INTEGER PRIMARY KEY, token TEXT NOT NULL, created_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS mailing_localizations (
                post_key TEXT NOT NULL, language TEXT NOT NULL, source_key TEXT NOT NULL,
                title TEXT NOT NULL, html TEXT NOT NULL,
                PRIMARY KEY(post_key,language,source_key)
            );
            CREATE TABLE IF NOT EXISTS mailing_prospects (
                email TEXT PRIMARY KEY, name TEXT NOT NULL, organization TEXT NOT NULL,
                source_url TEXT NOT NULL, relevance TEXT NOT NULL,
                language_hint TEXT NOT NULL, language_evidence TEXT NOT NULL,
                consent TEXT NOT NULL DEFAULT 'unknown', status TEXT NOT NULL DEFAULT 'candidate',
                researched_at TEXT NOT NULL
            );
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
            CREATE TABLE IF NOT EXISTS mailing_delivery_blocks (
                email TEXT PRIMARY KEY, state TEXT NOT NULL, reason TEXT NOT NULL,
                status TEXT NOT NULL, hold_until TEXT, created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS mailing_bounce_events (
                id INTEGER PRIMARY KEY, message_id TEXT NOT NULL, email TEXT NOT NULL,
                action TEXT NOT NULL, status TEXT NOT NULL, reason TEXT NOT NULL,
                applied INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL,
                UNIQUE(message_id,email,action,status)
            );
            CREATE TABLE IF NOT EXISTS mailing_bounce_cursors (
                mailbox_key TEXT PRIMARY KEY, uidvalidity TEXT NOT NULL,
                last_uid INTEGER NOT NULL, updated_at TEXT NOT NULL
            );
        """)
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            subscriber_columns = {row["name"] for row in self.db.execute("PRAGMA table_info(mailing_subscribers)")}
            if "language" not in subscriber_columns:
                self.db.execute("ALTER TABLE mailing_subscribers ADD COLUMN language TEXT NOT NULL DEFAULT 'ko'")
            columns = {row["name"] for row in self.db.execute("PRAGMA table_info(mailing_outbox)")}
            for name in ("attempted_at", "sent_at", "batch_message_id"):
                if name not in columns:
                    self.db.execute(f"ALTER TABLE mailing_outbox ADD COLUMN {name} TEXT")
            self.db.execute(
                "CREATE INDEX IF NOT EXISTS mailing_outbox_batch ON mailing_outbox(batch_message_id)"
            )

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.close()

    def close(self):
        self.db.close()

    def subscribe(self, email: str, telegram_user_id: int | None = None, *, language: str | None = None) -> str:
        email = normalize_email(email)
        if language is not None:
            language = normalize_language(language)
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
                self.db.execute("DELETE FROM mailing_delivery_blocks WHERE email=?", (email,))
            else:
                self.db.execute(
                    "INSERT INTO mailing_subscribers(email,token,telegram_user_id,created_at,language) VALUES(?,?,?,?,?)",
                    (email, secrets.token_urlsafe(24), telegram_user_id, datetime.now(UTC).isoformat(), language or "ko"),
                )
            self.db.execute("UPDATE mailing_prospects SET status='subscribed',consent='self_subscribed' WHERE email=?", (email,))
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

    def set_pending_unsubscribe(self, user_id: int, token: str, created_at: float) -> None:
        with self.db:
            self.db.execute(
                "INSERT INTO mailing_unsubscribe_confirmations VALUES(?,?,?) "
                "ON CONFLICT(user_id) DO UPDATE SET token=excluded.token, created_at=excluded.created_at",
                (user_id, token, created_at),
            )

    def pop_pending_unsubscribe(self, user_id: int) -> tuple[str, float] | None:
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            row = self.db.execute(
                "SELECT token,created_at FROM mailing_unsubscribe_confirmations WHERE user_id=?", (user_id,),
            ).fetchone()
            self.db.execute("DELETE FROM mailing_unsubscribe_confirmations WHERE user_id=?", (user_id,))
        return (row["token"], row["created_at"]) if row else None

    def get_pending_unsubscribe(self, user_id: int) -> tuple[str, float] | None:
        row = self.db.execute(
            "SELECT token,created_at FROM mailing_unsubscribe_confirmations WHERE user_id=?", (user_id,),
        ).fetchone()
        return (row["token"], row["created_at"]) if row else None

    def set_pending_language(self, user_id: int, token: str, created_at: float) -> None:
        with self.db:
            self.db.execute(
                "INSERT INTO mailing_language_confirmations VALUES(?,?,?) "
                "ON CONFLICT(user_id) DO UPDATE SET token=excluded.token,created_at=excluded.created_at",
                (user_id, token, created_at),
            )

    def get_pending_language(self, user_id: int) -> tuple[str, float] | None:
        row = self.db.execute(
            "SELECT token,created_at FROM mailing_language_confirmations WHERE user_id=?", (user_id,),
        ).fetchone()
        return (row["token"], row["created_at"]) if row else None

    def clear_pending_language(self, user_id: int) -> None:
        with self.db:
            self.db.execute("DELETE FROM mailing_language_confirmations WHERE user_id=?", (user_id,))

    def set_language(self, user_id: int, language: str) -> bool:
        language = normalize_language(language)
        with self.db:
            return bool(self.db.execute(
                "UPDATE mailing_subscribers SET language=? WHERE telegram_user_id=? AND active=1",
                (language, user_id),
            ).rowcount)

    def set_language_token(self, token: str, language: str) -> bool:
        language = normalize_language(language)
        with self.db:
            return bool(self.db.execute(
                "UPDATE mailing_subscribers SET language=? WHERE token=? AND active=1", (language, token),
            ).rowcount)

    def get_language(self, user_id: int) -> str | None:
        row = self.db.execute(
            "SELECT language FROM mailing_subscribers WHERE telegram_user_id=? AND active=1", (user_id,),
        ).fetchone()
        return row["language"] if row else None

    def get_language_token(self, token: str) -> str | None:
        row = self.db.execute(
            "SELECT language FROM mailing_subscribers WHERE token=? AND active=1", (token,),
        ).fetchone()
        return row["language"] if row else None

    def language_counts(self) -> dict[str, int]:
        return dict(self.db.execute(
            "SELECT language,COUNT(*) FROM mailing_subscribers WHERE active=1 GROUP BY language"
        ))

    def get_localized_post(self, post_key: str, language: str, source_key: str) -> tuple[str, str] | None:
        row = self.db.execute(
            "SELECT title,html FROM mailing_localizations WHERE post_key=? AND language=? AND source_key=?",
            (post_key, language, source_key),
        ).fetchone()
        return (row["title"], row["html"]) if row else None

    def cache_localized_post(self, post_key: str, language: str, source_key: str, title: str, html: str) -> None:
        with self.db:
            self.db.execute(
                "INSERT OR REPLACE INTO mailing_localizations VALUES(?,?,?,?,?)",
                (post_key, language, source_key, title, html),
            )

    def import_prospects(self, records: list[dict]) -> ImportResult:
        result = ImportResult()
        with self.db:
            for record in records:
                try:
                    email = normalize_email(record["email"])
                    hint = normalize_language(record.get("language_hint") or "bilingual")
                    source = urlsplit(record["source_url"])
                    required = ("name", "organization", "source_url", "relevance", "language_evidence", "researched_at")
                    if any(not isinstance(record.get(key), str) or not record[key].strip() for key in required):
                        raise ValueError
                    if source.scheme != "https" or not source.hostname or source.username or source.password:
                        raise ValueError
                except (ValueError, KeyError, TypeError, AttributeError):
                    result.invalid += 1
                    continue
                if self.db.execute("SELECT 1 FROM mailing_subscribers WHERE email=?", (email,)).fetchone():
                    result.duplicates += 1
                    continue
                added = self.db.execute(
                    "INSERT OR IGNORE INTO mailing_prospects"
                    "(email,name,organization,source_url,relevance,language_hint,language_evidence,researched_at) "
                    "VALUES(?,?,?,?,?,?,?,?)",
                    (email, record["name"], record["organization"], record["source_url"], record["relevance"],
                     hint, record["language_evidence"], record["researched_at"]),
                ).rowcount
                if added:
                    result.imported += 1
                else:
                    result.duplicates += 1
        return result

    def prospect_counts(self) -> dict[str, int]:
        return dict(self.db.execute("SELECT status,COUNT(*) FROM mailing_prospects GROUP BY status"))

    def import_xlsx(self, path: Path, *, sheet_name: str | None = None, include_review: bool = False) -> ImportResult:
        result = ImportResult()
        # Parse before mutating so malformed later sheets cannot leave a partial import.
        addresses = list(_xlsx_addresses(Path(path), sheet_name=sheet_name, include_review=include_review))
        with self.db:
            for candidate, excluded, language in addresses:
                if excluded:
                    result.excluded += 1
                    continue
                try:
                    email = normalize_email(candidate)
                    language = normalize_language(language)
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
                    "INSERT INTO mailing_subscribers(email,token,created_at,language) VALUES(?,?,?,?)",
                    (email, secrets.token_urlsafe(24), datetime.now(UTC).isoformat(), language),
                )
                self.db.execute("UPDATE mailing_prospects SET status='imported' WHERE email=?", (email,))
                result.imported += 1
        return result

    def status_counts(self) -> dict[str, int]:
        counts = dict(self.db.execute("SELECT active,COUNT(*) FROM mailing_subscribers GROUP BY active"))
        return {"active": counts.get(1, 0), "unsubscribed": counts.get(0, 0)}

    def remove_subscriber(self, email: str) -> bool:
        email = normalize_email(email)
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            row = self.db.execute(
                "SELECT token FROM mailing_subscribers WHERE email=? AND active=1", (email,),
            ).fetchone()
            if row is None:
                return False
            self.db.execute(
                "UPDATE mailing_outbox SET status='suppressed' WHERE email=? AND status IN ('queued','failed')",
                (email,),
            )
            self.db.execute("UPDATE mailing_subscribers SET active=0 WHERE email=?", (email,))
        return True

    def edit_subscriber(self, email: str, *, new_email: str | None = None, language: str | None = None) -> bool:
        email = normalize_email(email)
        if new_email is None and language is None:
            raise ValueError("새 이메일 또는 수신 언어를 지정하세요.")
        replacement = normalize_email(new_email) if new_email is not None else email
        if language is not None:
            language = normalize_language(language)
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            row = self.db.execute(
                "SELECT * FROM mailing_subscribers WHERE email=? AND active=1", (email,),
            ).fetchone()
            if row is None:
                return False
            if replacement == email:
                if language is not None:
                    self.db.execute("UPDATE mailing_subscribers SET language=? WHERE email=?", (language, email))
                return True
            if self.db.execute("SELECT 1 FROM mailing_subscribers WHERE email=?", (replacement,)).fetchone():
                raise ValueError("새 이메일은 이미 등록되었거나 수신 해지된 주소입니다.")
            self.db.execute(
                "UPDATE mailing_outbox SET status='suppressed' WHERE email=? AND status IN ('queued','failed')",
                (email,),
            )
            self.db.execute(
                "UPDATE mailing_subscribers SET active=0,telegram_user_id=NULL WHERE email=?", (email,),
            )
            self.db.execute(
                "INSERT INTO mailing_subscribers(email,token,telegram_user_id,created_at,language) VALUES(?,?,?,?,?)",
                (replacement, secrets.token_urlsafe(24), row["telegram_user_id"], datetime.now(UTC).isoformat(),
                 language or row["language"]),
            )
            self.db.execute("UPDATE mailing_prospects SET status='imported' WHERE email=?", (replacement,))
        return True

    def outbox_counts(self) -> dict[str, int]:
        return dict(self.db.execute("SELECT status,COUNT(*) FROM mailing_outbox GROUP BY status"))

    def bounce_matches(self, notice) -> bool:
        return bool(self.db.execute(
            "SELECT 1 FROM mailing_outbox WHERE batch_message_id=? AND email=? "
            "AND status IN ('sent','pending','uncertain') LIMIT 1",
            (notice.message_id, normalize_email(notice.recipient)),
        ).fetchone())

    def apply_bounce(self, notice) -> str:
        """Apply an authenticated DSN only to the exact original recipient/membership."""
        email = normalize_email(notice.recipient)
        now = datetime.now(UTC).isoformat()
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            sent = self.db.execute(
                "SELECT MIN(COALESCE(attempted_at,sent_at,created_at)) AS attempted_at "
                "FROM mailing_outbox WHERE batch_message_id=? AND email=? "
                "AND status IN ('sent','pending','uncertain')",
                (notice.message_id, email),
            ).fetchone()
            if not sent["attempted_at"]:
                return "unmatched"
            subscriber = self.db.execute(
                "SELECT active,created_at FROM mailing_subscribers WHERE email=?", (email,),
            ).fetchone()
            if subscriber is None:
                return "unmatched"
            stale = datetime.fromisoformat(subscriber["created_at"]) > datetime.fromisoformat(sent["attempted_at"])
            added = self.db.execute(
                "INSERT OR IGNORE INTO mailing_bounce_events"
                "(message_id,email,action,status,reason,applied,created_at) VALUES(?,?,?,?,?,?,?)",
                (notice.message_id, email, notice.action, notice.status, notice.reason, 0, now),
            ).rowcount
            if not added:
                return "duplicate"
            if stale or not subscriber["active"]:
                return "stale"
            previous = self.db.execute(
                "SELECT * FROM mailing_delivery_blocks WHERE email=?", (email,),
            ).fetchone()
            hold_until = (datetime.now(UTC) + timedelta(hours=48)).isoformat() if notice.action == "delayed" else None
            if previous and previous["state"] == "held" and previous["hold_until"] is None:
                hold_until = None
            self.db.execute(
                "INSERT INTO mailing_delivery_blocks VALUES(?,?,?,?,?,?) "
                "ON CONFLICT(email) DO UPDATE SET state=excluded.state,reason=excluded.reason,"
                "status=excluded.status,hold_until=excluded.hold_until,created_at=excluded.created_at",
                (email, "bounced" if notice.permanent else "held", notice.reason, notice.status, hold_until, now),
            )
            if notice.permanent:
                self.db.execute("UPDATE mailing_subscribers SET active=0 WHERE email=?", (email,))
            self.db.execute(
                "UPDATE mailing_outbox SET status='suppressed',error='recipient_bounced' "
                "WHERE email=? AND status IN ('queued','failed')", (email,),
            )
            self.db.execute(
                "UPDATE mailing_bounce_events SET applied=1 WHERE message_id=? AND email=? AND action=? AND status=?",
                (notice.message_id, email, notice.action, notice.status),
            )
        return "applied"

    def bounce_status(self) -> dict:
        return {
            "eligible_subscribers": self.db.execute(
                "SELECT COUNT(*) FROM mailing_subscribers s WHERE active=1 AND NOT EXISTS "
                "(SELECT 1 FROM mailing_delivery_blocks b WHERE b.email=s.email)"
            ).fetchone()[0],
            "held_subscribers": self.db.execute(
                "SELECT COUNT(*) FROM mailing_delivery_blocks b JOIN mailing_subscribers s ON s.email=b.email "
                "WHERE b.state='held' AND s.active=1"
            ).fetchone()[0],
            "bounced_subscribers": self.db.execute(
                "SELECT COUNT(*) FROM mailing_delivery_blocks WHERE state='bounced'"
            ).fetchone()[0],
            "event_count": self.db.execute("SELECT COUNT(*) FROM mailing_bounce_events").fetchone()[0],
            "reasons": dict(self.db.execute(
                "SELECT reason,COUNT(*) FROM mailing_delivery_blocks GROUP BY reason"
            )),
        }

    def release_expired_bounce_holds(self) -> int:
        with self.db:
            return self.db.execute(
                "DELETE FROM mailing_delivery_blocks WHERE state='held' AND hold_until IS NOT NULL AND hold_until<=?",
                (datetime.now(UTC).isoformat(),),
            ).rowcount

    def resume_bounce_hold(self, email: str) -> bool:
        email = normalize_email(email)
        with self.db:
            return bool(self.db.execute(
                "DELETE FROM mailing_delivery_blocks WHERE email=? AND state='held' "
                "AND EXISTS (SELECT 1 FROM mailing_subscribers s WHERE s.email=? AND s.active=1)",
                (email, email),
            ).rowcount)

    def bounce_cursor(self, mailbox_key: str) -> tuple[str, int] | None:
        row = self.db.execute(
            "SELECT uidvalidity,last_uid FROM mailing_bounce_cursors WHERE mailbox_key=?", (mailbox_key,),
        ).fetchone()
        return (row[0], row[1]) if row else None

    def save_bounce_cursor(self, mailbox_key: str, uidvalidity: str, last_uid: int) -> None:
        with self.db:
            self.db.execute(
                "INSERT INTO mailing_bounce_cursors VALUES(?,?,?,?) ON CONFLICT(mailbox_key) DO UPDATE "
                "SET uidvalidity=excluded.uidvalidity,last_uid=CASE "
                "WHEN mailing_bounce_cursors.uidvalidity=excluded.uidvalidity "
                "THEN MAX(mailing_bounce_cursors.last_uid,excluded.last_uid) ELSE excluded.last_uid END,"
                "updated_at=excluded.updated_at",
                (mailbox_key, uidvalidity, last_uid, datetime.now(UTC).isoformat()),
            )

    def bounce_scan_since(self) -> str:
        earliest = self.db.execute(
            "SELECT MIN(COALESCE(attempted_at,sent_at,created_at)) FROM mailing_outbox "
            "WHERE batch_message_id IS NOT NULL"
        ).fetchone()[0]
        start = datetime.fromisoformat(earliest) - timedelta(days=1) if earliest else datetime.now(UTC) - timedelta(days=30)
        months = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
        return f"{start.day:02}-{months[start.month - 1]}-{start.year}"

    def _recent_message_count(self, statuses: tuple[str, ...]) -> int:
        cutoff = (datetime.now(UTC) - timedelta(hours=24)).isoformat()
        placeholders = ",".join("?" for _ in statuses)
        return self.db.execute(
            "SELECT COUNT(DISTINCT COALESCE(batch_message_id,'legacy:' || id)) FROM mailing_outbox "
            f"WHERE status IN ({placeholders}) AND COALESCE(sent_at,attempted_at,created_at)>?",
            (*statuses, cutoff),
        ).fetchone()[0]

    def delivery_status(self, settings) -> dict:
        sent = self._recent_message_count(("sent",))
        reserved = self._recent_message_count(("pending", "uncertain"))
        pending = self.db.execute(
            "SELECT COUNT(DISTINCT o.email) FROM mailing_outbox o "
            "JOIN mailing_subscribers s ON s.email=o.email WHERE o.status='queued' AND s.active=1 "
            "AND NOT EXISTS (SELECT 1 FROM mailing_delivery_blocks b WHERE b.email=s.email)"
        ).fetchone()[0]
        latest = self.db.execute(
            "SELECT MAX(COALESCE(sent_at,attempted_at,created_at)) FROM mailing_outbox WHERE status='sent'"
        ).fetchone()[0]
        paused = self.db.execute(
            "SELECT COUNT(DISTINCT o.email) FROM mailing_outbox o "
            "JOIN mailing_subscribers s ON s.email=o.email "
            "WHERE o.status='queued' AND o.error='smtp_paused' AND s.active=1 "
            "AND NOT EXISTS (SELECT 1 FROM mailing_delivery_blocks b WHERE b.email=s.email)"
        ).fetchone()[0]
        return {
            "pending_recipients": pending,
            "paused_recipients": paused,
            "sent_messages_last_24h": sent,
            "reserved_messages_last_24h": reserved,
            "remaining_daily_messages": max(0, settings.mailing_daily_limit - sent - reserved),
            "last_sent_at": latest,
            "batch_limit": settings.mailing_batch_limit,
            "daily_limit": settings.mailing_daily_limit,
        }

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
                    "SELECT ?,email,? FROM mailing_subscribers s WHERE active=1 AND created_at<=? "
                    "AND NOT EXISTS (SELECT 1 FROM mailing_delivery_blocks b WHERE b.email=s.email)",
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
            self.db.execute("BEGIN IMMEDIATE")
            row = self.db.execute(
                "SELECT batch_message_id FROM mailing_outbox "
                "WHERE id=? AND status IN ('pending','uncertain','failed')", (delivery_id,),
            ).fetchone()
            if not row:
                raise ValueError("해당 ID의 미확인 이메일 전송 기록이 없습니다.")
            condition = "batch_message_id=?" if row["batch_message_id"] else "id=?"
            updated = self.db.execute(
                "UPDATE mailing_outbox SET status=CASE WHEN ?='queued' AND (NOT EXISTS ("
                "SELECT 1 FROM mailing_subscribers s WHERE s.email=mailing_outbox.email AND s.active=1) "
                "OR EXISTS (SELECT 1 FROM mailing_delivery_blocks b WHERE b.email=mailing_outbox.email)) "
                "THEN 'suppressed' ELSE ? END,error='', "
                "sent_at=CASE WHEN ? THEN NULL ELSE COALESCE(sent_at,attempted_at,created_at) END "
                f"WHERE {condition} AND status IN ('pending','uncertain','failed')",
                ("queued" if retry else "sent", "queued" if retry else "sent", retry,
                 row["batch_message_id"] or delivery_id),
            ).rowcount
            if not updated:
                raise ValueError("해당 ID의 미확인 이메일 전송 기록이 없습니다.")

    def claim(self, delivery_id: int) -> bool:
        with self.db:
            return bool(self.db.execute(
                "UPDATE mailing_outbox SET status='pending',attempts=attempts+1,error='',attempted_at=? "
                "WHERE id=? AND status='queued' "
                "AND EXISTS (SELECT 1 FROM mailing_subscribers s WHERE s.email=mailing_outbox.email AND s.active=1) "
                "AND NOT EXISTS (SELECT 1 FROM mailing_delivery_blocks b WHERE b.email=mailing_outbox.email)",
                (datetime.now(UTC).isoformat(), delivery_id),
            ).rowcount)

    def claim_batch(self, delivery_ids: list[int], message_id: str, *, daily_limit: int | None = None) -> bool:
        ids = list(dict.fromkeys(delivery_ids))
        if not ids:
            return False
        placeholders = ",".join("?" for _ in ids)
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            if daily_limit is not None and self._recent_message_count(("sent", "pending", "uncertain")) >= daily_limit:
                return False
            eligible = self.db.execute(
                f"SELECT COUNT(*),COUNT(DISTINCT o.email) FROM mailing_outbox o "
                f"WHERE o.id IN ({placeholders}) AND o.status='queued' "
                "AND EXISTS (SELECT 1 FROM mailing_subscribers s WHERE s.email=o.email AND s.active=1) "
                "AND NOT EXISTS (SELECT 1 FROM mailing_delivery_blocks b WHERE b.email=o.email)", ids,
            ).fetchone()
            if eligible[0] != len(ids) or eligible[1] != 1:
                return False
            self.db.execute(
                "UPDATE mailing_outbox SET status='pending',attempts=attempts+1,error='', "
                f"attempted_at=?,sent_at=NULL,batch_message_id=? WHERE id IN ({placeholders})",
                (datetime.now(UTC).isoformat(), message_id, *ids),
            )
        return True

    def finish(self, delivery_id: int, status: str, error: str = ""):
        self.finish_batch([delivery_id], status, error)

    def finish_batch(self, delivery_ids: list[int], status: str, error: str = "") -> None:
        if status not in {"sent", "failed", "uncertain", "suppressed", "queued"}:
            raise ValueError("올바르지 않은 이메일 전송 상태입니다.")
        if not delivery_ids:
            return
        placeholders = ",".join("?" for _ in delivery_ids)
        with self.db:
            self.db.execute(
                "UPDATE mailing_outbox SET status=CASE WHEN ?='queued' AND NOT EXISTS ("
                "SELECT 1 FROM mailing_subscribers s JOIN mailing_posts p ON p.post_key=mailing_outbox.post_key "
                "WHERE s.email=mailing_outbox.email AND s.active=1 AND s.created_at<=p.created_at "
                "AND NOT EXISTS (SELECT 1 FROM mailing_delivery_blocks b WHERE b.email=s.email)"
                ") THEN 'suppressed' ELSE ? END, "
                f"error=?,sent_at=? WHERE id IN ({placeholders})",
                (status, status, error, datetime.now(UTC).isoformat() if status == "sent" else None, *delivery_ids),
            )


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
                invite_url: str, unsubscribe_url: str, message_id: str = "",
                subscribe_url: str = "", language: str = "ko", preferences_url: str = "") -> EmailMessage:
    normalize_email(sender)
    recipient = normalize_email(recipient)
    content = _PostContent()
    content.feed(telegram_html)
    plain = "".join(content.plain)
    html = "".join(content.html)
    copy = language_copy(language)
    if invite_url:
        plain += f"\n\n{copy['invite']}: {invite_url}"
        html += f'<br><br><a href="{escape(invite_url, quote=True)}">{escape(copy["invite"])}</a>'
    if subscribe_url:
        plain += f"\n\n{copy['subscribe']}: {subscribe_url}\n{copy['share_hint']}"
        html += (f'<br><br><a href="{escape(subscribe_url, quote=True)}">{escape(copy["subscribe"])}</a>'
                 f'<br>{escape(copy["share_hint"])}')
    if preferences_url:
        plain += f"\n\n{copy['preferences']}: {preferences_url}\n{copy['preferences_hint']}"
        html += (f'<br><br><a href="{escape(preferences_url, quote=True)}">{escape(copy["preferences"])}</a>'
                 f'<br>{escape(copy["preferences_hint"])}')
    plain += f"\n\n{copy['unsubscribe']}: {unsubscribe_url}\n{copy['unsubscribe_hint']}"
    html += (f'<br><br><a href="{escape(unsubscribe_url, quote=True)}">{escape(copy["unsubscribe"])}</a>'
             f'<br>{escape(copy["unsubscribe_hint"])}')
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
        f'<!doctype html><html lang="{copy["html_lang"]}"><body style="font-family:sans-serif;line-height:1.7">'
        + html + "</body></html>", subtype="html",
    )
    return message


class EmailDeliveryError(RuntimeError):
    """The SMTP server definitely did not accept this message."""


class EmailDeliveryUncertain(RuntimeError):
    """The SMTP server may have accepted this message; require operator resolution."""


class EmailConnectionError(EmailDeliveryError):
    """A batch-wide SMTP configuration or connection failure; stop this batch."""


class EmailDeliveryPaused(EmailDeliveryError):
    """A definite SMTP temporary or server-wide rejection; keep the batch queued."""


class EmailContentError(EmailDeliveryError):
    """The SMTP server rejected DATA permanently; stop before repeating the content."""


class EmailRecipientRejected(EmailDeliveryError):
    """A definite recipient-level SMTP rejection, with a sanitized structured reason."""

    def __init__(self, message: EmailMessage, response: bytes):
        from .mail_bounces import BounceNotice, classify_bounce

        match = re.search(rb"\b5\.\d{1,3}\.\d{1,3}\b", response)
        status = match[0].decode("ascii") if match else "5.0.0"
        reason, permanent = classify_bounce("failed", status) or ("recipient_rejected", False)
        self.notice = BounceNotice(
            str(message["Message-ID"] or ""), normalize_email(str(message["To"])),
            "failed", status, reason, permanent,
        )
        super().__init__("SMTP 서버가 수신 주소를 거절했습니다.")


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
                raise smtplib.SMTPRecipientsRefused(refused)
        except smtplib.SMTPRecipientsRefused as error:
            if any(
                400 <= response[0] < 500 or any(marker in response[1].lower() for marker in (
                    b"5.4.5", b"daily user sending", b"sending limit", b"rate limit", b"daily limit",
                ))
                for response in error.recipients.values()
            ):
                raise EmailDeliveryPaused("SMTP 서버가 발송을 일시적으로 제한했습니다.") from None
            response = next(iter(error.recipients.values()))[1] if len(error.recipients) == 1 else b""
            raise EmailRecipientRejected(message, response) from None
        except smtplib.SMTPDataError as error:
            response = error.smtp_error.lower()
            if 400 <= error.smtp_code < 500 or any(
                marker in response for marker in (b"5.4.5", b"quota", b"sending limit", b"rate limit", b"daily limit")
            ):
                raise EmailDeliveryPaused("SMTP 서버가 발송을 제한했습니다. 발송 한도를 확인하세요.") from None
            raise EmailContentError("SMTP 서버가 메일 내용을 거절했습니다.") from None
        except smtplib.SMTPSenderRefused:
            raise EmailConnectionError("SMTP 서버가 발신 주소를 거절했습니다.") from None
        except smtplib.SMTPResponseException as error:
            if 400 <= error.smtp_code < 500:
                raise EmailDeliveryPaused("SMTP 서버가 발송을 일시적으로 제한했습니다.") from None
            if sending:
                raise EmailDeliveryUncertain("SMTP 전송 결과를 확인할 수 없습니다.") from None
            raise EmailConnectionError("SMTP 연결 또는 인증에 실패했습니다.") from None
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


def _digest_groups(rows):
    """Bound each recipient digest so a long backlog still produces readable emails."""
    recipients = {}
    for row in rows:
        recipients.setdefault(row["email"], []).append(row)
    for posts in recipients.values():
        digest = []
        size = 0
        for row in posts:
            post_size = len((row["title"] + row["telegram_html"]).encode("utf-8"))
            if digest and (len(digest) >= 20 or size + post_size > 60_000):
                yield digest
                digest, size = [], 0
            digest.append(row)
            size += post_size
        if digest:
            yield digest


def deliver_pending(settings, store: MailingStore, *, bot_username: str = "", sender=None) -> DeliveryReport:
    localizer = MailLocalizer(settings, store)
    try:
        return _deliver_pending(settings, store, bot_username=bot_username, sender=sender, localizer=localizer)
    finally:
        localizer.close()


def _deliver_pending(settings, store: MailingStore, *, bot_username: str, sender, localizer) -> DeliveryReport:
    """Send recipient digests within batch/24h caps; reserve uncertain results."""
    username = (bot_username or settings.bot_username).lstrip("@")
    if not re.fullmatch(r"[A-Za-z0-9_]{5,32}", username):
        raise ValueError("수신 해지 링크에 사용할 텔레그램 봇 사용자명이 필요합니다.")
    settings.require_mail()
    send = sender or SMTPMailer(settings).send
    report = DeliveryReport()
    pending = store.db.execute(
        "SELECT o.id,o.post_key,o.email,o.attempts,s.token,s.active,s.language,p.title,p.telegram_html "
        "FROM mailing_outbox o JOIN mailing_subscribers s ON s.email=o.email "
        "JOIN mailing_posts p ON p.post_key=o.post_key WHERE o.status='queued' ORDER BY o.id"
    ).fetchall()
    for rows in _digest_groups(pending):
        row = rows[0]
        delivery_ids = [item["id"] for item in rows]
        if not row["active"] or store.db.execute(
            "SELECT 1 FROM mailing_delivery_blocks WHERE email=?", (row["email"],),
        ).fetchone():
            store.finish_batch(delivery_ids, "suppressed")
            report.skipped += 1
            continue
        if report.sent + report.failed + report.uncertain >= settings.mailing_batch_limit:
            break
        if store._recent_message_count(("sent", "pending", "uncertain")) >= settings.mailing_daily_limit:
            break
        language = normalize_language(row["language"])
        try:
            localized = [localizer.localize(item["post_key"], item["title"], item["telegram_html"], language)
                         for item in rows]
        except (MailLocalizationUnavailable, SummaryError, ValueError):
            report.localization_failed += 1
            placeholders = ",".join("?" for _ in delivery_ids)
            with store.db:
                store.db.execute(
                    f"UPDATE mailing_outbox SET error='localization_unavailable' WHERE id IN ({placeholders}) "
                    "AND status='queued'", delivery_ids,
                )
            continue
        unsubscribe_url = f"https://t.me/{username}?start=unsubscribe_{row['token']}"
        digest = hashlib.sha256(
            (row["email"] + "\0" + "\0".join(
                f"{item['post_key']}:{item['id']}:{item['attempts'] + 1}" for item in rows
            )).encode()
        ).hexdigest()
        domain = normalize_email(settings.smtp_from).rsplit("@", 1)[1]
        message_id = f"<{digest}@{domain}>"
        title = localized[0][0] if len(rows) == 1 else language_copy(language)["digest_subject"].format(count=len(rows))
        if language == "ko" and len(rows) > 1:
            title = f"{settings.channel_name} | 소식 {len(rows)}건"
        content = localized[0][1] if len(rows) == 1 else "\n\n".join(
            f"<b>{index}. {escape(localized_title)}</b>\n{localized_html}"
            for index, (localized_title, localized_html) in enumerate(localized, 1)
        )
        message = build_email(
            sender=settings.smtp_from, recipient=row["email"], title=title,
            telegram_html=content, invite_url=settings.telegram_invite_url,
            unsubscribe_url=unsubscribe_url, message_id=message_id,
            subscribe_url=f"https://t.me/{username}?start=subscribe",
            language=language, preferences_url=f"https://t.me/{username}?start=language_{row['token']}",
        )
        if not store.claim_batch(delivery_ids, message_id, daily_limit=settings.mailing_daily_limit):
            if store.delivery_status(settings)["remaining_daily_messages"] == 0:
                break
            report.skipped += 1
            continue
        try:
            send(message)
        except EmailDeliveryPaused:
            store.finish_batch(delivery_ids, "queued", "smtp_paused")
            report.paused = True
            break
        except EmailContentError:
            store.finish_batch(delivery_ids, "failed", "smtp_rejected")
            report.failed += 1
            break
        except EmailConnectionError:
            store.finish_batch(delivery_ids, "failed", "smtp_connection")
            report.failed += 1
            break
        except EmailRecipientRejected as error:
            store.apply_bounce(error.notice)
            store.finish_batch(delivery_ids, "failed", "smtp_recipient_rejected")
            report.failed += 1
        except EmailDeliveryError:
            store.finish_batch(delivery_ids, "failed", "smtp_rejected")
            report.failed += 1
        except (EmailDeliveryUncertain, OSError, smtplib.SMTPException):
            # A disconnect during DATA may happen after the server accepted the message.
            store.finish_batch(delivery_ids, "uncertain", "delivery_uncertain")
            report.uncertain += 1
        else:
            store.finish_batch(delivery_ids, "sent")
            report.sent += 1
    report.deferred = store.delivery_status(settings)["pending_recipients"]
    return report
