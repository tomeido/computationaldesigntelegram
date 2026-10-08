import asyncio
import json
from dataclasses import replace
from types import SimpleNamespace

import pytest

from compdesign_bot.cli import dispatch
from compdesign_bot.config import Settings
from compdesign_bot.mailing import MailingStore, build_email, deliver_pending
from test_mailing import workbook


def test_stop_requests_cancel_unsent_preserve_history_and_prevent_import_and_subscribe(tmp_path):
    exclusions = tmp_path / "exclusions.json"
    exclusions.write_text(json.dumps(["stop@example.com", "future@example.com", "STOP@example.com"]))
    xlsx = workbook(tmp_path / "list.xlsx", {"Contacts": [["Email"], ["stop@example.com"], ["future@example.com"]]})
    with MailingStore(tmp_path / "mail.db") as mail:
        mail.subscribe("stop@example.com", 1, language="de")
        for key, status in enumerate(("queued", "failed", "sent", "uncertain")):
            mail.store_post(str(key), "News", "News")
            mail.db.execute("UPDATE mailing_outbox SET status=? WHERE post_key=?", (status, str(key)))
        mail.db.commit()
        assert mail.apply_exclusions(exclusions) == 1
        assert mail.apply_exclusions(exclusions) == 0
        assert mail.outbox_counts() == {"suppressed": 2, "sent": 1, "uncertain": 1}
        assert mail.import_xlsx(xlsx).suppressed == 2
        assert mail.subscribe("stop@example.com", 1) == "email_in_use"
        assert mail.subscribe("future@example.com", 2) == "email_in_use"
        assert mail.db.execute("SELECT language FROM mailing_subscribers WHERE email='stop@example.com'").fetchone()[0] == "de"
        mail.store_post("future", "Future", "Future")
        assert mail.status_counts() == {"active": 0, "unsubscribed": 2}
        assert mail.outbox_counts()["suppressed"] == 2


@pytest.mark.parametrize("content", ["not json", '{"email":"stop@example.com"}', '["stop@example.com", 1]',
                                     '["stop@example.com", "invalid"]'])
def test_invalid_exclusion_list_has_no_partial_effect(tmp_path, content):
    path = tmp_path / "exclusions.json"
    path.write_text(content)
    with MailingStore(tmp_path / "mail.db") as mail:
        mail.subscribe("stop@example.com")
        with pytest.raises(ValueError):
            mail.apply_exclusions(path)
        assert mail.status_counts()["active"] == 1


def test_cli_applies_private_exclusions_without_sending_or_printing_addresses(tmp_path, capsys, monkeypatch):
    path = tmp_path / "exclusions.json"
    path.write_text('["private@example.com"]')
    settings = Settings(database_path=tmp_path / "mail.db", mailing_exclusions_path=path)
    with MailingStore(settings.database_path) as mail:
        mail.subscribe("private@example.com")
    monkeypatch.setattr("smtplib.SMTP", lambda *a, **kw: pytest.fail("must not send"))
    asyncio.run(dispatch(SimpleNamespace(command="mail-apply-exclusions"), settings))
    output = capsys.readouterr().out
    assert json.loads(output) == {"removed_from_active_list": 1}
    assert "private@example.com" not in output


