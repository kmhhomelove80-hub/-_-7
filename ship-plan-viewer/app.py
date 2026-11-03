"""선박등기구 생산계획 조회 프로그램."""
import csv
import queue
import threading
import tkinter as tk
from pathlib import Path
from tkinter import ttk, filedialog, messagebox
from tkinter import font as tkfont
from data import DETAIL_HEADERS, COLUMNS, load_plan, matches, sort_key

class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title('선박등기구 · 생산계획 조회')
        self.geometry('1380x850')
        self.minsize(920, 600)
        self.configure(bg='#eef2f7')
        self.plan = None
        self.visible = []
        self.results = queue.Queue()
        self.sort_col = None
        self.descending = False
        self.search_job = None
        style = ttk.Style(self)
        style.theme_use('clam')
        style.configure('.', font=('맑은 고딕', 11))
        style.configure('Treeview', rowheight=34, background='white', fieldbackground='white', borderwidth=0)
        style.configure('Treeview.Heading', background='#e5edf6', foreground='#18324f', font=('맑은 고딕', 11, 'bold'), padding=10)
        style.map('Treeview', background=[('selected', '#cee4fc')], foreground=[('selected', '#102b48')])
        style.configure('TButton', padding=(12, 8))
        root = ttk.Frame(self, padding=22)
        root.pack(fill='both', expand=True)
        top = ttk.Frame(root)
        top.pack(fill='x')
        ttk.Label(top, text='선박등기구 생산계획', font=('맑은 고딕', 23, 'bold')).pack(side='left')
        self.open_button = ttk.Button(top, text='엑셀 파일 열기', command=self.open_file)
        self.open_button.pack(side='right')
        self.file_label = ttk.Label(root, text='엑셀 파일을 선택하면 생산계획을 조회할 수 있습니다.', foreground='#53657c')
        self.file_label.pack(fill='x', pady=(10, 18))
        filters = ttk.Frame(root)
        filters.pack(fill='x')
        self.query = tk.StringVar()
        self.scope = tk.StringVar(value='전체 항목')
        self.schedule = tk.StringVar(value='전체 일정')
        ttk.Label(filters, text='검색 항목').grid(row=0, column=0, sticky='w')
        ttk.Label(filters, text='검색어 · 여러 단어는 모두 포함').grid(row=0, column=1, sticky='w', padx=10)
        ttk.Label(filters, text='생산일정').grid(row=0, column=2, sticky='w')
        self.scope_box = ttk.Combobox(filters, textvariable=self.scope, state='readonly', values=['전체 항목'], width=18)
        self.scope_box.grid(row=1, column=0, pady=6)
        entry = ttk.Entry(filters, textvariable=self.query)
        entry.grid(row=1, column=1, sticky='ew', padx=10)
        self.schedule_box = ttk.Combobox(filters, textvariable=self.schedule, state='readonly', values=['전체 일정'], width=22)
        self.schedule_box.grid(row=1, column=2)
        ttk.Button(filters, text='초기화', command=self.reset).grid(row=1, column=3, padx=10)
        self.export_button = ttk.Button(filters, text='검색 결과 저장', command=self.export, state='disabled')
        self.export_button.grid(row=1, column=4)
        filters.columnconfigure(1, weight=1)
        self.count = ttk.Label(root, text='파일 선택 대기 중', foreground='#275b8c')
        self.count.pack(anchor='w', pady=(12, 8))
        panel = ttk.Panedwindow(root, orient='vertical')
        panel.pack(fill='both', expand=True)
        table_frame = ttk.Frame(panel)
        self.table = ttk.Treeview(table_frame, columns=('row', *COLUMNS), show='headings', selectmode='browse')
        for col in ('row', *COLUMNS):
            self.table.heading(col, text='원본 행' if col == 'row' else col, command=lambda c=col: self.sort(c))
            self.table.column(col, width=80 if col == 'row' else 155, minwidth=65, stretch=False)
        self.compact_labels = {'C': '계획일', 'D': '납기', 'L': '완료', 'P': '잔량'}
        text_font = tkfont.Font(self, font=('맑은 고딕', 11))
        date_width = max(text_font.measure('2026-12-31'), text_font.measure('8888-88-88')) + 16
        for col in ('C', 'D'):
            self.table.column(col, width=date_width, minwidth=date_width, anchor='center')
        for col in ('L', 'P'):
            width = max(text_font.measure('999999'), text_font.measure('완료')) + 20
            self.table.column(col, width=width, minwidth=width, anchor='e')
        self.table.column('row', width=text_font.measure('원본 행') + 24, minwidth=55)
        for col, label in self.compact_labels.items():
            self.table.heading(col, text=label)
        self.table.column('S', width=260, stretch=True)
        self.table.column('AB', width=220, stretch=True)
        self.table.tag_configure('even', background='#f1f5fa')
        self.table.tag_configure('odd', background='#ffffff')
        vertical = ttk.Scrollbar(table_frame, orient='vertical', command=self.table.yview)
        horizontal = ttk.Scrollbar(table_frame, orient='horizontal', command=self.table.xview)
        self.main_horizontal = horizontal
        self.table.configure(yscrollcommand=vertical.set, xscrollcommand=self.sync_horizontal)
        self.table.grid(row=0, column=0, sticky='nsew')
        vertical.grid(row=0, column=1, sticky='ns')
        horizontal.grid(row=1, column=0, sticky='ew')
        table_frame.rowconfigure(0, weight=1)
        table_frame.columnconfigure(0, weight=1)
        panel.add(table_frame, weight=5)
        detail_frame = ttk.LabelFrame(panel, text='선택한 행 상세 · 부품 정보', padding=10)
        self.detail_row = ttk.Treeview(detail_frame, columns=('row', *COLUMNS), show='headings', height=1, selectmode='none')
        self.detail_row.pack(fill='x', pady=(0, 8))
        self.detail_row.tag_configure('selected', background='#cee4fc', foreground='#102b48')
        detail_body = ttk.Frame(detail_frame)
        detail_body.pack(fill='both', expand=True)
        self.detail = tk.Text(detail_body, height=5, wrap='word', font=('맑은 고딕', 11), relief='flat', state='disabled')
        detail_scroll = ttk.Scrollbar(detail_body, command=self.detail.yview)
        self.detail.configure(yscrollcommand=detail_scroll.set)
        self.detail.pack(side='left', fill='both', expand=True)
        detail_scroll.pack(side='right', fill='y')
        panel.add(detail_frame, weight=2)
        ttk.Label(root, text='원본 파일은 변경하지 않습니다.  |  열 제목 클릭: 정렬  |  검색: Ctrl+F  |  파일 열기: Ctrl+O', foreground='#64748b').pack(anchor='w', pady=(12, 0))
        self.table.bind('<<TreeviewSelect>>', self.show_detail)
        self.table.bind('<ButtonRelease-1>', self.sync_detail_columns, add='+')
        self.table.bind('<Configure>', self.sync_detail_columns, add='+')
        for var in (self.query, self.scope, self.schedule):
            var.trace_add('write', self.debounce)
        self.bind('<Control-f>', lambda e: entry.focus_set())
        self.bind('<Control-o>', lambda e: self.open_file())
        self.after(100, self.poll)
        self.after(250, self.open_file)

    def open_file(self):
        if str(self.open_button['state']) == 'disabled':
            return
        path = filedialog.askopenfilename(title='생산계획 엑셀 파일 선택', filetypes=[('Excel 통합 문서', '*.xlsx *.xlsm')])
        if not path:
            return
        self.open_button.configure(state='disabled')
        self.count.configure(text='파일을 읽는 중입니다…')
        def worker():
            try:
                self.results.put((path, load_plan(path), None))
            except Exception as error:
                self.results.put((path, None, str(error)))
        threading.Thread(target=worker, daemon=True).start()

    def poll(self):
        try:
            path, plan, error = self.results.get_nowait()
        except queue.Empty:
            pass
        else:
            self.open_button.configure(state='normal')
            if error:
                messagebox.showerror('파일을 열 수 없습니다', error + '\n파일 형식과 시트 이름을 확인해 주세요.')
                self.refresh()
            else:
                self.plan = plan
                self.sort_col = None
                self.file_label.configure(text=f'{Path(path).name}  ·  선박등기구  ·  원본 수정 없음')
                self.scope_box.configure(values=['전체 항목'] + [f'{c} · {h}' for c, h in zip(COLUMNS, plan.headers)])
                schedules = sorted({v[0] for _, v in plan.rows}, key=sort_key)
                self.schedule_values = [None] + schedules
                self.schedule_box.configure(values=['전체 일정'] + [s or '(일정 없음)' for s in schedules])
                for c, h in zip(COLUMNS, plan.headers):
                    self.table.heading(c, text=self.compact_labels.get(c, h))
                self.reset()
                self.export_button.configure(state='normal')
        self.after(100, self.poll)

    def debounce(self, *_):
        if self.search_job:
            self.after_cancel(self.search_job)
        self.search_job = self.after(180, self.refresh)

    def reset(self):
        self.query.set('')
        self.scope_box.current(0)
        self.schedule_box.current(0)
        self.refresh()

    def refresh(self):
        self.search_job = None
        if not self.plan:
            self.count.configure(text='파일 선택 대기 중')
            return
        scope_index = self.scope_box.current() - 1
        schedule_index = self.schedule_box.current()
        schedule = self.schedule_values[schedule_index] if schedule_index > 0 else None
        self.visible = [(r, v) for r, v in self.plan.rows if matches(v, self.query.get(), scope_index if scope_index >= 0 else None, schedule)]
        if self.sort_col is not None:
            self.visible.sort(key=lambda item: item[0] if self.sort_col == 'row' else sort_key(item[1][COLUMNS.index(self.sort_col)]), reverse=self.descending)
        self.table.delete(*self.table.get_children())
        for i, (r, values) in enumerate(self.visible):
            self.table.insert('', 'end', iid=str(r), values=(r, *values), tags=('even' if i % 2 == 0 else 'odd',))
        self.count.configure(text=f'검색 결과 {len(self.visible):,}건  /  전체 {len(self.plan.rows):,}건' + ('  ·  조건에 맞는 항목이 없습니다.' if not self.visible else ''))
        self.detail_row.delete(*self.detail_row.get_children())
        self.sync_detail_columns()
        self.set_detail('행을 선택하면 광원부(V) · 반제품(W) · PCB 번호(Y) · 광원부코드(AA)를 확인할 수 있습니다.')

    def sort(self, column):
        self.descending = not self.descending if self.sort_col == column else False
        self.sort_col = column
        self.refresh()

    def set_detail(self, text):
        self.detail.configure(state='normal')
        self.detail.delete('1.0', 'end')
        self.detail.insert('1.0', text)
        self.detail.configure(state='disabled')

    def show_detail(self, _=None):
        selection = self.table.selection()
        if selection and self.plan:
            row = int(selection[0])
            values = next(v for r, v in self.visible if r == row)
            parts = self.plan.details.get(row, ['', '', '', ''])
            component_text = '\n'.join(f'{h}: {v or "—"}' for h, v in zip(DETAIL_HEADERS, parts))
            self.sync_detail_columns()
            self.detail_row.delete(*self.detail_row.get_children())
            self.detail_row.insert('', 'end', iid=str(row), values=(row, *values), tags=('selected',))
            self.set_detail(component_text)
            self.detail.yview_moveto(0)

    def sync_detail_columns(self, _=None):
        if not hasattr(self, 'detail_row'):
            return
        for col in ('row', *COLUMNS):
            self.detail_row.heading(col, text=self.table.heading(col, 'text'))
            self.detail_row.column(col, width=self.table.column(col, 'width'),
                                   minwidth=self.table.column(col, 'minwidth'),
                                   anchor=self.table.column(col, 'anchor'), stretch=False)
        self.detail_row.xview_moveto(self.table.xview()[0])

    def sync_horizontal(self, first, last):
        # Both row summaries follow the main table's horizontal scrollbar.
        if hasattr(self, 'main_horizontal'):
            self.main_horizontal.set(first, last)
        if hasattr(self, 'detail_row'):
            self.detail_row.xview_moveto(first)


    def export(self):
        if not self.plan:
            return
        path = filedialog.asksaveasfilename(title='검색 결과 저장', defaultextension='.csv', initialfile='선박등기구_검색결과.csv', filetypes=[('CSV', '*.csv')])
        if not path:
            return
        try:
            with open(path, 'w', encoding='utf-8-sig', newline='') as f:
                writer = csv.writer(f)
                writer.writerow(['원본 행', *self.plan.headers])
                # Neutralize spreadsheet formulas in exported text fields.
                for r, values in self.visible:
                    writer.writerow([r, *["'" + v if v.startswith(('=', '+', '-', '@', '\t', '\r')) else v for v in values]])
            messagebox.showinfo('저장 완료', f'{len(self.visible):,}건을 저장했습니다.')
        except OSError as error:
            messagebox.showerror('저장 실패', str(error))

if __name__ == '__main__':
    App().mainloop()
