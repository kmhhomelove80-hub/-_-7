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
from core import Config, Rule, Holding, D, Halt, Journal, Engine, KST, buy_qty, sale_qty, select_target, fill_totals, number
from broker import Kiwoom


def config(daily=3):
    return Config((Rule('005930', 'A종목', D(10), D(20)),
                   Rule('000660', 'B종목', D(10), D(20))), 5_000_000, daily)


def holdings():
    return {'005930': Holding('005930', 'A종목', 100, 100, D(10000), D(9000), 1_000_000),
            '000660': Holding('000660', 'B종목', 100, 100, D(20000), D(20000), 2_000_000)}


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
            raise Halt('통신 끊김: 접수 여부 불명')
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
            return 1, D(20000)
        price = self.orders[int(order)-1][3]
        return expected, price * expected


class EngineTests(unittest.TestCase):
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

    def test_full_cycle_with_zero_initial_cash(self):
        self.assertTrue(self.engine.cycle())
        self.assertEqual([x[0] for x in self.broker.orders], ['매도','매수'])
        self.assertEqual(self.broker.orders[0][2], 20)
        self.assertEqual(self.broker.orders[1][2], 44)
        self.assertEqual(self.broker.hs['005930'].qty, 144)
        self.assertLess(self.broker.hs['005930'].drop, D(10))
        self.assertFalse(self.journal.active())
        self.assertFalse(self.engine.cycle())
        self.assertEqual(len(self.broker.orders), 2)

    def test_larger_drop_first(self):
        h = self.broker.hs['000660']
        self.broker.hs[h.code] = replace(h, current=D(16000))
        self.engine.cycle()
        self.assertEqual(self.broker.orders[0][1], '005930')
        self.assertEqual(self.broker.orders[1][1], '000660')

    def test_equal_drop_a_first(self):
        hs = holdings()
        hs['000660'] = replace(hs['000660'], current=D(18000))
        self.assertEqual(select_target(config(), hs), 0)

    def test_unknown_sell_is_not_retried_after_restart(self):
        self.broker.unknown_sell = True
        with self.assertRaises(Halt):
            self.engine.cycle()
        self.assertEqual(len(self.broker.orders), 1)
        journal2 = Journal(self.path)
        restarted = Engine(self.broker, config(), journal2, self.broker.account, lambda: True, lambda *a: None)
        with self.assertRaises(Halt):
            restarted.cycle()
        self.assertEqual(len(self.broker.orders), 1)
        self.assertEqual(journal2.active()[0][3], 'SELL_INTENT')

    def test_partial_sell_stop_never_buys(self):
        self.broker.partial = True
        with self.assertRaises(Halt):
            self.engine.cycle()
        self.assertEqual(len(self.broker.orders), 1)
        self.assertTrue(self.journal.active())

    def test_daily_limit_persists_across_restart(self):
        self.engine.config = config(1)
        self.engine.cycle()
        other = Engine(self.broker, config(1), Journal(self.path), self.broker.account, lambda: True, lambda *a: None)
        with self.assertRaises(Halt):
            other.cycle()
        self.assertEqual(len(self.broker.orders), 2)

    def test_failure_reserves_daily_slot_even_after_reconcile(self):
        ident = self.journal.reserve('20261102', 'acct', 1, {})
        self.journal.reconcile(ident)
        with self.assertRaises(Halt):
            self.journal.reserve('20261102', 'acct', 1, {})
        self.journal.reserve('20261103', 'acct', 1, {})

    def test_cap_prevents_any_order(self):
        self.engine.config = replace(config(), cap=500_000)
        with self.assertRaises(Halt):
            self.engine.cycle()
        self.assertEqual(self.broker.orders, [])

    def test_minimum_quantity_prevents_sale(self):
        self.engine.config = replace(config(), rules=(replace(config().rules[0], sell_percent=D('.01')), config().rules[1]))
        with self.assertRaises(Halt):
            self.engine.cycle()
        self.assertFalse(self.broker.orders)

    def test_existing_pending_order_blocks(self):
        self.broker.pending_orders = [{'ord_no':'1'}]
        with self.assertRaises(Halt):
            self.engine.cycle()
        self.assertFalse(self.broker.orders)

    def test_wrong_account_blocks(self):
        self.engine.account = '9999999999'
        with self.assertRaises(Halt):
            self.engine.cycle()
        self.assertFalse(self.broker.orders)

    def test_market_closed_blocks(self):
        self.engine.market_open = lambda: False
        with self.assertRaises(Halt):
            self.engine.cycle()
        self.assertFalse(self.broker.orders)

    def test_stop_blocks_order(self):
        self.engine.request_stop()
        with self.assertRaises(Halt):
            self.engine.cycle()
        self.assertFalse(self.broker.orders)

    def test_stop_during_account_check_blocks_send(self):
        original = self.broker.verify_account
        calls = []
        def verify(expected):
            original(expected)
            calls.append(expected)
            if len(calls) == 2:
                self.engine.stop.set()
        self.broker.verify_account = verify
        with self.assertRaises(Halt):
            self.engine.cycle()
        self.assertFalse(self.broker.orders)

    def test_buy_timeout_not_retried(self):
        original = self.broker.order
        def order(side, *args):
            if side == '매수':
                self.broker.orders.append((side, *args))
                raise Halt('매수 응답 끊김')
            return original(side, *args)
        self.broker.order = order
        with self.assertRaises(Halt):
            self.engine.cycle()
        self.assertEqual(len(self.broker.orders), 2)
        self.assertEqual(self.journal.active()[0][3], 'BUY_INTENT')

    def test_external_trade_detected_before_buy(self):
        original = self.broker.order
        def order(side, *args):
            result = original(side, *args)
            if side == '매도':
                h = self.broker.hs['005930']
                self.broker.hs[h.code] = replace(h, qty=h.qty+1)
            return result
        self.broker.order = order
        with patch.object(self.engine.stop, 'wait', return_value=False):
            with self.assertRaises(Halt):
                self.engine.cycle()
        self.assertEqual(len(self.broker.orders), 1)

    def test_immediate_repeat_until_daily_limit(self):
        self.broker.hs['005930'] = replace(self.broker.hs['005930'], current=D(5000))
        self.engine.config = config(2)
        self.engine.run()
        self.assertEqual(len(self.broker.orders), 4)
        self.assertFalse(self.journal.active())


