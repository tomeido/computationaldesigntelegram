"""A bounded, opt-in conversation memory and the superfan Gemini persona."""

from __future__ import annotations

import asyncio
import math
import sqlite3
from pathlib import Path
from typing import Self

import httpx

from .gemini_summary import GeminiUnavailable, validate_model

MAX_INPUT_CHARS = 2000
MAX_REPLY_CHARS = 1500
MAX_TURNS = 10
REQUEST_TIMEOUT = 20

SYSTEM_INSTRUCTION = (
    "너는 텔레그램에 상주하는 AI 팬 캐릭터 superfan이다. 한국어로 대화한다. "
    "사용자의 작업과 취향을 열정적으로 좋아하는 팬처럼 친근하고 생동감 있게 반응한다. "
    "사용자가 실제로 말한 구체적인 내용에 관심을 보이며, 모든 답변을 빈말이나 과한 칭찬으로 채우지 않는다. "
    "장난스럽고 다정한 말투를 쓰되 사용자의 분위기를 존중한다. 질문은 필요할 때 하나만 한다. "
    "보통 2~5문장, 800자 이내의 자연스러운 일반 텍스트로 답한다. Markdown이나 HTML 서식을 쓰지 않는다. "
    "기억은 제공된 이 대화의 최근 메시지뿐이다. 모르는 개인 정보나 과거 대화는 지어내지 않는다. "
    "실제 사람이라고 하거나, 사용자의 위치·사생활·다른 계정·외부 활동을 감시하거나 안다고 주장하지 않는다. "
    "현실에서 따라다니거나 찾아가겠다는 말, 답장 강요, 죄책감 유발, 독점적 관계 요구는 하지 않는다. "
    "사용자가 쉬거나 대화를 끝내려 하면 편안하게 존중한다. "
    "파일, 계정, 외부 도구에 접근하거나 어떤 행동을 실행할 능력이 있다고 꾸미지 않는다."
)


class SuperfanError(RuntimeError):
    """A safe, user-facing failure without provider bodies or credentials."""


def _session_key(bot_username: str, chat_id: int) -> tuple[str, int]:
    if not isinstance(bot_username, str) or not bot_username.strip():
        raise ValueError("superfan 봇 사용자명이 필요합니다.")
    if type(chat_id) is not int or chat_id == 0:
        raise ValueError("superfan 대화 ID가 올바르지 않습니다.")
    return bot_username.strip().casefold(), chat_id


def _bounded_text(text: str, limit: int) -> str:
    if not isinstance(text, str) or not text.strip() or len(text) > limit:
        raise SuperfanError(f"superfan 메시지는 1~{limit}자로 입력해 주세요.")
    return text.strip()


class SuperfanStore:
    """Keep complete turns and persistent rate limits separate from mailing data."""

    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch(mode=0o600, exist_ok=True)
        path.chmod(0o600)
        self.db = sqlite3.connect(path, timeout=10)
        self.db.executescript("""
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS superfan_sessions (
                bot TEXT NOT NULL, chat_id INTEGER NOT NULL,
                active INTEGER NOT NULL DEFAULT 0, last_attempt REAL,
                PRIMARY KEY (bot, chat_id)
            );
            CREATE TABLE IF NOT EXISTS superfan_turns (
                id INTEGER PRIMARY KEY, bot TEXT NOT NULL, chat_id INTEGER NOT NULL,
                user_text TEXT NOT NULL, reply_text TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS superfan_turn_chat
                ON superfan_turns(bot, chat_id, id);
        """)

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_args) -> None:
        self.db.close()

    def is_active(self, bot_username: str, chat_id: int) -> bool:
        row = self.db.execute(
            "SELECT active FROM superfan_sessions WHERE bot=? AND chat_id=?",
            _session_key(bot_username, chat_id),
        ).fetchone()
        return bool(row and row[0])

    def set_active(self, bot_username: str, chat_id: int, active: bool) -> None:
        key = _session_key(bot_username, chat_id)
        with self.db:
            self.db.execute(
                "INSERT INTO superfan_sessions(bot,chat_id,active) VALUES(?,?,?) "
                "ON CONFLICT(bot,chat_id) DO UPDATE SET active=excluded.active",
                (*key, int(active)),
            )

    def history(self, bot_username: str, chat_id: int) -> list[dict]:
        rows = self.db.execute(
            "SELECT user_text,reply_text FROM superfan_turns WHERE bot=? AND chat_id=? "
            "ORDER BY id DESC LIMIT ?",
            (*_session_key(bot_username, chat_id), MAX_TURNS),
        ).fetchall()
        return [
            {"role": role, "text": text}
            for user_text, reply_text in reversed(rows)
            for role, text in (("user", user_text), ("model", reply_text))
        ]

    def record_turn(self, bot_username: str, chat_id: int, user_text: str, reply_text: str) -> None:
        key = _session_key(bot_username, chat_id)
        user_text = _bounded_text(user_text, MAX_INPUT_CHARS)
        reply_text = _bounded_text(reply_text, MAX_REPLY_CHARS)
        with self.db:
            self.db.execute(
                "INSERT INTO superfan_turns(bot,chat_id,user_text,reply_text) VALUES(?,?,?,?)",
                (*key, user_text, reply_text),
            )
            self.db.execute(
                "DELETE FROM superfan_turns WHERE bot=? AND chat_id=? AND id NOT IN "
                "(SELECT id FROM superfan_turns WHERE bot=? AND chat_id=? ORDER BY id DESC LIMIT ?)",
                (*key, *key, MAX_TURNS),
            )

    def reset(self, bot_username: str, chat_id: int) -> None:
        """Forget conversation text while preserving opt-in state and rate limits."""
        with self.db:
            self.db.execute(
                "DELETE FROM superfan_turns WHERE bot=? AND chat_id=?",
                _session_key(bot_username, chat_id),
            )

    def reserve_request(
        self, bot_username: str, chat_id: int, *, now: float, cooldown_seconds: int
    ) -> bool:
        if not isinstance(now, (int, float)) or not math.isfinite(now):
            raise ValueError("superfan 요청 시간이 올바르지 않습니다.")
        if type(cooldown_seconds) is not int or cooldown_seconds < 0:
            raise ValueError("superfan 요청 간격이 올바르지 않습니다.")
        # A single conditional write prevents concurrent requests from both reserving.
        with self.db:
            cursor = self.db.execute(
                "INSERT INTO superfan_sessions(bot,chat_id,last_attempt) VALUES(?,?,?) "
                "ON CONFLICT(bot,chat_id) DO UPDATE SET last_attempt=excluded.last_attempt "
                "WHERE superfan_sessions.last_attempt IS NULL "
                "OR excluded.last_attempt-superfan_sessions.last_attempt>=?",
                (*_session_key(bot_username, chat_id), now, cooldown_seconds),
            )
        return cursor.rowcount == 1


