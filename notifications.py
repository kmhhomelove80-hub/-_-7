"""Asynchronous SMTP email notifications; no mail is sent on import."""
from dataclasses import dataclass, asdict
from email.message import EmailMessage
from email.utils import formatdate
import queue
import re
import smtplib
import ssl
import threading
import time
from core import Halt


@dataclass(frozen=True)
class MailSettings:
    enabled: bool = False
    host: str = ''
    port: int = 465
    security: str = 'SSL'
    username: str = ''
    password: str = ''
    sender: str = ''
    recipient: str = ''

    def validate(self):
        if not self.host or any(c.isspace() for c in self.host) or not 1 <= self.port <= 65535:
            raise Halt('SMTP 서버/포트를 확인하세요.')
        if self.security not in ('SSL', 'STARTTLS'):
            raise Halt('SMTP는 SSL 또는 STARTTLS만 지원합니다.')
        if not self.username or not self.password:
            raise Halt('SMTP 사용자명과 비밀번호/앱 비밀번호가 필요합니다.')
        for address in (self.sender, self.recipient):
            if not re.fullmatch(r'[^\s<>@,;]+@[^\s<>@,;]+\.[^\s<>@,;]+', address):
                raise Halt('발신/수신 이메일 주소를 확인하세요.')
        for text in (self.host, self.username, self.sender, self.recipient):
            if '\r' in text or '\n' in text:
                raise Halt('메일 설정에 줄바꿈을 사용할 수 없습니다.')

    def to_dict(self):
        return asdict(self)


def send_mail(settings, subject, body):
    settings.validate()
    message = EmailMessage()
    message['Subject'] = '[키움 순환매] ' + subject
    message['From'], message['To'] = settings.sender, settings.recipient
    message['Date'] = formatdate(localtime=True)
    message.set_content(body + '\n\n휴대폰 알림 수신은 메일 앱의 알림 설정을 확인하세요.')
    context = ssl.create_default_context()
    if settings.security == 'SSL':
        connection = smtplib.SMTP_SSL(settings.host, settings.port, timeout=15, context=context)
    else:
        connection = smtplib.SMTP(settings.host, settings.port, timeout=15)
    with connection as smtp:
        if settings.security == 'STARTTLS':
            smtp.ehlo()
            smtp.starttls(context=context)
            smtp.ehlo()
        smtp.login(settings.username, settings.password)
        refused = smtp.send_message(message)
        if refused:
            raise smtplib.SMTPException('Recipient refused')


class EmailNotifier:
    def __init__(self, settings, report, sender=send_mail, label='이메일', spacing=0):
        self.sender, self.label, self.spacing = sender, label, spacing
        self.last_attempt = 0.0
        self.settings = settings
        self.report = report
        self.queue = queue.Queue(maxsize=100)
        self.closed = threading.Event()
        self.thread = threading.Thread(target=self.run, daemon=True)
        self.thread.start()

    def configure(self, settings):
        if settings.enabled:
            settings.validate()
        self.settings = settings

    def notify(self, subject, body, force=False):
        settings = self.settings
        if self.closed.is_set() or (not settings.enabled and not force):
            return False
        try:
            settings.validate()
            self.queue.put_nowait((settings, subject, body))
            return True
        except queue.Full:
            self.report(self.label + ' 대기열이 가득 차 알림을 저장하지 못했습니다. 거래 처리는 계속합니다.')
        except Halt as exc:
            self.report(str(exc))
        return False

    @property
    def pending(self):
        return self.queue.unfinished_tasks

    def run(self):
        while not self.closed.is_set() or not self.queue.empty():
            try:
                settings, subject, body = self.queue.get(timeout=.2)
            except queue.Empty:
                continue
            try:
                delay = self.spacing - (time.monotonic() - self.last_attempt)
                if delay > 0:
                    time.sleep(delay)
                self.last_attempt = time.monotonic()
                self.sender(settings, subject, body)
                self.report(self.label + ' 발송 완료: ' + subject)
            except Exception as exc:
                # SMTP errors can contain passwords/addresses; expose only exception type.
                self.report(f'{self.label} 발송 실패({type(exc).__name__}): {subject}. 거래 주문은 재실행하지 않습니다.')
            finally:
                self.queue.task_done()

    def close(self):
        self.closed.set()


@dataclass(frozen=True)
class TelegramSettings:
    enabled: bool = False
    token: str = ''
    chat_id: str = ''

    def validate(self):
        if not re.fullmatch(r'[0-9]+:[A-Za-z0-9_-]{20,}', self.token):
            raise Halt('텔레그램 봇 토큰 형식을 확인하세요.')
        if not re.fullmatch(r'-?[0-9]+', self.chat_id):
            raise Halt('텔레그램 수신 Chat ID를 확인하세요.')

    def to_dict(self):
        return asdict(self)


def telegram_request(token, method, data):
    import requests
    if not re.fullmatch(r'[0-9]+:[A-Za-z0-9_-]{20,}', token):
        raise Halt('텔레그램 봇 토큰 형식을 확인하세요.')
    try:
        response = requests.post('https://api.telegram.org/bot' + token + '/' + method,
                                 json=data, timeout=(5, 15), allow_redirects=False)
        result = response.json()
        if response.status_code != 200 or result.get('ok') is not True:
            raise Halt('텔레그램 요청 실패. 토큰, 봇 시작 여부, Chat ID를 확인하세요.')
        return result['result']
    except Halt:
        raise
    except Exception as exc:
        # Token is part of the URL, so never expose request exception strings.
        raise Halt(f'텔레그램 통신 오류({type(exc).__name__})') from None


def send_telegram(settings, subject, body):
    settings.validate()
    telegram_request(settings.token, 'sendMessage', {
        'chat_id': settings.chat_id, 'text': ('[키움 순환매] ' + subject + '\n' + body)[:4000],
        'disable_notification': False})


def private_chat_ids(token):
    result = telegram_request(token, 'getUpdates', {'timeout': 0, 'limit': 100})
    return sorted({str(item['message']['chat']['id']) for item in result
                   if item.get('message', {}).get('chat', {}).get('type') == 'private'})


class TelegramNotifier(EmailNotifier):
    def __init__(self, settings, report):
        super().__init__(settings, report, send_telegram, '텔레그램', 1.1)
