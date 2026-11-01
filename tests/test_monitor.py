from datetime import datetime, timedelta
from pathlib import Path
import sys
import threading
import unittest
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from broker import Kiwoom
from core import D, Halt, KST
from monitor import AccountMonitor
from test_trader import Response, Session


class SnapshotTests(unittest.TestCase):
    def broker(self, positions):
        session = Session([
            Response({'return_code': 0, 'acnt_evlt_remn_indv_tot': positions}),
            Response({'return_code': 0, 'entr': '123456', '100stk_ord_alow_amt': '100000', 'd2_entra': '-1000'})])
        broker = Kiwoom('key', 'secret', session)
        broker.token = 'test'
        broker.expires = datetime.now(KST) + timedelta(hours=2)
        return broker, session

    def position(self, **kw):
        row = dict(stk_cd='A005930', stk_nm='삼성전자', rmnd_qty='10', trde_able_qty='8',
                   pur_pric='10000', cur_prc='-9000', pur_amt='100000', evlt_amt='90000',
                   evltv_prft='-10200', prft_rt='-10.20', crd_tp='00', crd_tp_nm='')
        row.update(kw)
        return row

    @patch('broker.time.sleep')
    def test_all_positions_and_negative_deposit_can_be_displayed(self, _):
        broker, session = self.broker([self.position(), self.position(stk_cd='A000660', crd_tp='03', crd_tp_nm='자기융자')])
        data = broker.account_snapshot()
        self.assertEqual(len(data['positions']), 2)
        self.assertEqual(data['positions'][0]['current'], D(9000))
        self.assertEqual(data['positions'][1]['kind'], '자기융자')
        self.assertEqual(data['total_cost'], 200000)
        self.assertEqual(data['total_value'], 180000)
        self.assertEqual(data['total_profit'], D(-20400))
        self.assertEqual(data['total_profit_rate'], D('-10.20'))
        self.assertEqual(data['d2_deposit'], D(-1000))
        self.assertTrue(all('ordr' not in call[0] for call in session.calls))

    @patch('broker.time.sleep')
    def test_empty_account_zero_totals(self, _):
        broker, _ = self.broker([])
        data = broker.account_snapshot()
        self.assertEqual(data['positions'], [])
        self.assertEqual(data['total_profit_rate'], 0)
        self.assertEqual(data['total_cost'], 0)

    @patch('broker.time.sleep')
    def test_display_has_no_dependency_on_strategy_codes(self, _):
        broker, _ = self.broker([self.position(stk_cd='A035420', stk_nm='NAVER')])
        self.assertEqual(broker.account_snapshot()['positions'][0]['code'], '035420')


class MonitorBroker:
    def __init__(self):
        self.session = self
        self.closed = False
        self.calls = 0
        self.stop_on_snapshot = None
        self.fail = False

    def verify_account(self, account):
        if account != '1234567890':
            raise Halt('wrong account')

    def account_snapshot(self):
        self.calls += 1
        if self.fail:
            raise Halt('temporary error')
        if self.stop_on_snapshot:
            self.stop_on_snapshot.set()
        return {'positions': []}

    def close(self):
        self.closed = True


class MonitorTests(unittest.TestCase):
    def test_masked_account_and_freshness_timestamp(self):
        broker = MonitorBroker()
        events = []
        monitor = AccountMonitor(broker, '1234567890', lambda *e: events.append(e))
        snapshot = monitor.refresh()
        self.assertEqual(snapshot['account_label'], '******7890')
        self.assertIn('updated_at', snapshot)
        self.assertIn('received_at', snapshot)
        self.assertEqual(events[0][0], 'balance')
        self.assertNotIn('1234567890', str(events))

    def test_stop_during_request_does_not_publish_stale_result(self):
        broker = MonitorBroker()
        stop = threading.Event()
        broker.stop_on_snapshot = stop
        events = []
        monitor = AccountMonitor(broker, '1234567890', lambda *e: events.append(e), stop)
        monitor.run()
        self.assertFalse(events)
        self.assertTrue(broker.closed)

    def test_transient_read_error_retries_and_closes_session(self):
        broker = MonitorBroker()
        broker.fail = True
        stop = threading.Event()
        events = []
        def emit(event, data):
            events.append(event)
            if event == 'balance_error':
                broker.fail = False
            else:
                stop.set()
        monitor = AccountMonitor(broker, '1234567890', emit, stop, interval=.01)
        monitor.run()
        self.assertEqual(events, ['balance_error', 'balance'])
        self.assertEqual(broker.calls, 2)
        self.assertTrue(broker.closed)

    def test_account_mismatch_never_reads_snapshot(self):
        broker = MonitorBroker()
        monitor = AccountMonitor(broker, '9999999999', lambda *e: None)
        with self.assertRaises(Halt):
            monitor.refresh()
        self.assertEqual(broker.calls, 0)
