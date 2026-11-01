"""Two-stock live trading engine. No network calls occur on import."""
from dataclasses import dataclass, replace
from contextlib import contextmanager
from decimal import Decimal, InvalidOperation, ROUND_FLOOR
from datetime import datetime, timedelta, timezone
import json
import re
import sqlite3
import threading
import time

KST = timezone(timedelta(hours=9))
D = Decimal

class Halt(RuntimeError):
    pass


def number(value):
    try:
        result = D(str(value).strip().replace(',', ''))
    except InvalidOperation as exc:
        raise Halt('응답 숫자 형식 오류') from exc
    if not result.is_finite():
        raise Halt('유효하지 않은 숫자')
    return result


def integer(value):
    n = number(value)
    if n != n.to_integral_value() or n < 0:
        raise Halt('수량/금액이 음수이거나 정수가 아닙니다.')
    return int(n)


def code(value):
    s = str(value).strip()
    if len(s) == 7 and s.startswith('A'):
        s = s[1:]
    if not re.fullmatch(r'[0-9]{6}', s):
        raise Halt('6자리 국내주식 종목코드가 필요합니다.')
    return s


@dataclass(frozen=True)
class Rule:
    code: str
    name: str
    drop: Decimal
    sell_percent: Decimal  # Percentage of the OTHER holding sold when THIS falls.

    def validate(self):
        code(self.code)
        if not self.name.strip():
            raise Halt('종목명을 입력하세요.')
        if not D('0') < self.drop < D('100'):
            raise Halt('하락 기준은 0% 초과 100% 미만입니다.')
        if not D('0') < self.sell_percent <= D('100'):
            raise Halt('상대 종목 매도비율은 0% 초과 100% 이하입니다.')


@dataclass(frozen=True)
class Config:
    rules: tuple
    cap: int
    daily_max: int
    reserve_percent: Decimal = D('1')
    poll_seconds: int = 3

    def validate(self):
        if len(self.rules) != 2:
            raise Halt('두 종목을 지정해야 합니다.')
        for r in self.rules:
            r.validate()
        if self.rules[0].code == self.rules[1].code:
            raise Halt('서로 다른 두 종목을 지정하세요.')
        if not 500_000 <= self.cap <= 1_000_000_000:
            raise Halt('투자 한도는 50만~10억원입니다.')
        if not isinstance(self.daily_max, int) or not 1 <= self.daily_max <= 1000:
            raise Halt('일일 최대 횟수는 1~1000회입니다.')
        if not D('0.5') <= self.reserve_percent <= D('10'):
            raise Halt('거래비용 예비율은 0.5~10%입니다.')
        if not 2 <= self.poll_seconds <= 60:
            raise Halt('조회 간격은 2~60초입니다.')


@dataclass(frozen=True)
class Holding:
    code: str
    name: str
    qty: int
    available: int
    average: Decimal
    current: Decimal
    cost: int

    @property
    def drop(self):
        if self.average <= 0 or self.current <= 0:
            raise Halt('평균 매입가/현재가가 0 이하입니다.')
        return (self.average - self.current) / self.average * 100


def select_target(config, holdings):
    """Larger absolute loss percentage first; exact ties use A deterministically."""
    candidates = [i for i, r in enumerate(config.rules)
                  if holdings[r.code].qty > 0 and holdings[r.code].drop >= r.drop]
    return max(candidates, key=lambda i: holdings[config.rules[i].code].drop) if candidates else None


def sale_qty(source, percent):
    return min(source.available, int(D(source.qty) * percent / 100))


def buy_qty(price, proceeds, cash, headroom, reserve_percent):
    if price <= 0:
        raise Halt('매수 지정가 오류')
    budget = min(number(proceeds), number(cash), number(headroom))
    if budget <= 0:
        return 0
    return int((budget / (price * (1 + reserve_percent / 100))).to_integral_value(rounding=ROUND_FLOOR))


def fill_totals(rows, order_id, symbol, side, expected):
    """kt00009 per-execution rows, deduplicated by execution number."""
    unique = {}
    for row in rows:
        if str(row['ord_no']).lstrip('0') != str(order_id).lstrip('0'):
            continue
        if code(row['stk_cd']) != symbol or row['io_tp_nm'].strip() != '현금' + side:
            raise Halt('주문번호와 체결 종목/방향이 일치하지 않습니다.')
        qty = integer(row['cntr_qty'])
        if not qty:
            continue
        key = str(row['cntr_no']).strip()
        if not key or set(key) == {'0'}:
            raise Halt('체결번호가 없어 중복 체결을 구분할 수 없습니다.')
        price = number(row['cntr_uv'])
        if price <= 0:
            raise Halt('체결가격 오류')
        item = (qty, price)
        if key in unique and unique[key] != item:
            raise Halt('동일 체결번호의 데이터가 충돌합니다.')
        unique[key] = item
    quantity = sum(v[0] for v in unique.values())
    if quantity > expected:
        raise Halt('예상 주문수량을 초과하는 체결 데이터')
    return quantity, sum((D(q) * p for q, p in unique.values()), D(0))