class MathTests(unittest.TestCase):
    def test_buy_respects_all_three_budgets_and_cost_reserve(self):
        for proceeds, cash, headroom in [(400000, 999999,999999),(999999,400000,999999),(999999,999999,400000)]:
            n = buy_qty(D(9000), proceeds, cash, headroom, D(1))
            self.assertEqual(n, 44)
            self.assertLessEqual(n * D(9000) * D('1.01'), min(proceeds,cash,headroom))

    def test_sale_uses_holdings_fraction_and_available_cap(self):
        h = replace(holdings()['005930'], available=12)
        self.assertEqual(sale_qty(h, D(20)), 12)
        self.assertEqual(sale_qty(replace(h, qty=7, available=7), D(20)), 1)

    def test_nan_inf_not_accepted(self):
        for value in ['NaN','Infinity','-Infinity','']:
            with self.assertRaises(Halt):
                number(value)

    def test_config_bounds(self):
        for cap in (499999, 1000000001):
            with self.assertRaises(Halt):
                replace(config(), cap=cap).validate()
        for cap in (500000, 1000000000):
            replace(config(), cap=cap).validate()
        with self.assertRaises(Halt):
            replace(config(), daily_max=0).validate()

    def test_execution_dedup_and_weighted_amount(self):
        a = {'ord_no':'0000012','stk_cd':'A005930','io_tp_nm':'현금매도',
             'cntr_qty':'3','cntr_uv':'9000','cntr_no':'1'}
        b = dict(a, cntr_qty='2', cntr_uv='9100', cntr_no='2')
        self.assertEqual(fill_totals([a,a,b], '12', '005930','매도',5), (5,D(45200)))
        with self.assertRaises(Halt):
            fill_totals([a, dict(a, cntr_qty='4')], '12', '005930','매도',5)
        with self.assertRaises(Halt):
            fill_totals([dict(a, stk_cd='000660')], '12', '005930','매도',5)
        with self.assertRaises(Halt):
            fill_totals([a,b], '12', '005930','매도',4)


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
