import asyncio
from types import SimpleNamespace

from compdesign_bot import bot_runtime, cli
from compdesign_bot.config import Settings


def test_run_with_only_token_starts_private_listener(monkeypatch):
    called = []

    async def listen(settings):
        called.append(("listen", settings.channel_id))

    async def schedule(settings):
        called.append(("schedule", settings.channel_id))

    monkeypatch.setattr(bot_runtime, "listen", listen)
    monkeypatch.setattr(cli, "serve", schedule)
    asyncio.run(cli.dispatch(SimpleNamespace(command="run"), Settings(bot_token="TEST")))
    assert called == [("listen", "")]


def test_channel_connection_enables_scheduler_and_private_listener(monkeypatch):
    called = []

    async def listen(settings):
        called.append("listen")

    async def schedule(settings):
        called.append("schedule")

    monkeypatch.setattr(bot_runtime, "listen", listen)
    monkeypatch.setattr(cli, "serve", schedule)
    asyncio.run(cli.dispatch(SimpleNamespace(command="run"), Settings(bot_token="TEST", channel_id="@test")))
    assert sorted(called) == ["listen", "schedule"]


def test_doctor_checks_token_and_local_model_without_channel_or_ai_key(monkeypatch, capsys):
    methods = []

    class Bot:
        def __init__(self, client, token, channel):
            assert token == "TEST"
            assert channel == ""

        async def call(self, method):
            methods.append(method)
            return {"username": "test_bot"}

    monkeypatch.setattr(cli, "Telegram", Bot)
    monkeypatch.setattr(Settings, "require_summary", lambda self: None)
    asyncio.run(cli.doctor(Settings(bot_token="TEST")))
    assert methods == ["getMe"]
    assert "채널 미연결" in capsys.readouterr().out


def test_discovery_reads_listener_state_without_polling_telegram(monkeypatch, tmp_path, capsys):
    settings = Settings(bot_token="TEST", database_path=tmp_path / "state.sqlite3")
    bot_runtime.write_listener_state(
        settings.database_path,
        {
            "offset": 8,
            "channels": [{"id": -100123, "type": "channel", "title": "디자인 소식"}],
        },
    )

    def unexpected_client(*args, **kwargs):
        raise AssertionError("Passive discovery must not start a second Telegram poller")

    monkeypatch.setattr(cli.httpx, "AsyncClient", unexpected_client)
    asyncio.run(cli.discover_channel(settings))
    assert "TELEGRAM_CHANNEL_ID=-100123" in capsys.readouterr().out
