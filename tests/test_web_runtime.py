from datetime import datetime,timedelta
from dataclasses import replace
from pathlib import Path
import json
import tempfile
import threading
import unittest
from unittest.mock import MagicMock,patch
from core import ApiFailure,Halt,D,KST,pending_details
from web_runtime import Controller,MemoryVault,ReadRetryBroker,DEFAULT_CONFIG,stop_card
from test_trader import FakeBroker


class WebFakeBroker(FakeBroker):
    def __init__(self):
        super().__init__();self.session=MagicMock();self.audit_callback=None;self.quote_snapshots={}
    def quote(self,symbol,name):
        value=super().quote(symbol,name)
        self.quote_snapshots[symbol]={'price':str(value),'received_at':datetime.now(KST).isoformat()}
        return value
    def account_snapshot(self):
        return {'positions':[],'deposit':'0','cash_available':'0','d2_deposit':'0','total_cost':0,'total_value':0,'total_profit':'0','total_profit_rate':'0'}


class ControllerTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.vault=MemoryVault();self.brokers=[]
        def factory(*args):
            broker=WebFakeBroker();self.brokers.append(broker);return broker
        self.c=Controller(self.temp.name,self.vault,broker_factory=factory,clock_factory=lambda:lambda:True)
        self.c.save_credentials({'appkey':'private-app-key','secret':'private-app-secret','account':'1234567890'})
        cfg=dict(DEFAULT_CONFIG,a_code='005930',a_name='A종목',b_code='000660',b_name='B종목',cap='5000000')
        self.c.save_config(cfg)
    def tearDown(self):
        self.c.close()
        if self.c.trade_thread:self.c.trade_thread.join(3)
        if self.c.monitor_thread:self.c.monitor_thread.join(3)
        self.c.telegram.thread.join(1);self.c.email.thread.join(1)
        self.temp.cleanup()
    def test_snapshot_never_returns_credentials(self):
        data=json.dumps(self.c.snapshot(),default=str)
        self.assertNotIn('private-app-key',data)
        self.assertNotIn('private-app-secret',data)
        self.assertNotIn('1234567890',data)
        self.assertIn('******7890',data)
    def test_inspection_is_read_only_and_no_implicit_state_reset(self):
        report=self.c.inspect()
        self.assertTrue(report['can_start'])
        self.assertIsNone(self.c.journal.inventory('1234567890','005930','000660'))
        self.assertTrue(all(not b.orders for b in self.brokers))
    def test_start_requires_fresh_preview_and_both_confirmations(self):
        result=self.c.inspect()
        with self.assertRaises(Halt):self.c.start({'inspection_id':result['id'],'live_confirmed':True})
        with self.assertRaises(Halt):self.c.start({'inspection_id':result['id'],'live_confirmed':'false','preview_confirmed':'false'})
        self.c.inspection['created']-=31
        with self.assertRaises(Halt):self.c.start({'inspection_id':result['id'],'live_confirmed':True,'preview_confirmed':True})
        self.assertFalse(self.c.running())
    def test_stop_during_start_preparation_prevents_trading(self):
        result=self.c.inspect()
        entered=threading.Event();release=threading.Event()
        def slow_clock():
            entered.set();release.wait(3);return lambda:True
        self.c.clock_factory=slow_clock
        errors=[]
        def start():
            try:self.c.start({'inspection_id':result['id'],'live_confirmed':True,'preview_confirmed':True})
            except Exception as exc:errors.append(exc)
        thread=threading.Thread(target=start);thread.start()
        self.assertTrue(entered.wait(2))
        self.c.stop();release.set();thread.join(3)
        self.assertFalse(errors)
        self.assertFalse(self.c.running())
        self.assertTrue(self.c.engine.stop.is_set())
        self.assertTrue(all(not b.orders for b in self.brokers))

    def test_configuration_change_invalidates_preview(self):
        result=self.c.inspect();self.c.save_config(dict(self.c.config,gap='3'))
        with self.assertRaises(Halt):self.c.require_inspection(result['id'])
    def test_incomplete_reconciliation_is_separate_and_never_places_orders(self):
        identifier=self.c.journal.reserve(datetime.now(KST).strftime('%Y%m%d'),'1234567890',10,{})
        report=self.c.inspect();self.assertFalse(report['can_start'])
        with self.assertRaises(Halt):self.c.reconcile({'inspection_id':report['id']})
        self.c.reconcile({'inspection_id':report['id'],'confirmation':'HTS_CHECKED'})
        self.assertFalse(self.c.journal.active());self.assertFalse(self.c.running())
        self.assertTrue(all(not b.orders for b in self.brokers))
        self.assertEqual(self.c.journal.count(datetime.now(KST).strftime('%Y%m%d'),'1234567890'),1)
    def test_demo_forbids_real_mutations(self):
        demo=Controller(Path(self.temp.name)/'demo',MemoryVault(),demo=True)
        try:
            with self.assertRaises(Halt):demo.save_credentials({'appkey':'x'})
            with self.assertRaises(Halt):demo.start({})
            with self.assertRaises(Halt):demo.inspect()
        finally:demo.close();demo.telegram.thread.join(1);demo.email.thread.join(1)
    def test_immediate_signal_is_explained_before_start(self):
        original=self.c.broker_factory
        def factory(*args):
            broker=original(*args);broker.hs['000660']=replace(broker.hs['000660'],current=D(9500));return broker
        self.c.broker_factory=factory
        report=self.c.inspect()
        self.assertIn('즉시',report['immediate'])
        self.assertIn('A종목',report['immediate'])
        self.assertFalse(self.c.running())

    def test_repeated_monitor_error_does_not_override_fresh_trade_checks(self):
        self.c.schedule['enabled']=True;self.c.telegram.notify=MagicMock();self.c.email.notify=MagicMock()
        with patch.object(self.c,'running',return_value=True):
            for i in range(5):self.c.event('balance_error','일시적 표시 조회 오류')
        self.assertFalse(self.c.schedule['blocked']);self.assertFalse(self.c.active_stop.is_set())
        self.assertEqual(self.c.telegram.notify.call_count,1)

    def test_legacy_reference_settings_not_imported_into_new_strategy(self):
        self.c.config_path.write_text(json.dumps(dict(self.c.config,revision='old-b-only',initial_price='12000',initial_mode='SELL',a_sell='50',b_sell='5')))
        other=Controller(Path(self.temp.name),self.vault,broker_factory=lambda *a:WebFakeBroker(),clock_factory=lambda:lambda:True,autoconnect=False)
        try:
            self.assertNotIn('initial_price',other.config);self.assertNotIn('initial_mode',other.config)
            self.assertEqual(other.config['a_sell'],'10');self.assertEqual(other.config['b_sell'],'10')
            self.assertEqual(other.config['revision'],'average-gap-market-v2')
        finally:other.close();other.telegram.thread.join(1);other.email.thread.join(1)


