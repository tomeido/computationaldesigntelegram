import asyncio
from html import escape
from zipfile import ZipFile

import pytest

from compdesign_bot import bot_runtime
from compdesign_bot.bot_runtime import CommandHandler
from compdesign_bot.config import Settings
from compdesign_bot.mailing import MailingStore, deliver_pending


class FakeTelegram:
    def __init__(self):
        self.calls = []

    async def call(self, method, **payload):
        assert method == "sendMessage"
        self.calls.append(payload)
        return {"message_id": 1}


def send(handler, text):
    asyncio.run(handler.handle({"message": {
        "chat": {"id": 42, "type": "private"}, "from": {"id": 42, "is_bot": False}, "text": text,
    }}))


def contacts(path, addresses):
    namespace = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
    with ZipFile(path, "w") as archive:
        archive.writestr("xl/workbook.xml", (
            f'<workbook xmlns="{namespace}" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
            '<sheets><sheet name="Contacts" sheetId="1" r:id="rId1"/></sheets></workbook>'
        ))
        archive.writestr("xl/_rels/workbook.xml.rels", (
            '<Relationships><Relationship Id="rId1" Target="worksheets/sheet1.xml"/></Relationships>'
        ))
        rows = ["Email", *addresses]
        archive.writestr("xl/worksheets/sheet1.xml", (
            f'<worksheet xmlns="{namespace}"><sheetData>'
            + "".join(
                f'<row r="{index}"><c r="A{index}" t="inlineStr"><is><t>{escape(value)}</t></is></c></row>'
                for index, value in enumerate(rows, 1)
            ) + '</sheetData></worksheet>'
        ))
    return path


def test_membership_edit_delete_rejoin_preserves_preferences_and_only_sends_new_queue(tmp_path, monkeypatch):
    def forbidden(*_args, **_kwargs):
        pytest.fail("Membership tests must not connect to SMTP or external translation APIs")

    monkeypatch.setattr("smtplib.SMTP", forbidden)
    monkeypatch.setattr("smtplib.SMTP_SSL", forbidden)
    monkeypatch.setattr("compdesign_bot.mail_localization.httpx.Client", forbidden)
    settings = Settings(
        database_path=tmp_path / "test.sqlite3", mailing_enabled=True,
        smtp_host="smtp.example.com", smtp_from="sender@example.com", bot_username="example_bot",
        telegram_invite_url="https://t.me/example_channel",
    )
    telegram = FakeTelegram()
    handler = CommandHandler(settings, telegram, "example_bot")
    send(handler, "/subscribe")
    send(handler, "invalid")
    handler = CommandHandler(settings, telegram, "example_bot")
    send(handler, "first@example.com")
    send(handler, "/language fr")
    with MailingStore(settings.database_path) as store:
        assert not store.awaiting_email(42)
        assert store.get_language(42) == "fr"
        store.store_post("before-change", "이전 소식", "기존 주소에만 대기 중인 소식")

    send(handler, "/subscribe second@example.com")
    with MailingStore(settings.database_path) as store:
        assert store.get_language(42) == "fr"
        old = store.db.execute(
            "SELECT active,telegram_user_id FROM mailing_subscribers WHERE email='first@example.com'"
        ).fetchone()
        assert tuple(old) == (0, None)
        assert store.outbox_counts() == {"suppressed": 1}
        store.store_post("before-unsubscribe", "이전 소식", "해지 전에 대기 중인 소식")
    send(handler, "/language ja")
    send(CommandHandler(settings, telegram, "example_bot"), "/unsubscribe")
    book = contacts(tmp_path / "contacts.xlsx", ["first@example.com", "second@example.com"])
    with MailingStore(settings.database_path) as store:
        assert store.status_counts() == {"active": 0, "unsubscribed": 2}
        assert store.outbox_counts() == {"suppressed": 2}
        imported = store.import_xlsx(book)
        assert (imported.imported, imported.suppressed) == (0, 2)
        publication_before_rejoin = store.db.execute(
            "SELECT created_at FROM mailing_posts WHERE post_key='before-unsubscribe'"
        ).fetchone()[0]

    send(handler, "/subscribe second@example.com")
    with MailingStore(settings.database_path) as store:
        assert store.get_language(42) == "ja"
        store.store_post("historical", "과거 소식", "재가입 전 소식", published_at=publication_before_rejoin)
        assert store.outbox_counts() == {"suppressed": 2}
        store.store_post("after-rejoin", "새 소식", "재가입 후 새로 게시한 소식")
    send(handler, "/language ko")
    sent = []
    with MailingStore(settings.database_path) as store:
        report = deliver_pending(settings, store, sender=sent.append)
        assert report.sent == 1 and report.failed == 0
        assert store.outbox_counts() == {"sent": 1, "suppressed": 2}
    assert len(sent) == 1
    assert str(sent[0]["To"]) == "second@example.com"
    body = sent[0].get_body(preferencelist=("plain",)).get_content()
    assert "재가입 후 새로 게시한 소식" in body
    assert "재가입 전 소식" not in body and "기존 주소에만" not in body


