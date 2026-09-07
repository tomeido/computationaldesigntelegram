import hashlib
import json
import re
import sqlite3
import unicodedata
from contextlib import contextmanager
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path

from .models import Article, Summary
from .ranking import _url_key


def identity(article: Article) -> tuple[str, str]:
    url = _url_key(article.url)
    title = re.sub(r"[\W_]+", "", unicodedata.normalize("NFKC", article.title).casefold())
    return url, hashlib.sha256(title.encode()).hexdigest()


def cache_key(article: Article, model: str) -> str:
    raw = json.dumps(
        ["v2", model, article.kind, article.title, article.summary, article.source], ensure_ascii=False
    )
    return hashlib.sha256(raw.encode()).hexdigest()


class Store:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, timeout=10)
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS deliveries (
                id INTEGER PRIMARY KEY, channel TEXT NOT NULL, url TEXT NOT NULL,
                title_key TEXT NOT NULL, title TEXT NOT NULL,
                status TEXT NOT NULL, message_id INTEGER, created_at TEXT NOT NULL, slot TEXT,
                UNIQUE(channel, url), UNIQUE(channel, title_key)
            );
            CREATE TABLE IF NOT EXISTS summaries (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS slots (channel TEXT, slot TEXT, PRIMARY KEY(channel, slot));
            CREATE TABLE IF NOT EXISTS channel_aliases (alias TEXT PRIMARY KEY, channel TEXT NOT NULL);
        """)
        columns = {row[1] for row in self.db.execute("PRAGMA table_info(deliveries)")}
        if "slot" not in columns:
            self.db.execute("ALTER TABLE deliveries ADD COLUMN slot TEXT")
            self.db.commit()

    def close(self):
        self.db.close()

    def channel_for(self, alias: str) -> str:
        row = self.db.execute("SELECT channel FROM channel_aliases WHERE alias=?", (alias,)).fetchone()
        return row[0] if row else alias

    def remember_channel(self, alias: str, channel: str):
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO channel_aliases VALUES(?,?)", (alias, channel))

    def seen(self, article: Article, channel: str) -> bool:
        url, title_key = identity(article)
        return (
            self.db.execute(
                "SELECT 1 FROM deliveries WHERE channel=? AND (url=? OR title_key=?)",
                (channel, url, title_key),
            ).fetchone()
            is not None
        )

    def reserve(self, article: Article, channel: str, slot: str | None = None) -> int:
        url, title_key = identity(article)
        with self.db:
            cursor = self.db.execute(
                "INSERT INTO deliveries(channel,url,title_key,title,status,created_at,slot) VALUES(?,?,?,?,?,?,?)",
                (channel, url, title_key, article.title, "pending", datetime.now(UTC).isoformat(), slot),
            )
        return cursor.lastrowid

    def sent(self, delivery_id: int, message_id: int | None = None):
        with self.db:
            self.db.execute(
                "UPDATE deliveries SET status='sent', message_id=? WHERE id=?", (message_id, delivery_id)
            )

    def release(self, delivery_id: int):
        with self.db:
            self.db.execute("DELETE FROM deliveries WHERE id=? AND status!='sent'", (delivery_id,))

    def uncertain(self, delivery_id: int):
        with self.db:
            self.db.execute("UPDATE deliveries SET status='uncertain' WHERE id=?", (delivery_id,))

    def unresolved(self) -> list[dict]:
        return [dict(r) for r in self.db.execute("SELECT * FROM deliveries WHERE status!='sent' ORDER BY id")]

    def status_counts(self) -> dict:
        return dict(self.db.execute("SELECT status, COUNT(*) FROM deliveries GROUP BY status"))

    def get_summary(self, key: str) -> tuple[bool, Summary | None]:
        row = self.db.execute("SELECT value FROM summaries WHERE key=?", (key,)).fetchone()
        if not row:
            return False, None
        data = json.loads(row[0])
        if data is None:
            return True, None
        data["bullets"] = tuple(data["bullets"])
        return True, Summary(**data)

    def cache_summary(self, key: str, summary: Summary | None):
        with self.db:
            self.db.execute(
                "INSERT OR REPLACE INTO summaries VALUES(?,?)",
                (key, json.dumps(asdict(summary) if summary else None, ensure_ascii=False)),
            )

    def slot_done(self, channel: str, slot: str) -> bool:
        return bool(
            self.db.execute("SELECT 1 FROM slots WHERE channel=? AND slot=?", (channel, slot)).fetchone()
        )

    def slot_usage(self, channel: str, slot: str) -> int:
        return self.db.execute(
            "SELECT COUNT(*) FROM deliveries WHERE channel=? AND slot=?", (channel, slot)
        ).fetchone()[0]

    def complete_slot(self, channel: str, slot: str):
        with self.db:
            self.db.execute("INSERT OR IGNORE INTO slots VALUES(?,?)", (channel, slot))


@contextmanager
def job_lock(path: Path):
    # The deployment target is Linux. The lock also serializes manual publish with scheduled runs.
    import fcntl

    path.parent.mkdir(parents=True, exist_ok=True)
    with path.with_suffix(".lock").open("a") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError("다른 작업이 실행 중입니다. 완료 후 다시 실행하세요.") from None
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)
