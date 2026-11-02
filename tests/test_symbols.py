import unittest
from dataclasses import replace
from core import code, Rule, Config, Halt


class SymbolTests(unittest.TestCase):
    def test_numeric_and_kiwoom_prefix(self):
        for value in ('005930','A005930','a005930',' A005930 ','\ufeff005930\u200b','００５９３０',"'005930",'005 930'):
            self.assertEqual(code(value),'005930')

    def test_alphanumeric_codes(self):
        for value in ('0001A0','a0001a0','０００１ａ０'):
            self.assertEqual(code(value),'0001A0')
        self.assertEqual(code('0105F0'),'0105F0')
        self.assertEqual(code('A0105F0'),'0105F0')
        self.assertEqual(code('498400'),'498400')
        self.assertEqual(code('A12345'),'A12345')
        self.assertEqual(code('AA12345'),'A12345')

    def test_no_guessing_missing_digits_or_names(self):
        for value in ('5930','삼성전자','005930 삼성전자','KR7005930003','005930_NX','ABCDEF','0059300',''):
            with self.assertRaises(Halt):
                code(value)

    def test_rule_stores_canonical_code_for_api_and_holdings_lookup(self):
        rule=Rule(' A005930 ','삼성전자')
        self.assertEqual(rule.code,'005930')
        self.assertIn(rule.code,{'005930':object()})

    def test_duplicate_detected_after_normalization(self):
        config=Config((Rule('A005930','삼성전자'),Rule('005930','삼성전자')),1000000,10)
        with self.assertRaises(Halt):
            config.validate()


class LookupTests(unittest.TestCase):
    def adapter(self, data):
        from datetime import datetime, timedelta
        from broker import Kiwoom
        from core import KST
        from test_trader import Session, Response
        broker = Kiwoom('key','secret',Session([Response(dict(return_code=0,**data))]))
        broker.token='test'
        broker.expires=datetime.now(KST)+timedelta(hours=1)
        return broker

    def test_lookup_returns_name_and_canonical_code(self):
        broker=self.adapter(dict(stk_cd='A005930',stk_nm='삼성전자',cur_prc='-9000'))
        info=broker.stock_info(' A005930 ')
        self.assertEqual(info['code'],'005930')
        self.assertEqual(info['name'],'삼성전자')
        self.assertEqual(broker.session.calls[0][1]['json']['stk_cd'],'005930')

    def test_unexpected_symbol_rejected(self):
        broker=self.adapter(dict(stk_cd='000660',stk_nm='다른종목',cur_prc='9000'))
        with self.assertRaises(Halt):
            broker.stock_info('005930')
