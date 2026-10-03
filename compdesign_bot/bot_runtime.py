"""Private-chat commands and one persistent, serial Telegram polling consumer."""

from __future__ import annotations

import asyncio
import fcntl
import json
import logging
import os
import re
import sqlite3
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
from .github_sources import load_repositories
from .mail_localization import LANGUAGE_NAMES, normalize_language
from .mailing import MailingStore
from .pipeline import run_digest
from .resources import public_resource_url
from .telegram import Telegram, TelegramError, channel_invite_url, mailing_signup_url, message_payload

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
        self.invite_url = channel_invite_url(settings.channel_id, settings.telegram_invite_url)
        self.subscribe_url = mailing_signup_url(username)

    async def _reply(self, chat_id: int, text: str, *, post: bool = False) -> None:
        await self.telegram.call(
            "sendMessage",
            **message_payload(
                chat_id, text,
                invite_url=self.invite_url if post else "",
                subscribe_url=self.subscribe_url if post else "",
            ),
        )

    def _welcome(self) -> str:
        name = escape(self.settings.channel_name[:100])
        text = (
            f"<b>{name}</b>\n\n"
            "Web3·블록체인과 생성 예술·컴퓨테이셔널 디자인의 접점을 최우선으로 살펴봅니다. "
            "AI 디자인 소식과 관련 논문·투자 및 지원 소식·작품과 실험도 "
            "한국어 핵심과 원문 링크로 전합니다.\n\n"
            "/latest — 최신 브리핑 최대 3건\n"
            "/tools — 추천 GitHub 도구와 활용법\n"
            "/sources — 수집 출처\n"
            "/invite — 텔레그램 방 초대 링크\n"
            "/share — 친구에게 공유할 방·메일링 가입 링크\n"
            "/subscribe — 이메일 브리핑 가입\n"
            "/language — 이메일 수신 언어 선택\n"
            "/unsubscribe — 이메일 브리핑 수신 거부\n"
            "/cancel — 이메일 입력·수신 거부 취소\n"
            "/help — 이용 안내\n\n"
            "최신 브리핑은 1시간 동안 함께 사용하며, 개인별 요청 간격은 1분입니다."
        )
        channel = self.settings.channel_id
        if self.invite_url:
            text += f'\n\n<a href="{escape(self.invite_url, quote=True)}">텔레그램 방 참여</a>'
        elif channel:
            text += "\n\n연결된 채널의 초대 링크는 채널 운영자에게 확인하세요."
        else:
            text += (
                "\n\n아직 방송 채널이 연결되지 않았습니다. Telegram 앱에서 ‘새 채널’을 만든 뒤 "
                "이 봇을 관리자로 추가하고 ‘메시지 게시’ 권한을 켜세요. "
                "운영자가 채널 ID를 설정하면 예약 브리핑이 시작됩니다. "
                "봇 토큰만으로 채널을 새로 만들 수는 없습니다."
            )
        text += (
            "\n\nEmail briefings: /subscribe. Choose your email language with /language en "
            "(ko, en, ja, zh, de, fr, es, pt, bilingual)."
        )
        return text

    async def _invite(self, chat_id: int) -> None:
        if self.invite_url:
            await self._reply(
                chat_id, f'<a href="{escape(self.invite_url, quote=True)}">텔레그램 방 초대 링크</a>',
            )
        else:
            await self._reply(chat_id, "방 초대 링크가 아직 설정되지 않았습니다. 운영자에게 확인해 주세요.")

    async def _share(self, chat_id: int) -> None:
        reply = (
            f"<b>{escape(self.settings.channel_name[:100])}</b>\n"
            "Web3·AI·컴퓨테이셔널 디자인 소식을 한국어 핵심과 원문 링크로 받아보세요.\n"
            "친구에게 아래 가입 링크를 공유해 주세요.\n"
        )
        if self.invite_url:
            reply += (
                f'\n텔레그램 방 참여: <a href="{escape(self.invite_url, quote=True)}">'
                f"{escape(self.invite_url)}</a>"
            )
        if self.subscribe_url:
            reply += (
                f'\n이메일 브리핑 가입: <a href="{escape(self.subscribe_url, quote=True)}">'
                f"{escape(self.subscribe_url)}</a>"
            )
        await self._reply(chat_id, reply, post=True)

    async def _subscribe(self, user_id: int, email: str = "") -> None:
        subscribed = False
        with MailingStore(self.settings.database_path) as store:
            store.pop_pending_unsubscribe(user_id)
            store.clear_pending_language(user_id)
            if not email:
                store.set_awaiting_email(user_id, True)
                reply = (
                    "Computational Web3 브리핑을 받을 본인의 이메일 주소를 이 개인 대화에 입력해 주세요.\n"
                    "주소를 보내면 브리핑 이메일 수신에 동의하며, /unsubscribe로 언제든 수신을 중단할 수 있습니다.\n"
                    "가입 이후 새로 게시되는 브리핑부터 보내드립니다.\n"
                    "다른 이메일을 등록하면 이 계정의 기존 이메일 구독은 새 주소로 바뀝니다.\n"
                    "가입을 취소하려면 /cancel을 입력하세요.\n\n"
                    "Send your own email here to subscribe. Sending your address opts you in; "
                    "/unsubscribe stops emails and /cancel cancels signup.\n"
                    "New subscribers receive Korean + English. After signup, choose a language with /language en."
                )
            else:
                try:
                    language = store.get_language(user_id) or "bilingual"
                    result = store.subscribe(email, telegram_user_id=user_id, language=language)
                except ValueError:
                    store.set_awaiting_email(user_id, True)
                    reply = "이메일 주소 하나를 정확히 입력해 주세요. 예: name@example.com\n취소: /cancel"
                else:
                    store.set_awaiting_email(user_id, False)
                    if result == "subscribed":
                        subscribed = True
                        reply = (
                            "메일링 리스트에 가입했습니다. 가입 이후 새로 게시되는 브리핑부터 보내드립니다.\n"
                            "이 계정의 기존 이메일 구독이 있었다면 새 주소로 바뀌었습니다.\n"
                            "/unsubscribe로 언제든 수신을 중단할 수 있습니다.\n"
                            "친구에게 가입 링크를 알려주려면 /share를 입력하세요."
                        )
                        language = store.get_language(user_id) or "bilingual"
                        reply += (
                            f"\n수신 언어 / Email language: {escape(LANGUAGE_NAMES[language])}\n"
                            "다른 언어 선택 / Change language: /language en (한국어: /language ko).\n"
                            "Subscribed. You will receive new briefings after signup; /unsubscribe stops emails."
                        )
                        if (
                            not self.settings.mailing_enabled
                            or not self.settings.smtp_host
                            or not self.settings.smtp_from
                        ):
                            reply += "\n메일 발송 준비가 완료되면 새 브리핑을 보내드립니다."
                    elif result == "email_in_use":
                        reply = (
                            "이 주소로는 이 계정의 구독을 등록하거나 변경할 수 없습니다.\n"
                            "기존 수신 상태는 바뀌지 않았습니다. 본인의 다른 이메일 주소로 /subscribe를 입력해 주세요.\n"
                            "수신 거부는 받은 메일의 링크를 이용하세요."
                        )
                    else:
                        reply = (
                            "이미 등록된 이메일입니다. 기존 가입은 유지됩니다.\n"
                            "이 계정에서 가입했다면 /unsubscribe, 그렇지 않다면 받은 메일의 수신 거부 링크를 이용하세요.\n"
                            "수신 언어 선택 / Choose email language: /language."
                        )
        await self._reply(user_id, reply, post=subscribed)

    @staticmethod
    def _language_choices() -> str:
        return "\n".join(f"/language {code} — {escape(name)}" for code, name in LANGUAGE_NAMES.items())

    async def _language(self, user_id: int, argument: str = "") -> None:
        if argument:
            try:
                language = normalize_language(argument)
            except ValueError:
                await self._reply(
                    user_id,
                    "지원하는 언어 코드 하나를 입력하세요. / Enter one supported language code.\n"
                    + self._language_choices(),
                )
                return
        else:
            language = ""
        with MailingStore(self.settings.database_path) as store:
            pending = store.get_pending_language(user_id)
            if pending is None and store.get_pending_unsubscribe(user_id) is not None:
                reply = (
                    "현재 메일의 수신 거부를 확인 중입니다. 언어 변경은 해당 메일의 언어 설정 링크를 열어 주세요.\n"
                    "본인 구독을 관리하려면 /cancel로 확인을 종료하세요.\n"
                    "Open the language link in that email, or use /cancel to manage your own subscription."
                )
            elif pending is not None and time.time() - pending[1] > 900:
                reply = (
                    "언어 설정 확인 시간이 만료되었습니다. 받은 메일의 언어 설정 링크를 다시 열어 주세요.\n"
                    "Language link expired. Reopen the language link in your email.\n"
                    "다른 구독 관리 / Manage another subscription: /cancel."
                )
            else:
                current = store.get_language_token(pending[0]) if pending else store.get_language(user_id)
                if current is None:
                    reply = (
                        "설정할 활성 이메일 구독을 확인하지 못했습니다. / No active email subscription found.\n"
                        "가입하려면 /subscribe, 엑셀 등으로 등록했다면 받은 메일의 언어 설정 링크를 이용하세요.\n"
                        "Use /subscribe, or open the language link in your email.\n\n"
                        + self._language_choices()
                    )
                elif not language:
                    reply = (
                        f"수신 언어 / Current email language: {escape(LANGUAGE_NAMES[current])}\n\n"
                        "수신 언어를 선택하세요. / Choose your email language.\n"
                        + self._language_choices()
                    )
                else:
                    changed = (
                        store.set_language_token(pending[0], language)
                        if pending else store.set_language(user_id, language)
                    )
                    if changed:
                        reply = (
                            f"수신 언어를 설정했습니다. / Email language set: {escape(LANGUAGE_NAMES[language])}\n"
                            "다음 메일부터 적용됩니다. / Applies to future emails."
                        )
                        if pending:
                            reply += "\n다른 구독 관리 / Manage another subscription: /cancel."
                    else:
                        reply = "활성 이메일 구독을 확인하지 못했습니다. / No active email subscription found."
        await self._reply(user_id, reply)

    async def _start_language(self, user_id: int, token: str) -> None:
        with MailingStore(self.settings.database_path) as store:
            store.set_awaiting_email(user_id, False)
            store.pop_pending_unsubscribe(user_id)
            store.set_pending_language(user_id, token, time.time())
        await self._language(user_id)

    async def _unsubscribe(self, user_id: int) -> None:
        with MailingStore(self.settings.database_path) as store:
            # Keep the target until cancelled or changed: a replay after a restart
            # must not fall through to a different, account-linked mailbox.
            pending = store.get_pending_unsubscribe(user_id)
            store.set_awaiting_email(user_id, False)
            if pending is None and store.get_pending_language(user_id) is not None:
                reply = (
                    "현재 메일의 언어 설정 중입니다. 수신 거부는 해당 메일의 수신 거부 링크를 열어 주세요.\n"
                    "본인 구독을 관리하려면 /cancel로 확인을 종료하세요.\n"
                    "Open the unsubscribe link in that email, or use /cancel to manage your own subscription."
                )
            elif pending is not None:
                token, created_at = pending
                if time.time() - created_at > 900:
                    reply = (
                        "확인 시간이 만료되었습니다. 메일의 수신 거부 링크를 다시 열어 주세요.\n"
                        "다른 구독을 관리하려면 /cancel로 확인을 종료하세요."
                    )
                elif store.unsubscribe_token(token):
                    reply = (
                        "이메일 브리핑 수신을 중단했습니다.\n"
                        "다른 구독을 관리하려면 /cancel로 확인을 종료하세요."
                    )
                else:
                    reply = "유효한 수신 거부 링크를 확인하지 못했습니다. 받은 메일의 링크를 다시 확인해 주세요."
            elif store.unsubscribe_user(user_id):
                reply = "이메일 브리핑 수신을 중단했습니다. 다시 가입하려면 /subscribe를 입력하세요."
            else:
                reply = (
                    "이 텔레그램 계정으로 가입한 이메일이 없습니다.\n"
                    "엑셀 목록 등으로 가입했다면 받은 메일의 수신 거부 링크를 이용해 주세요."
                )
        await self._reply(user_id, reply)

    async def _start_unsubscribe(self, user_id: int, token: str) -> None:
        with MailingStore(self.settings.database_path) as store:
            store.set_awaiting_email(user_id, False)
            store.clear_pending_language(user_id)
            store.set_pending_unsubscribe(user_id, token, time.time())
        await self._reply(
            user_id,
            "이 메일링의 이메일 수신을 중단하려면 /unsubscribe를 입력해 주세요.\n취소: /cancel",
        )

    async def _cancel(self, user_id: int) -> None:
        with MailingStore(self.settings.database_path) as store:
            store.pop_pending_unsubscribe(user_id)
            store.clear_pending_language(user_id)
            store.set_awaiting_email(user_id, False)
        await self._reply(user_id, "입력을 취소했습니다. 다시 가입하려면 /subscribe를 입력하세요.")

    def _sources(self) -> str:
        text = "<b>브리핑 수집 출처</b>\nWeb3·블록체인 디자인 소식을 우선 선별합니다.\n"
        lines = [f"\n• {escape(s.name[:100])}" for s in load_sources(self.settings.sources_file) if s.enabled]
        if self.settings.repositories_file:
            repositories = load_repositories(self.settings.repositories_file)
            lines += ["\n\nGitHub 공식 릴리스 (/tools에서 활용법 확인)"]
            lines += [f"\n• {escape(r.full_name)}" for r in repositories if r.enabled]
        for line in lines:
            # Telegram uses UTF-16 code units; count escaped markup conservatively.
            if len((text + line).encode("utf-16-le")) // 2 > 3900:
                text += "\n• 그 밖의 설정된 출처"
                break
            text += line
        return text

    def _tools(self) -> list[str]:
        if not self.settings.repositories_file:
            return ["아직 추천 GitHub 목록이 설정되지 않았습니다."]
        messages = []
        text = "<b>추천 GitHub 도구</b>\n오래 두고 살펴볼 도구와 활용법입니다.\n"
        for repo in load_repositories(self.settings.repositories_file):
            if not repo.enabled:
                continue
            card = f"\n<b>{escape(repo.full_name)}</b>\n{escape(repo.why[:180])}\n"
            links = []
            for label, candidate in (("GitHub", repo.url), ("문서", repo.docs_url), ("예제", repo.example_url)):
                url = public_resource_url(candidate)
                if url:
                    link = f'<a href="{escape(url, quote=True)}">{label}</a>'
                    if len((card + " · ".join(links + [link])).encode("utf-16-le")) // 2 <= 3000:
                        links.append(link)
            card += " · ".join(links) + "\n"
            if len((text + card).encode("utf-16-le")) // 2 > 3600:
                messages.append(text)
                text = "<b>추천 GitHub 도구 · 계속</b>\n"
            text += card
        text += "\n실행 환경과 라이선스는 저장소에서 확인하세요. 코드 실행을 검증한 목록은 아닙니다."
        messages.append(text)
        return messages

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
            await self._reply(chat_id, message, post=True)

    async def handle(self, update: dict) -> None:
        message = update.get("message")
        if not isinstance(message, dict):
            return
        chat = message.get("chat")
        if not isinstance(chat, dict) or type(chat.get("id")) is not int:
            return
        sender = message.get("from", {})
        if isinstance(sender, dict) and sender.get("is_bot"):
            return
        text = message.get("text")
        if not isinstance(text, str):
            return
        match = re.match(r"^/([a-zA-Z]+)(?:@([a-zA-Z0-9_]+))?(?:\s|$)", text)
        if match is not None and match[2] and match[2].casefold() != self.username:
            return
        command = match[1].casefold() if match else ""
        argument = text[match.end():].strip() if match else ""
        chat_id = chat["id"]
        user_id = sender.get("id") if isinstance(sender, dict) else None
        verified_private = (
            chat.get("type") == "private" and type(user_id) is int and user_id == chat_id and user_id > 0
        )
        try:
            if chat.get("type") != "private":
                if chat.get("type") not in {"group", "supergroup"}:
                    return
                private_management_start = command == "start" and argument.startswith(("language_", "unsubscribe_"))
                if command in {"subscribe", "unsubscribe", "language"} or private_management_start or (
                    command == "start" and argument == "subscribe"
                ):
                    # Never parse, retain or repeat an email posted in a group.
                    link = (
                        self.subscribe_url.removesuffix("?start=subscribe")
                        if command in {"unsubscribe", "language"} or private_management_start else self.subscribe_url
                    )
                    reply = "이메일 주소는 봇과의 개인 대화에서 입력해 주세요."
                    if link:
                        reply += f'\n<a href="{escape(link, quote=True)}">개인 대화에서 메일링 관리</a>'
                    await self._reply(chat_id, reply)
                elif command == "invite":
                    await self._invite(chat_id)
                elif command == "share":
                    await self._share(chat_id)
                return
            if command == "subscribe" or (command == "start" and argument == "subscribe"):
                if verified_private:
                    await self._subscribe(user_id, argument if command == "subscribe" else "")
            elif command == "start" and argument.startswith("unsubscribe_"):
                token = argument.removeprefix("unsubscribe_")
                if verified_private and re.fullmatch(r"[A-Za-z0-9_-]{20,52}", token):
                    await self._start_unsubscribe(user_id, token)
                else:
                    await self._reply(chat_id, "수신 거부 링크를 확인해 주세요.")
            elif command == "start" and argument.startswith("language_"):
                token = argument.removeprefix("language_")
                if verified_private and re.fullmatch(r"[A-Za-z0-9_-]{20,52}", token):
                    await self._start_language(user_id, token)
                else:
                    await self._reply(chat_id, "언어 설정 링크를 확인해 주세요. / Check the language link in your email.")
            elif command == "language":
                if verified_private:
                    await self._language(user_id, argument)
            elif command == "unsubscribe":
                if verified_private:
                    await self._unsubscribe(user_id)
            elif command == "cancel":
                if verified_private:
                    await self._cancel(user_id)
            elif command in {"start", "help"}:
                await self._reply(chat_id, self._welcome(), post=True)
            elif command == "invite":
                await self._invite(chat_id)
            elif command == "share":
                await self._share(chat_id)
            elif command == "sources":
                await self._reply(chat_id, self._sources())
            elif command == "tools":
                for text in self._tools():
                    await self._reply(chat_id, text)
            elif command == "latest":
                await self._latest(chat_id)
            elif (
                match is None and not text.startswith("/") and verified_private
                and self.settings.database_path.exists()
            ):
                with MailingStore(self.settings.database_path) as store:
                    awaiting = store.awaiting_email(user_id)
                if awaiting:
                    await self._subscribe(user_id, text.strip())
        except TelegramError:
            # An uncertain reply must not be replayed automatically; mark the update handled.
            log.warning("개인 명령 응답을 전달하지 못했습니다.")
        except (OSError, ValueError, sqlite3.Error):
            log.warning("개인 명령 설정을 읽지 못했습니다.")
            try:
                await self._reply(chat_id, "지금은 요청을 처리하지 못했습니다. 잠시 후 다시 시도해 주세요.")
            except TelegramError:
                log.warning("개인 명령 응답을 전달하지 못했습니다.")


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
