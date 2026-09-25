"""Connect published Telegram posts to the durable recipient mail queue."""

import asyncio
import logging
import sqlite3

from .config import Settings
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
        log.info("메일 발송 %s건 / 실패 %s건 / 결과 불명 %s건", report.sent, report.failed, report.uncertain)
        return report
    except sqlite3.Error:
        log.warning("메일 DB를 사용할 수 없어 발송을 보류합니다.")
        return None
    except (OSError, RuntimeError, ValueError) as error:
        log.warning("메일 발송 대기: %s", error)
        return None