def test_delivery_applies_exclusions_and_builds_personalized_direct_links(tmp_path):
    path = tmp_path / "exclusions.json"
    path.write_text('["stop@example.com"]')
    settings = Settings(
        database_path=tmp_path / "mail.db", mailing_exclusions_path=path,
        smtp_host="smtp.example.com", smtp_from="sender@example.com", bot_username="example_bot",
        mailing_unsubscribe_base_url="https://briefing.example.com/list",
    )
    messages = []
    with MailingStore(settings.database_path) as mail:
        mail.subscribe("stop@example.com")
        mail.subscribe("reader@example.com")
        token = mail.db.execute("SELECT token FROM mailing_subscribers WHERE email='reader@example.com'").fetchone()[0]
        mail.store_post("news", "News", "News")
        assert deliver_pending(settings, mail, sender=messages.append).sent == 1
        assert mail.outbox_counts() == {"sent": 1, "suppressed": 1}
        message = messages[0]
        url = f"https://briefing.example.com/list/unsubscribe/{token}"
        assert message["To"] == "reader@example.com"
        assert message["List-Unsubscribe"] == f"<{url}>"
        assert message["List-Unsubscribe-Post"] == "List-Unsubscribe=One-Click"
        assert url in message.get_body(preferencelist=("plain",)).get_content()
        html = message.get_body(preferencelist=("html",)).get_content()
        assert url in html and "display:inline-block" in html
        assert "?start=unsubscribe_" not in str(message)
        assert "stop@example.com" not in str(message)


def test_unsubscribe_during_claim_is_checked_before_smtp(tmp_path, monkeypatch):
    settings = Settings(smtp_host="smtp.example.com", smtp_from="sender@example.com", bot_username="example_bot",
                        mailing_exclusions_path=tmp_path / "missing.json")
    with MailingStore(tmp_path / "mail.db") as mail:
        mail.subscribe("reader@example.com")
        mail.store_post("news", "News", "News")
        token = mail.db.execute("SELECT token FROM mailing_subscribers").fetchone()[0]
        claim = mail.claim_batch

        def unsubscribe_after_claim(*args, **kwargs):
            result = claim(*args, **kwargs)
            mail.unsubscribe_token(token)
            return result

        monkeypatch.setattr(mail, "claim_batch", unsubscribe_after_claim)
        report = deliver_pending(settings, mail, sender=lambda message: pytest.fail("unsubscribed recipient sent"))
        assert report.sent == 0 and report.skipped == 1
        assert mail.outbox_counts() == {"suppressed": 1}


@pytest.mark.parametrize("base", ["http://example.com", "https://user:pass@example.com", "https://example.com/?x=1",
                                 "https://example.com/#x", "https://example.com/../x", "https://example.com/\nx",
                                 "https://example.com?", "https://example.com#", "https://@example.com",
                                 "https://example.com/메일", "https://example.com/%2e%2e", "https://example.com//mail"])
def test_public_unsubscribe_base_requires_clean_https_url(base):
    with pytest.raises(ValueError, match="MAILING_UNSUBSCRIBE_BASE_URL"):
        Settings(mailing_unsubscribe_base_url=base)


@pytest.mark.parametrize("language", ["ko", "en", "ja", "zh", "de", "fr", "es", "pt", "bilingual"])
def test_one_click_footer_has_no_bot_confirmation_instructions(language):
    message = build_email(sender="sender@example.com", recipient="reader@example.com", title="News",
                          telegram_html="News", invite_url="", unsubscribe_url="https://example.com/unsubscribe/token",
                          language=language, one_click_unsubscribe=True)
    plain = message.get_body(preferencelist=("plain",)).get_content()
    assert all("/unsubscribe" not in line for line in plain.splitlines() if "https://" not in line)
    assert "?start=" not in str(message)


def test_new_environment_settings(monkeypatch):
    monkeypatch.setattr("compdesign_bot.config.load_dotenv", lambda: None)
    monkeypatch.setenv("MAILING_UNSUBSCRIBE_BASE_URL", "https://example.com/list/")
    monkeypatch.setenv("MAILING_UNSUBSCRIBE_HOST", "127.0.0.1")
    monkeypatch.setenv("MAILING_UNSUBSCRIBE_PORT", "8091")
    monkeypatch.setenv("MAILING_EXCLUSIONS_PATH", "data/private.json")
    settings = Settings.from_env()
    assert settings.mailing_unsubscribe_base_url == "https://example.com/list"
    assert settings.mailing_unsubscribe_port == 8091
    assert str(settings.mailing_exclusions_path) == "data/private.json"
    assert replace(settings, mailing_unsubscribe_base_url="").mailing_unsubscribe_base_url == ""
