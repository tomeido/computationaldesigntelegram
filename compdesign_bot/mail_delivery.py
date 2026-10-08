"""Connect published Telegram posts to the durable recipient mail queue."""

import asyncio
import logging
import sqlite3

from .config import Settings
from .mail_bounce_sync import BounceSyncUnavailable, sync_mail_bounces
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
            removed = mail.apply_exclusions(settings.mailing_exclusions_path)
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
            return {"subscribers": mail.status_counts(), "outbox": mail.outbox_counts(),
                    "imported": imported, "removed": removed}
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
            if settings.mailing_bounce_enabled:
                bounces = sync_mail_bounces(settings, mail)
                if bounces.backlog or bounces.untrusted:
                    raise BounceSyncUnavailable("반송 확인이 남아 있어 메일 발송을 보류합니다. mail-sync-bounces로 확인하세요.")
            else:
                mail.release_expired_bounce_holds()
            return deliver_pending(settings, mail, bot_username=settings.bot_username)
        finally:
            mail.close()


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
