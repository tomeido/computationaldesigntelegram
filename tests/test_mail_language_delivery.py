"""Language choices affect public briefing content without enrolling prospects."""

import sqlite3
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from compdesign_bot import mailing
from compdesign_bot.config import Settings
from compdesign_bot.mail_localization import MailLocalizationUnavailable, MailLocalizer
from compdesign_bot.mailing import EmailDeliveryUncertain, MailingStore, build_email, deliver_pending


def configured(**overrides):
    return replace(Settings(
        smtp_host="smtp.example.com", smtp_from="sender@example.com",
        bot_username="language_test_bot", telegram_invite_url="https://t.me/example_channel",
        mailing_enabled=True,
    ), **overrides)


def store_posts(store, count=2):
    for number in range(1, count + 1):
        store.store_post(
            f"post-{number}", f"한국어 제목 {number}",
            f'<b>한국어 본문 {number}</b>\n<a href="https://publisher.example/{number}">원문 보기</a>',
        )


def install_localizer(monkeypatch, *, fail_language=None):
    instances = []

    class FakeLocalizer:
        def __init__(self, *_args, **_kwargs):
            self.calls = []
            self.closed = False
            instances.append(self)

        def localize(self, post_key, title, html, language):
            self.calls.append((post_key, title, html, language))
            if language == fail_language:
                raise MailLocalizationUnavailable("translation unavailable")
            english_title = title.replace("한국어 제목", "English title")
            english_html = html.replace("한국어 본문", "English body").replace("원문 보기", "Read source")
            if language == "ko":
                return title, html
            if language == "bilingual":
                return f"{title} / {english_title}", f"{html}\n\n{english_html}"
            return english_title, english_html

        def close(self):
            self.closed = True

    monkeypatch.setattr(mailing, "MailLocalizer", FakeLocalizer)
    return instances


def body(message, kind="plain"):
    return message.get_body(preferencelist=(kind,)).get_content()


def prospect(email="candidate@example.com", **overrides):
    record = {
        "name": "Public studio contact", "organization": "Example generative art studio",
        "email": email, "source_url": "https://studio.example/contact",
        "relevance": "Official programme includes generative and digital art.",
        "language_hint": "en", "language_evidence": "Official contact page is in English.",
        "contact_type": "public_professional", "consent": "unknown",
        "researched_at": datetime.now(UTC).isoformat(),
    }
    return record | overrides


def test_recipient_language_changes_digest_body_and_private_preferences_footer(tmp_path, monkeypatch):
    instances = install_localizer(monkeypatch)
    messages = []
    with MailingStore(tmp_path / "mail.db") as store:
        store.subscribe("korean@example.com", 1, language="ko")
        store.subscribe("english@example.com", 2, language="en")
        tokens = {row["email"]: row["token"] for row in store.db.execute(
            "SELECT email,token FROM mailing_subscribers",
        )}
        store_posts(store)
        report = deliver_pending(configured(), store, sender=messages.append)
        assert (report.sent, report.localization_failed, report.deferred) == (2, 0, 0)
        assert store.outbox_counts() == {"sent": 4}
        by_email = {str(message["To"]): message for message in messages}
        korean = by_email["korean@example.com"]
        english = by_email["english@example.com"]
        assert "한국어 제목 1" in body(korean) and "한국어 본문 2" in body(korean)
        assert "English title 1" in body(english) and "English body 2" in body(english)
        assert "한국어 본문" not in body(english) and "한국어 제목" not in body(english)
        assert "Join the Telegram channel" in body(english)
        assert "Email language preferences" in body(english)
        assert "메일 언어 설정" in body(korean)
        assert '<html lang="en">' in body(english, "html")
        assert '<html lang="ko">' in body(korean, "html")
        for email, message in by_email.items():
            token = tokens[email]
            assert f"?start=language_{token}" in body(message)
            assert f"?start=unsubscribe_{token}" in str(message["List-Unsubscribe"])
            assert "https://t.me/language_test_bot?start=subscribe" in body(message)
            other = next(value for address, value in tokens.items() if address != email)
            assert other not in str(message)
        assert len(instances) == 1 and instances[0].closed
        assert {call[3] for call in instances[0].calls} == {"ko", "en"}
        public_inputs = repr(instances[0].calls)
        assert all(token not in public_inputs for token in tokens.values())
        assert all(email not in public_inputs for email in tokens)
        assert "?start=language_" not in public_inputs and "?start=unsubscribe_" not in public_inputs


