import unittest
from unittest.mock import patch
from datetime import datetime,timedelta
from core import D, KST, ApiFailure
from broker import Kiwoom
from test_trader import Session, Response
from diagnostics import trade_history


class PurchaseRegressionTests(unittest.TestCase):
    def broker(self,responses):
        broker=Kiwoom('key','secret',Session(responses))
        broker.token='test'
        broker.expires=datetime.now(KST)+timedelta(hours=1)
        return broker

    def test_general_allowance_does_not_override_100_percent_cash(self):
        broker=self.broker([Response({'return_code':0,'100stk_ord_alow_amt':'62000',
            'ord_alow_amt':'3800','d2_entra':'62100','ch_uncla':'0'})])
        self.assertEqual(broker.cash(),D(62000))
        self.assertEqual(broker.last_cash_snapshot['order_allowance'],'3800')

    def test_target_specific_capacity_uses_only_100_percent_fields(self):
        broker=self.broker([Response({'return_code':0,'profa_100ord_alow_amt':'62000',
            'profa_100ord_alowq':'4','profa_40ord_alowq':'999','uncla':'0'})])
        result=broker.cash_order_limit('000660',D(14750))
        self.assertEqual(result['quantity'],4)
        self.assertEqual(result['amount'],'62000')
        body=broker.session.calls[0][1]['json']
        self.assertEqual(body['trde_tp'],'2')
        self.assertEqual(body['uv'],'14750')

    @patch('broker.time.sleep')
    def test_no_execution_data_is_polled_not_order_retried(self,_):
        broker=self.broker([
            Response({'return_code':20,'return_msg':'[2000](501724:관련자료가없습니다)'}),
            Response({'return_code':0,'acnt_ord_cntr_prst_array':[
                {'ord_no':'123','stk_cd':'A005930','io_tp_nm':'현금매도',
                 'cntr_no':'1','cntr_qty':'2','cntr_uv':'10000'}]})])
        self.assertEqual(broker.fills('123','005930','매도',2),(0,D(0)))
        self.assertEqual(broker.last_fill_query['status'],'NO_EXECUTIONS_YET')
        self.assertEqual(broker.fills('123','005930','매도',2),(2,D(20000)))
        self.assertTrue(all(call[1]['headers']['api-id']=='kt00009' for call in broker.session.calls))

    def test_other_errors_are_not_treated_as_empty_fills(self):
        broker=self.broker([Response({'return_code':20,'return_msg':'[2000](509193:CB 발동중입니다.)'})])
        with self.assertRaises(ApiFailure):
            broker.fills('123','005930','매도',2)

    def test_history_merges_cumulative_fills_without_double_counting(self):
        records=[]
        for stage,qty in [('ORDER_ACCEPTED',0),('PARTIAL_FILL',1),('FILL_CONFIRMED',2)]:
            records.append({'trace':'EXAMPLE','at':'2026-01-01T10:00:00+09:00','stage':stage,
                'explanation':'test','data':{'symbol':'005930','side':'매도','order':'123','qty':2,
                                           'filled_qty':qty,'gross':str(qty*10000)}})
        rows=trade_history(records)
        self.assertEqual(len(rows),1)
        self.assertEqual(rows[0]['filled_qty'],2)
        self.assertEqual(rows[0]['stage'],'FILL_CONFIRMED')