class Journal:
    """Write-ahead order intent, persistent daily reservations, fail-closed restart."""
    def __init__(self, path):
        self.path = str(path)
        with self.connect() as db:
            db.executescript('''
            CREATE TABLE IF NOT EXISTS cycles(
              id INTEGER PRIMARY KEY, day TEXT NOT NULL, account TEXT NOT NULL,
              phase TEXT NOT NULL, detail TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS events(
              id INTEGER PRIMARY KEY, at TEXT NOT NULL, message TEXT NOT NULL);
            ''')

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=5)
        try:
            db.execute('PRAGMA synchronous=FULL')
            with db:
                yield db
        finally:
            db.close()

    def active(self):
        with self.connect() as db:
            return db.execute("SELECT id,day,account,phase,detail FROM cycles WHERE phase NOT IN ('DONE','RECONCILED') ORDER BY id").fetchall()

    def count(self, day, account):
        with self.connect() as db:
            return db.execute('SELECT COUNT(*) FROM cycles WHERE day=? AND account=?', (day, account)).fetchone()[0]

    def reserve(self, day, account, maximum, detail):
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            if db.execute("SELECT 1 FROM cycles WHERE phase NOT IN ('DONE','RECONCILED')").fetchone():
                raise Halt('미완료 거래가 있습니다. 영웅문에서 주문을 확인하세요.')
            if db.execute('SELECT COUNT(*) FROM cycles WHERE day=? AND account=?', (day, account)).fetchone()[0] >= maximum:
                raise Halt('하루 최대 실행 횟수에 도달했습니다.')
            cur = db.execute('INSERT INTO cycles(day,account,phase,detail) VALUES(?,?,?,?)',
                             (day, account, 'SELL_INTENT', json.dumps(detail, ensure_ascii=False)))
            return cur.lastrowid

    def update(self, ident, phase, detail):
        with self.connect() as db:
            db.execute('UPDATE cycles SET phase=?,detail=? WHERE id=?', (phase, json.dumps(detail, ensure_ascii=False), ident))

    def event(self, message):
        with self.connect() as db:
            db.execute('INSERT INTO events(at,message) VALUES(?,?)', (datetime.now(KST).isoformat(), message))

    def reconcile(self, ident):
        with self.connect() as db:
            db.execute("UPDATE cycles SET phase='RECONCILED' WHERE id=?", (ident,))
        self.event(f'사용자가 영웅문 대조 후 미완료 사이클 {ident} 해제. 당일 횟수는 유지.')


