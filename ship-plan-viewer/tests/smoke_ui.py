"""Run in a Windows desktop session; all data is synthetic."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from unittest.mock import patch
import tempfile
from openpyxl import Workbook
from app import App
from data import load_plan, COLUMNS

with tempfile.TemporaryDirectory() as d:
    p = Path(d) / 'test.xlsx'
    w = Workbook()
    s = w.active
    s.title = '선박등기구'
    s['A4'] = '인증절차\n대기건'
    s.merge_cells('A4:A5')
    s['X4'] = 'PR-001'
    s['X5'] = 'PR-002'
    s['V5'] = 'LIGHT-5'
    s['W5'] = 'SEMI-5'
    s['Y5'] = 'PCB-5'
    s['AA5'] = 'CODE-5'
    s['V4'] = 'LIGHT-4'
    s['Z5'] = 'VERY-LONG-ERP-CODE-0123456789-ABCDEFGHIJ'
    w.save(p)
    with patch('app.filedialog.askopenfilename', return_value=''):
        app = App()
        app.update()
        app.plan = load_plan(p)
        app.scope_box.configure(values=['전체 항목'] + list(COLUMNS))
        app.schedule_box.configure(values=['전체 일정', '11월 3일'])
        app.schedule_values = [None, '11월 3일']
        app.fit_content_columns()
        for col in ('A', 'Z'):
            idx = COLUMNS.index(col)
            assert app.table.column(col, 'width') >= max(app.table_font.measure(v[idx]) for _, v in app.plan.rows) + 20
        for col in ('row', *COLUMNS):
            assert app.table.column(col, 'anchor') == ('e' if col in ('L', 'P') else 'center')
            assert app.detail_row.column(col, 'anchor') == app.table.column(col, 'anchor')
        assert app.plan.rows[0][1][0] == '인증절차 대기건'
        app.reset()
        assert len(app.table.get_children()) == 2
        app.query.set('pr-002')
        app.refresh()
        assert len(app.table.get_children()) == 1
        app.table.selection_set('5')
        app.show_detail()
        assert 'PR-002' in app.detail_row.item('5', 'values')
        assert '기본 정보' not in app.detail.get('1.0', 'end')
        assert len(app.detail.get('1.0', 'end').strip().splitlines()) == 4
        assert app.detail_row.item('5', 'values') == app.table.item('5', 'values')
        for part in ('LIGHT-5', 'SEMI-5', 'PCB-5', 'CODE-5'):
            assert part in app.detail.get('1.0', 'end')
        assert app.table.heading('L', 'text') == '완료'
        assert app.table.heading('P', 'text') == '잔량'
        assert app.table.column('L', 'width') < app.table.column('X', 'width')
        app.query.set('no match')
        app.refresh()
        assert not app.table.get_children()
        assert not app.detail_row.get_children()
        app.reset()
        app.sort('X')
        assert app.visible[0][1][6] == 'PR-001'
        app.table.selection_set('4')
        app.show_detail()
        assert app.detail_row.get_children() == ('4',)
        assert app.detail_row.item('4', 'values') == app.table.item('4', 'values')
        assert 'LIGHT-4' in app.detail.get('1.0', 'end')
        assert 'LIGHT-5' not in app.detail.get('1.0', 'end')
        app.destroy()
print('UI smoke test passed')
