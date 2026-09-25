import smtplib
from dataclasses import asdict, replace
from datetime import UTC, datetime, timedelta
from html import escape
from zipfile import ZipFile

import pytest

from compdesign_bot.config import Settings
from compdesign_bot.mailing import (
    EmailConnectionError,
    EmailDeliveryError,
    EmailDeliveryUncertain,
    MailingStore,
    SMTPMailer,
    build_email,
    deliver_pending,
    normalize_email,
)

NS = 'http://schemas.openxmlformats.org/spreadsheetml/2006/main'
REL = 'http://schemas.openxmlformats.org/officeDocument/2006/relationships'


def workbook(path, sheets):
    with ZipFile(path, 'w') as archive:
        archive.writestr(
            'xl/workbook.xml',
            f'<workbook xmlns="{NS}" xmlns:r="{REL}"><sheets>'
            + ''.join(f'<sheet name="{name}" sheetId="{i}" r:id="rId{i}"/>'
                      for i, name in enumerate(sheets, 1)) + '</sheets></workbook>',
        )
        archive.writestr(
            'xl/_rels/workbook.xml.rels',
            '<Relationships>' + ''.join(
                f'<Relationship Id="rId{i}" Target="worksheets/sheet{i}.xml"/>'
                for i in range(1, len(sheets) + 1)
            ) + '</Relationships>',
        )
        for i, rows in enumerate(sheets.values(), 1):
            xml = f'<worksheet xmlns="{NS}"><sheetData>'
            for number, row in enumerate(rows, 1):
                xml += f'<row r="{number}">'
                for col, value in enumerate(row):
                    xml += (f'<c r="{chr(65 + col)}{number}" t="inlineStr">'
                            f'<is><t>{escape(value)}</t></is></c>')
                xml += '</row>'
            archive.writestr(f'xl/worksheets/sheet{i}.xml', xml + '</sheetData></worksheet>')
    return path


def mail_settings():
    return Settings(
        smtp_host='smtp.example.com', smtp_from='Briefing <briefing@example.com>',
        smtp_username='sender', smtp_password='SMTP_SECRET', bot_username='design_test_bot',
        telegram_invite_url='https://t.me/+room', mailing_enabled=True,
    )


@pytest.mark.parametrize('value,expected', [
    (' User.Name+Design@EXAMPLE.COM ', 'user.name+design@example.com'),
    ('mailto:person%2Btag@example.com?subject=Hello', 'person+tag@example.com'),
    ('Design <person@example.com>', 'person@example.com'),
    ('user@bücher.example', 'user@xn--bcher-kva.example'),
])
def test_email_normalization(value, expected):
    assert normalize_email(value) == expected


@pytest.mark.parametrize('value', [
    '', 'not-an-email', 'a@localhost', '.a@example.com', 'a..b@example.com',
    'a@-example.com', 'a@example..com', 'a@example.com\nBcc: hidden@example.com',
    'mailto:a@example.com%0d%0aBcc:evil@example.com', '한글@example.com',
    'a@example.com,b@example.com',
])
def test_invalid_email_rejected_without_echoing_address(value):
    with pytest.raises(ValueError) as error:
        normalize_email(value)
    assert '@' not in str(error.value)


def test_checked_sheet_import_holds_review_and_ignores_raw_or_note_addresses(tmp_path):
    path = workbook(tmp_path / 'list.xlsx', {
        '연락처': [['대표 이메일'], ['old@example.com']],
        '발송 점검': [
            ['메일링 목록'], ['이메일', '사용 판단', '메모'],
            ['OK@EXAMPLE.com', '기본 점검 통과', 'sender@example.com'],
            ['ok@example.com', '기본 점검 통과', ''],
            ['review@example.com', '추가 확인', ''],
            ['excluded@example.com', '발송 제외', ''],
            ['bad@-example.com', '기본 점검 통과', ''],
        ],
    })
    with MailingStore(tmp_path / 'mail.db') as store:
        result = store.import_xlsx(path)
        assert asdict(result) == {'imported': 1, 'duplicates': 1, 'invalid': 1, 'suppressed': 0, 'excluded': 2}
        assert store.status_counts() == {'active': 1, 'unsubscribed': 0}
        assert 'example.com' not in str(result)
        token = store.db.execute('SELECT token FROM mailing_subscribers').fetchone()[0]
        assert store.unsubscribe_token(token)
        again = store.import_xlsx(path)
        assert again.imported == 0
        assert again.suppressed == 2


