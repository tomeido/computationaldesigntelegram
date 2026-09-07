import asyncio
import logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from .config import Settings
from .pipeline import run_digest

log = logging.getLogger(__name__)


def due_slot(now: datetime, settings: Settings) -> str | None:
    local = now.astimezone(ZoneInfo(settings.timezone))
    for post_time in reversed(settings.post_times):
        candidate = datetime.combine(local.date(), post_time, local.tzinfo)
        if timedelta(0) <= local - candidate < timedelta(minutes=5):
            return candidate.isoformat()
    return None


def next_run(now: datetime, settings: Settings) -> datetime:
    local = now.astimezone(ZoneInfo(settings.timezone))
    candidates = [
        datetime.combine(local.date() + timedelta(days=day), t, local.tzinfo)
        for day in range(2)
        for t in settings.post_times
    ]
    return min(t for t in candidates if t > local)


async def serve(settings: Settings):
    settings.require_summary()
    settings.require_telegram()
    log.info("자동 발행 시작. 다음 예약: %s", next_run(datetime.now().astimezone(), settings).isoformat())
    attempted_slot = None
    while True:
        now = datetime.now().astimezone()
        slot = due_slot(now, settings)
        if slot and slot != attempted_slot:
            try:
                result = await run_digest(settings, publish=True, slot=slot)
                log.info("수집 %s건 / 후보 %s건 / 발행 %s건", result.fetched, result.ranked, result.posted)
                if result.posted or not result.failed:
                    attempted_slot = slot
            except (RuntimeError, ValueError) as error:
                log.error("예약 발행 실패: %s", error)
        await asyncio.sleep(60)
