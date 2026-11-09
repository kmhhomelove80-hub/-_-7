import json
import tempfile
from pathlib import Path
import unittest
from unittest.mock import patch
from core import ApiFailure, Halt, Journal, Engine, D
from diagnostics import readable
from test_trader import FakeBroker, config, Session, Response
from broker import Kiwoom
from datetime import datetime, timedelta
from core import KST


class DiagnosticsTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.journal=Journal(Path(self.temp.name)/'state.sqlite3')
        self.broker=FakeBroker()
        self.broker.hs['000660']=__import__('dataclasses').replace(self.broker.hs['000660'],current=D(9500))
        self.events=[]
        self.engine=Engine(self.broker,config(),self.journal,self.broker.account,lambda:True,lambda *e:self.events.append(e))
        self.broker.engine=self.engine

    def tearDown(self):
        self.temp.cleanup()

    def rows(self):
        return self.journal.audit_rows(self.engine.trace_id)

    def test_success_explains_reason_calculation_orders_and_new_basis(self):
        self.engine.cycle()
        rows=self.rows()
        signal=next(r for r in rows if r['stage']=='SIGNAL')
        self.assertEqual(signal['data']['state']['gap'],'5.00')
        self.assertEqual(signal['data']['strategy_revision'],'average-gap-market-v2')
        self.assertEqual(signal['data']['config_snapshot']['a_sell_percent'],'10')
        stages=[r['stage'] for r in rows]
        for stage in ('SELL_QUANTITY','PRE_SALE_BUDGET','ORDER_CALL','ORDER_ACCEPTED','FILL_CONFIRMED',
                      'POST_SALE_BUDGET','BALANCE_CHECK','PURCHASE_COMPLETED','COMPLETED'):
            self.assertIn(stage,stages)
        self.assertTrue(rows[-1]['data']['next_state']['armed'])
        self.assertTrue(all(r['trace']==self.engine.trace_id for r in rows))
        self.assertIn('갭',readable(signal))
        self.assertNotIn('1234567890',json.dumps(rows))

    def test_cash_only_delay_rechecks_without_selling_again(self):
        original=self.broker.cash
        calls=[]
        def cash():
            calls.append(1)
            if len(calls) in (2,3):
                return D(0)
            return original()
        self.broker.cash=cash
        with patch.object(self.engine.stop,'wait',return_value=False):
            self.engine.cycle()
        self.assertEqual(len(self.broker.orders),2)
        self.assertEqual(sum(r['stage']=='FUNDS_RECHECK' for r in self.rows()),2)

    def test_cash_never_available_halts_with_buy_not_sent(self):
        self.broker.cash=lambda:D(0)
        with patch.object(self.engine.stop,'wait',return_value=False):
            with self.assertRaises(Halt):
                self.engine.cycle()
        self.assertEqual(len(self.broker.orders),1)
        halted=self.rows()[-1]
        self.assertEqual(halted['stage'],'HALTED')
        self.assertEqual(halted['data']['orders'],{'SELL':'FILLED','BUY':'NOT_SENT'})
        self.assertEqual(halted['data']['completed_legs'],['SELL'])
        self.assertTrue(halted['data']['incomplete_cycle'])
        self.assertEqual(sum(r['stage']=='POST_SALE_BUDGET' for r in self.rows()),11)

    def test_target_capacity_can_prevent_purchase_even_with_account_cash(self):
        self.broker.cash_order_limit=lambda *args:{'quantity':0,'amount':'0','api':'kt00010'}
        with patch.object(self.engine.stop,'wait',return_value=False):
            with self.assertRaises(Halt):
                self.engine.cycle()
        self.assertEqual(len(self.broker.orders),1)
        self.assertEqual(self.rows()[-1]['data']['orders']['BUY'],'NOT_SENT')
        budget=next(row['data']['budget'] for row in self.rows() if row['stage']=='POST_SALE_BUDGET')
        self.assertEqual(budget['target_100_percent_capacity']['quantity'],0)

    def test_order_rejection_has_distinct_state_and_no_retry(self):
        original=self.broker.order
        def order(side,*args):
            if side=='매수':
                raise ApiFailure('매수가능금액 부족','REJECTED','kt10000','20')
            return original(side,*args)
        self.broker.order=order
        with self.assertRaises(Halt):
            self.engine.cycle()
        rows=self.rows()
        self.assertIn('ORDER_REJECTED',[r['stage'] for r in rows])
        self.assertEqual(rows[-1]['data']['orders']['BUY'],'REJECTED')
        self.assertEqual(len(self.broker.orders),1)

    def test_unknown_response_distinct_from_not_sent(self):
        def order(*args):
            raise ApiFailure('응답 끊김','UNKNOWN','kt10001')
        self.broker.order=order
        with self.assertRaises(Halt):
            self.engine.cycle()
        self.assertEqual(self.rows()[-1]['data']['orders']['SELL'],'UNKNOWN')
        self.assertEqual(self.rows()[-1]['data']['orders']['BUY'],'NOT_SENT')

    def test_audit_history_survives_restart(self):
        self.engine.cycle()
        other=Journal(self.journal.path)
        self.assertEqual(other.audit_rows(self.engine.trace_id),self.rows())
        self.assertEqual(other.audit_traces()[0][0],self.engine.trace_id)
        self.assertTrue(other.gap_armed(self.broker.account,'005930','000660'))


class WireTests(unittest.TestCase):
    def broker(self,responses):
        broker=Kiwoom('application-key','sensitive-secret',Session(responses))
        broker.token='sensitive-token'
        broker.expires=datetime.now(KST)+timedelta(hours=1)
        return broker

    def test_wire_attempt_recorded_and_business_rejection_scrubbed(self):
        broker=self.broker([Response({'return_code':20,'return_msg':'잔고 부족 sensitive-secret sensitive-token 1234567890'})])
        events=[]
        broker.audit_callback=lambda *e:events.append(e)
        with self.assertRaises(ApiFailure) as caught:
            broker.order('매수','005930',1,D(10000))
        self.assertEqual(events[0][0],'ORDER_TRANSMIT_ATTEMPT')
        self.assertEqual(caught.exception.outcome,'REJECTED')
        self.assertIn('잔고 부족',str(caught.exception))
        self.assertNotIn('sensitive',str(caught.exception))
        self.assertNotIn('1234567890',str(caught.exception))
        self.assertNotIn('authorization',str(events))

    def test_timeout_is_unknown_not_rejected(self):
        broker=self.broker([TimeoutError('sensitive-token')])
        with self.assertRaises(ApiFailure) as caught:
            broker.order('매수','005930',1,D(10000))
        self.assertEqual(caught.exception.outcome,'UNKNOWN')
        self.assertNotIn('sensitive-token',str(caught.exception))
        self.assertEqual(len(broker.session.calls),1)