def test_xlsx_shared_strings_and_mailto_hyperlinks(tmp_path):
    path = workbook(tmp_path / 'list.xlsx', {'Contacts': [['Email'], ['placeholder']]})
    with ZipFile(path, 'a') as archive:
        archive.writestr('xl/sharedStrings.xml', f'<sst xmlns="{NS}"><si><t>person@example.com</t></si></sst>')
        archive.writestr('xl/worksheets/_rels/sheet1.xml.rels',
                        '<Relationships><Relationship Id="rId1" '
                        'Target="mailto:linked%2Bperson@example.com?subject=test"/></Relationships>')
        # Use a new sheet to avoid duplicate ZIP entries.
        archive.writestr('xl/worksheets/sheet2.xml', f'''
            <worksheet xmlns="{NS}" xmlns:r="{REL}"><sheetData>
            <row><c r="A1" t="s"><v>0</v></c><c r="B1" t="str"><v>Person</v></c></row>
            </sheetData><hyperlinks><hyperlink ref="B1" r:id="rId1"/></hyperlinks></worksheet>''')
        archive.writestr('xl/worksheets/_rels/sheet2.xml.rels',
                        archive.read('xl/worksheets/_rels/sheet1.xml.rels'))
        from compdesign_bot.mailing import _sheet_rows
        rows = _sheet_rows(archive, 'xl/worksheets/sheet2.xml', ['person@example.com'])
    assert rows == [{'A': 'person@example.com', 'B': 'linked+person@example.com'}]


def test_headerless_xlsx_deduplicates_addresses_across_columns(tmp_path):
    path = workbook(tmp_path / 'list.xlsx', {'Sheet1': [
        ['first@example.com', 'mailto:second@example.com; FIRST@example.com'],
    ]})
    with MailingStore(tmp_path / 'mail.db') as store:
        result = store.import_xlsx(path)
        assert result.imported == 2
        assert result.duplicates == 1


def test_membership_owner_changes_and_unsubscribe_survive_restart(tmp_path):
    path = tmp_path / 'mail.db'
    with MailingStore(path) as store:
        assert store.subscribe('first@example.com', 101) == 'subscribed'
        assert store.subscribe('first@example.com', 102) == 'email_in_use'
        assert store.subscribe('second@example.com', 101) == 'subscribed'
        assert store.status_counts() == {'active': 1, 'unsubscribed': 1}
        store.set_awaiting_email(101, True)
    with MailingStore(path) as store:
        assert store.awaiting_email(101)
        assert store.unsubscribe_user(101) == 1
        assert not store.awaiting_email(101)
        assert store.status_counts() == {'active': 0, 'unsubscribed': 2}
        assert store.subscribe('second@example.com', 101) == 'subscribed'
        assert store.subscribe('second@example.com', 101) == 'already_subscribed'


def test_imported_address_cannot_be_claimed_by_knowing_address(tmp_path):
    with MailingStore(tmp_path / 'mail.db') as store:
        store.subscribe('imported@example.com')
        assert store.subscribe('imported@example.com', 999) == 'already_subscribed'
        assert store.unsubscribe_user(999) == 0
        assert store.status_counts()['active'] == 1
        token = store.db.execute('SELECT token FROM mailing_subscribers').fetchone()[0]
        store.unsubscribe_token(token)
        assert store.subscribe('imported@example.com', 999) == 'email_in_use'
        assert store.status_counts()['active'] == 0


