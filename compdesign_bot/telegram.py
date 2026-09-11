import asyncio
from html.parser import HTMLParser
from urllib.parse import urlsplit

import httpx


class _SourceLinkParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.href = None
        self.label = []
        self.url = None

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            self.href = dict(attrs).get("href")
            self.label = []

    def handle_data(self, data):
        if self.href is not None:
            self.label.append(data)

    def handle_endtag(self, tag):
        if tag == "a":
            if "".join(self.label).strip() == "원문 보기" and self.href:
                try:
                    parts = urlsplit(self.href)
                    if parts.scheme in {"http", "https"} and parts.hostname and not parts.username:
                        self.url = self.href
                except ValueError:
                    pass
            self.href = None


def message_payload(chat_id: str | int, text: str) -> dict:
    """Keep source links accessible through a native URL button in both delivery paths."""
    parser = _SourceLinkParser()
    parser.feed(text)
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
        "link_preview_options": {"is_disabled": True},
    }
    if parser.url:
        payload["reply_markup"] = {
            "inline_keyboard": [[{"text": "원문 보기", "url": parser.url}]],
        }
    return payload


class TelegramError(RuntimeError):
    """A definite API rejection: no delivery occurred."""


class DeliveryUncertain(TelegramError):
    """Telegram may have accepted the message; automatic retry would risk duplication."""


class Telegram:
    def __init__(self, client: httpx.AsyncClient, token: str, channel_id: str):
        self.client, self.token, self.channel_id = client, token, channel_id

    async def call(self, method: str, **payload):
        for attempt in range(3):
            try:
                response = await self.client.post(
                    f"https://api.telegram.org/bot{self.token}/{method}",
                    json=payload,
                    timeout=30,
                )
            except httpx.HTTPError:
                raise DeliveryUncertain("텔레그램 연결이 끊겨 결과를 확인할 수 없습니다.") from None
            if response.status_code >= 500:
                raise DeliveryUncertain(f"텔레그램 HTTP {response.status_code}: 전송 결과 확인이 필요합니다.")
            try:
                data = response.json()
                if not isinstance(data, dict):
                    raise TypeError
            except (ValueError, TypeError):
                raise DeliveryUncertain("텔레그램 응답을 해석할 수 없습니다.") from None
            if data.get("ok") is True:
                return data.get("result")
            if data.get("ok") is not False:
                raise DeliveryUncertain("텔레그램 응답에 전송 결과가 없습니다.")
            code = data.get("error_code", response.status_code)
            parameters = data.get("parameters")
            delay = parameters.get("retry_after", 1) if isinstance(parameters, dict) else 1
            if code == 429 and isinstance(delay, (int, float)) and 0 < delay <= 30 and attempt < 2:
                await asyncio.sleep(delay)
                continue
            # Never expose request URLs or response descriptions: they may contain the bot token.
            raise TelegramError(f"텔레그램 API 오류 {code}: 토큰·채널 ID·관리자 권한을 확인하세요.")

    async def check_access(self) -> dict:
        me = await self.call("getMe")
        chat = await self.call("getChat", chat_id=self.channel_id)
        if chat.get("type") != "channel":
            raise TelegramError("TELEGRAM_CHANNEL_ID는 방송용 채널을 가리켜야 합니다.")
        member = await self.call("getChatMember", chat_id=self.channel_id, user_id=me["id"])
        if member.get("status") not in ("administrator", "creator"):
            raise TelegramError("봇을 채널 관리자로 추가하세요.")
        if member.get("status") != "creator" and not member.get("can_post_messages"):
            raise TelegramError("봇의 관리자 권한에서 ‘메시지 게시’를 켜세요.")
        return {"bot": me.get("username"), "channel": chat.get("title"), "id": chat["id"]}

    async def send(self, text: str) -> int:
        result = await self.call(
            "sendMessage",
            **message_payload(self.channel_id, text),
        )
        if not isinstance(result, dict) or not isinstance(result.get("message_id"), int):
            raise DeliveryUncertain("전송 응답에 메시지 ID가 없습니다.")
        return result["message_id"]
