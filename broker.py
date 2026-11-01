"""Kiwoom production REST adapter; query pagination; never retries orders."""
from datetime import datetime, timedelta
import time
import threading
from core import D, KST, Halt, Holding, code, number, integer, fill_totals

BASE = 'https://api.kiwoom.com'
ACCOUNT_PATH = '/api/dostk/acnt'


class Kiwoom:
    _network_lock = threading.RLock()
    _last_call = 0.0

    def __init__(self, appkey, secret, session=None):
        if session is None:
            import requests
            session = requests.Session()
        self.session = session
        self.appkey, self.secret = appkey, secret
        self.token = None
        self.expires = datetime.min.replace(tzinfo=KST)
        self.last_call = 0.0

    def post(self, path, body, api_id=None, cont='N', next_key=''):
        # Account monitor and trading clients share one process-wide API budget.
        with Kiwoom._network_lock:
            return self._post(path, body, api_id, cont, next_key)

    def _post(self, path, body, api_id=None, cont='N', next_key=''):
        delay = .55 - (time.monotonic() - Kiwoom._last_call)
        if delay > 0:
            time.sleep(delay)
        headers = {'Content-Type': 'application/json;charset=UTF-8'}
        if api_id:
            headers.update({'authorization': 'Bearer ' + self.token,
                            'api-id': api_id, 'cont-yn': cont, 'next-key': next_key})
        Kiwoom._last_call = time.monotonic()
        try:
            response = self.session.post(BASE + path, json=body, headers=headers,
                                         timeout=(5, 15), allow_redirects=False)
            if response.status_code != 200:
                raise Halt(f'{api_id or "인증"}: HTTP {response.status_code}. 주문은 자동 재전송하지 않습니다.')
            data = response.json()
        except Halt:
            raise
        except Exception as exc:
            raise Halt(f'{api_id or "인증"}: 통신/응답 오류({type(exc).__name__}). 접수 여부 확인 필요; 자동 재전송 안 함.') from exc
        if not isinstance(data, dict) or str(data.get('return_code')) != '0':
            # Return messages can echo app keys; only expose the broker's numeric code.
            result = data.get('return_code', 'missing') if isinstance(data, dict) else 'invalid'
            result = str(result)
            if not result.lstrip('-').isdigit():
                result = 'invalid'
            raise Halt(f'{api_id or "인증"}: 키움 오류 코드 {result}. 영웅문/REST API 설정을 확인하세요.')
        return data, response.headers

    def authenticate(self):
        if self.token and datetime.now(KST) + timedelta(minutes=2) < self.expires:
            return
        data, _ = self.post('/oauth2/token', {'grant_type': 'client_credentials',
                                             'appkey': self.appkey, 'secretkey': self.secret})
        try:
            self.token = data['token']
            self.expires = datetime.strptime(data['expires_dt'], '%Y%m%d%H%M%S').replace(tzinfo=KST)
            if not self.token or self.expires <= datetime.now(KST):
                raise ValueError()
        except (KeyError, ValueError, TypeError) as exc:
            self.token = None
            raise Halt('토큰/만료시간 응답 오류') from exc

    def request(self, api, body, path=ACCOUNT_PATH):
        self.authenticate()
        return self.post(path, body, api)[0]

    def rows(self, api, body, key):
        self.authenticate()
        result, cursor, seen = [], '', set()
        for page in range(100):
            data, headers = self.post(ACCOUNT_PATH, body, api, 'Y' if page else 'N', cursor)
            rows = data.get(key)
            if not isinstance(rows, list) or any(not isinstance(r, dict) for r in rows):
                raise Halt(f'{api}: {key} 응답 누락/형식 변경. 자동매매 중지')
            result.extend(rows)
            if headers.get('cont-yn', 'N') != 'Y':
                return result
            cursor = headers.get('next-key', '')
            if not cursor or cursor in seen:
                raise Halt('연속조회 키 오류')
            seen.add(cursor)
        raise Halt('연속조회 페이지 제한 초과')

    def verify_account(self, expected):
        data = self.request('ka00001', {})
        actual = data.get('acctNo')
        # A REST key is bound to its account. Never pass arbitrary account overrides.
        if not isinstance(actual, str) or actual.replace('-', '').strip() != expected:
            raise Halt('API 키 연결 계좌가 입력한 실전 계좌번호와 일치하지 않습니다.')

    def holdings(self):
        rows = self.rows('kt00018', {'qry_tp': '2', 'dmst_stex_tp': 'KRX'}, 'acnt_evlt_remn_indv_tot')
        result = {}
        for row in rows:
            if integer(row['rmnd_qty']) == 0:
                continue
            if row.get('crd_tp') != '00':
                raise Halt('신용 잔고가 있습니다. 현금계좌 전용 프로그램입니다.')
            symbol = code(row['stk_cd'])
            if symbol in result:
                raise Halt('동일 종목 복수 잔고가 발견되어 평균가를 확정할 수 없습니다.')
            qty, available = integer(row['rmnd_qty']), integer(row['trde_able_qty'])
            avg, current, cost = number(row['pur_pric']), abs(number(row['cur_prc'])), integer(row['pur_amt'])
            if available > qty or avg <= 0 or current <= 0 or cost <= 0:
                raise Halt('잔고 수량/평균가/현재가/매입원금 오류')
            result[symbol] = Holding(symbol, row['stk_nm'], qty, available, avg, current, cost)
        return result

    def account_snapshot(self):
        # Read-only parser includes all positions, including credit positions.
        # Trading retains its separate cash-only holdings validation.
        rows = self.rows('kt00018', {'qry_tp': '2', 'dmst_stex_tp': 'KRX'},
                         'acnt_evlt_remn_indv_tot')
        positions = []
        for row in rows:
            qty = integer(row['rmnd_qty'])
            if not qty:
                continue
            positions.append({
                'code': str(row['stk_cd']).removeprefix('A'), 'name': row['stk_nm'],
                'qty': qty, 'available': integer(row['trde_able_qty']),
                'average': number(row['pur_pric']), 'current': abs(number(row['cur_prc'])),
                'cost': integer(row['pur_amt']), 'value': integer(row['evlt_amt']),
                'profit': number(row['evltv_prft']), 'profit_rate': number(row['prft_rt']),
                'kind': row.get('crd_tp_nm') or ('현금' if row.get('crd_tp') == '00' else '신용/기타')})
        deposit = self.request('kt00001', {'qry_tp': '3'})
        cost = sum(p['cost'] for p in positions)
        profit = sum((p['profit'] for p in positions), D(0))
        return {'positions': positions, 'deposit': number(deposit['entr']),
                'cash_available': number(deposit['100stk_ord_alow_amt']),
                'd2_deposit': number(deposit['d2_entra']), 'total_cost': cost,
                'total_value': sum(p['value'] for p in positions), 'total_profit': profit,
                'total_profit_rate': profit / cost * 100 if cost else D(0)}

    def pending(self, symbols):
        rows = self.rows('ka10075', {'all_stk_tp': '0', 'trde_tp': '0',
                                    'stk_cd': '', 'stex_tp': '0'}, 'oso')
        return [r for r in rows if code(r['stk_cd']) in symbols and integer(r['oso_qty']) > 0]

    def quote(self, symbol, expected_name):
        data = self.request('ka10001', {'stk_cd': symbol}, '/api/dostk/stkinfo')
        if code(data['stk_cd']) != symbol or data['stk_nm'].strip() != expected_name.strip():
            raise Halt('시세 종목코드/종목명이 설정과 일치하지 않습니다.')
        value = abs(number(data['cur_prc']))
        if value <= 0 or value != value.to_integral_value():
            raise Halt('현재가 오류')
        # The last traded price already conforms to the instrument's tick size.
        return value

    def cash(self):
        data = self.request('kt00001', {'qry_tp': '3'})
        if number(data['ch_uncla']) > 0 or number(data['d2_entra']) < 0:
            raise Halt('현금미수금 또는 D+2 예수금 부족 상태입니다.')
        return max(D(0), min(number(data['100stk_ord_alow_amt']), number(data['ord_alow_amt']),
                             number(data['d2_entra'])))

    def order(self, side, symbol, qty, price):
        if side not in ('매수', '매도') or not isinstance(qty, int) or qty <= 0 or price <= 0:
            raise Halt('주문 인자 오류')
        api = 'kt10000' if side == '매수' else 'kt10001'
        data = self.request(api, {'dmst_stex_tp': 'KRX', 'stk_cd': code(symbol),
                                 'ord_qty': str(qty), 'ord_uv': str(int(price)),
                                 'trde_tp': '0', 'cond_uv': ''}, '/api/dostk/ordr')
        order = str(data.get('ord_no', '')).strip()
        if not order.isdigit() or int(order) <= 0:
            raise Halt('주문번호 응답 오류. 영웅문에서 접수 여부를 확인하세요.')
        return order

    def fills(self, order_id, symbol, side, expected):
        rows = self.rows('kt00009', {'ord_dt': datetime.now(KST).strftime('%Y%m%d'),
                                    'stk_bond_tp': '1', 'mrkt_tp': '0',
                                    'sell_tp': '1' if side == '매도' else '2', 'qry_tp': '1',
                                    'stk_cd': symbol, 'fr_ord_no': '', 'dmst_stex_tp': 'KRX'},
                         'acnt_ord_cntr_prst_array')
        return fill_totals(rows, order_id, symbol, side, expected)


class MarketClock:
    """KRX holiday/special-open calendar; exclude first minute and closing auction."""
    def __init__(self):
        import exchange_calendars as xcals
        today = datetime.now(KST).date()
        self.calendar = xcals.get_calendar('XKRX', start=str(today - timedelta(days=7)),
                                          end=str(today + timedelta(days=370)))

    def __call__(self):
        import pandas as pd
        now = pd.Timestamp.now(tz='UTC')
        label = pd.Timestamp(datetime.now(KST).date())
        try:
            if not self.calendar.is_session(label):
                return False
            opened = self.calendar.session_open(label) + pd.Timedelta(minutes=1)
            closed = self.calendar.session_close(label) - pd.Timedelta(minutes=10)
            return opened <= now < closed
        except Exception as exc:
            raise Halt('KRX 거래일 달력을 확인할 수 없습니다. 자동매매 중지') from exc