def test_single_english_post_uses_translated_subject_and_body(tmp_path, monkeypatch):
    install_localizer(monkeypatch)
    messages = []
    with MailingStore(tmp_path / "mail.db") as store:
        store.subscribe("english@example.com", 1, language="en")
        store_posts(store, 1)
        assert deliver_pending(configured(), store, sender=messages.append).sent == 1
        assert str(messages[0]["Subject"]) == "English title 1"
        assert "English body 1" in body(messages[0])
        assert "한국어 본문" not in body(messages[0])


def test_bilingual_recipient_receives_both_bodies(tmp_path, monkeypatch):
    install_localizer(monkeypatch)
    messages = []
    with MailingStore(tmp_path / "mail.db") as store:
        store.subscribe("bilingual@example.com", 1, language="bilingual")
        store_posts(store, 1)
        assert deliver_pending(configured(), store, sender=messages.append).sent == 1
        assert "한국어 본문 1" in body(messages[0])
        assert "English body 1" in body(messages[0])
        assert '<html lang="ko">' in body(messages[0], "html")


def test_failed_foreign_translation_leaves_mail_unclaimed_and_korean_still_sends(tmp_path, monkeypatch):
    instances = install_localizer(monkeypatch, fail_language="en")
    messages = []
    with MailingStore(tmp_path / "mail.db") as store:
        store.subscribe("english@example.com", 1, language="en")
        store.subscribe("korean@example.com", 2, language="ko")
        store_posts(store)
        report = deliver_pending(configured(), store, sender=messages.append)
        assert (report.sent, report.localization_failed, report.failed, report.deferred) == (1, 1, 0, 1)
        assert [str(message["To"]) for message in messages] == ["korean@example.com"]
        english_rows = store.db.execute(
            "SELECT status,attempts,attempted_at,batch_message_id FROM mailing_outbox "
            "WHERE email='english@example.com'",
        ).fetchall()
        assert all(row["status"] == "queued" and row["attempts"] == 0 for row in english_rows)
        assert all(not row["attempted_at"] and not row["batch_message_id"] for row in english_rows)
        assert store.delivery_status(configured())["sent_messages_last_24h"] == 1
    assert len(instances) == 1 and instances[0].closed


def test_shared_translation_cache_avoids_repeat_provider_calls_for_same_post_language(tmp_path, monkeypatch):
    calls = []
    instances = []

    def translate(_self, texts, language):
        calls.append((list(texts), language))
        return [text.replace("한국어 제목", "English title").replace(
            "한국어 본문", "English body",
        ).replace("원문 보기", "Read source") for text in texts]

    class TrackingLocalizer(MailLocalizer):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.closed = False
            instances.append(self)

        def close(self):
            self.closed = True
            super().close()

    monkeypatch.setattr(MailLocalizer, "_translate", translate)
    monkeypatch.setattr(mailing, "MailLocalizer", TrackingLocalizer)
    settings = configured(translation_provider="gemini", gemini_api_key="unit-test-key")
    messages = []
    with MailingStore(tmp_path / "mail.db") as store:
        store.subscribe("first@example.com", 1, language="en")
        store.subscribe("second@example.com", 2, language="en")
        store_posts(store)
        tokens = [row[0] for row in store.db.execute("SELECT token FROM mailing_subscribers")]
        assert deliver_pending(settings, store, sender=messages.append).sent == 2
        assert len(calls) == 2
        assert {language for _, language in calls} == {"en"}
        assert all("English body 1" in body(message) and "English body 2" in body(message) for message in messages)
        assert store.db.execute("SELECT COUNT(*) FROM mailing_localizations").fetchone()[0] == 2
        assert all(token not in repr(calls) for token in tokens)
        assert "first@example.com" not in repr(calls) and "second@example.com" not in repr(calls)
        assert "?start=language_" not in repr(calls) and "?start=unsubscribe_" not in repr(calls)
    assert len(instances) == 1 and instances[0].closed


def test_localized_uncertain_digest_still_reserves_message_quota(tmp_path, monkeypatch):
    instances = install_localizer(monkeypatch)
    calls = []

    def uncertain(message):
        calls.append(message)
        raise EmailDeliveryUncertain("SMTP response lost")

    settings = configured(mailing_daily_limit=1)
    with MailingStore(tmp_path / "mail.db") as store:
        store.subscribe("english@example.com", 1, language="en")
        store.subscribe("korean@example.com", 2, language="ko")
        store_posts(store)
        report = deliver_pending(settings, store, sender=uncertain)
        assert (report.sent, report.uncertain, report.localization_failed, report.deferred) == (0, 1, 0, 1)
        assert store.outbox_counts() == {"queued": 2, "uncertain": 2}
        assert "English body 1" in body(calls[0])
        status = store.delivery_status(settings)
        assert status["reserved_messages_last_24h"] == 1 and status["remaining_daily_messages"] == 0
        assert len(calls) == 1
    assert len(instances) == 1 and instances[0].closed


