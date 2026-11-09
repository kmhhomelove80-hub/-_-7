"""Two-stock live trading engine. No network calls occur on import."""
from dataclasses import dataclass, replace
from contextlib import contextmanager
from decimal import Decimal, InvalidOperation, ROUND_FLOOR, ROUND_HALF_UP
from datetime import datetime, timedelta, timezone
import json
import re
import unicodedata
import sqlite3
import threading
import time
import uuid

KST = timezone(timedelta(hours=9))
D = Decimal

class Halt(RuntimeError):
    pass


class ApiFailure(Halt):
    def __init__(self, message, outcome='QUERY_FAILED', api=None, code=None):
        super().__init__(message)
        self.outcome, self.api, self.error_code = outcome, api, code



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
    # Normalize copy/paste formatting, without inventing missing leading digits.
    s = unicodedata.normalize('NFKC', str(value)).upper()
    s = ''.join(c for c in s if not c.isspace() and c not in '\u200b\u200c\u200d\ufeff')
    s = s.strip("'\"")
    if len(s) == 7 and s.startswith('A'):
        s = s[1:]
    if not re.fullmatch(r'[0-9A-Z]{6}', s) or not any(c.isdigit() for c in s):
        raise Halt('종목명 대신 6자리 종목코드(숫자·영문)를 입력하세요. 예: 005930. 앞자리 0도 필요합니다.')
    return s


@dataclass(frozen=True)
class Rule:
    code: str
    name: str

    def __post_init__(self):
        object.__setattr__(self, 'code', code(self.code))

    def validate(self):
        code(self.code)
        if not self.name.strip():
            raise Halt('종목명을 입력하세요.')


@dataclass(frozen=True)
class Config:
    rules: tuple
    cap: int
    daily_max: int
    reserve_percent: Decimal = D('1')
    poll_seconds: int = 3
    gap_percent: Decimal = D('5')
    quantity_mode: str = 'DAILY_FIXED'
    a_sell_percent: Decimal = D('10')
    b_sell_percent: Decimal = D('10')

    def validate(self):
        if len(self.rules) != 2:
            raise Halt('A와 B 두 종목을 지정해야 합니다.')
        for r in self.rules:
            r.validate()
        if self.rules[0].code == self.rules[1].code:
            raise Halt('서로 다른 두 종목을 지정하세요.')
        if not 1_000_000 <= self.cap <= 1_000_000_000:
            raise Halt('투자 한도는 100만~10억원입니다.')
        if not isinstance(self.daily_max, int) or not 1 <= self.daily_max <= 1000:
            raise Halt('일일 최대 횟수는 1~1000회입니다.')
        if not 0 < number(self.gap_percent) < 100:raise Halt('갭은 0 초과 100 미만 %p입니다.')
        if self.quantity_mode not in ('DAILY_FIXED','PERCENT'):raise Halt('매도수량 방식 오류')
        for value in (self.a_sell_percent, self.b_sell_percent):
            if not 0 < number(value) <= 100:
                raise Halt('매도 수량 비율은 0% 초과 100% 이하입니다.')
        if not D('0.5') <= number(self.reserve_percent) <= 10:
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


def gap_snapshot(config, hs):
    a,b=(hs[r.code] for r in config.rules)
    for h in (a,b):
        if h.qty<=0 or h.average<=0 or h.current<=0:raise Halt('갭 계산에는 A/B 모두 보유수량과 양수 평균 매입가가 필요합니다.')
    ar=(a.current/a.average-1)*100;br=(b.current/b.average-1)*100
    gap=ar-br
    return {'a_return':str(ar),'b_return':str(br),'gap':str(gap),'absolute_gap':str(abs(gap)),
            'threshold':str(config.gap_percent),'direction':'A_TO_B' if gap>=config.gap_percent else 'B_TO_A' if gap<=-config.gap_percent else None,
            'a_average':str(a.average),'b_average':str(b.average),'a_current':str(a.current),'b_current':str(b.current)}


def rounded_lot(qty, percent):
    return int((D(qty)*number(percent)/100).to_integral_value(rounding=ROUND_HALF_UP))


def sale_qty(source, percent):
    # Do not silently shrink a configured sale when stock is reserved/unavailable.
    qty=rounded_lot(source.qty,percent)
    return qty if 0<qty<=source.available else 0