class Engine:
    def __init__(self, broker, config, journal, account, market_open, emit, stop=None):
        config.validate()
        self.broker, self.config, self.journal = broker, config, journal
        self.account, self.market_open, self.emit = account, market_open, emit
        self.stop = stop or threading.Event()
        self.order_gate = threading.Lock()

    def request_stop(self):
        # Shared with the actual send boundary: no later order after stop returns.
        with self.order_gate:
            self.stop.set()

    def check(self):
        if self.stop.is_set():
            raise Halt('사용자 중지. 진행 주문은 영웅문에서 확인하세요.')
        if not self.market_open():
            raise Halt('KRX 연속매매 허용 시간 밖입니다.')

    def snapshot(self):
        self.broker.verify_account(self.account)
        hs = self.broker.holdings()
        for rule in self.config.rules:
            if rule.code not in hs or hs[rule.code].qty <= 0:
                raise Halt(f'{rule.name}: 보유 잔고가 없습니다.')
            if hs[rule.code].name.strip() != rule.name.strip():
                raise Halt(f'{rule.code}: 입력한 종목명과 계좌 종목명이 다릅니다.')
            hs[rule.code] = replace(hs[rule.code], current=self.broker.quote(rule.code, rule.name))
            hs[rule.code].drop  # validate fresh prices
        if self.broker.pending(set(r.code for r in self.config.rules)):
            raise Halt('두 종목에 미체결 주문이 있습니다. 영웅문에서 확인하세요.')
        return hs

    def wait_fill(self, order, symbol, side, qty):
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            self.check()
            done, gross = self.broker.fills(order, symbol, side, qty)
            self.emit('log', f'{side} 주문 {order}: 체결 {done}/{qty}주')
            if done == qty:
                if self.broker.pending({symbol}):
                    raise Halt('전량 체결 조회 후 미체결 주문 발견')
                return gross
            if self.stop.wait(2):
                self.check()
        raise Halt('60초 내 전량 체결 확인 실패. 부분체결/잔량은 영웅문에서 확인·취소하세요. 후속 주문을 중단합니다.')

    def send(self, ident, phase, detail, side, symbol, qty, price):
        with self.order_gate:
            self.check()
            self.broker.verify_account(self.account)
            self.check()
            self.journal.update(ident, phase + '_INTENT', detail)
            self.check()
            # No automatic retry, including network timeouts or rejected responses.
            order = self.broker.order(side, symbol, qty, price)
            detail[phase.lower() + '_order'] = order
            self.journal.update(ident, phase + '_SENT', detail)
            return order

    def cycle(self):
        self.check()
        if self.journal.active():
            raise Halt('미완료 거래 기록이 있습니다. 자동 재주문하지 않습니다.')
        day = datetime.now(KST).strftime('%Y%m%d')
        if self.journal.count(day, self.account) >= self.config.daily_max:
            raise Halt('하루 최대 실행 횟수에 도달했습니다.')
        hs = self.snapshot()
        self.emit('holdings', hs)
        total = sum(hs[r.code].cost for r in self.config.rules)
        if total > self.config.cap:
            raise Halt('기존 두 종목 합산 매입원금이 투자 한도를 초과합니다.')
        idx = select_target(self.config, hs)
        if idx is None:
            self.emit('log', '조건 대기 중')
            return False
        rule = self.config.rules[idx]
        target, source = hs[rule.code], hs[self.config.rules[1 - idx].code]
        qty = sale_qty(source, rule.sell_percent)
        if qty < 1:
            raise Halt('설정 비율로 매도 가능한 상대 종목 수량이 1주 미만입니다.')
        sell_price = self.broker.quote(source.code, source.name)
        buy_price = self.broker.quote(target.code, target.name)
        # Precheck avoids selling if estimated proceeds cannot buy even one share.
        headroom = D(self.config.cap - total) + D(source.cost) * qty / source.qty
        cash_before = self.broker.cash()  # validate cash account, even when initially zero
        if buy_qty(buy_price, sell_price * qty, cash_before + sell_price * qty, headroom, self.config.reserve_percent) < 1:
            raise Halt('예상 매도금/현금/투자 한도로 대상 종목 1주를 매수할 수 없습니다.')
        detail = {'source': source.code, 'target': target.code, 'sell_qty': qty,
                  'sell_limit': str(sell_price), 'before_source': source.qty,
                  'before_target': target.qty}
        self.check()
        ident = self.journal.reserve(day, self.account, self.config.daily_max, detail)
        self.emit('log', f'사이클 {ident}: {source.name} {qty}주 매도 → {target.name} 추가 매수')
        order = self.send(ident, 'SELL', detail, '매도', source.code, qty, sell_price)
        proceeds = self.wait_fill(order, source.code, '매도', qty)
        detail['gross_proceeds'] = str(proceeds)
        self.journal.update(ident, 'SELL_FILLED', detail)
        # Wait for account quantities to reflect execution, never reuse stale average.
        refreshed = self.await_balances(source.code, source.qty - qty, target.code, target.qty)
        self.check()
        current_total = sum(refreshed[r.code].cost for r in self.config.rules if r.code in refreshed)
        buy_price = self.broker.quote(target.code, target.name)
        n = buy_qty(buy_price, proceeds, self.broker.cash(), self.config.cap - current_total,
                    self.config.reserve_percent)
        if n < 1:
            raise Halt('매도 후 실제 매수 가능수량이 1주 미만입니다. 확보 현금은 계좌에 남습니다.')
        detail.update(buy_qty=n, buy_limit=str(buy_price))
        order = self.send(ident, 'BUY', detail, '매수', target.code, n, buy_price)
        self.wait_fill(order, target.code, '매수', n)
        refreshed = self.await_balances(source.code, source.qty - qty, target.code, target.qty + n)
        if sum(refreshed[r.code].cost for r in self.config.rules if r.code in refreshed) > self.config.cap:
            raise Halt('체결 후 투자 한도 초과 감지. 자동매매 중지')
        self.journal.update(ident, 'DONE', detail)
        self.emit('holdings', refreshed)
        self.emit('log', f'사이클 {ident} 완료 / 오늘 {self.journal.count(day, self.account)}회')
        return True

    def await_balances(self, source, source_qty, target, target_qty):
        for _ in range(10):
            self.check()
            hs = self.broker.holdings()
            sq = hs[source].qty if source in hs else 0
            tq = hs[target].qty if target in hs else 0
            if sq == source_qty and tq == target_qty:
                return hs
            if self.stop.wait(1):
                self.check()
        raise Halt('계좌 잔고가 예상 체결수량과 일치하지 않습니다. 수동거래/잔고 지연을 확인하세요.')

    def run(self):
        try:
            while not self.stop.is_set():
                traded = self.cycle()
                # Immediate next evaluation after completed cycles; rate limits still apply.
                if not traded and self.stop.wait(self.config.poll_seconds):
                    break
        except Exception as exc:
            # Do not surface arbitrary HTTP payloads that might contain credentials.
            message = str(exc) if isinstance(exc, Halt) else f'내부 오류({type(exc).__name__}). 자동매매 중지'
            self.journal.event(message)
            self.emit('log', message)
        finally:
            self.stop.set()
            self.emit('stopped', None)
