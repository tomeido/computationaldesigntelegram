import asyncio
import json
from dataclasses import replace
from types import SimpleNamespace
from typing import ClassVar

import pytest

from compdesign_bot import cli, config, mail_delivery
from compdesign_bot.config import Settings
from compdesign_bot.mailing import DeliveryReport, MailingStore


@pytest.mark.parametrize("field,environment_name,upper", [
    ("mailing_batch_limit", "MAILING_BATCH_LIMIT", 1000),
    ("mailing_daily_limit", "MAILING_DAILY_LIMIT", 10000),
])
def test_mail_limits_validate_direct_settings_and_environment(monkeypatch, field, environment_name, upper):
    monkeypatch.setattr(config, "load_dotenv", lambda: None)
    for invalid in (0, upper + 1, True, "200", 1.5):
        with pytest.raises(ValueError, match=environment_name):
            replace(Settings(), **{field: invalid})
    for valid in (1, upper):
        assert getattr(replace(Settings(), **{field: valid}), field) == valid
        monkeypatch.setenv(environment_name, str(valid))
        assert getattr(Settings.from_env(), field) == valid
    for invalid in ("0", str(upper + 1), "1.5", "invalid"):
        monkeypatch.setenv(environment_name, invalid)
        with pytest.raises(ValueError, match=environment_name):
            Settings.from_env()


def test_mail_limits_default_and_custom_environment(monkeypatch):
    monkeypatch.setattr(config, "load_dotenv", lambda: None)
    monkeypatch.delenv("MAILING_BATCH_LIMIT", raising=False)
    monkeypatch.delenv("MAILING_DAILY_LIMIT", raising=False)
    defaults = Settings.from_env()
    assert (defaults.mailing_batch_limit, defaults.mailing_daily_limit) == (200, 400)
    monkeypatch.setenv("MAILING_BATCH_LIMIT", "75")
    monkeypatch.setenv("MAILING_DAILY_LIMIT", "300")
    settings = Settings.from_env()
    assert (settings.mailing_batch_limit, settings.mailing_daily_limit) == (75, 300)


class FakeTelegram:
    calls: ClassVar[list] = []

    def __init__(self, _client, _token, _channel):
        pass

    async def call(self, method, **payload):
        self.calls.append((method, payload))
        return {"username": "example_bot"} if method == "getMe" else True


@pytest.mark.parametrize("overrides,expected", [
    ({"mailing_enabled": False}, "메일 자동 발송: 꺼짐"),
    ({"smtp_host": ""}, "메일 자동 발송 준비 대기"),
    ({"bot_username": ""}, "메일 자동 발송 준비 대기"),
    ({"telegram_invite_url": ""}, "메일 자동 발송 준비 대기"),
    ({}, "메일 자동 발송 설정 준비 완료"),
])
def test_doctor_reports_mail_readiness_without_connecting_or_exposing_secrets(
    monkeypatch, capsys, overrides, expected,
):
    settings = Settings(
        bot_token="BOT_SECRET", mailing_enabled=True,
        smtp_host="smtp.example.com", smtp_from="sender@example.com",
        smtp_username="PRIVATE_USERNAME", smtp_password="PRIVATE_PASSWORD",
        bot_username="example_bot", telegram_invite_url="https://t.me/example_channel",
        mailing_batch_limit=75, mailing_daily_limit=300,
    )
    FakeTelegram.calls = []
    monkeypatch.setattr(cli, "Telegram", FakeTelegram)
    monkeypatch.setattr(Settings, "require_summary", lambda self: None)

    def forbidden(*_args, **_kwargs):
        pytest.fail("doctor must not connect to SMTP or send email")

    monkeypatch.setattr("smtplib.SMTP", forbidden)
    monkeypatch.setattr("smtplib.SMTP_SSL", forbidden)
    asyncio.run(cli.doctor(replace(settings, **overrides)))
    output = capsys.readouterr().out
    assert expected in output
    assert "한 번에 75통 / 최근 24시간 300통" in output
    assert not any(secret in output for secret in ("BOT_SECRET", "PRIVATE_USERNAME", "PRIVATE_PASSWORD"))
    assert [method for method, _ in FakeTelegram.calls] == ["getMe"]


