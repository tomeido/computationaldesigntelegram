"""Private-chat commands and one persistent, serial Telegram polling consumer."""

from __future__ import annotations

import asyncio
import fcntl
import json
import logging
import os
import re
import tempfile
import time
from collections import OrderedDict
from contextlib import contextmanager
from dataclasses import replace
from html import escape
from pathlib import Path

import httpx

from .config import Settings
from .feeds import load_sources
from .pipeline import run_digest
from .telegram import Telegram, TelegramError

log = logging.getLogger(__name__)

CACHE_SECONDS = 3600
FETCH_COOLDOWN_SECONDS = 300
CHAT_COOLDOWN_SECONDS = 60
MAX_CHATS = 2048
MAX_CHANNELS = 40
ALLOWED_UPDATES = ["message", "channel_post", "my_chat_member"]


def offset_path(database_path: Path) -> Path:
    return database_path.with_suffix(".updates.json")


def read_listener_state(database_path: Path) -> dict:
    """Read passive channel metadata too, so discovery never needs a second poller."""
    path = offset_path(database_path)
    if not path.exists():
        return {"offset": 0, "channels": []}
    try:
        if path.stat().st_size > 65536:
            raise ValueError
        state = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(state, dict):
            raise TypeError
        offset = state.get("offset")
        channels = state.get("channels", [])
        if type(offset) is not int or offset < 0:
            raise ValueError
        if not isinstance(channels, list) or len(channels) > MAX_CHANNELS:
            raise ValueError
        if any(not isinstance(row, dict) or type(row.get("id")) is not int for row in channels):
            raise ValueError
        result = {"offset": offset, "channels": channels}
        if "bot_id" in state:
            bot_id = state["bot_id"]
            if type(bot_id) is not int or bot_id <= 0:
                raise ValueError
            result["bot_id"] = bot_id
        return result
    except (ValueError, TypeError, UnicodeError):
        # Resetting corrupt offsets would replay old commands and could duplicate replies.
        raise RuntimeError("봇 수신 상태 파일을 읽을 수 없습니다. 파일을 확인하세요.") from None


def write_listener_state(database_path: Path, state: dict) -> None:
    """Atomically replace state; never save incoming message text or sender details."""
    path = offset_path(database_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", prefix=f".{path.name}.", dir=path.parent, delete=False
        ) as handle:
            temporary = Path(handle.name)
            os.chmod(temporary, 0o600)
            json.dump(state, handle, ensure_ascii=False)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def read_offset(database_path: Path) -> int:
    return read_listener_state(database_path)["offset"]


def write_offset(database_path: Path, offset: int) -> None:
    if type(offset) is not int or offset < 0:
        raise ValueError("수신 위치는 0 이상의 정수여야 합니다.")
    state = read_listener_state(database_path)
    state["offset"] = offset
    write_listener_state(database_path, state)


@contextmanager
def listener_lock(database_path: Path):
    """Keep polling exclusive without blocking the digest's separate job lock."""
    database_path.parent.mkdir(parents=True, exist_ok=True)
    with database_path.with_suffix(".listener.lock").open("a") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError("봇 수신기가 이미 실행 중입니다. 수신기는 하나만 실행하세요.") from None
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def remember_channel(state: dict, update: dict) -> None:
    """Remember IDs for an operator to inspect; this never changes configuration."""
    event = update.get("channel_post") or update.get("my_chat_member")
    if not isinstance(event, dict):
        return
    chat = event.get("chat")
    if not isinstance(chat, dict) or chat.get("type") != "channel" or type(chat.get("id")) is not int:
        return
    metadata = {"id": chat["id"], "type": "channel"}
    for key, limit in (("title", 200), ("username", 64)):
        if isinstance(chat.get(key), str):
            metadata[key] = chat[key][:limit]
    existing = [row for row in state["channels"] if row["id"] != chat["id"]]
    state["channels"] = (existing + [metadata])[-MAX_CHANNELS:]


