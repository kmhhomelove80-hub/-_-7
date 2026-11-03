"""Workbook extraction. The source workbook is never saved or modified."""
from dataclasses import dataclass, field
from datetime import date, datetime
from openpyxl import load_workbook
from openpyxl.utils import column_index_from_string

DETAIL_COLUMNS = ('V', 'W', 'Y', 'AA')
DETAIL_HEADERS = ('광원부', '반제품', 'PCB 번호', '광원부코드')

COLUMNS = ('A', 'C', 'D', 'L', 'P', 'S', 'X', 'Z', 'AB')

@dataclass
class Plan:
    headers: list[str]
    rows: list[tuple[int, list[str]]]
    warnings: list[str]
    details: dict[int, list[str]] = field(default_factory=dict)

def display(value):
    if value is None:
        return ''
    if isinstance(value, (datetime, date)):
        return value.strftime('%Y-%m-%d')
    return str(value).strip()

def load_plan(path):
    wb = load_workbook(path, data_only=True)
    try:
        if '선박등기구' not in wb.sheetnames:
            raise ValueError("'선박등기구' 시트가 없습니다.")
        ws = wb['선박등기구']
        def merged_value(row, col):
            cell = ws.cell(row, col)
            if cell.value is not None:
                return cell.value
            for area in ws.merged_cells.ranges:
                if area.min_row <= row <= area.max_row and area.min_col <= col <= area.max_col:
                    return ws.cell(area.min_row, area.min_col).value
            return None
        headers = []
        for letter in COLUMNS:
            col = column_index_from_string(letter)
            parts = []
            for r in (2, 3):
                label = ' '.join(display(merged_value(r, col)).split())
                if label and label not in parts:
                    parts.append(label)
            headers.append(' / '.join(parts) or letter)
        headers[0] = '생산일정'
        schedule = {}
        for area in ws.merged_cells.ranges:
            if area.min_col == area.max_col == 1 and area.max_row >= 4:
                for row in range(max(4, area.min_row), area.max_row + 1):
                    schedule[row] = ws.cell(area.min_row, 1).value
        rows = []
        details = {}
        for r in range(4, ws.max_row + 1):
            values = [display(ws[f'{c}{r}'].value) for c in COLUMNS]
            # Ignore empty formatting rows, including those inside an A merge.
            if not any(values):
                if not any(ws[f'{c}{r}'].value is not None for c in COLUMNS[1:]):
                    continue
            if r in schedule:
                values[0] = display(schedule[r])
            rows.append((r, values))
            details[r] = [display(ws[f'{c}{r}'].value) for c in DETAIL_COLUMNS]
        return Plan(headers, rows, ['수식은 엑셀에 저장된 계산 결과를 표시합니다. 값이 비어 있으면 엑셀에서 재계산 후 저장해 주세요.'], details)
    finally:
        wb.close()

def matches(values, query='', column=None, schedule=None):
    if schedule is not None and values[0] != schedule:
        return False
    haystack = ' '.join(values if column is None else [values[column]]).casefold()
    return all(word in haystack for word in query.casefold().split())

def sort_key(value):
    try:
        return (0, float(value.replace(',', '')))
    except ValueError:
        return (1, value.casefold())
