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
from core import Config, Rule, Holding, D, Halt, Journal, Engine, KST, buy_qty, sale_qty, gap_snapshot, rounded_lot, fill_totals, number
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

    def market_buy_bound(self,symbol,price):
        return price

    def cash(self):
        return self.cash_value

    def cash_order_limit(self, symbol, price):
        return {'quantity':1000000,'amount':'100000000000','api':'kt00010'}

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

    def test_unknown_sell_blocked_after_restart(self):
        self.price(9500)
        self.broker.unknown_sell = True
        with self.assertRaises(Halt):
            self.engine.cycle()
        with self.assertRaises(Halt):
            self.engine.cycle()
        self.assertEqual(len(self.broker.orders),1)

    def test_cap_prevents_orders(self):
        self.price(9500)
        self.engine.config = replace(config(), cap=1_000_000)
        with self.assertRaises(Halt):
            self.engine.cycle()
        self.assertFalse(self.broker.orders)

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

    def test_user_stop_and_market_closed_do_not_order(self):
        self.engine.stop.set()
        with self.assertRaises(Halt):
            self.engine.cycle()
        self.engine.stop.clear()
        self.engine.market_open=lambda:False
        with self.assertRaises(Halt):
            self.engine.cycle()
        self.assertFalse(self.broker.orders)

    def test_two_way_gap_trades_without_fixed_b_role(self):
        self.broker.hs['005930']=replace(self.broker.hs['005930'],current=D(10500))
        self.assertTrue(self.engine.cycle());self.assertEqual(self.broker.orders[0][:2],('매도','005930'))
        self.assertFalse(self.journal.gap_armed(self.broker.account,'005930','000660'))

    def test_completed_average_down_below_threshold_rearms(self):
        self.broker.hs['005930']=replace(self.broker.hs['005930'],current=D(10100))
        self.price(9600);self.assertTrue(self.engine.cycle())
        self.assertTrue(self.journal.gap_armed(self.broker.account,'005930','000660'))

    def test_market_buy_budget_bounds_full_fill_cost(self):
        self.price(9500)
        self.broker.market_buy_bound=lambda symbol,price:price*D('1.3')
        self.assertTrue(self.engine.cycle())
        sale,buy=self.broker.orders
        self.assertLessEqual(buy[2]*buy[3]*D('1.01'),sale[2]*sale[3])

    def test_transient_query_before_order_recovers_without_user(self):
        original=self.broker.verify_account;calls=[]
        def verify(account):
            calls.append(1)
            if len(calls)==1:raise __import__('core').ApiFailure('일시적 서버 오류')
            return original(account)
        self.broker.verify_account=verify
        waits=[]
        def wait(seconds):
            waits.append(seconds)
            if seconds==30:return False
            self.engine.stop.set();return True
        with patch.object(self.engine.stop,'wait',side_effect=wait):self.engine.run()
        stages=[p['stage'] for e,p in self.events if e=='audit']
        self.assertIn('QUERY_PAUSED',stages);self.assertIn('WAITING',stages)
        self.assertNotIn('HALTED',stages);self.assertFalse(self.broker.orders);self.assertIn(30,waits)

    def test_equal_returns_do_not_trade_even_when_b_falls(self):
        for symbol in self.broker.hs:self.broker.hs[symbol]=replace(self.broker.hs[symbol],current=D(9000))
        self.assertFalse(self.engine.cycle());self.assertFalse(self.broker.orders)

    def test_reentry_requires_below_then_recross_and_persists_restart(self):
        self.price(9000);self.assertTrue(self.engine.cycle())
        h=self.broker.hs['000660'];self.price(h.average*D('.90'))
        self.assertFalse(self.engine.cycle());self.assertEqual(len(self.broker.orders),2)
        self.engine=Engine(self.broker,config(),Journal(self.path),self.broker.account,lambda:True,lambda *a:None)
        self.assertFalse(self.engine.cycle())
        self.price(h.average);self.assertFalse(self.engine.cycle())
        self.price(h.average*D('.95'));self.assertTrue(self.engine.cycle())
        self.assertEqual([o[2] for o in self.broker.orders if o[0]=='매도'],[10,10])

    def test_daily_lots_round_half_up_and_restart_does_not_recompute(self):
        h=self.broker.hs['005930'];self.broker.hs[h.code]=replace(h,qty=45,available=45,cost=450000)
        self.assertFalse(self.engine.cycle())
        lots=self.journal.lots(datetime.now(KST).strftime('%Y%m%d'),self.broker.account,config(),self.broker.hs)
        self.assertEqual(lots['lots']['005930'],5)
        self.engine.config=replace(config(),a_sell_percent=D(20))
        self.assertFalse(self.engine.cycle())
        again=Journal(self.path).lots(datetime.now(KST).strftime('%Y%m%d'),self.broker.account,self.engine.config,self.broker.hs)
        self.assertEqual(again['lots']['005930'],5)

    def test_external_inventory_changes_block_orders(self):
        self.engine.cycle();self.broker.hs['005930']=replace(self.broker.hs['005930'],qty=99)
        with self.assertRaises(Halt):self.engine.cycle()
        self.assertFalse(self.broker.orders)

    def test_quantity_shortage_waits_without_shrinking_order(self):
        self.engine.cycle();self.broker.hs['005930']=replace(self.broker.hs['005930'],current=D(10500),available=3)
        self.assertFalse(self.engine.cycle());self.assertFalse(self.broker.orders)

    def test_partial_fill_blocks_followup_and_retains_active_cycle(self):
        self.price(9500);self.broker.partial=True
        with self.assertRaises(Halt):self.engine.cycle()
        self.assertEqual(len(self.broker.orders),1);self.assertTrue(self.journal.active())
        self.assertFalse(self.journal.gap_armed(self.broker.account,'005930','000660'))

    def test_both_fills_required_before_inventory_commit(self):
        self.price(9500)
        original=self.broker.order
        def order(side,*args):
            if side=='매수':raise Halt('매수 접수 불명')
            return original(side,*args)
        self.broker.order=order
        with self.assertRaises(Halt):self.engine.cycle()
        self.assertTrue(self.journal.active());self.assertFalse(self.journal.gap_armed(self.broker.account,'005930','000660'))

    def test_gap_exact_threshold_both_signs(self):
        hs=holdings();hs['000660']=replace(hs['000660'],current=D(9500))
        self.assertEqual(gap_snapshot(config(),hs)['direction'],'A_TO_B')
        hs['000660']=replace(hs['000660'],current=D(10500))
        self.assertEqual(gap_snapshot(config(),hs)['direction'],'B_TO_A')



