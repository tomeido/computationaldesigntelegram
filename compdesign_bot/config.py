import os
from dataclasses import dataclass, field
from datetime import time
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from dotenv import load_dotenv

from .gemini_summary import DEFAULT_GEMINI_MODEL, GeminiUnavailable, validate_model


def _integer(name: str, default: int, low: int, high: int) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except ValueError:
        raise ValueError(f"{name}: 정수를 입력하세요.") from None
    if not low <= value <= high:
        raise ValueError(f"{name}: {low}~{high} 범위로 입력하세요.")
    return value


@dataclass(frozen=True)
class Settings:
    bot_token: str = field(default="", repr=False)
    channel_id: str = ""
    translation_provider: str = "local"
    gemini_api_key: str = field(default="", repr=False)
    gemini_model: str = DEFAULT_GEMINI_MODEL
    local_model_path: Path = Path("data/models/m2m100")
    channel_name: str = "컴퓨트 디자인 브리핑 | Web3 · AI"
    timezone: str = "Asia/Seoul"
    post_times: tuple[time, ...] = (time(9), time(18))
    max_posts: int = 5
    max_age_days: int = 7
    max_candidates: int = 15
    sources_file: Path = Path("config/sources.json")
    database_path: Path = Path("data/bot.sqlite3")

    def __post_init__(self) -> None:
        if self.translation_provider not in {"local", "gemini"}:
            raise ValueError("TRANSLATION_PROVIDER: local 또는 gemini를 입력하세요.")
        try:
            validate_model(self.gemini_model)
        except GeminiUnavailable:
            raise ValueError("GEMINI_MODEL: 유효한 Gemini 모델 ID를 입력하세요.") from None

    @classmethod
    def from_env(cls) -> "Settings":
        load_dotenv()
        timezone = os.getenv("TIMEZONE", "Asia/Seoul")
        try:
            ZoneInfo(timezone)
        except ZoneInfoNotFoundError:
            raise ValueError("TIMEZONE: 유효한 시간대가 필요합니다. 예: Asia/Seoul") from None
        try:
            times = tuple(
                sorted(
                    {time.fromisoformat(t.strip()) for t in os.getenv("POST_TIMES", "09:00,18:00").split(",")}
                )
            )
            if any(t.tzinfo or t.second or t.microsecond for t in times):
                raise ValueError
        except ValueError:
            raise ValueError("POST_TIMES: 09:00,18:00 형식으로 입력하세요.") from None
        return cls(
            bot_token=os.getenv("TELEGRAM_BOT_TOKEN", "").strip(),
            channel_id=os.getenv("TELEGRAM_CHANNEL_ID", "").strip(),
            translation_provider=os.getenv("TRANSLATION_PROVIDER", "local").strip().lower(),
            gemini_api_key=os.getenv("GEMINI_API_KEY", "").strip(),
            gemini_model=os.getenv("GEMINI_MODEL", cls.gemini_model).strip(),
            local_model_path=Path(os.getenv("LOCAL_MODEL_PATH", "data/models/m2m100")),
            channel_name=os.getenv("CHANNEL_NAME", cls.channel_name).strip()[:100],
            timezone=timezone,
            post_times=times,
            max_posts=_integer("MAX_POSTS", 5, 1, 10),
            max_age_days=_integer("MAX_AGE_DAYS", 7, 1, 30),
            max_candidates=_integer("MAX_CANDIDATES", 15, 1, 30),
            sources_file=Path(os.getenv("SOURCES_FILE", "config/sources.json")),
            database_path=Path(os.getenv("DATABASE_PATH", "data/bot.sqlite3")),
        )

    def require_summary(self) -> None:
        if self.translation_provider == "gemini":
            if not self.gemini_api_key:
                raise ValueError(".env에 GEMINI_API_KEY를 입력하세요.")
            return
        from .local_summary import check_model

        check_model(self.local_model_path)

    def require_token(self) -> None:
        if not self.bot_token:
            raise ValueError(".env에 TELEGRAM_BOT_TOKEN을 입력하세요.")

    def require_telegram(self) -> None:
        if not self.bot_token or not self.channel_id:
            raise ValueError(".env에 TELEGRAM_BOT_TOKEN과 TELEGRAM_CHANNEL_ID를 입력하세요.")