class CommandHandler:
    def __init__(self, settings: Settings, telegram: Telegram, username: str):
        self.settings = settings
        self.telegram = telegram
        self.username = username.casefold()
        self.latest_requests: OrderedDict[int, float] = OrderedDict()
        self.cached_messages: list[str] | None = None
        self.cached_at = float("-inf")
        self.last_fetch_at = float("-inf")

    async def _reply(self, chat_id: int, text: str) -> None:
        await self.telegram.call(
            "sendMessage",
            chat_id=chat_id,
            text=text,
            parse_mode="HTML",
            link_preview_options={"is_disabled": True},
        )

    def _welcome(self) -> str:
        name = escape(self.settings.channel_name[:100])
        text = (
            f"<b>{name}</b>\n\n"
            "Web3·블록체인과 생성 예술·컴퓨테이셔널 디자인의 접점을 최우선으로 살펴봅니다. "
            "AI 디자인 소식도 한국어 핵심과 원문 링크로 전합니다.\n\n"
            "/latest — 최신 브리핑 최대 3건\n"
            "/sources — 수집 출처\n"
            "/help — 이용 안내\n\n"
            "최신 브리핑은 1시간 동안 함께 사용하며, 개인별 요청 간격은 1분입니다."
        )
        channel = self.settings.channel_id
        if re.fullmatch(r"@[A-Za-z][A-Za-z0-9_]{4,31}", channel):
            text += f'\n\n<a href="https://t.me/{channel[1:]}">브리핑 채널 보기</a>'
        elif channel:
            text += "\n\n연결된 채널의 초대 링크는 채널 운영자에게 확인하세요."
        else:
            text += (
                "\n\n아직 방송 채널이 연결되지 않았습니다. Telegram 앱에서 ‘새 채널’을 만든 뒤 "
                "이 봇을 관리자로 추가하고 ‘메시지 게시’ 권한을 켜세요. "
                "운영자가 채널 ID를 설정하면 예약 브리핑이 시작됩니다. "
                "봇 토큰만으로 채널을 새로 만들 수는 없습니다."
            )
        return text

    def _sources(self) -> str:
        text = "<b>브리핑 수집 출처</b>\nWeb3·블록체인 디자인 소식을 우선 선별합니다.\n"
        for source in load_sources(self.settings.sources_file):
            if not source.enabled:
                continue
            line = f"\n• {escape(source.name[:100])}"
            # Telegram uses UTF-16 code units; count escaped markup conservatively.
            if len((text + line).encode("utf-16-le")) // 2 > 3900:
                text += "\n• 그 밖의 설정된 출처"
                break
            text += line
        return text

    async def _latest(self, chat_id: int) -> None:
        now = time.monotonic()
        previous = self.latest_requests.get(chat_id, float("-inf"))
        if now - previous < CHAT_COOLDOWN_SECONDS:
            await self._reply(chat_id, "최신 브리핑은 1분 간격으로 요청할 수 있습니다. 잠시 후 다시 확인하세요.")
            return
        self.latest_requests[chat_id] = now
        self.latest_requests.move_to_end(chat_id)
        while len(self.latest_requests) > MAX_CHATS:
            self.latest_requests.popitem(last=False)

        if self.cached_messages is not None and now - self.cached_at < CACHE_SECONDS:
            messages = self.cached_messages
        elif now - self.last_fetch_at < FETCH_COOLDOWN_SECONDS:
            await self._reply(chat_id, "새 소식을 확인하는 중입니다. 5분 후 다시 요청해 주세요.")
            return
        else:
            # Record before starting so repeated failures cannot bypass the global limit.
            self.last_fetch_at = now
            await self._reply(chat_id, "최신 소식을 모아 한국어로 정리하고 있습니다. 잠시만 기다려 주세요.")
            try:
                async with asyncio.timeout(180):
                    result = await run_digest(
                        replace(self.settings, channel_id="", max_posts=min(self.settings.max_posts, 3)),
                        publish=False,
                    )
                messages = (result.messages or [])[:3]
                if not messages and getattr(result, "failed", 0):
                    raise RuntimeError("브리핑 준비가 완료되지 않았습니다.")
            except Exception as error:  # noqa: BLE001 - one request must not stop the listener
                log.warning("브리핑 준비 실패 (%s).", type(error).__name__)
                await self._reply(chat_id, "지금은 새 소식을 가져오지 못했습니다. 5분 후 다시 요청해 주세요.")
                return
            self.cached_messages = messages
            self.cached_at = time.monotonic()

        if not messages:
            await self._reply(chat_id, "최근 수집한 소식 중 주제에 맞는 새 브리핑이 없습니다. 다음 갱신을 기다려 주세요.")
            return
        for message in messages:
            await self._reply(chat_id, message)

    async def handle(self, update: dict) -> None:
        message = update.get("message")
        if not isinstance(message, dict):
            return
        chat = message.get("chat")
        if not isinstance(chat, dict) or chat.get("type") != "private" or type(chat.get("id")) is not int:
            return
        sender = message.get("from", {})
        if isinstance(sender, dict) and sender.get("is_bot"):
            return
        text = message.get("text")
        if not isinstance(text, str):
            return
        match = re.match(r"^/([a-zA-Z]+)(?:@([a-zA-Z0-9_]+))?(?:\s|$)", text)
        if match is None or (match[2] and match[2].casefold() != self.username):
            return
        command, chat_id = match[1].casefold(), chat["id"]
        try:
            if command in {"start", "help"}:
                await self._reply(chat_id, self._welcome())
            elif command == "sources":
                await self._reply(chat_id, self._sources())
            elif command == "latest":
                await self._latest(chat_id)
        except TelegramError:
            # An uncertain reply must not be replayed automatically; mark the update handled.
            log.warning("개인 명령 응답을 전달하지 못했습니다.")
        except (OSError, ValueError):
            log.warning("개인 명령 설정을 읽지 못했습니다.")


