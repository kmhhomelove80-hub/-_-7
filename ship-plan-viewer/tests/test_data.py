import tempfile
import unittest
from pathlib import Path
from datetime import datetime
from openpyxl import Workbook
from data import load_plan, matches, sort_key

class ExtractionTests(unittest.TestCase):
    def test_merges_headers_blank_rows_and_dates(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / 'test.xlsx'
            w = Workbook()
            s = w.active
            s.title = '선박등기구'
            s['A2'] = '생산\n일정'
            s.merge_cells('A2:A3')
            s['C2'] = '계획일'
            s.merge_cells('C2:C3')
            s['A4'] = '인증절차\n대기건'
            s.merge_cells('A4:A7')
            s['C4'] = datetime(2026, 11, 3)
            s['X5'] = 'PR-002'
            s['V5'] = 'LIGHT-5'
            s['W5'] = 'SEMI-5'
            s['Y5'] = 'PCB-5'
            s['AA5'] = 'CODE-5'
            s['V7'] = 'LIGHT-7'
            s['L5'] = 0
            s['S7'] = '검사 대기'
            s['X8'] = 'NO-SCHEDULE'
            w.save(path)
            before = path.read_bytes()
            result = load_plan(path)
            self.assertEqual([r for r, _ in result.rows], [4, 5, 7, 8])
            self.assertEqual([v[0] for _, v in result.rows], ['인증절차 대기건']*3 + [''])
            self.assertEqual(result.rows[0][1][1], '2026-11-03')
            self.assertEqual(result.rows[1][1][3], '0')
            self.assertEqual(result.headers[:2], ['생산일정', '계획일'])
            self.assertEqual(result.details[5], ['LIGHT-5', 'SEMI-5', 'PCB-5', 'CODE-5'])
            self.assertEqual(result.details[7], ['LIGHT-7', '', '', ''])
            self.assertEqual(path.read_bytes(), before)

    def test_missing_sheet(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / 'test.xlsx'
            Workbook().save(path)
            with self.assertRaisesRegex(ValueError, '선박등기구'):
                load_plan(path)

    def test_filter(self):
        row = ['관리자라인', '2026-11-03', '', '0', '2', '검사 대기', 'PR-002', 'ERP', '현장']
        self.assertTrue(matches(row, 'pr-002 검사'))
        self.assertFalse(matches(row, 'PR-002', column=5))
        self.assertTrue(matches(row, '검사', schedule='관리자라인'))
        self.assertFalse(matches(row, '', schedule=''))
        self.assertLess(sort_key('2'), sort_key('10'))

if __name__ == '__main__':
    unittest.main()