def planned_qty(config, source, lots):
    qty=lots['lots'].get(source.code,0) if config.quantity_mode=='DAILY_FIXED' else rounded_lot(source.qty,
        config.a_sell_percent if source.code==config.rules[0].code else config.b_sell_percent)
    return qty if 0<qty<=source.available and qty<=source.qty else 0


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
            CREATE TABLE IF NOT EXISTS audit(
              id INTEGER PRIMARY KEY, trace TEXT NOT NULL, at TEXT NOT NULL,
              stage TEXT NOT NULL, explanation TEXT NOT NULL, data TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS audit_trace ON audit(trace,id);
            CREATE TABLE IF NOT EXISTS gap_state(account TEXT,a TEXT,b TEXT,armed INTEGER NOT NULL,PRIMARY KEY(account,a,b));
            CREATE TABLE IF NOT EXISTS daily_lots(day TEXT,account TEXT,a TEXT,b TEXT,detail TEXT NOT NULL,PRIMARY KEY(day,account,a,b));
            CREATE TABLE IF NOT EXISTS gap_inventory(account TEXT,a TEXT,b TEXT,detail TEXT NOT NULL,PRIMARY KEY(account,a,b));
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

    def reserve(self, day, account, maximum, detail, gap_pair=None):
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            if db.execute("SELECT 1 FROM cycles WHERE phase NOT IN ('DONE','RECONCILED')").fetchone():
                raise Halt('미완료 거래가 있습니다. 영웅문에서 주문을 확인하세요.')
            if db.execute('SELECT COUNT(*) FROM cycles WHERE day=? AND account=?', (day, account)).fetchone()[0] >= maximum:
                raise Halt('하루 최대 실행 횟수에 도달했습니다.')
            cur = db.execute('INSERT INTO cycles(day,account,phase,detail) VALUES(?,?,?,?)',
                             (day, account, 'SELL_INTENT', json.dumps(detail, ensure_ascii=False)))
            if gap_pair:db.execute('INSERT OR REPLACE INTO gap_state VALUES(?,?,?,0)',(account,*gap_pair))
            return cur.lastrowid

    def update(self, ident, phase, detail):
        with self.connect() as db:
            db.execute('UPDATE cycles SET phase=?,detail=? WHERE id=?', (phase, json.dumps(detail, ensure_ascii=False), ident))

    def gap_armed(self,account,a,b):
        with self.connect() as db:
            row=db.execute('SELECT armed FROM gap_state WHERE account=? AND a=? AND b=?',(account,a,b)).fetchone()
        return bool(row[0]) if row else True

    def arm_gap(self,account,a,b):
        with self.connect() as db:db.execute('INSERT OR REPLACE INTO gap_state VALUES(?,?,?,1)',(account,a,b))

    def lots(self,day,account,config,hs,persist=False):
        a,b=(r.code for r in config.rules)
        with self.connect() as db:
            if persist:db.execute('BEGIN IMMEDIATE')
            row=db.execute('SELECT detail FROM daily_lots WHERE day=? AND account=? AND a=? AND b=?',(day,account,a,b)).fetchone()
            if row:return json.loads(row[0])
            result={'day':day,'captured_at':datetime.now(KST).isoformat(),'baseline':{r.code:hs[r.code].qty for r in config.rules},
                    'lots':{a:rounded_lot(hs[a].qty,config.a_sell_percent),b:rounded_lot(hs[b].qty,config.b_sell_percent)},
                    'percents':{a:str(config.a_sell_percent),b:str(config.b_sell_percent)},'saved':persist}
            if persist:db.execute('INSERT INTO daily_lots VALUES(?,?,?,?,?)',(day,account,a,b,json.dumps(result)))
            return result

    def inventory(self,account,a,b):
        with self.connect() as db:row=db.execute('SELECT detail FROM gap_inventory WHERE account=? AND a=? AND b=?',(account,a,b)).fetchone()
        return json.loads(row[0]) if row else None

    @staticmethod
    def inventory_detail(config,hs):
        return {r.code:{'qty':hs[r.code].qty,'average':str(hs[r.code].average)} for r in config.rules}

    def save_inventory(self,account,config,hs,ident=None,detail=None,armed=None):
        a,b=(r.code for r in config.rules)
        with self.connect() as db:
            db.execute('INSERT OR REPLACE INTO gap_inventory VALUES(?,?,?,?)',(account,a,b,json.dumps(self.inventory_detail(config,hs))))
            if ident is not None:db.execute("UPDATE cycles SET phase='DONE',detail=? WHERE id=?",(json.dumps(detail),ident))
            if armed is not None:db.execute('INSERT OR REPLACE INTO gap_state VALUES(?,?,?,?)',(account,a,b,int(armed)))

    def clear_inventory(self,account,a,b):
        # Explicit HTS reconciliation preserves daily lots and re-entry latch.
        with self.connect() as db:db.execute('DELETE FROM gap_inventory WHERE account=? AND a=? AND b=?',(account,a,b))

    def event(self, message):
        with self.connect() as db:
            db.execute('INSERT INTO events(at,message) VALUES(?,?)', (datetime.now(KST).isoformat(), message))

    def audit(self, trace, stage, explanation, data):
        at = datetime.now(KST).isoformat(timespec='milliseconds')
        payload = json.loads(json.dumps(data, ensure_ascii=False, default=str))
        with self.connect() as db:
            db.execute('INSERT INTO audit(trace,at,stage,explanation,data) VALUES(?,?,?,?,?)',
                       (trace, at, stage, explanation, json.dumps(payload, ensure_ascii=False)))
        return {'trace': trace, 'at': at, 'stage': stage, 'explanation': explanation, 'data': payload}

    def audit_rows(self, trace=None, limit=10000):
        with self.connect() as db:
            if trace is None:
                rows = db.execute('SELECT trace,at,stage,explanation,data FROM audit ORDER BY id DESC LIMIT ?', (limit,)).fetchall()[::-1]
            else:
                rows = db.execute('SELECT trace,at,stage,explanation,data FROM audit WHERE trace=? ORDER BY id', (trace,)).fetchall()
        return [dict(trace=r[0],at=r[1],stage=r[2],explanation=r[3],data=json.loads(r[4])) for r in rows]

    def known_orders(self,account=None,day=None):
        known=set()
        with self.connect() as db:
            if account is not None and day is not None:
                rows=db.execute('SELECT detail FROM cycles WHERE account=? AND day=?',(account,day)).fetchall()
            else:
                rows=db.execute('SELECT detail FROM cycles').fetchall()
        for row in rows:
            try:
                detail=json.loads(row[0])
                for key in ('sell_order','buy_order'):
                    if detail.get(key):
                        known.add(str(detail[key]).lstrip('0'))
            except (ValueError,TypeError):
                continue
        return known

    def audit_traces(self):
        with self.connect() as db:
            return db.execute('SELECT trace,MAX(at),COUNT(*) FROM audit GROUP BY trace ORDER BY MAX(id) DESC LIMIT 300').fetchall()

    def reconcile(self, ident):
        with self.connect() as db:
            db.execute("UPDATE cycles SET phase='RECONCILED' WHERE id=?", (ident,))
        self.event(f'사용자가 영웅문 대조 후 미완료 사이클 {ident} 해제. 당일 횟수는 유지.')


def pending_details(rows, known_orders=()):
    result=[]
    for row in rows:
        order=str(row.get('ord_no','')).strip()
        result.append({'order':order,'symbol':str(row.get('stk_cd','')).removeprefix('A') if len(str(row.get('stk_cd','')))==7 else str(row.get('stk_cd','')),
            'name':str(row.get('stk_nm','')), 'side':str(row.get('io_tp_nm','')),
            'ordered':str(row.get('ord_qty','')), 'filled':str(row.get('cntr_qty','')),
            'remaining':str(row.get('oso_qty','')), 'price':str(row.get('ord_pric','')),
            'time':str(row.get('tm','')), 'origin':'이 앱 주문' if order.lstrip('0') in known_orders else '외부 주문 또는 출처 확인 불가'})
    return result


class Engine:
    def __init__(self, broker, config, journal, account, market_open, emit, stop=None):
        config.validate()
        self.broker, self.config, self.journal = broker, config, journal
        self.account, self.market_open, self.emit = account, market_open, emit
        self.stop = stop or threading.Event()
        self.order_gate = threading.Lock()
        self.trace_id = None
        self.cycle_id = None
        self.last_stage = 'NOT_STARTED'
        self.order_outcome = None
        self.completed_legs = []
        self.order_statuses = {'SELL':'NOT_SENT','BUY':'NOT_SENT'}
        self.failure_stage = None
        self.last_wait_audit = 0.0

    def audit(self, stage, explanation, **data):
        self.last_stage = stage
        if self.trace_id is None:
            self.trace_id = 'T' + datetime.now(KST).strftime('%Y%m%d-%H%M%S-') + uuid.uuid4().hex[:8]
        self.emit('trade_phase', {'stage':stage,'explanation':explanation})
        record = self.journal.audit(self.trace_id, stage, explanation,
                                    dict(cycle_id=self.cycle_id, **data))
        self.emit('audit', record)
        return record

    def budget(self, price, proceeds, cash, headroom):
        budgets = {'매도금': number(proceeds), '주문가능현금': number(cash), '한도여유': number(headroom)}
        count = buy_qty(price, proceeds, cash, headroom, self.config.reserve_percent)
        return dict(price=str(price), proceeds=str(proceeds), cash=str(cash), headroom=str(headroom),
                    reserve_percent=str(self.config.reserve_percent),
                    one_share_with_reserve=str(price * (1 + self.config.reserve_percent / 100)),
                    limiting_factors=[k for k,v in budgets.items() if v == min(budgets.values())],
                    quantity=count, cash_query=getattr(self.broker, 'last_cash_snapshot', None))

    def feasibility(self, hs):
        total = sum(hs[r.code].cost for r in self.config.rules)
        result = {}
        for label, source, target, percent in (
            ('A→B', hs[self.config.rules[0].code], hs[self.config.rules[1].code], self.config.a_sell_percent),
            ('B→A', hs[self.config.rules[1].code], hs[self.config.rules[0].code], self.config.b_sell_percent)):
            lots=self.journal.lots(datetime.now(KST).strftime('%Y%m%d'),self.account,self.config,hs)
            qty = planned_qty(self.config,source,lots)
            proceeds = source.current * qty
            headroom = D(self.config.cap-total) + (D(source.cost)*qty/source.qty if source.qty else 0)
            n = buy_qty(target.current, proceeds, proceeds, headroom, self.config.reserve_percent)
            result[label] = {'sell_qty': qty, 'estimated_proceeds': str(proceeds), 'estimated_buy_qty': n,
                             'possible': qty > 0 and n > 0,
                             'note': '현재가 추정. 현금 반영·호가·실제 체결은 주문 전 재확인'}
        self.emit('feasibility', result)
        return result

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
        self.last_stage = 'ACCOUNT_VERIFY'
        self.broker.verify_account(self.account)
        self.last_stage = 'HOLDINGS_QUERY'
        hs = self.broker.holdings()
        for rule in self.config.rules:
            self.last_stage = 'QUOTE_QUERY:' + rule.code
            quote = self.broker.quote(rule.code, rule.name)
            if rule.code in hs:
                if hs[rule.code].name.strip() != rule.name.strip():
                    raise Halt('설정 종목명과 계좌 종목명이 다릅니다.')
                hs[rule.code] = replace(hs[rule.code], current=quote)
            else:
                hs[rule.code] = Holding(rule.code, rule.name, 0, 0, D(0), quote, 0)
        self.last_stage = 'PENDING_ORDER_QUERY'
        pending=self.broker.pending(set(r.code for r in self.config.rules))
        if pending:
            details=pending_details(pending,self.journal.known_orders(self.account,datetime.now(KST).strftime('%Y%m%d')))
            self.emit('pending',details)
            self.audit('PENDING_ORDERS','A/B 미체결 주문 발견: 새 주문을 차단합니다.',pending_orders=details)
            raise Halt('A/B에 미체결 주문이 있습니다. 영웅문에서 확인하세요.')
        return hs

    def notice(self, title, body):
        prefix = f'[{self.trace_id}] ' if self.trace_id else ''
        self.emit('notice', {'subject': prefix + title, 'body': body})

    def strategy(self, state, current=None):
        text=f"A 수익률 {number(state['a_return']):+.4f}% / B 수익률 {number(state['b_return']):+.4f}% / 갭 {number(state['gap']):+.4f}%p / 기준 {self.config.gap_percent}%p"
        self.emit('strategy',text);self.emit('gap',state)
        return text

    def quote_pair(self,hs):
        for r in self.config.rules:
            current=self.broker.quote(r.code,r.name)
            hs[r.code]=replace(hs.get(r.code,Holding(r.code,r.name,0,0,D(0),current,0)),current=current)
        snapshots=getattr(self.broker,'quote_snapshots',None)
        if isinstance(snapshots,dict) and snapshots:
            times=[datetime.fromisoformat(snapshots[r.code]['received_at']) for r in self.config.rules]
            now=datetime.now(KST)
            if any(not 0<=(now-t).total_seconds()<=30 for t in times) or abs((times[0]-times[1]).total_seconds())>5:
                raise Halt('두 종목 시세 시각 차이 5초 초과 또는 30초 이상 지연. 새 주문 차단')
        return hs

    def waiting(self,state,reason):
        self.strategy(state);self.emit('trade_wait',dict(state,reason=reason))
        if time.monotonic()-self.last_wait_audit>30:
            self.audit('WAITING',reason,state=state);self.last_wait_audit=time.monotonic()
        return False

    def wait_fill(self, order, symbol, side, qty):
        deadline = time.monotonic() + 60
        previous = -1
        while time.monotonic() < deadline:
            self.check()
            self.last_stage = 'FILL_QUERY:' + side
            done, gross = self.broker.fills(order, symbol, side, qty)
            self.order_statuses['SELL' if side=='매도' else 'BUY'] = ('FILLED_QUERY_CONFIRMED' if done==qty else 'PARTIALLY_FILLED' if done else 'ACCEPTED_UNFILLED')
            if done != previous:
                self.emit('log', f'{side} 주문 {order}: 체결 {done}/{qty}주')
                self.audit('FILL_CONFIRMED' if done == qty else 'PARTIAL_FILL' if done else 'AWAITING_FILL',
                           f'{side} {symbol}: 확인 체결 {done}/{qty}주, 누적 {gross:,.0f}원',
                           order=order, symbol=symbol, side=side, requested_qty=qty, filled_qty=done,
                           gross=str(gross), average=str(gross/done) if done else None,
                           query_status=getattr(self.broker,'last_fill_query',None))
                if done:
                    self.notice(f'{side} 체결 {symbol}', f'주문 {order} / 체결 {done}/{qty}주 / 누적 {gross:,.0f}원 / 평균 {gross / done:,.4f}원')
                previous = done
            if done == qty:
                if self.broker.pending({symbol}):
                    raise Halt('전량 체결 조회 후 미체결 주문 발견')
                return gross
            if self.stop.wait(2):
                self.check()
        self.order_outcome = 'PARTIAL_OR_UNFILLED'
        raise Halt('60초 내 전량 체결 확인 실패. 부분체결/잔량은 영웅문에서 확인·취소하세요. 후속 주문을 중단합니다.')

    def send(self, ident, phase, detail, side, symbol, qty, price):
        with self.order_gate:
            self.check()
            self.last_stage = phase + '_PRE_ORDER_ACCOUNT_VERIFY'
            self.broker.verify_account(self.account)
            self.check()
            self.journal.update(ident, phase + '_INTENT', detail)
            self.check()
            self.audit('ORDER_CALL', f'{side} 주문 함수 호출: {symbol} {qty}주 / 시장가 수량 산정 참고가 {price:,.0f}원',
                       side=side, symbol=symbol, qty=qty, reference_price=str(price), order_type='시장가',
                       settings={'cap':self.config.cap,'reserve_percent':str(self.config.reserve_percent)})
            # The broker hook runs immediately before the actual HTTP transmission.
            def transmitted(stage,data):
                self.order_statuses[phase] = 'TRANSMIT_ATTEMPT'
                self.audit(stage,'실제 증권사 주문 HTTP 전송 시도',**data)
            self.broker.audit_callback = transmitted
            self.order_outcome = 'CALL_ENTERED'
            try:
                order = self.broker.order(side, symbol, qty, price)
            except ApiFailure as exc:
                self.order_outcome = exc.outcome
                self.order_statuses[phase] = exc.outcome if exc.outcome != 'QUERY_FAILED' else 'NOT_SENT_AUTH_QUERY_FAILED'
                self.audit('ORDER_' + exc.outcome, str(exc), side=side, symbol=symbol,
                           api=exc.api, broker_code=exc.error_code, auto_retry=False)
                raise
            except Exception:
                self.order_outcome = 'UNKNOWN'
                self.order_statuses[phase] = 'UNKNOWN'
                self.audit('ORDER_UNKNOWN', '주문 함수 오류: 실제 접수 여부를 확인하지 못했습니다. 자동 재전송하지 않습니다.',
                           side=side, symbol=symbol, auto_retry=False)
                raise
            finally:
                self.broker.audit_callback = None
            self.order_outcome = 'ACCEPTED'
            self.order_statuses[phase] = 'ACCEPTED'
            self.audit('ORDER_ACCEPTED', f'{side} 접수 확인 / 주문번호 {order}', side=side, symbol=symbol,
                       qty=qty, reference_price=str(price),order_type='시장가',order=order)
            detail[phase.lower() + '_order'] = order
            self.journal.update(ident, phase + '_SENT', detail)
            self.notice(f'{side} 주문 접수 {symbol}', f'주문번호 {order} / {qty}주 / 시장가 · 산정 참고가 {price:,.0f}원')
            return order

    def cycle(self):
        self.trace_id = 'T' + datetime.now(KST).strftime('%Y%m%d-%H%M%S-') + uuid.uuid4().hex[:8]
        self.cycle_id = None
        self.last_stage = 'START_CHECKS'
        self.order_outcome = None
        self.completed_legs = []
        self.order_statuses = {'SELL':'NOT_SENT','BUY':'NOT_SENT'}
        self.failure_stage = None
        self.last_total_cost = None
        try:
            return self._cycle()
        except Exception as exc:
            failed_stage = self.last_stage
            self.failure_stage = failed_stage
            reason = str(exc) if isinstance(exc, Halt) else f'내부 오류({type(exc).__name__})'
            a,b = self.config.rules
            state = {'armed':self.journal.gap_armed(self.account,a.code,b.code)}
            unresolved = bool(self.journal.active())
            recoverable=isinstance(exc,ApiFailure) and exc.outcome=='QUERY_FAILED' and (exc.error_code is None or str(exc.error_code) in ('429','500','502','503','504')) and not unresolved and all(v=='NOT_SENT' for v in self.order_statuses.values())
            self.audit('QUERY_PAUSED' if recoverable else 'HALTED', reason, failed_stage=failed_stage,
                       order_outcome=self.order_outcome or 'NOT_SENT', orders=self.order_statuses, completed_legs=self.completed_legs,
                       cash_query=getattr(self.broker,'last_cash_snapshot',None),
                       api=getattr(exc,'api',None), broker_code=getattr(exc,'error_code',None),
                       cap=self.config.cap, total_cost=self.last_total_cost, daily_max=self.config.daily_max,
                       daily_count=self.journal.count(datetime.now(KST).strftime('%Y%m%d'),self.account),
                       next_state=state, incomplete_cycle=unresolved,
                       actions=['후속 주문 중단', '자동 재주문 없음',
                                '영웅문에서 주문·체결·잔고 확인 필요' if unresolved else '조건/설정 확인 후 수동 재개'],
                       cash_note='첫 매도 전량 체결 확인. 후속 매수 미완료: 일부 체결 여부와 현금 잔액을 영웅문에서 확인' if self.completed_legs == ['SELL'] else None)
            raise

    def _cycle(self):
        self.check()
        if self.journal.active():
            raise Halt('미완료 거래 기록이 있습니다. 자동 재주문하지 않습니다.')
        day = datetime.now(KST).strftime('%Y%m%d')
        if self.journal.count(day, self.account) >= self.config.daily_max:
            raise Halt('하루 최대 실행 횟수에 도달했습니다.')
        hs = self.snapshot()
        self.emit('holdings', hs)
        feasibility = self.feasibility(hs)
        total = sum(hs[r.code].cost for r in self.config.rules)
        self.last_total_cost = total
        if total > self.config.cap:
            raise Halt('기존 두 종목 합산 매입원금이 투자 한도를 초과합니다.')
        a,b=self.config.rules
        hs=self.quote_pair(hs)
        try:state=gap_snapshot(self.config,hs)
        except Halt as exc:
            self.emit('trade_wait',{'reason':str(exc),'direction':None})
            self.audit('WAITING',str(exc),no_order=True)
            self.emit('notice',{'subject':'갭 계산 보류','body':str(exc)+' 보유종목과 평균가를 확인하세요. 조건 복구 시 자동 재평가합니다.'})
            return False
        previous=self.journal.inventory(self.account,a.code,b.code)
        current_inventory=self.journal.inventory_detail(self.config,hs)
        if previous:
            for r in self.config.rules:
                old=previous[r.code];new=current_inventory[r.code]
                if old['qty']!=new['qty'] or abs(number(old['average'])-number(new['average']))>D('1'):
                    raise Halt('프로그램 예상과 다른 보유수량/평균가 변경 감지. 수동거래·분할·잔고 응답을 영웅문과 대조하세요.')
        else:self.journal.save_inventory(self.account,self.config,hs)
        lots=self.journal.lots(day,self.account,self.config,hs,persist=True)
        state.update(armed=self.journal.gap_armed(self.account,a.code,b.code),daily_lots=lots)
        self.strategy(state)
        if number(state['absolute_gap'])<self.config.gap_percent:
            if not state['armed']:self.journal.arm_gap(self.account,a.code,b.code);state['armed']=True
            return self.waiting(state,'정상 대기: 갭이 기준 미만입니다. 다음 기준 도달을 감시합니다.')
        if not state['armed']:return self.waiting(state,'재진입 대기: 거래 후 갭이 기준 미만으로 줄어든 것을 확인해야 합니다.')
        action=state['direction']
        source,target=(hs[a.code],hs[b.code]) if action=='A_TO_B' else (hs[b.code],hs[a.code])
        qty=planned_qty(self.config,source,lots)
        if qty<1:
            self.notice('거래 수량 보류',f'{source.name}: 당일 고정/비율 수량이 0주이거나 매도가능수량 부족. 보유·예약 주문을 확인하세요. 조건 복구 시 재평가합니다.')
            return self.waiting(state,'수량 조건 대기: 설정 수량 0주 또는 매도가능수량 부족. 임의로 수량을 줄여 팔지 않습니다.')
        self.audit('SIGNAL','평균 매입가 대비 수익률 갭 도달: 수익률 높은 종목 매도 → 낮은 종목 매수',
            state=state,action=action,strategy_revision='average-gap-market-v2',
            config_snapshot={'A':{'code':a.code,'name':a.name},'B':{'code':b.code,'name':b.name},
                'gap_percent':str(self.config.gap_percent),'quantity_mode':self.config.quantity_mode,
                'a_sell_percent':str(self.config.a_sell_percent),'b_sell_percent':str(self.config.b_sell_percent)},
            quote_snapshots=getattr(self.broker,'quote_snapshots',None))
        self.audit('SELL_QUANTITY',f'{source.name}: {self.config.quantity_mode} 방식 매도 {qty}주',source=source.code,
            target=target.code,held=source.qty,available=source.available,qty=qty,daily_lots=lots)
        self.last_stage = 'PRE_SALE_PRICE_QUERY'
        sell_price = self.broker.quote(source.code, source.name)
        buy_current = self.broker.quote(target.code, target.name)
        buy_price = self.broker.market_buy_bound(target.code,buy_current)
        # Precheck avoids selling if estimated proceeds cannot buy even one share.
        headroom = D(self.config.cap - total) + D(source.cost) * qty / source.qty
        self.last_stage = 'PRE_SALE_CASH_QUERY'
        cash_before = self.broker.cash()  # validate cash account, even when initially zero
        self.last_stage = 'PRE_SALE_TARGET_CAPACITY_QUERY'
        capacity_before = self.broker.cash_order_limit(target.code,buy_price)
        preflight = self.budget(buy_price, sell_price*qty, cash_before+sell_price*qty, headroom)
        preflight['target_capacity_before_sale'] = capacity_before
        preflight['target_capacity_note'] = '매도 전 현재 한도. 매도금 반영 전이므로 후속 매수 확정 수량이 아닙니다.'
        self.audit('PRE_SALE_BUDGET', '매도 전 매수 가능수량 사전 검사. 현금은 매도대금 반영을 가정한 예상값입니다.',
                   budget=preflight, cash_before=str(cash_before), sell_price=str(sell_price))
        if preflight['quantity'] < 1:
            self.notice('매수 예산 보류','예상 매도금과 비용 예비율로 상대 종목 1주를 살 수 없습니다. 수량/가격/한도를 확인하세요.')
            return self.waiting(state,'예산 조건 대기: 예상 매도금으로 대상 종목 1주 매수 불가')
        hs=self.quote_pair(hs);confirmed=gap_snapshot(self.config,hs)
        if confirmed['direction']!=action:return self.waiting(confirmed,'주문 직전 갭이 변경되어 주문 보류')
        sell_price=hs[source.code].current;buy_price=hs[target.code].current
        detail = {'action': action, 'previous_state': state, 'source': source.code, 'target': target.code, 'sell_qty': qty,
                  'sell_reference': str(sell_price),'order_type':'시장가', 'before_source': source.qty,
                  'before_target': target.qty}
        self.check()
        fresh=self.broker.holdings()
        if any(r.code not in fresh or fresh[r.code].qty!=hs[r.code].qty or abs(fresh[r.code].average-hs[r.code].average)>1 for r in self.config.rules) or fresh[source.code].available<qty:
            raise Halt('주문 직전 잔고/매도가능수량 변경. 새 주문 차단')
        self.check()
        ident = self.journal.reserve(day, self.account, self.config.daily_max, detail,gap_pair=(a.code,b.code))
        self.cycle_id = ident
        detail['trace_id'] = self.trace_id
        self.audit('CYCLE_RESERVED', f'사이클 {ident} 일일 실행 횟수 예약 완료', detail=detail)
        self.emit('log', f'사이클 {ident}: {source.name} {qty}주 매도 → {target.name} 추가 매수')
        self.notice('순환매 신호 발생', f'{action} /  {source.name} {qty}주 매도 → {target.name} 매수\n' + self.strategy(state))
        order = self.send(ident, 'SELL', detail, '매도', source.code, qty, sell_price)
        proceeds = self.wait_fill(order, source.code, '매도', qty)
        self.completed_legs.append('SELL')
        self.order_statuses['SELL'] = 'FILLED'
        self.audit('SALE_COMPLETED', f'{source.name} {qty}주 전량 매도. {proceeds:,.0f}원 체결 확인',
                   source=source.code, quantity=qty, proceeds=str(proceeds),
                   estimated_profit_before_costs=str(proceeds-D(source.cost)*qty/source.qty))
        detail['gross_proceeds'] = str(proceeds)
        self.journal.update(ident, 'SELL_FILLED', detail)
        # Wait for account quantities to reflect execution, never reuse stale average.
        refreshed = self.await_balances(source.code, source.qty - qty, target.code, target.qty)
        self.check()
        current_total = sum(refreshed[r.code].cost for r in self.config.rules if r.code in refreshed)
        n, buy_price = self.funded_quantity(target, proceeds, self.config.cap-current_total)
        if n < 1:
            raise Halt('매도 후 실제 매수 가능수량이 1주 미만입니다. 후속 매수 주문은 전송하지 않았습니다.')
        detail.update(buy_qty=n, buy_budget_basis=str(buy_price))
        order = self.send(ident, 'BUY', detail, '매수', target.code, n, buy_price)
        buy_gross = self.wait_fill(order, target.code, '매수', n)
        self.completed_legs.append('BUY')
        self.order_statuses['BUY'] = 'FILLED'
        self.audit('PURCHASE_COMPLETED', f'{target.name} {n}주 전량 매수. 실제 {buy_gross:,.0f}원',
                   target=target.code, quantity=n, gross=str(buy_gross), average=str(buy_gross/n))
        self.journal.update(ident, 'BUY_FILLED', detail)
        refreshed = self.await_balances(source.code, source.qty - qty, target.code, target.qty + n,expected_average=(target.average*target.qty+buy_gross)/(target.qty+n),target=target.code)
        refreshed=self.quote_pair(refreshed)
        if sum(refreshed[r.code].cost for r in self.config.rules if r.code in refreshed) > self.config.cap:
            raise Halt('체결 후 투자 한도 초과 감지. 자동매매 중지')
        try:
            next_state=gap_snapshot(self.config,refreshed)
            armed=number(next_state['absolute_gap'])<self.config.gap_percent
        except Halt:next_state=dict(state,direction=None);armed=False
        self.journal.save_inventory(self.account,self.config,refreshed,ident,detail,armed=armed)
        next_state.update(armed=armed,daily_lots=lots)
        self.audit('COMPLETED', '매도와 상대 종목 매수, 잔고 확인을 완료했습니다. 갭이 기준 미만으로 줄어든 후 재도달하는 조건을 기다립니다.',
                   previous_state=state, next_state=next_state, completed_legs=self.completed_legs,
                   remaining_sale_cash_before_costs=str(proceeds-buy_gross),
                   total_cost=sum(refreshed[r.code].cost for r in self.config.rules if r.code in refreshed),
                   post_holdings={r.code:{'qty':refreshed[r.code].qty,'average':str(refreshed[r.code].average),
                       'cost':refreshed[r.code].cost,'current':str(refreshed[r.code].current)}
                       for r in self.config.rules if r.code in refreshed})
        self.notice('순환매 완료', f'{source.name} 매도 → {target.name} 매수 완료\n' + self.strategy(next_state))
        self.emit('holdings', refreshed)
        self.emit('log', f'사이클 {ident} 완료 / 오늘 {self.journal.count(day, self.account)}회')
        return True

    def funded_quantity(self, target, proceeds, headroom):
        for attempt in range(11):
            self.check()
            self.last_stage = 'POST_SALE_PRICE_QUERY'
            current = self.broker.quote(target.code,target.name)
            price = self.broker.market_buy_bound(target.code,current)
            self.last_stage = 'POST_SALE_CASH_QUERY'
            cash = self.broker.cash()
            self.last_stage = 'TARGET_CASH_CAPACITY_QUERY'
            capacity = self.broker.cash_order_limit(target.code, price)
            effective_cash = min(cash, number(capacity['amount']))
            budget = self.budget(price,proceeds,effective_cash,headroom)
            budget['account_cash'] = str(cash)
            budget['target_100_percent_capacity'] = capacity
            budget['quantity'] = min(budget['quantity'], capacity['quantity'])
            self.audit('POST_SALE_BUDGET', '실제 매도금·조회 현금·한도 여유 중 최소 금액으로 후속 매수수량 계산',
                       target=target.code, attempt=attempt+1, budget=budget)
            if budget['quantity'] > 0:
                return budget['quantity'], price
            # Only read-only cash refresh can be retried. No buy order exists yet.
            without_cash_limit = buy_qty(price,proceeds,proceeds,headroom,self.config.reserve_percent)
            if without_cash_limit < 1:
                raise Halt('매도금 또는 투자 한도 여유로 비용을 포함한 1주를 살 수 없습니다. B/A 매수 미전송')
            if attempt == 10:
                raise Halt('매도 후 현금 조회값으로 1주 매수 불가. 최대 10회 추가 조회 후 중단했습니다. 반영 지연 여부는 확정할 수 없습니다.')
            self.audit('FUNDS_RECHECK', '현금 조회값이 부족해 2초 후 읽기 전용 재조회. 아직 후속 매수 주문을 보내지 않았습니다.',
                       attempt=attempt+1, buy_order_sent=False)
            if self.stop.wait(2):
                self.check()
        raise Halt('현금 재조회 종료')

    def await_balances(self, source, source_qty, target_code, target_qty,expected_average=None,target=None):
        for _ in range(10):
            self.check()
            self.last_stage = 'BALANCE_QUERY'
            hs = self.broker.holdings()
            sq = hs[source].qty if source in hs else 0
            tq = hs[target_code].qty if target_code in hs else 0
            self.audit('BALANCE_CHECK', '체결 후 계좌 보유수량 대조', source=source, target=target,
                       expected_source=source_qty, actual_source=sq, expected_target=target_qty, actual_target=tq,expected_average=str(expected_average))
            average_ok=expected_average is None or target in hs and abs(hs[target].average-expected_average)<=D('1')
            if sq == source_qty and tq == target_qty and average_ok:
                return hs
            if self.stop.wait(1):
                self.check()
        raise Halt('계좌 잔고가 예상 체결수량과 일치하지 않습니다. 수동거래/잔고 지연을 확인하세요.')

    def run(self):
        self.notice('자동매매 시작', f'A: {self.config.rules[0].name} / B: {self.config.rules[1].name} / 한도 {self.config.cap:,}원')
        try:
            while not self.stop.is_set():
                try:traded = self.cycle()
                except ApiFailure as exc:
                    safe=(exc.outcome=='QUERY_FAILED' and (exc.error_code is None or str(exc.error_code) in ('429','500','502','503','504')) and
                          not self.journal.active() and all(v=='NOT_SENT' for v in self.order_statuses.values()))
                    if not safe:raise
                    self.notice('조회 장애 · 자동복구 대기',str(exc)+'\n원인: 읽기 조회 통신/서버 응답 실패. 주문 전송 없음.\n조치: 새 주문 보류, 30초 후 계좌·시세 재점검. 연결이 복구되면 자동 재개합니다.')
                    self.emit('trade_wait',{'reason':'조회 장애 · 30초 후 자동 재점검','query_paused':True})
                    if self.stop.wait(30):break
                    continue
                # Immediate next evaluation after completed cycles; rate limits still apply.
                if not traded and self.stop.wait(self.config.poll_seconds):
                    break
        except Exception as exc:
            # Do not surface arbitrary HTTP payloads that might contain credentials.
            message = str(exc) if isinstance(exc, Halt) else f'내부 오류({type(exc).__name__}). 자동매매 중지'
            self.journal.event(message)
            self.emit('log', message)
            self.notice('자동매매 중지 / 확인 필요', f'중단 단계: {self.failure_stage}\n매도/매수 상태: {self.order_statuses}\n{message}\n상세 내용은 같은 추적번호의 거래 설명/진단을 확인하세요.')
        finally:
            self.stop.set()
            self.notice('자동매매 종료', '자동매매가 중지되었습니다. 접수된 주문은 영웅문에서 확인하세요.')
            self.emit('stopped', None)