class RetryTests(unittest.TestCase):
    def test_transient_read_retries_and_recovers(self):
        broker=MagicMock();broker.holdings.side_effect=[ApiFailure('조회 오류'),ApiFailure('조회 오류'),{}]
        events=[];stop=threading.Event()
        with patch.object(stop,'wait',return_value=False):
            result=ReadRetryBroker(broker,lambda *e:events.append(e),'trade',stop).holdings()
        self.assertEqual(result,{})
        self.assertEqual(broker.holdings.call_count,3)
        self.assertEqual([x[0] for x in events],['query_retry','query_retry','query_restored'])
    def test_business_query_error_not_blindly_retried(self):
        broker=MagicMock();broker.holdings.side_effect=ApiFailure('인증 오류','QUERY_FAILED','kt00018','20')
        with self.assertRaises(ApiFailure):ReadRetryBroker(broker,lambda *a:None,'trade').holdings()
        self.assertEqual(broker.holdings.call_count,1)
    def test_order_timeout_is_not_retried(self):
        broker=WebFakeBroker();broker.quote('005930','A종목');broker.order=MagicMock(side_effect=ApiFailure('응답 불명','UNKNOWN','kt10000'))
        wrapper=ReadRetryBroker(broker,lambda *a:None,'trade')
        with self.assertRaises(ApiFailure):wrapper.order('매수','005930',1,D(10000))
        self.assertEqual(broker.order.call_count,1)
    def test_stale_quote_blocks_before_order(self):
        broker=WebFakeBroker();broker.quote_snapshots['005930']={'received_at':(datetime.now(KST)-timedelta(seconds=31)).isoformat()}
        with self.assertRaises(ApiFailure) as exc:ReadRetryBroker(broker,lambda *a:None,'trade').order('매수','005930',1,D(10000))
        self.assertEqual(exc.exception.outcome,'NOT_SENT_STALE')
        self.assertFalse(broker.orders)


class PendingCardTests(unittest.TestCase):
    def test_pending_details_strip_account_and_identify_origin(self):
        result=pending_details([{'acnt_no':'1234567890','ord_no':'000123','stk_cd':'A005930','oso_qty':'6','cntr_qty':'4','ord_qty':'10','ord_pric':'10000','tm':'103000'}],{'123'})
        self.assertNotIn('1234567890',str(result));self.assertEqual(result[0]['origin'],'이 앱 주문')
        self.assertEqual(result[0]['remaining'],'6')
    def test_unknown_order_origin_is_not_assumed_to_be_manual(self):
        result=pending_details([{'ord_no':'321','oso_qty':'1'}],set())
        self.assertIn('출처 확인 불가',result[0]['origin'])
    def test_stop_card_distinguishes_order_unknown_from_query_failure(self):
        card=stop_card({'trace':'t','explanation':'응답 끊김','data':{'order_outcome':'UNKNOWN','orders':{'BUY':'UNKNOWN'}}},[])
        self.assertIn('접수 여부 불명',card['title'])
        self.assertIn('재전송',str(card['actions']))
