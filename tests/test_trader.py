import copy
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from core import Config, Rule, Holding, D, Halt, Journal, Engine, KST, buy_qty, sale_qty, signal, fill_totals, number
from broker import Kiwoom


def config(daily=10):
    return Config((Rule('005930', 'A종목'), Rule('000660', 'B종목')), 5_000_000, daily)


def holdings():
    return {'005930': Holding('005930', 'A종목', 100, 100, D(10000), D(10000), 1_000_000),
            '000660': Holding('000660', 'B종목', 100, 100, D(10000), D(10000), 1_000_000)}


class FakeBroker:
    def __init__(self):
        self.hs = holdings()
        self.orders = []
        self.unknown_sell = False
        self.pending_orders = []
        self.cash_value = D(0)
        self.partial = False
        self.engine = None
        self.account = '1234567890'

    def verify_account(self, expected):
        if expected != self.account:
            raise Halt('계좌 불일치')

    def holdings(self):
        return copy.deepcopy(self.hs)

    def pending(self, symbols):
        return self.pending_orders

    def quote(self, symbol, name):
        return self.hs[symbol].current

    def cash(self):
        return self.cash_value

    def order(self, side, symbol, qty, price):
        self.orders.append((side, symbol, qty, price))
        if self.unknown_sell and side == '매도':
            raise Halt('접수 여부 불명')
        h = self.hs[symbol]
        if not self.partial:
            if side == '매도':
                cost = int(D(h.cost) * (h.qty - qty) / h.qty)
                self.hs[symbol] = replace(h, qty=h.qty-qty, available=h.available-qty, cost=cost)
                self.cash_value += qty * price
            else:
                cost = h.cost + int(qty * price)
                self.hs[symbol] = replace(h, qty=h.qty+qty, available=h.available+qty,
                                         cost=cost, average=D(cost)/(h.qty+qty))
                self.cash_value -= qty * price
        return str(len(self.orders))

    def fills(self, order, symbol, side, expected):
        if self.partial:
            self.engine.stop.set()
            return 1, D(10000)
        return expected, self.orders[int(order)-1][3] * expected


class StrategyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / 'journal.sqlite3'
        self.journal = Journal(self.path)
        self.broker = FakeBroker()
        self.events = []
        self.engine = Engine(self.broker, config(), self.journal, self.broker.account,
                             lambda: True, lambda k,v: self.events.append((k,v)))
        self.broker.engine = self.engine

    def tearDown(self):
        self.tmp.cleanup()

    def price(self, price):
        h = self.broker.hs['000660']
        self.broker.hs[h.code] = replace(h, current=D(price))

    def state(self):
        return self.journal.state(self.broker.account, '005930', '000660')

    def test_10500_sell_then_9975_buy_then_10473_75_sell(self):
        self.price(10500)
        self.assertTrue(self.engine.cycle())
        self.assertEqual(self.broker.orders[0][:3], ('매도','000660',5))
        self.assertEqual(self.broker.orders[1][1], '005930')
        self.assertEqual(self.state(), {'reference':'10500', 'mode':'BUY'})
        self.assertFalse(self.engine.cycle())
        self.price(9976)
        self.assertFalse(self.engine.cycle())
        self.price(9975)
        self.assertTrue(self.engine.cycle())
        self.assertEqual(self.broker.orders[2][:3], ('매도','005930',10))
        self.assertEqual(self.broker.orders[3][1], '000660')
        self.assertEqual(self.state(), {'reference':'9975', 'mode':'SELL'})
        self.price(10473)
        self.assertFalse(self.engine.cycle())
        self.price(10474)
        self.assertTrue(self.engine.cycle())
        self.assertEqual(self.state()['mode'], 'BUY')

    def test_after_buy_further_drop_does_not_buy_again(self):
        self.price(9500)
        self.engine.cycle()
        self.assertEqual(self.state(), {'reference':'9500', 'mode':'SELL'})
        self.price(8500)
        self.assertFalse(self.engine.cycle())
        self.assertEqual(len(self.broker.orders),2)

    def test_a_price_is_not_a_signal(self):
        h = self.broker.hs['005930']
        self.broker.hs[h.code] = replace(h, current=D(1000))
        self.assertFalse(self.engine.cycle())
        self.assertFalse(self.broker.orders)

    def test_initial_custom_price_and_direction(self):
        self.engine.config = replace(config(), initial_price=D(12000), initial_mode='BUY')
        self.price(12600)
        self.assertFalse(self.engine.cycle())
        self.price(11400)
        self.assertTrue(self.engine.cycle())

    def test_state_persists_across_restart_and_average_changes(self):
        self.price(10500)
        self.engine.cycle()
        self.price(9975)
        h = self.broker.hs['000660']
        self.broker.hs[h.code] = replace(h, average=D(30000))
        self.engine = Engine(self.broker, config(), Journal(self.path), self.broker.account,
                             lambda:True, lambda *a:None)
        self.assertTrue(self.engine.cycle())
        self.assertEqual(self.state()['reference'],'9975')

    def test_unknown_sell_blocked_after_restart(self):
        self.price(9500)
        self.broker.unknown_sell = True
        with self.assertRaises(Halt):
            self.engine.cycle()
        with self.assertRaises(Halt):
            self.engine.cycle()
        self.assertEqual(len(self.broker.orders),1)

    def test_partial_sell_never_buys_or_changes_reference(self):
        self.price(10500)
        self.broker.partial = True
        with self.assertRaises(Halt):
            self.engine.cycle()
        self.assertEqual(len(self.broker.orders),1)
        self.assertEqual(self.state()['reference'],'10000')
        self.assertTrue(self.journal.active())

    def test_b_sell_reference_saved_even_if_a_buy_fails(self):
        self.price(10500)
        original = self.broker.order
        def order(side, *args):
            if side == '매수':
                raise Halt('매수 응답 끊김')
            return original(side,*args)
        self.broker.order=order
        with self.assertRaises(Halt):
            self.engine.cycle()
        self.assertEqual(self.state(), {'reference':'10500','mode':'BUY'})
        self.assertTrue(self.journal.active())

    def test_cap_prevents_orders(self):
        self.price(9500)
        self.engine.config = replace(config(), cap=1_000_000)
        with self.assertRaises(Halt):
            self.engine.cycle()
        self.assertFalse(self.broker.orders)

    def test_daily_limit_shared_both_directions(self):
        self.engine.config = config(1)
        self.price(10500)
        self.engine.cycle()
        self.price(9975)
        with self.assertRaises(Halt):
            self.engine.cycle()
        self.assertEqual(len(self.broker.orders),2)

    def test_missing_a_inventory_does_not_impose_minimum_balance(self):
        self.broker.hs['005930'] = replace(self.broker.hs['005930'], qty=0, available=0,cost=0,average=D(0))
        self.price(10500)
        self.assertTrue(self.engine.cycle())
        self.assertGreater(self.broker.hs['005930'].qty,0)

    def test_reset_preserves_daily_count_and_blocks_active_cycle(self):
        self.price(10500)
        self.engine.cycle()
        day=datetime.now(KST).strftime('%Y%m%d')
        self.journal.reset_state(self.broker.account,'005930','000660')
        self.assertIsNone(self.state())
        self.assertEqual(self.journal.count(day,self.broker.account),1)
        self.journal.reserve(day,self.broker.account,10,{})
        with self.assertRaises(Halt):
            self.journal.reset_state(self.broker.account,'005930','000660')

    def test_stop_during_account_check_blocks_send(self):
        self.price(9500)
        original=self.broker.verify_account
        calls=[]
        def verify(expected):
            original(expected)
            calls.append(1)
            if len(calls)==2:
                self.engine.stop.set()
        self.broker.verify_account=verify
        with self.assertRaises(Halt):
            self.engine.cycle()
        self.assertFalse(self.broker.orders)

    def test_notice_events_cover_signal_orders_fills_and_completion(self):
        self.price(10500)
        self.engine.cycle()
        notices=[p for e,p in self.events if e=='notice']
        self.assertEqual(len(notices),6)
        self.assertTrue(notices[0]['subject'].endswith('순환매 신호 발생'))
        self.assertTrue(notices[-1]['subject'].endswith('순환매 완료'))
        self.assertIn('10500', str(self.state()))

    def test_user_stop_and_market_closed_do_not_order(self):
        self.engine.stop.set()
        with self.assertRaises(Halt):
            self.engine.cycle()
        self.engine.stop.clear()
        self.engine.market_open=lambda:False
        with self.assertRaises(Halt):
            self.engine.cycle()
        self.assertFalse(self.broker.orders)