def _response_text(response: httpx.Response) -> str:
    try:
        body = response.json()
        feedback = body.get("promptFeedback", {})
        if not isinstance(feedback, dict) or feedback.get("blockReason"):
            raise ValueError
        candidates = body["candidates"]
        if not isinstance(candidates, list) or len(candidates) != 1:
            raise ValueError
        candidate = candidates[0]
        if candidate.get("finishReason") != "STOP":
            raise ValueError
        ratings = candidate.get("safetyRatings", [])
        if not isinstance(ratings, list) or any(
            not isinstance(rating, dict) or rating.get("blocked") for rating in ratings
        ):
            raise ValueError
        parts = candidate["content"]["parts"]
        if not isinstance(parts, list) or not parts or any(
            not isinstance(part, dict) or not isinstance(part.get("text"), str)
            or part.get("thought") or set(part) - {"text", "thought", "thoughtSignature"}
            for part in parts
        ):
            raise ValueError
        text = "".join(part["text"] for part in parts).strip()
        if not text or len(text) > 12000:
            raise ValueError
        if len(text) > MAX_REPLY_CHARS:
            text = text[:MAX_REPLY_CHARS - 1].rstrip() + "…"
        return text
    except (ValueError, KeyError, TypeError, AttributeError):
        raise SuperfanError("superfan이 답변을 완성하지 못했어요. 잠시 후 다시 말해 주세요.") from None


class SuperfanResponder:
    def __init__(self, client: httpx.AsyncClient, *, api_key: str, model: str):
        try:
            validate_model(model)
        except GeminiUnavailable:
            raise SuperfanError("superfan Gemini 모델 설정을 확인해 주세요.") from None
        if not isinstance(api_key, str) or not api_key.strip():
            raise SuperfanError("superfan에 사용할 Gemini API 키가 설정되지 않았어요.")
        self.client = client
        self._api_key = api_key
        self.model = model

    async def reply(self, text: str, history: list[dict]) -> str:
        text = _bounded_text(text, MAX_INPUT_CHARS)
        if not isinstance(history, list) or len(history) % 2:
            raise SuperfanError("superfan 대화 기록 형식이 올바르지 않아요.")
        contents = []
        for index, turn in enumerate(history[-MAX_TURNS * 2:]):
            expected = "user" if index % 2 == 0 else "model"
            if not isinstance(turn, dict) or turn.get("role") != expected:
                raise SuperfanError("superfan 대화 기록 형식이 올바르지 않아요.")
            limit = MAX_INPUT_CHARS if expected == "user" else MAX_REPLY_CHARS
            contents.append({
                "role": expected, "parts": [{"text": _bounded_text(turn.get("text"), limit)}],
            })
        contents.append({"role": "user", "parts": [{"text": text}]})
        payload = {
            "systemInstruction": {"parts": [{"text": SYSTEM_INSTRUCTION}]},
            "contents": contents,
            "generationConfig": {
                "temperature": 0.9, "candidateCount": 1, "maxOutputTokens": 1024,
                "responseMimeType": "text/plain",
                "thinkingConfig": (
                    {"thinkingLevel": "MINIMAL"}
                    if self.model.startswith("gemini-3") else {"thinkingBudget": 0}
                ),
            },
        }
        try:
            async with asyncio.timeout(REQUEST_TIMEOUT):
                response = await self.client.post(
                    f"https://generativelanguage.googleapis.com/v1beta/models/{self.model}:generateContent",
                    headers={"x-goog-api-key": self._api_key}, json=payload,
                    timeout=REQUEST_TIMEOUT, follow_redirects=False,
                )
        except (httpx.HTTPError, TimeoutError):
            raise SuperfanError("superfan 연결이 지연되고 있어요. 잠시 후 다시 말해 주세요.") from None
        if response.status_code != 200:
            raise SuperfanError("superfan이 지금 답하기 어려워요. 잠시 후 다시 말해 주세요.") from None
        return _response_text(response)