def test_post_snapshot_one_recipient_each_and_no_repeat_after_restart(tmp_path):
    path = tmp_path / 'mail.db'
    messages = []
    with MailingStore(path) as store:
        store.subscribe('first@example.com', 1)
        store.subscribe('second@example.com', 2)
        store.store_post('post1', 'Web3 연구', '<b>한국어 제목</b>\n<a href="https://example.com/a">원문 보기</a>')
        store.subscribe('later@example.com', 3)
        store.store_post('post1', 'Changed title', 'Changed content')
        report = deliver_pending(mail_settings(), store, sender=messages.append)
        assert report.sent == 2
        assert len(messages) == 2
        for message in messages:
            assert len(message.get_all('To')) == 1
            assert 'Cc' not in message and 'Bcc' not in message
            plain = message.get_body(preferencelist=('plain',)).get_content()
            html = message.get_body(preferencelist=('html',)).get_content()
            assert '한국어 제목' in plain and '<b>한국어 제목</b>' in html
            assert 'https://example.com/a' in plain
            assert 'https://t.me/+room' in plain and 'https://t.me/+room' in html
            assert 'https://t.me/design_test_bot?start=unsubscribe_' in plain
            assert 'later@example.com' not in str(message)
            assert 'List-Unsubscribe-Post' not in message
    with MailingStore(path) as store:
        assert deliver_pending(mail_settings(), store, sender=messages.append).sent == 0
        assert len(messages) == 2
        assert store.outbox_counts() == {'sent': 2}


def test_unsubscribe_before_dispatch_suppresses_snapshot(tmp_path):
    with MailingStore(tmp_path / 'mail.db') as store:
        store.subscribe('person@example.com', 1)
        store.store_post('post1', 'Title', 'Body')
        store.unsubscribe_user(1)
        messages = []
        result = deliver_pending(mail_settings(), store, sender=messages.append)
        assert result.sent == 0
        assert messages == []
        assert store.outbox_counts() == {'suppressed': 1}


def test_uncertain_and_interrupted_attempts_require_explicit_resolution(tmp_path):
    path = tmp_path / 'mail.db'
    calls = []

    def uncertain(message):
        calls.append(message)
        raise EmailDeliveryUncertain('response lost')

    with MailingStore(path) as store:
        store.subscribe('person@example.com')
        store.store_post('post1', 'Title', 'Body')
        result = deliver_pending(mail_settings(), store, sender=uncertain)
        assert result.uncertain == 1
        unresolved = store.unresolved()
        assert unresolved[0]['status'] == 'uncertain'
        assert 'person@example.com' not in str(unresolved)
        store.store_post('post2', 'Title', 'Body')
        pending_id = store.db.execute("SELECT id FROM mailing_outbox WHERE post_key='post2'").fetchone()[0]
        assert store.claim(pending_id)
    with MailingStore(path) as store:
        assert deliver_pending(mail_settings(), store, sender=uncertain).uncertain == 0
        assert len(calls) == 1
        store.resolve(unresolved[0]['id'], retry=True)
        assert deliver_pending(mail_settings(), store, sender=calls.append).sent == 1
        store.resolve(pending_id, retry=False)
        assert store.outbox_counts() == {'sent': 2}


def test_definite_rejection_requires_explicit_retry_without_repeating_sent_recipients(tmp_path):
    calls = []

    def send(message):
        calls.append(str(message['To']))
        if str(message['To']).startswith('second'):
            raise EmailDeliveryError('rejected')

    with MailingStore(tmp_path / 'mail.db') as store:
        store.subscribe('first@example.com')
        store.subscribe('second@example.com')
        store.store_post('post1', 'Title', 'Body')
        report = deliver_pending(mail_settings(), store, sender=send)
        assert (report.sent, report.failed) == (1, 1)
        assert deliver_pending(mail_settings(), store, sender=send).failed == 0
        store.resolve(store.unresolved()[0]['id'], retry=True)
        assert deliver_pending(mail_settings(), store, sender=calls.append).sent == 1
        assert calls.count('first@example.com') == 1