def test_language_change_does_not_requeue_uncertain_digest(tmp_path, monkeypatch):
    install_localizer(monkeypatch)
    calls = []

    def uncertain(message):
        calls.append(message)
        raise EmailDeliveryUncertain("SMTP response lost")

    with MailingStore(tmp_path / "mail.db") as store:
        store.subscribe("reader@example.com", 1, language="en")
        store_posts(store)
        assert deliver_pending(configured(), store, sender=uncertain).uncertain == 1
        assert store.set_language(1, "ko")
        assert deliver_pending(configured(), store, sender=calls.append).sent == 0
        assert store.outbox_counts() == {"uncertain": 2}
        assert len(calls) == 1


@pytest.mark.parametrize("language,label,html_lang", [
    ("ko", "메일 언어 설정", "ko"),
    ("en", "Email language preferences", "en"),
    ("ja", "メールの言語設定", "ja"),
    ("fr", "Langue des e-mails", "fr"),
])
def test_build_email_preferences_footer_uses_requested_language(language, label, html_lang):
    message = build_email(
        sender="sender@example.com", recipient="reader@example.com", title="Public title", telegram_html="Public body",
        invite_url="https://t.me/example_channel", unsubscribe_url="https://t.me/language_test_bot?start=unsubscribe_TOKEN",
        subscribe_url="https://t.me/language_test_bot?start=subscribe", language=language,
        preferences_url="https://t.me/language_test_bot?start=language_TOKEN",
    )
    for kind in ("plain", "html"):
        content = body(message, kind)
        assert label in content
        assert content.count("https://t.me/language_test_bot?start=language_TOKEN") == 1
        assert "https://t.me/language_test_bot?start=subscribe" in content
    assert f'<html lang="{html_lang}">' in body(message, "html")
    assert str(message["List-Unsubscribe"]) == "<https://t.me/language_test_bot?start=unsubscribe_TOKEN>"
    assert "List-Unsubscribe-Post" not in message


def test_language_preferences_only_change_active_subscribers_without_binding_token_owner(tmp_path):
    with MailingStore(tmp_path / "mail.db") as store:
        store.subscribe("owned@example.com", 1, language="en")
        store.subscribe("imported@example.com", language="ko")
        store.subscribe("inactive@example.com", 2, language="fr")
        tokens = {row["email"]: row["token"] for row in store.db.execute("SELECT email,token FROM mailing_subscribers")}
        assert store.unsubscribe_user(2) == 1
        assert store.set_language(1, "ja")
        assert store.get_language(1) == "ja"
        assert store.set_language_token(tokens["imported@example.com"], "en")
        assert store.get_language_token(tokens["imported@example.com"]) == "en"
        assert not store.set_language(2, "en") and store.get_language(2) is None
        assert not store.set_language_token(tokens["inactive@example.com"], "en")
        assert store.get_language_token(tokens["inactive@example.com"]) is None
        assert not store.set_language(999, "en") and not store.set_language_token("invalid-token", "en")
        assert store.get_language_token("invalid-token") is None
        imported = store.db.execute(
            "SELECT telegram_user_id,active FROM mailing_subscribers WHERE email='imported@example.com'",
        ).fetchone()
        assert imported["telegram_user_id"] is None and imported["active"] == 1
        assert store.status_counts() == {"active": 2, "unsubscribed": 1}
        assert store.language_counts() == {"en": 1, "ja": 1}
        with pytest.raises(ValueError):
            store.set_language(1, "unsupported-language")
        assert store.get_language(1) == "ja"


def test_duplicate_signup_cannot_change_existing_mailbox_language(tmp_path):
    with MailingStore(tmp_path / "mail.db") as store:
        store.subscribe("imported@example.com", language="ko")
        assert store.subscribe("imported@example.com", 99, language="en") == "already_subscribed"
        row = store.db.execute("SELECT language,telegram_user_id FROM mailing_subscribers").fetchone()
        assert row["language"] == "ko" and row["telegram_user_id"] is None