class MathTests(unittest.TestCase):
    def test_limit_bounds(self):
        for cap in (999999,1000000001):
            with self.assertRaises(Halt):
                replace(config(),cap=cap).validate()
        for cap in (1000000,1000000000):
            replace(config(),cap=cap).validate()

    def test_signal_exact_thresholds(self):
        cfg=config()
        self.assertEqual(signal(cfg,{'reference':'10500','mode':'BUY'},D(9975)),'BUY')
        self.assertIsNone(signal(cfg,{'reference':'10500','mode':'BUY'},D(9976)))
        self.assertEqual(signal(cfg,{'reference':'9975','mode':'SELL'},D('10473.75')),'SELL')

    def test_buy_budgets(self):
        self.assertEqual(buy_qty(D(9000),400000,999999,999999,D(1)),44)
        self.assertEqual(buy_qty(D(9000),999999,400000,999999,D(1)),44)
        self.assertEqual(buy_qty(D(9000),999999,999999,400000,D(1)),44)

    def test_fraction_rounding_and_available(self):
        self.assertEqual(sale_qty(replace(holdings()['005930'],available=3),D(10)),3)
        self.assertEqual(sale_qty(replace(holdings()['000660'],qty=19,available=19),D(5)),0)

    def test_nonfinite_values_blocked(self):
        for value in ('NaN','Infinity',''):
            with self.assertRaises(Halt):
                number(value)

    def test_weighted_fills_deduplicated(self):
        a=dict(ord_no='0000012',stk_cd='A005930',io_tp_nm='현금매도',cntr_qty='3',cntr_uv='9000',cntr_no='1')
        b=dict(a,cntr_qty='2',cntr_uv='9100',cntr_no='2')
        self.assertEqual(fill_totals([a,a,b],'12','005930','매도',5),(5,D(45200)))
        with self.assertRaises(Halt):
            fill_totals([a,dict(a,cntr_qty='4')],'12','005930','매도',5)


class Response:
    def __init__(self, body, headers=None, status=200):
        self.body, self.headers, self.status_code = body, headers or {}, status
    def json(self):
        return self.body


class Session:
    def __init__(self, responses):
        self.responses, self.calls = list(responses), []
    def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        result = self.responses.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


class AdapterTests(unittest.TestCase):
    def adapter(self, responses):
        session = Session(responses)
        api = Kiwoom('key', 'secret', session)
        api.token = 'test-token'
        api.expires = datetime.now(KST) + timedelta(hours=2)
        return api, session

    @patch('broker.time.sleep')
    def test_pagination_and_headers(self, _):
        api, session = self.adapter([
            Response({'return_code':0,'oso':[{'n':1}]}, {'cont-yn':'Y','next-key':'next'}),
            Response({'return_code':0,'oso':[{'n':2}]})])
        self.assertEqual(api.rows('ka10075', {}, 'oso'), [{'n':1},{'n':2}])
        self.assertEqual(session.calls[1][1]['headers']['next-key'], 'next')
        self.assertEqual(session.calls[1][1]['headers']['cont-yn'], 'Y')

    def test_timeout_order_single_attempt(self):
        api, session = self.adapter([TimeoutError()])
        with self.assertRaises(Halt):
            api.order('매수','005930',1,D(9000))
        self.assertEqual(len(session.calls),1)
        url, kwargs = session.calls[0]
        self.assertEqual(url, 'https://api.kiwoom.com/api/dostk/ordr')
        self.assertFalse(kwargs['allow_redirects'])
        self.assertEqual(kwargs['json']['trde_tp'],'0')
        self.assertEqual(kwargs['json']['ord_uv'],'9000')

    def test_error_response_does_not_echo_secrets(self):
        api, _ = self.adapter([Response({'return_code':1,'return_msg':'secret-value'})])
        with self.assertRaises(Halt) as result:
            api.cash()
        self.assertNotIn('secret-value',str(result.exception))

    def test_account_mismatch(self):
        api, _ = self.adapter([Response({'return_code':0,'acctNo':'1234567890'})])
        with self.assertRaises(Halt):
            api.verify_account('9999999999')

    def test_official_account_field(self):
        api, _ = self.adapter([Response({'return_code':0,'acctNo':'1234567890'})])
        api.verify_account('1234567890')

    def test_missing_schema_blocks(self):
        api, _ = self.adapter([Response({'return_code':0})])
        with self.assertRaises(Halt):
            api.rows('kt00018',{},'acnt_evlt_remn_indv_tot')

    def test_cash_uses_100_percent_cash_and_d2(self):
        api, _ = self.adapter([Response({'return_code':0,'ch_uncla':'0','d2_entra':'5000',
                                        'ord_alow_amt':'100000','100stk_ord_alow_amt':'7000'})])
        self.assertEqual(api.cash(), D(5000))

    def test_credit_balance_rejected(self):
        api, _ = self.adapter([Response({'return_code':0,'acnt_evlt_remn_indv_tot':[
            {'rmnd_qty':'10','crd_tp':'03'}]})])
        with self.assertRaises(Halt):
            api.holdings()


if __name__ == '__main__':
    unittest.main()