def test_connection_failure_stops_batch_and_leaves_remaining_recipients_queued(tmp_path):
    calls = []

    def unavailable(message):
        calls.append(message)
        raise EmailConnectionError('authentication failed')

    with MailingStore(tmp_path / 'mail.db') as store:
        store.subscribe('first@example.com')
        store.subscribe('second@example.com')
        store.store_post('post', 'Title', 'Body')
        assert deliver_pending(mail_settings(), store, sender=unavailable).failed == 1
        assert len(calls) == 1
        assert store.outbox_counts() == {'failed': 1, 'queued': 1}


def test_delayed_snapshot_excludes_later_subscribers_and_resubscriptions(tmp_path):
    with MailingStore(tmp_path / 'mail.db') as store:
        old = (datetime.now(UTC) - timedelta(days=1)).isoformat()
        store.subscribe('person@example.com', 1)
        store.store_post('old', 'Historic title', 'Body', published_at=old)
        assert store.outbox_counts() == {}
        store.store_post('current', 'Current title', 'Body')
        store.unsubscribe_user(1)
        store.subscribe('person@example.com', 1)
        store.store_post('future', 'Future title', 'Body')
        calls = []
        assert deliver_pending(mail_settings(), store, sender=calls.append).sent == 1
        assert str(calls[0]['Subject']) == 'Future title'


def test_claim_is_atomic_across_two_workers(tmp_path):
    path = tmp_path / 'mail.db'
    with MailingStore(path) as first, MailingStore(path) as second:
        first.subscribe('person@example.com')
        first.store_post('post', 'Title', 'Body')
        delivery_id = first.db.execute('SELECT id FROM mailing_outbox').fetchone()[0]
        assert first.claim(delivery_id)
        assert not second.claim(delivery_id)


def test_smtp_tls_login_envelope_and_uncertain_disconnect(monkeypatch):
    events = []

    class Connection:
        def __init__(self, host, port, **kwargs):
            events.append(('connect', host, port))

        def ehlo(self):
            events.append('ehlo')

        def starttls(self, *, context):
            assert context.check_hostname
            events.append('tls')

        def login(self, username, password):
            events.append('login')

        def send_message(self, message, *, from_addr, to_addrs):
            events.append(('send', from_addr, to_addrs))
            raise smtplib.SMTPServerDisconnected('response lost: secret@example.com')

        def quit(self):
            events.append('quit')

    monkeypatch.setattr(smtplib, 'SMTP', Connection)
    message = build_email(sender='Briefing <sender@example.com>', recipient='one@example.com',
                          title='Title', telegram_html='Body', invite_url='', unsubscribe_url='https://t.me/bot')
    with pytest.raises(EmailDeliveryUncertain) as error:
        SMTPMailer(mail_settings()).send(message)
    assert 'secret@example.com' not in str(error.value)
    assert events[:5] == [('connect', 'smtp.example.com', 587), 'ehlo', 'tls', 'ehlo', 'login']
    assert events[5] == ('send', 'sender@example.com', ['one@example.com'])


def test_smtp_final_rejection_is_definite_but_quit_failure_after_acceptance_is_not_failure(monkeypatch):
    reject = True

    class Connection:
        def __init__(self, *_args, **_kwargs):
            pass

        def login(self, *_args):
            pass

        def send_message(self, *_args, **_kwargs):
            if reject:
                raise smtplib.SMTPDataError(554, b'not accepted')
            return {}

        def quit(self):
            raise smtplib.SMTPServerDisconnected('already closed')

        def close(self):
            pass

    monkeypatch.setattr(smtplib, 'SMTP_SSL', Connection)
    message = build_email(sender='sender@example.com', recipient='one@example.com', title='Title',
                          telegram_html='Body', invite_url='', unsubscribe_url='https://t.me/bot')
    mailer = SMTPMailer(replace(mail_settings(), smtp_security='ssl', smtp_port=465))
    with pytest.raises(EmailDeliveryError):
        mailer.send(message)
    reject = False
    mailer.send(message)