def test_legacy_subscriber_schema_preserves_identity_status_dates_and_korean_default(tmp_path):
    path = tmp_path / "mail.db"
    created_at = (datetime.now(UTC) - timedelta(days=7)).isoformat()
    db = sqlite3.connect(path)
    db.execute("""
        CREATE TABLE mailing_subscribers (
            email TEXT PRIMARY KEY, token TEXT NOT NULL UNIQUE, telegram_user_id INTEGER UNIQUE,
            active INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL
        )
    """)
    db.executemany("INSERT INTO mailing_subscribers VALUES(?,?,?,?,?)", [
        ("active@example.com", "active-token", 11, 1, created_at),
        ("inactive@example.com", "inactive-token", 12, 0, created_at),
    ])
    db.commit()
    db.close()
    for _ in range(2):
        with MailingStore(path) as store:
            columns = [row[1] for row in store.db.execute("PRAGMA table_info(mailing_subscribers)")]
            assert columns[:5] == ["email", "token", "telegram_user_id", "active", "created_at"]
            assert columns.count("language") == 1
            assert columns.count("operator_excluded") == 1
            rows = {row["email"]: dict(row) for row in store.db.execute("SELECT * FROM mailing_subscribers")}
            assert rows["active@example.com"] == {
                "email": "active@example.com", "token": "active-token", "telegram_user_id": 11,
                "active": 1, "created_at": created_at, "language": "ko", "operator_excluded": 0,
            }
            assert rows["inactive@example.com"]["active"] == 0
            assert rows["inactive@example.com"]["language"] == "ko"
            assert rows["inactive@example.com"]["created_at"] == created_at
            assert store.get_language(11) == "ko" and store.get_language(12) is None
            assert store.language_counts() == {"ko": 1}


def test_prospect_import_never_enrolls_contacts_or_trusts_record_consent(tmp_path):
    with MailingStore(tmp_path / "mail.db") as store:
        store.subscribe("active@example.com", 1, language="ko")
        store.subscribe("unsubscribed@example.com", 2, language="fr")
        store.unsubscribe_user(2)
        forged = prospect(consent="granted", status="subscribed", active=True, language="en")
        result = store.import_prospects([
            forged, prospect("active@example.com"), prospect("unsubscribed@example.com"), forged,
        ])
        assert (result.imported, result.duplicates, result.invalid) == (1, 3, 0)
        assert store.prospect_counts() == {"candidate": 1}
        assert store.status_counts() == {"active": 1, "unsubscribed": 1}
        assert store.language_counts() == {"ko": 1}
        candidate = store.db.execute("SELECT consent,status FROM mailing_prospects").fetchone()
        assert tuple(candidate) == ("unknown", "candidate")
        store_posts(store, 1)
        assert store.db.execute(
            "SELECT COUNT(*) FROM mailing_outbox WHERE email='candidate@example.com'",
        ).fetchone()[0] == 0


@pytest.mark.parametrize("overrides", [
    {"source_url": "http://studio.example/contact"},
    {"source_url": "https://user:password@studio.example/contact"},
    {"source_url": "not-a-url"},
    {"source_url": "https:///contact"},
    {"relevance": ""},
    {"language_evidence": None},
    {"researched_at": ""},
    {"email": "invalid-address"},
])
def test_invalid_prospect_sources_and_incomplete_evidence_are_rejected(tmp_path, overrides):
    with MailingStore(tmp_path / "mail.db") as store:
        result = store.import_prospects([prospect(**overrides)])
        assert (result.imported, result.invalid) == (0, 1)
        assert store.prospect_counts() == {} and store.status_counts()["active"] == 0


def test_prospect_self_subscription_sets_consent_and_only_queues_future_posts(tmp_path):
    with MailingStore(tmp_path / "mail.db") as store:
        record = prospect(language_hint="fr")
        assert store.import_prospects([record]).imported == 1
        old = (datetime.now(UTC) - timedelta(days=1)).isoformat()
        store.store_post("past", "Past briefing", "PAST-BODY", published_at=old)
        assert store.outbox_counts() == {}
        assert store.subscribe(record["email"], 5, language="en") == "subscribed"
        assert store.get_language(5) == "en"
        assert store.prospect_counts() == {"subscribed": 1}
        candidate = store.db.execute("SELECT consent,status FROM mailing_prospects").fetchone()
        assert tuple(candidate) == ("self_subscribed", "subscribed")
        subscribed_at = store.db.execute("SELECT created_at FROM mailing_subscribers").fetchone()[0]
        assert datetime.fromisoformat(subscribed_at) > datetime.fromisoformat(old)
        store.store_post("past", "Past briefing", "PAST-BODY", published_at=old)
        assert store.outbox_counts() == {}
        store.store_post("future", "Future briefing", "FUTURE-BODY")
        assert store.outbox_counts() == {"queued": 1}


def test_default_legacy_code_subscription_keeps_korean_language(tmp_path):
    with MailingStore(tmp_path / "mail.db") as store:
        assert store.subscribe("reader@example.com", 1) == "subscribed"
        assert store.get_language(1) == "ko"
