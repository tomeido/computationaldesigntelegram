"""Connect published Telegram posts to the durable recipient mail queue."""

import asyncio
import logging
import sqlite3

from .config import Settings
from .mail_bounce_sync import BounceSyncUnavailable, sync_mail_bounces
from .mail_unsubscribe_sync import UnsubscribeSyncUnavailable, sync_mail_unsubscribes
from .mailing import MailingStore, deliver_pending
from .storage import Store, job_lock

log = logging.getLogger(__name__)


def sync_mail_queue(settings: Settings) -> dict:
    """Recover posts from Telegram delivery records without fetching or republishing news."""
    with job_lock(settings.database_path.with_suffix(".mail.sqlite3")):
        mail = MailingStore(settings.database_path)
        posts = Store(settings.database_path)
        try:
            imported = None
            if settings.mailing_xlsx_path:
                imported = mail.import_xlsx(settings.mailing_xlsx_path)
            for row in posts.db.execute(
                "SELECT id,title,post_html,created_at FROM deliveries "
                "WHERE status='sent' AND post_html IS NOT NULL "
                "ORDER BY id"
            ):
                mail.store_post(
                    f"telegram:{row['id']}", row["title"], row["post_html"], published_at=row["created_at"],
                )
            return {"subscribers": mail.status_counts(), "outbox": mail.outbox_counts(), "imported": imported}
        finally:
            posts.close()
            mail.close()


def send_mail_queue(settings: Settings):
    settings.require_mail()
    if not settings.telegram_invite_url or not settings.bot_username:
        raise ValueError("메일 발송에는 TELEGRAM_INVITE_URL과 TELEGRAM_BOT_USERNAME 설정이 필요합니다.")
    sync_mail_queue(settings)
    with job_lock(settings.database_path.with_suffix(".mail.sqlite3")):
        mail = MailingStore(settings.database_path)
        try:
            if settings.mailing_unsubscribe_enabled:
                with job_lock(settings.database_path.with_suffix(".unsubscribe.sqlite3"), wait=True):
                    unsubscribes = sync_mail_unsubscribes(settings, mail)
                if unsubscribes.backlog:
                    raise UnsubscribeSyncUnavailable("해지 요청 확인이 남아 있어 메일 발송을 보류합니다.")
            if settings.mailing_bounce_enabled:
                bounces = sync_mail_bounces(settings, mail)
                if bounces.backlog or bounces.untrusted:
                    raise BounceSyncUnavailable("반송 확인이 남아 있어 메일 발송을 보류합니다. mail-sync-bounces로 확인하세요.")
            else:
                mail.release_expired_bounce_holds()
            return deliver_pending(settings, mail, bot_username=settings.bot_username)
        finally:
            mail.close()


def sync_unsubscribe_requests(settings: Settings):
    """Process opt-outs without publishing or sending any messages."""
    settings.require_unsubscribes()
    with (
        job_lock(settings.database_path.with_suffix(".unsubscribe.sqlite3"), wait=True),
        MailingStore(settings.database_path) as mail,
    ):
        return sync_mail_unsubscribes(settings, mail)


async def watch_unsubscribe_requests(settings: Settings):
    """Poll independently of the publication schedule, including at startup."""
    if not settings.mailing_unsubscribe_enabled:
        return
    log.info("이메일 답장 자동 해지 확인을 시작했습니다. 확인 간격: 60초")
    while True:
        try:
            report = await asyncio.to_thread(sync_unsubscribe_requests, settings)
            if report.applied or report.backlog:
                log.info("답장 자동 해지 %s명 / 추가 확인 필요 %s", report.applied, report.backlog)
            if report.oversized:
                log.warning("본문을 완전히 확인할 수 없는 대용량 답장 %s건을 건너뛰었습니다.", report.oversized)
        except (OSError, RuntimeError, ValueError, sqlite3.Error):
            # Credentials, addresses and server text must never appear in logs.
            log.warning("해지 요청 확인을 완료하지 못했습니다. 다음 확인 주기에 다시 시도합니다.")
        await asyncio.sleep(60)


async def auto_send_mail(settings: Settings):
    """A mail configuration/outage must not undo successful Telegram publication."""
    if not settings.mailing_enabled:
        return None
    try:
        await asyncio.to_thread(sync_mail_queue, settings)
        report = await asyncio.to_thread(send_mail_queue, settings)
        log.info(
            "메일 발송 %s통 / 실패 %s통 / 결과 불명 %s통 / 대기 수신자 %s명",
            report.sent, report.failed, report.uncertain, report.deferred,
        )
        if report.paused:
            log.warning("SMTP 서버가 발송을 제한해 중단했습니다. 대기열을 유지하며 mail-status로 확인할 수 있습니다.")
        if report.localization_failed:
            log.warning("언어별 번역을 준비하지 못해 수신자 %s명의 발송을 보류합니다.", report.localization_failed)
        return report
    except sqlite3.Error:
        log.warning("메일 DB를 사용할 수 없어 발송을 보류합니다.")
        return None
    except (OSError, RuntimeError, ValueError) as error:
        log.warning("메일 발송 대기: %s", error)
        return None