@pytest.mark.parametrize("pending_purpose", ["language", "unsubscribe"])
@pytest.mark.parametrize("expired", [False, True])
def test_other_token_purpose_never_falls_back_to_owned_subscription_after_restart(
    tmp_path, monkeypatch, pending_purpose, expired,
):
    now = [100.0]
    monkeypatch.setattr(bot_runtime.time, "time", lambda: now[0])
    settings = Settings(database_path=tmp_path / "test.sqlite3")
    telegram = FakeTelegram()
    with MailingStore(settings.database_path) as store:
        store.subscribe("own@example.com", 42, language="fr")
        store.subscribe("imported@example.com", language="de")
        token = store.db.execute(
            "SELECT token FROM mailing_subscribers WHERE email='imported@example.com'"
        ).fetchone()[0]
        store.store_post("queued", "소식", "대기 중인 소식")
    send(CommandHandler(settings, telegram, "example_bot"), f"/start {pending_purpose}_{token}")
    if expired:
        now[0] += 901
    different_command = "/unsubscribe" if pending_purpose == "language" else "/language en"
    for _ in range(2):
        send(CommandHandler(settings, telegram, "example_bot"), different_command)
        assert "/cancel" in telegram.calls[-1]["text"]
    with MailingStore(settings.database_path) as store:
        assert store.get_language(42) == "fr"
        assert store.get_language_token(token) == "de"
        assert store.status_counts() == {"active": 2, "unsubscribed": 0}
        assert store.outbox_counts() == {"queued": 2}
    assert all(token not in str(payload) for payload in telegram.calls)
    send(CommandHandler(settings, telegram, "example_bot"), "/cancel")
    send(CommandHandler(settings, telegram, "example_bot"), different_command)
    with MailingStore(settings.database_path) as store:
        assert store.get_language_token(token) == "de"
        if pending_purpose == "language":
            assert store.get_language(42) is None
            assert store.outbox_counts() == {"queued": 1, "suppressed": 1}
        else:
            assert store.get_language(42) == "en"
            assert store.outbox_counts() == {"queued": 2}


def test_imported_token_language_and_unsubscribe_survive_restart_without_claiming_or_reimporting(tmp_path):
    settings = Settings(database_path=tmp_path / "test.sqlite3")
    telegram = FakeTelegram()
    book = contacts(tmp_path / "contacts.xlsx", ["imported@example.com"])
    with MailingStore(settings.database_path) as store:
        assert store.import_xlsx(book).imported == 1
        store.subscribe("own@example.com", 42, language="fr")
        token = store.db.execute(
            "SELECT token FROM mailing_subscribers WHERE email='imported@example.com'"
        ).fetchone()[0]
        store.store_post("queued", "소식", "대기 중인 소식")
    send(CommandHandler(settings, telegram, "example_bot"), f"/start language_{token}")
    with MailingStore(settings.database_path) as store:
        assert store.get_language_token(token) == "ko"
    send(CommandHandler(settings, telegram, "example_bot"), "/language en")
    with MailingStore(settings.database_path) as store:
        assert store.get_language_token(token) == "en"
        assert store.get_language(42) == "fr"
        assert store.db.execute(
            "SELECT telegram_user_id FROM mailing_subscribers WHERE email='imported@example.com'"
        ).fetchone()[0] is None
    send(CommandHandler(settings, telegram, "example_bot"), f"/start unsubscribe_{token}")
    with MailingStore(settings.database_path) as store:
        assert store.status_counts() == {"active": 2, "unsubscribed": 0}
    for _ in range(2):
        send(CommandHandler(settings, telegram, "example_bot"), "/unsubscribe")
    with MailingStore(settings.database_path) as store:
        assert store.get_language(42) == "fr"
        assert store.status_counts() == {"active": 1, "unsubscribed": 1}
        assert store.outbox_counts() == {"queued": 1, "suppressed": 1}
        imported = store.import_xlsx(book)
        assert (imported.imported, imported.suppressed) == (0, 1)
    send(CommandHandler(settings, telegram, "example_bot"), "/subscribe imported@example.com")
    with MailingStore(settings.database_path) as store:
        assert store.get_language(42) == "fr"
        assert store.status_counts() == {"active": 1, "unsubscribed": 1}
    assert all(token not in str(payload) for payload in telegram.calls)