async def listen(settings: Settings) -> None:
    """Listen until cancelled. Only private replies are authorized by public commands."""
    if not settings.bot_token:
        raise ValueError(".env에 TELEGRAM_BOT_TOKEN을 입력하세요.")
    with listener_lock(settings.database_path):
        state = read_listener_state(settings.database_path)
        async with httpx.AsyncClient(follow_redirects=False) as client:
            telegram = Telegram(client, settings.bot_token, settings.channel_id)
            webhook = await telegram.call("getWebhookInfo")
            if not isinstance(webhook, dict) or webhook.get("url"):
                raise RuntimeError("웹훅이 연결되어 있어 수신기를 시작할 수 없습니다. 기존 웹훅 설정을 확인하세요.")
            me = await telegram.call("getMe")
            if (
                not isinstance(me, dict)
                or not isinstance(me.get("username"), str)
                or type(me.get("id")) is not int
                or me["id"] <= 0
            ):
                raise RuntimeError("봇 계정을 확인할 수 없습니다.")
            if state.get("bot_id") != me["id"]:
                # Telegram update IDs belong to one bot; another account's cursor
                # can discard this bot's pending updates. Leave delivery history alone.
                state = {"bot_id": me["id"], "offset": 0, "channels": []}
                write_listener_state(settings.database_path, state)
            handler = CommandHandler(settings, telegram, me["username"])
            delay = 1
            log.info("개인 명령 수신기를 시작했습니다.")
            while True:
                try:
                    updates = await telegram.call(
                        "getUpdates", offset=state["offset"], timeout=15, allowed_updates=ALLOWED_UPDATES
                    )
                    if not isinstance(updates, list):
                        raise TelegramError("봇 수신 응답 형식이 올바르지 않습니다.")
                except TelegramError:
                    log.warning("봇 수신 연결을 다시 시도합니다.")
                    await asyncio.sleep(delay)
                    delay = min(delay * 2, 30)
                    continue
                delay = 1
                for update in updates:
                    if not isinstance(update, dict) or type(update.get("update_id")) is not int:
                        continue
                    if update["update_id"] < state["offset"]:
                        continue
                    await handler.handle(update)
                    remember_channel(state, update)
                    state["offset"] = update["update_id"] + 1
                    write_listener_state(settings.database_path, state)
