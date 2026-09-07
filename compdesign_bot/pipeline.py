import asyncio
import logging
from dataclasses import dataclass

import httpx

from .config import Settings
from .feeds import collect_articles, load_sources
from .formatting import render_post
from .ranking import rank_articles
from .storage import Store, cache_key, job_lock
from .summarizer import Summarizer, SummaryError
from .telegram import DeliveryUncertain, Telegram, TelegramError

log = logging.getLogger(__name__)


@dataclass
class RunResult:
    fetched: int = 0
    ranked: int = 0
    posted: int = 0
    failed: int = 0
    messages: list[str] | None = None


async def run_digest(settings: Settings, *, publish: bool = False, slot: str | None = None) -> RunResult:
    settings.require_summary()
    if publish:
        settings.require_telegram()
    with job_lock(settings.database_path):
        store = Store(settings.database_path)
        try:
            async with httpx.AsyncClient(follow_redirects=False) as client:
                # Feed fetcher follows public feed redirects explicitly; credential APIs never redirect.
                telegram = Telegram(client, settings.bot_token, settings.channel_id)
                channel = store.channel_for(settings.channel_id)
                if publish:
                    access = await telegram.check_access()
                    channel = str(access["id"])
                    store.remember_channel(settings.channel_id, channel)
                if slot and store.slot_done(channel, slot):
                    return RunResult(messages=[])
                remaining = settings.max_posts - (store.slot_usage(channel, slot) if slot else 0)
                if remaining <= 0:
                    if slot:
                        store.complete_slot(channel, slot)
                    return RunResult(messages=[])
                report = await collect_articles(load_sources(settings.sources_file), client)
                for error in report.errors:
                    log.warning("피드: %s", error)
                if not report.articles and report.errors:
                    raise RuntimeError("기사를 수집하지 못했습니다. 피드 연결 상태를 확인하세요.")
                ranked = rank_articles(report.articles, max_age_days=settings.max_age_days)
                result = RunResult(fetched=len(report.articles), ranked=len(ranked), messages=[])
                candidates = [item for item in ranked if not store.seen(item.article, channel)]
                summarizer = Summarizer(client, settings.openai_api_key, settings.openai_model)
                for item in candidates[: settings.max_candidates]:
                    if len(result.messages) >= remaining:
                        break
                    key = cache_key(item.article, settings.openai_model)
                    found, summary = store.get_summary(key)
                    if not found:
                        try:
                            summary = await summarizer.summarize(item)
                        except SummaryError as error:
                            log.error("%s", error)
                            result.failed += 1
                            continue
                        store.cache_summary(key, summary)
                    if summary is None:
                        continue
                    try:
                        post = render_post(item, summary, settings.timezone)
                    except ValueError as error:
                        log.warning("%s", error)
                        result.failed += 1
                        continue
                    if publish:
                        delivery_id = store.reserve(item.article, channel, slot)
                        try:
                            message_id = await telegram.send(post)
                        except DeliveryUncertain:
                            store.uncertain(delivery_id)
                            raise RuntimeError(
                                f"발행 결과 불명: 기록 #{delivery_id}. 채널 확인 후 resolve-delivery로 처리하세요."
                            ) from None
                        except TelegramError:
                            store.release(delivery_id)
                            raise
                        store.sent(delivery_id, message_id)
                        result.posted += 1
                        await asyncio.sleep(1.1)
                    result.messages.append(post)
                # Complete even partially successful slots to enforce the per-slot post budget.
                # Zero-post failures are retried during the scheduler's 5 minute window.
                if publish and slot and (result.posted or not result.failed):
                    store.complete_slot(channel, slot)
                return result
        finally:
            store.close()
