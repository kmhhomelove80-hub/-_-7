from dataclasses import replace
import smtplib
import sys
import types
import unittest
from unittest.mock import MagicMock, patch
from core import Halt
from notifications import MailSettings, TelegramSettings, send_mail, send_telegram, private_chat_ids, telegram_request, EmailNotifier


def mail():
    return MailSettings(True, 'smtp.example.com', 465, 'SSL', 'user', 'secret-password', 'sender@example.com', 'receiver@example.com')


def telegram():
    return TelegramSettings(True, '123456:' + 'A'*30, '123456789')


class MailTests(unittest.TestCase):
    def test_ssl_mail_and_login(self):
        smtp = MagicMock()
        smtp.__enter__.return_value = smtp
        smtp.send_message.return_value = {}
        with patch('notifications.smtplib.SMTP_SSL', return_value=smtp) as constructor:
            send_mail(mail(), '매도 체결', 'B 매도 5주 완료')
        constructor.assert_called_once()
        smtp.login.assert_called_once_with('user','secret-password')
        message = smtp.send_message.call_args.args[0]
        self.assertEqual(message['To'], 'receiver@example.com')
        self.assertIn('매도 체결', message['Subject'])

    def test_starttls_before_login(self):
        smtp = MagicMock()
        smtp.__enter__.return_value = smtp
        smtp.send_message.return_value = {}
        with patch('notifications.smtplib.SMTP',return_value=smtp):
            send_mail(replace(mail(), security='STARTTLS', port=587),'테스트','내용')
        names = [call[0] for call in smtp.method_calls]
        self.assertLess(names.index('starttls'),names.index('login'))

    def test_header_injection_and_cleartext_disabled(self):
        for settings in (replace(mail(),sender='a@example.com\nBcc: bad@example.com'), replace(mail(),security='NONE')):
            with self.assertRaises(Halt):
                settings.validate()

    def test_disabled_notification_does_not_send(self):
        sent = []
        notifier = EmailNotifier(replace(mail(), enabled=False), lambda *a:None, sender=lambda *a:sent.append(a))
        try:
            self.assertFalse(notifier.notify('subject','body'))
            self.assertEqual(sent,[])
        finally:
            notifier.close()
            notifier.thread.join(1)

    def test_failure_sanitized_and_next_notification_continues(self):
        reports, calls = [], []
        def send(*args):
            calls.append(args)
            if len(calls)==1:
                raise smtplib.SMTPException('secret-password sensitive response')
        notifier = EmailNotifier(mail(),reports.append,sender=send)
        notifier.notify('first','body')
        notifier.notify('second','body')
        notifier.queue.join()
        notifier.close()
        notifier.thread.join(1)
        self.assertEqual(len(calls),2)
        self.assertNotIn('secret-password',str(reports))
        self.assertIn('발송 완료',reports[-1])


class TelegramTests(unittest.TestCase):
    def response(self, result=None, ok=True, status=200):
        response = MagicMock()
        response.status_code=status
        response.json.return_value={'ok':ok, 'result':result}
        return response

    def test_push_request_uses_correct_private_chat_and_sound(self):
        fake=types.SimpleNamespace(post=MagicMock(return_value=self.response({'message_id':1})))
        with patch.dict(sys.modules, {'requests':fake}):
            send_telegram(telegram(),'순환매 완료','B 매도 완료')
        args=fake.post.call_args
        self.assertTrue(args.args[0].endswith('/sendMessage'))
        self.assertEqual(args.kwargs['json']['chat_id'],'123456789')
        self.assertFalse(args.kwargs['json']['disable_notification'])
        self.assertFalse(args.kwargs['allow_redirects'])

    def test_token_not_leaked_in_network_error(self):
        fake=types.SimpleNamespace(post=MagicMock(side_effect=RuntimeError(telegram().token)))
        with patch.dict(sys.modules, {'requests':fake}):
            with self.assertRaises(Halt) as caught:
                send_telegram(telegram(),'test','body')
        self.assertNotIn(telegram().token,str(caught.exception))

    def test_api_rejection_is_not_retried(self):
        fake=types.SimpleNamespace(post=MagicMock(return_value=self.response(ok=False,status=400)))
        with patch.dict(sys.modules, {'requests':fake}):
            with self.assertRaises(Halt):
                send_telegram(telegram(),'test','body')
        self.assertEqual(fake.post.call_count,1)

    def test_chat_lookup_deduplicates_only_private_chats(self):
        updates=[{'message':{'chat':{'type':'private','id':123}}},
                 {'message':{'chat':{'type':'private','id':123}}},
                 {'message':{'chat':{'type':'group','id':-456}}}]
        with patch('notifications.telegram_request',return_value=updates):
            self.assertEqual(private_chat_ids(telegram().token),['123'])

    def test_bad_token_and_chat_id_rejected(self):
        for settings in (replace(telegram(),token='invalid'),replace(telegram(),chat_id='@unknown')):
            with self.assertRaises(Halt):
                settings.validate()