class MathTests(unittest.TestCase):
    def test_limit_bounds(self):
        for cap in (999999,1000000001):
            with self.assertRaises(Halt):
                replace(config(),cap=cap).validate()
        for cap in (1000000,1000000000):
            replace(config(),cap=cap).validate()

    def test_buy_budgets(self):
        self.assertEqual(buy_qty(D(9000),400000,999999,999999,D(1)),44)
        self.assertEqual(buy_qty(D(9000),999999,400000,999999,D(1)),44)
        self.assertEqual(buy_qty(D(9000),999999,999999,400000,D(1)),44)

    def test_fraction_rounding_and_available(self):
        self.assertEqual(sale_qty(replace(holdings()['005930'],available=3),D(10)),0)
        self.assertEqual(rounded_lot(45,D(10)),5)
        self.assertEqual(rounded_lot(4,D(10)),0)

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
        self.assertEqual(kwargs['json']['trde_tp'],'3')
        self.assertEqual(kwargs['json']['ord_uv'],'')

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

class MarketOrderWireTests(unittest.TestCase):
    def broker(self,responses):
        return AdapterTests.adapter(self,responses)[0]

    def test_sell_market_payload_has_no_limit_price(self):
        broker=self.broker([Response({'return_code':0,'ord_no':'123'})])
        broker.order('매도','005930',4,D(10000))
        body=broker.session.calls[0][1]['json']
        self.assertEqual(body['trde_tp'],'3');self.assertEqual(body['ord_uv'],'')
    def test_market_budget_uses_confirmed_upper_limit(self):
        broker=self.broker([Response({'return_code':0,'stk_cd':'005930','stk_nm':'A종목','cur_prc':'10000','upl_pric':'13000'})])
        price=broker.quote('005930','A종목')
        self.assertEqual(broker.market_buy_bound('005930',price),D(13000))
    def test_missing_upper_limit_blocks_market_budget(self):
        broker=self.broker([])
        with self.assertRaises(Halt):broker.market_buy_bound('005930',D(10000))
