"""Read-only account monitor, independent of the trading lifecycle."""
from datetime import datetime
import threading
import time
from core import Halt, KST


class AccountMonitor:
    def __init__(self, broker, account, emit, stop=None, interval=5):
        self.broker, self.account, self.emit = broker, account, emit
        self.stop = stop or threading.Event()
        self.interval = interval

    def refresh(self):
        self.broker.verify_account(self.account)
        snapshot = self.broker.account_snapshot()
        snapshot['account_label'] = '*' * 6 + self.account[-4:]
        snapshot['updated_at'] = datetime.now(KST).strftime('%H:%M:%S')
        snapshot['received_at'] = time.monotonic()
        if not self.stop.is_set():
            self.emit('balance', snapshot)
        return snapshot

    def run(self):
        try:
            while not self.stop.is_set():
                started = time.monotonic()
                try:
                    self.refresh()
                except Exception as exc:
                    if not self.stop.is_set():
                        message = str(exc) if isinstance(exc, Halt) else f'잔고 응답 오류({type(exc).__name__})'
                        self.emit('balance_error', message)
                if self.stop.wait(max(.1, self.interval - (time.monotonic() - started))):
                    break
        finally:
            self.broker.session.close()