def test_configure_bot_includes_share_in_both_language_menus(monkeypatch):
    FakeTelegram.calls = []
    monkeypatch.setattr(cli, "Telegram", FakeTelegram)
    asyncio.run(cli.configure_bot(Settings(bot_token="BOT_SECRET")))
    menus = [payload for method, payload in FakeTelegram.calls if method == "setMyCommands"]
    assert [payload["language_code"] for payload in menus] == ["", "ko"]
    for payload in menus:
        assert any(command["command"] == "share" for command in payload["commands"])
    descriptions = [payload for method, payload in FakeTelegram.calls if method == "setMyDescription"]
    assert all("/share" in payload["description"] for payload in descriptions)


def test_mail_status_includes_delivery_capacity_without_exposing_addresses(monkeypatch, tmp_path, capsys):
    settings = Settings(database_path=tmp_path / "bot.sqlite3")
    delivery = {
        "pending_recipients": 1, "sent_messages_last_24h": 10, "remaining_daily_messages": 390,
        "last_sent_at": "2026-10-03T09:00:00+00:00", "batch_limit": 200, "daily_limit": 400,
    }
    with MailingStore(settings.database_path) as store:
        store.subscribe("PRIVATE_ADDRESS@example.com")
    monkeypatch.setattr(mail_delivery, "sync_mail_queue", lambda _settings: None)
    monkeypatch.setattr(MailingStore, "delivery_status", lambda self, current: delivery, raising=False)
    asyncio.run(cli.dispatch(SimpleNamespace(command="mail-status"), settings))
    output = capsys.readouterr().out
    assert json.loads(output)["delivery"] == delivery
    assert "PRIVATE_ADDRESS" not in output
    assert "private_address" not in output


def test_mail_send_reports_intentionally_deferred_recipients_as_success(monkeypatch, capsys):
    report = DeliveryReport(sent=3, deferred=7)
    monkeypatch.setattr(mail_delivery, "send_mail_queue", lambda _settings: report)
    asyncio.run(cli.dispatch(SimpleNamespace(command="mail-send"), Settings()))
    output = json.loads(capsys.readouterr().out)
    assert output["sent"] == 3
    assert output["deferred"] == 7
    assert output["failed"] == 0


def test_automatic_mail_log_distinguishes_sent_messages_and_waiting_recipients(monkeypatch, caplog):
    report = DeliveryReport(sent=3, deferred=7)
    monkeypatch.setattr(mail_delivery, "sync_mail_queue", lambda _settings: None)
    monkeypatch.setattr(mail_delivery, "send_mail_queue", lambda _settings: report)
    with caplog.at_level("INFO", logger="compdesign_bot.mail_delivery"):
        result = asyncio.run(mail_delivery.auto_send_mail(Settings(mailing_enabled=True)))
    assert result is report
    assert "메일 발송 3통" in caplog.text
    assert "대기 수신자 7명" in caplog.text


def test_mail_send_reports_provider_pause_and_preserved_queue(monkeypatch, capsys):
    report = DeliveryReport(sent=1, deferred=7, paused=True)
    monkeypatch.setattr(mail_delivery, "send_mail_queue", lambda _settings: report)
    with pytest.raises(RuntimeError, match="SMTP 서버.*대기열은 유지.*mail-status"):
        asyncio.run(cli.dispatch(SimpleNamespace(command="mail-send"), Settings()))
    output = json.loads(capsys.readouterr().out)
    assert output["paused"] is True
    assert output["deferred"] == 7


def test_automatic_mail_warns_of_provider_pause_without_raising_or_exposing_secrets(monkeypatch, caplog):
    report = DeliveryReport(deferred=7, paused=True)
    monkeypatch.setattr(mail_delivery, "sync_mail_queue", lambda _settings: None)
    monkeypatch.setattr(mail_delivery, "send_mail_queue", lambda _settings: report)
    settings = Settings(mailing_enabled=True, smtp_username="PRIVATE_USERNAME", smtp_password="PRIVATE_PASSWORD")
    with caplog.at_level("WARNING", logger="compdesign_bot.mail_delivery"):
        result = asyncio.run(mail_delivery.auto_send_mail(settings))
    assert result is report
    assert "SMTP 서버가 발송을 제한해 중단" in caplog.text
    assert "대기열을 유지" in caplog.text
    assert "PRIVATE_USERNAME" not in caplog.text
    assert "PRIVATE_PASSWORD" not in caplog.text
