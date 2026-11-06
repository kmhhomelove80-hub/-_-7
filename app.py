"""Windows desktop UI. Starts stopped; live trading requires explicit user action."""
import json
import os
from pathlib import Path
import queue
import re
import sys
import threading
import time
import tkinter as tk
from tkinter import ttk, messagebox, filedialog
from datetime import datetime
from core import Config, Rule, D, Halt, Journal, Engine, KST, code
from broker import Kiwoom, MarketClock
from monitor import AccountMonitor
from diagnostics import readable, trade_history
from notifications import EmailNotifier, MailSettings, TelegramNotifier, TelegramSettings, private_chat_ids

SERVICE = 'KiwoomTwoStockTrader.v1'


def data_directory():
    if sys.platform != 'win32':
        raise Halt('이 배포본은 Windows 10/11용입니다.')
    root = Path(os.environ['LOCALAPPDATA']) / 'KiwoomTwoStockTrader'
    root.mkdir(parents=True, exist_ok=True)
    return root


def vault():
    # Explicit native backend; no plaintext fallback.
    from keyring.backends.Windows import WinVaultKeyring
    return WinVaultKeyring()


class App(tk.Tk):
    def __init__(self, root):
        super().__init__()
        self.root_path = root
        self.title('두 종목 순환매매 · 키움 실전 계좌')
        self.geometry(f'{min(1280,self.winfo_screenwidth()-60)}x{min(860,self.winfo_screenheight()-90)}')
        self.minsize(980, 640)
        self.configure(bg='#edf2f7')
        self.messages = queue.Queue()
        self.engine = None
        self.worker = None
        self.monitor_worker = None
        self.monitor_stop = None
        self.monitor_generation = 0
        self.last_balance = None
        self.balance_error = None
        self.journal = Journal(root / 'journal.sqlite3')
        self.values = {}
        self.lookup_pending = set()
        self.lookup_done = {}
        self.inputs = []
        self.live = tk.BooleanVar(value=False)
        self.status = tk.StringVar(value='중지됨 · 실제 주문 없음')
        self.audit_status = tk.StringVar(value='거래 진단: 대기 중 · 상세 기록은 거래 설명/진단에서 확인')
        self.feasibility_status = tk.StringVar(value='양방향 자금/수량 예상: 자동매매 시작 시 확인')
        self.strategy_status = tk.StringVar(value='B 순환매 기준: 시작 시 저장된 기준을 불러옵니다.')
        self.telegram_settings = TelegramSettings()
        self.telegram_notifier = TelegramNotifier(self.telegram_settings, lambda text: self.emit('log', text))
        self.mail_settings = MailSettings()
        self.notifier = EmailNotifier(self.mail_settings, lambda text: self.emit('log', text))
        style = ttk.Style(self)
        style.theme_use('clam')
        style.configure('TLabel', font=('맑은 고딕', 10))
        style.configure('TButton', font=('맑은 고딕', 9), padding=4)
        style.configure('Title.TLabel', font=('맑은 고딕', 15, 'bold'))
        base = ttk.Frame(self, padding=8)
        base.pack(fill='both', expand=True)
        ttk.Label(base, text='두 종목 순환매매', style='Title.TLabel').pack(anchor='w')
        ttk.Label(base, text='실전 전용 · A: 자금 보관 ETF / B: 변동 종목 · B 직전 체결가 기준 순환매').pack(anchor='w', pady=(0, 3))
        self.pages = ttk.Notebook(base)
        self.pages.pack(fill='both',expand=True)
        self.dashboard = ttk.Frame(self.pages,padding=4)
        self.settings_page = ttk.Frame(self.pages,padding=6)
        self.account_page = ttk.Frame(self.pages,padding=6)
        self.pages.add(self.dashboard,text='거래 현황 · 잔고/거래내역')
        self.pages.add(self.settings_page,text='전략 설정')
        self.pages.add(self.account_page,text='계좌 연결 설정')
        auth = ttk.LabelFrame(self.account_page, text='실전 계좌 연결 / Windows 보안 저장소', padding=10)
        auth.pack(fill='x')
        self.field(auth, 'appkey', 'App Key', 0, 0, secret=True)
        self.field(auth, 'secret', 'Secret Key', 0, 2, secret=True)
        self.field(auth, 'account', '연결 계좌번호', 1, 0, secret=True)
        self.save_button = ttk.Button(auth, text='인증정보 1회 저장', command=self.save_credentials)
        self.save_button.grid(row=1, column=2, padx=6)
        self.delete_button = ttk.Button(auth, text='저장 인증정보 삭제', command=self.delete_credentials)
        self.delete_button.grid(row=1, column=3, padx=6)
        ttk.Label(auth, text='영웅문 ID·비밀번호는 필요하지 않습니다. 실전 REST API 키에 연결된 계좌를 확인합니다.').grid(row=2, column=0, columnspan=4, sticky='w', pady=6)
        params = ttk.LabelFrame(self.settings_page, text='전략 설정', padding=10)
        params.pack(fill='x', pady=10)
        self.lookup_status = tk.StringVar(value='코드를 입력하고 다른 칸으로 이동하면 종목명을 조회합니다.')
        for j, label in enumerate(('역할', '종목코드 6자리', '정확한 종목명')):
            ttk.Label(params, text=label).grid(row=0, column=j, padx=8, sticky='w')
        for i, prefix in enumerate(('a', 'b'), 1):
            ttk.Label(params, text='A · 자금 ETF' if prefix == 'a' else 'B · 변동 종목').grid(row=i, column=0)
            for j, suffix in enumerate(('code', 'name'), 1):
                self.entry(params, prefix + '_' + suffix, i, j, width=14 if suffix=='code' else 32)
        signals = ttk.Frame(params)
        signals.grid(row=3,column=0,columnspan=4,sticky='w',pady=4)
        self.field(signals, 'drop', 'B 하락 신호 %', 0, 0, default='5')
        self.field(signals, 'a_sell', 'A 매도수량 %', 0, 2, default='10')
        self.field(signals, 'rise', 'B 상승 신호 %', 1, 0, default='5')
        self.field(signals, 'b_sell', 'B 매도수량 %', 1, 2, default='5')
        self.lookup_button = ttk.Button(params, text='종목명 자동 조회', command=lambda: self.lookup_symbols(True))
        self.lookup_button.grid(row=0, column=3, sticky='w')
        ttk.Label(params, textvariable=self.lookup_status).grid(row=7, column=0, columnspan=5, sticky='w')
        limits = ttk.Frame(params)
        limits.grid(row=4,column=0,columnspan=5,sticky='w',pady=4)
        self.field(limits, 'cap', '합산 매입원금 한도(원)', 0, 0, default='1000000')
        self.field(limits, 'daily', '하루 최대 사이클', 0, 2)
        self.field(limits, 'reserve', '비용 예비율 %', 1, 0, default='1')
        self.field(limits, 'poll', '조건 대기 조회 간격(초)', 1, 2, default='3')
        self.field(limits, 'initial_price', '첫 B 기준가(0=평균가)', 2, 0, default='0')
        ttk.Label(limits, text='첫 방향 BOTH / BUY / SELL').grid(row=2, column=2)
        self.entry(limits, 'initial_mode', 2, 3, default='BOTH')
        ttk.Label(params, text='한도 1,000,000~1,000,000,000원 · A/B 합산 매입원금 · A 최소 잔고 없음 · 매도수량 소수점 버림').grid(row=5,column=0,columnspan=5,sticky='w')
        ttk.Label(params, text='지정가: 주문 직전 현재가 / 60초 내 전량 미체결 시 정지 / 장 마감 10분 전부터 새 주문 차단').grid(row=6,column=0,columnspan=5,sticky='w')
        self.live_check = ttk.Checkbutton(base, text='실전 계좌에서 실제 매도·매수 주문을 실행합니다.', variable=self.live)
        self.live_check.pack(anchor='w',before=self.pages)
        actions = ttk.Frame(base)
        actions.pack(fill='x', pady=3,before=self.pages)
        self.read_button = ttk.Button(actions, text='계좌 연결 / 잔고 자동갱신', command=lambda: self.begin(False))
        self.read_button.pack(side='left')
        self.start_button = ttk.Button(actions, text='실전 자동매매 시작', command=lambda: self.begin(True))
        self.start_button.pack(side='left', padx=8)
        ttk.Button(actions, text='자동매매 중지', command=self.stop).pack(side='left')
        self.recover_button = ttk.Button(self.settings_page, text='미완료 거래 확인 / 해제', command=self.recover)
        self.recover_button.pack(anchor='w',pady=4)
        ttk.Button(actions, text='거래 설명/진단', command=self.diagnostic_dialog).pack(side='right', padx=4)
        self.reset_button = ttk.Button(self.settings_page, text='B 기준 초기화', command=self.reset_strategy)
        self.reset_button.pack(anchor='w',pady=4)
        ttk.Button(self.account_page,text='휴대폰 알림 설정',command=self.notification_dialog).pack(anchor='w',pady=5)
        ttk.Label(self.dashboard, textvariable=self.status, foreground='#ad2626').pack(anchor='w', pady=1)
        ttk.Label(self.dashboard, textvariable=self.strategy_status,wraplength=1180).pack(anchor='w')
        ttk.Label(self.dashboard, textvariable=self.feasibility_status, wraplength=1180).pack(anchor='w')
        ttk.Label(self.dashboard, textvariable=self.audit_status, wraplength=1180).pack(anchor='w')
        self.main_panes = ttk.Panedwindow(self.dashboard,orient='vertical')
        self.main_panes.pack(fill='both',expand=True,pady=3)
        balance_box = ttk.LabelFrame(self.main_panes, text='계좌 전체 잔고 · 5초 간격 재조회', padding=4)
        self.main_panes.add(balance_box,weight=1)
        self.balance_status = tk.StringVar(value='미연결 · 계좌 연결 버튼을 누르세요.')
        self.summary_values = {}
        summary = ttk.Frame(balance_box)
        summary.pack(fill='x')
        for i, (key, title) in enumerate((('deposit', '예수금'), ('cash_available', '주문가능현금(100%)'),
                ('d2_deposit', 'D+2 추정예수금'), ('total_cost', '총매입금액'),
                ('total_value', '총평가금액'), ('total_profit', '평가손익 / 수익률'))):
            cell = ttk.Frame(summary, padding=2)
            cell.grid(row=0, column=i, sticky='w', padx=5)
            summary.columnconfigure(i,weight=1)
            ttk.Label(cell, text=title).pack(anchor='w')
            value = tk.StringVar(value='—')
            self.summary_values[key] = value
            ttk.Label(cell, textvariable=value, font=('맑은 고딕', 10, 'bold')).pack(anchor='w')
        toolbar = ttk.Frame(balance_box)
        toolbar.pack(fill='x')
        ttk.Label(toolbar, textvariable=self.balance_status).pack(side='left')
        ttk.Button(toolbar, text='잔고 조회 연결 해제', command=self.disconnect_monitor).pack(side='right')
        columns = ('code', 'name', 'kind', 'qty', 'available', 'average', 'current', 'cost', 'value', 'profit', 'rate')
        grid = ttk.Frame(balance_box)
        grid.pack(fill='both',expand=True,pady=3)
        self.table = ttk.Treeview(grid, columns=columns, show='headings', height=5)
        for key, title in zip(columns, ('코드', '종목명', '구분', '보유', '매도가능', '평균 매입가', '현재가', '매입금액', '평가금액', '평가손익', '수익률 %')):
            self.table.heading(key, text=title)
            self.table.column(key, width=95, minwidth=65, anchor='e')
        scroll = ttk.Scrollbar(grid, orient='vertical', command=self.table.yview)
        self.table.configure(yscrollcommand=scroll.set)
        self.table.pack(side='left', fill='both', expand=True)
        scroll.pack(side='right', fill='y')
        history_box=ttk.LabelFrame(self.main_panes,text='주문·체결 내역 / 거래 진행 로그',padding=4)
        self.main_panes.add(history_box,weight=1)
        self.history_pages=ttk.Notebook(history_box)
        self.history_pages.pack(fill='both',expand=True)
        history_frame=ttk.Frame(self.history_pages)
        log_frame=ttk.Frame(self.history_pages)
        self.history_pages.add(history_frame,text='주문·체결 내역')
        self.history_pages.add(log_frame,text='실시간 진행 로그')
        columns=('time','name','side','filled','requested','price','amount','status','order')
        self.history_table=ttk.Treeview(history_frame,columns=columns,show='headings',height=5)
        for key,title in zip(columns,('시각','종목','매수/매도','체결수량','주문수량','평균가/지정가','체결금액','상태','주문번호')):
            self.history_table.heading(key,text=title)
            self.history_table.column(key,width=110,minwidth=55,anchor='e')
        history_scroll=ttk.Scrollbar(history_frame,command=self.history_table.yview)
        self.history_table.configure(yscrollcommand=history_scroll.set)
        self.history_table.pack(side='left',fill='both',expand=True)
        history_scroll.pack(side='right',fill='y')
        self.logs = tk.Text(log_frame,height=7,bg='#142337',fg='#e1eaf5',font=('맑은 고딕',10),state='disabled',wrap='word')
        log_scroll=ttk.Scrollbar(log_frame,command=self.logs.yview)
        self.logs.configure(yscrollcommand=log_scroll.set)
        self.logs.pack(side='left',fill='both',expand=True)
        log_scroll.pack(side='right',fill='y')
        self.render_history()
        self.load()
        if '--smoke-test' not in sys.argv:
            try:
                saved_credentials = self.credentials()
            except Halt:
                pass
            else:
                self.after(300, lambda: self.connect_monitor(saved_credentials))
        if self.journal.active():
            self.log('미완료 거래 기록이 있습니다. 영웅문 주문·체결·잔고를 대조한 뒤 해제하세요.')
        self.after(100, self.drain)
        self.protocol('WM_DELETE_WINDOW', self.close)

    def entry(self, parent, name, row, col, width=25, secret=False, default=''):
        var = tk.StringVar(value=default)
        self.values[name] = var
        widget = ttk.Entry(parent, textvariable=var, width=width, show='*' if secret else '')
        widget.grid(row=row, column=col, padx=6, pady=5, sticky='w')
        self.inputs.append(widget)
        if name in ('a_code', 'b_code'):
            widget.bind('<FocusOut>', lambda event, prefix=name[0]: self.lookup_symbol(prefix))

    def field(self, parent, name, title, row, col, secret=False, default=''):
        ttk.Label(parent, text=title).grid(row=row, column=col, sticky='w', padx=5)
        self.entry(parent, name, row, col + 1, width=18, secret=secret, default=default)

    def load(self):
        settings = self.root_path / 'settings.json'
        if settings.exists():
            try:
                for k, v in json.loads(settings.read_text(encoding='utf-8')).items():
                    if k in self.values and k not in ('appkey', 'secret', 'account'):
                        self.values[k].set(v)
            except Exception:
                self.log('설정 파일을 읽지 못했습니다. 설정을 다시 입력하세요.')
        try:
            if int(self.values['cap'].get().replace(',', '')) < 1_000_000:
                self.values['cap'].set('1000000')
        except ValueError:
            pass
        try:
            telegram = vault().get_password(SERVICE, 'telegram')
            if telegram:
                self.telegram_settings = TelegramSettings(**json.loads(telegram))
                self.telegram_notifier.configure(self.telegram_settings)
            mail = vault().get_password(SERVICE, 'email')
            if mail:
                self.mail_settings = MailSettings(**json.loads(mail))
                self.notifier.configure(self.mail_settings)
            payload = vault().get_password(SERVICE, 'credentials')
            if payload:
                for k, v in json.loads(payload).items():
                    if k in ('appkey', 'secret', 'account'):
                        self.values[k].set(v)
        except Exception:
            self.log('Windows 보안 저장소를 읽지 못했습니다. 인증정보를 다시 입력하세요.')

    def credentials(self):
        result = {k: self.values[k].get().strip() for k in ('appkey', 'secret', 'account')}
        result['account'] = result['account'].replace('-', '')
        if not result['appkey'] or not result['secret'] or not re.fullmatch(r'[0-9]{10}', result['account']):
            raise Halt('App Key, Secret Key 및 연결 계좌번호 10자리를 입력하세요.')
        return result

    def save_credentials(self):
        try:
            vault().set_password(SERVICE, 'credentials', json.dumps(self.credentials()))
            self.log('인증정보를 Windows 보안 저장소에 저장했습니다.')
        except Halt as exc:
            messagebox.showerror('입력 확인', str(exc))
        except Exception:
            messagebox.showerror('저장 실패', 'Windows 보안 저장소 저장 실패. 평문으로 저장하지 않습니다.')

    def delete_credentials(self):
        self.disconnect_monitor()
        try:
            store = vault()
            if store.get_password(SERVICE, 'credentials'):
                store.delete_password(SERVICE, 'credentials')
            for key in ('appkey', 'secret', 'account'):
                self.values[key].set('')
            self.log('저장 인증정보를 삭제했습니다.')
        except Exception:
            messagebox.showerror('삭제 실패', 'Windows 자격 증명 관리자를 확인하세요.')

    def config(self):
        try:
            get = lambda k: self.values[k].get().strip()
            rules = []
            for prefix in ('a', 'b'):
                try:
                    rule = Rule(get(prefix + '_code'), get(prefix + '_name'))
                    rule.validate()
                except Halt as exc:
                    raise Halt(f'{prefix.upper()} 종목 입력 확인: {exc}') from exc
                self.values[prefix + '_code'].set(rule.code)
                rules.append(rule)
            rules = tuple(rules)
            cfg = Config(rules, int(get('cap').replace(',', '')), int(get('daily')),
                         D(get('reserve')), int(get('poll')),
                         D(get('drop')), D(get('rise')), D(get('a_sell')), D(get('b_sell')),
                         D(get('initial_price')), get('initial_mode').upper())
            cfg.validate()
            return cfg
        except Halt:
            raise
        except Exception as exc:
            raise Halt('숫자 설정을 확인하세요. 종목·비율·한도·일일 횟수는 필수입니다.') from exc

    def save_settings(self):
        values = {k: v.get() for k, v in self.values.items() if k not in ('appkey', 'secret', 'account')}
        path = self.root_path / 'settings.json'
        temp = path.with_suffix('.tmp')
        temp.write_text(json.dumps(values, ensure_ascii=False, indent=2), encoding='utf-8')
        temp.replace(path)

    def busy(self, value):
        state = 'disabled' if value else 'normal'
        for widget in self.inputs + [self.save_button, self.delete_button, self.start_button,
                                     self.read_button, self.recover_button, self.reset_button, self.lookup_button, self.live_check]:
            widget.configure(state=state)

    def emit(self, event, payload):
        self.messages.put((event, payload))

    def begin(self, trade):
        if not trade:
            self.connect_monitor()
            return
        if self.worker and self.worker.is_alive():
            return
        try:
            unresolved = any(self.lookup_done.get(p) != (code(self.values[p + '_code'].get()), self.values[p + '_name'].get().strip())
                             for p in ('a', 'b'))
        except Halt as exc:
            messagebox.showerror('종목코드 확인', str(exc))
            return
        if unresolved:
            self.lookup_symbols(True)
            self.status.set('종목명을 조회 중입니다. 조회 결과를 확인한 후 시작 버튼을 다시 누르세요.')
            return
        try:
            cfg, credentials = self.config(), self.credentials()
            if trade and not self.live.get():
                raise Halt('실전 주문 실행 항목을 선택해야 자동매매를 시작할 수 있습니다.')
            if trade and self.journal.active():
                raise Halt('미완료 거래 기록이 있습니다. 먼저 확인/해제하세요.')
            self.save_settings()
        except Halt as exc:
            messagebox.showerror('시작할 수 없음', str(exc))
            return
        self.connect_monitor(credentials)
        self.busy(True)
        self.status.set('실전 자동매매 연결 중' if trade else '읽기 전용 잔고 조회 중')
        # Created before background connection, so stop works during authentication.
        stop_event = threading.Event()
        self.pending_stop = stop_event
        self.engine = None
        def work():
            try:
                broker = Kiwoom(credentials['appkey'], credentials['secret'])
                broker.verify_account(credentials['account'])
                engine = Engine(broker, cfg, self.journal, credentials['account'],
                                MarketClock() if trade else lambda: False, self.emit, stop_event)
                self.engine = engine
                if trade:
                    self.emit('log', '실전 자동매매 시작. 수동 주문 및 다른 자동매매 프로그램을 함께 사용하지 마세요.')
                    engine.run()
                else:
                    hs = engine.snapshot()
                    self.emit('holdings', hs)
                    self.emit('log', '실전 계좌 연결·종목명·잔고 확인 완료. 주문은 실행하지 않았습니다.')
                    self.emit('stopped', None)
            except Exception as exc:
                message = str(exc) if isinstance(exc, Halt) else f'연결 오류({type(exc).__name__}). 의존성/응답 규격을 확인하세요.'
                self.emit('log', message)
                self.emit('notice', {'subject': '자동매매 시작 실패', 'body': message})
                self.emit('stopped', None)
        self.worker = threading.Thread(target=work, daemon=True)
        self.worker.start()

    def disconnect_monitor(self):
        if self.monitor_stop:
            self.monitor_stop.set()
        self.monitor_generation += 1
        self.last_balance = None
        self.balance_error = None
        self.balance_status.set('잔고 조회 연결 해제됨')
        for value in self.summary_values.values():
            value.set('—')
        for item in self.table.get_children():
            self.table.delete(item)

    def connect_monitor(self, credentials=None):
        try:
            credentials = credentials or self.credentials()
        except Halt as exc:
            messagebox.showerror('계좌 연결', str(exc))
            return
        self.disconnect_monitor()
        self.balance_status.set('계좌 연결 중 · 조회 전용 / 주문 없음')
        self.monitor_stop = threading.Event()
        generation = self.monitor_generation
        broker = Kiwoom(credentials['appkey'], credentials['secret'])
        callback = lambda event, data: self.emit(event, (generation, data))
        monitor = AccountMonitor(broker, credentials['account'], callback, self.monitor_stop)
        self.monitor_worker = threading.Thread(target=monitor.run, daemon=True)
        self.monitor_worker.start()
        self.lookup_symbols()

    def render_balance(self, data):
        self.last_balance = data
        self.balance_error = None
        for key, value in self.summary_values.items():
            if key == 'total_profit':
                value.set(f'{data[key]:+,.0f}원 / {data["total_profit_rate"]:+.2f}%')
            else:
                value.set(f'{data[key]:,.0f}원')
        self.balance_status.set(f'계좌 {data["account_label"]} · 마지막 갱신 {data["updated_at"]} · 5초 간격 조회')
        selected = set(self.table.selection())
        existing = set(self.table.get_children())
        for i, p in enumerate(data['positions']):
            ident = str(i)
            values = (p['code'], p['name'], p['kind'], p['qty'], p['available'],
                      f'{p["average"]:,.2f}', f'{p["current"]:,.0f}', f'{p["cost"]:,}',
                      f'{p["value"]:,}', f'{p["profit"]:+,.0f}', f'{p["profit_rate"]:+.2f}')
            if ident in existing:
                self.table.item(ident, values=values)
            else:
                self.table.insert('', 'end', iid=ident, values=values)
        valid = set(str(i) for i in range(len(data['positions'])))
        for ident in existing - valid:
            self.table.delete(ident)
        if selected & valid:
            self.table.selection_set(tuple(selected & valid))

    def lookup_symbols(self, force=False):
        for prefix in ('a', 'b'):
            self.lookup_symbol(prefix, force)

    def lookup_symbol(self, prefix, force=False):
        if self.worker and self.worker.is_alive():
            return
        raw = self.values[prefix + '_code'].get()
        if not raw.strip():
            return
        try:
            symbol = code(raw)
            credentials = self.credentials()
        except Halt as exc:
            if force:
                self.lookup_status.set(f'{prefix.upper()}: {exc}')
            return
        self.values[prefix + '_code'].set(symbol)
        if (prefix, symbol) in self.lookup_pending:
            return
        if not force and self.lookup_done.get(prefix) == (symbol, self.values[prefix + '_name'].get().strip()):
            return
        self.lookup_pending.add((prefix, symbol))
        self.lookup_status.set(f'{prefix.upper()} 종목명 조회 중…')
        def work():
            broker = None
            try:
                broker = Kiwoom(credentials['appkey'], credentials['secret'])
                broker.verify_account(credentials['account'])
                info = broker.stock_info(symbol)
                self.emit('symbol_lookup', (prefix, symbol, info['name'], None))
            except Exception as exc:
                error = str(exc) if isinstance(exc, Halt) else f'종목 조회 오류({type(exc).__name__})'
                self.emit('symbol_lookup', (prefix, symbol, None, error))
            finally:
                if broker is not None:
                    broker.session.close()
        threading.Thread(target=work, daemon=True).start()

    def render_history(self):
        rows=trade_history(self.journal.audit_rows())[:500]
        selected=self.history_table.selection()
        for ident in self.history_table.get_children():
            self.history_table.delete(ident)
        status={'ORDER_ACCEPTED':'접수','PARTIAL_FILL':'부분체결','FILL_CONFIRMED':'전량체결','ORDER_REJECTED':'거절','ORDER_UNKNOWN':'접수 불명'}
        for i,row in enumerate(rows):
            price=row.get('average') or row.get('price') or row.get('limit_price')
            self.history_table.insert('','end',iid=str(i),values=(row['at'][11:19],row.get('name',row.get('symbol','')),
                row.get('side',''),row.get('filled_qty',0),row.get('requested_qty',row.get('qty','')),
                f'{D(price):,.2f}' if price else '—',f'{D(row["gross"]):,.0f}' if row.get('gross') else '—',
                status.get(row['stage'],row['stage']),row.get('order','—')))

    def diagnostic_dialog(self):
        window = tk.Toplevel(self)
        window.title('거래 설명 / 단계별 진단 · 인증정보 제외')
        window.geometry('1180x760')
        frame = ttk.Frame(window,padding=12)
        frame.pack(fill='both',expand=True)
        ttk.Label(frame,text='추적번호를 선택하면 신호 → 계산 → 주문 전송/접수 → 체결 → 후속 조치 순서로 표시합니다.').pack(anchor='w')
        tree=ttk.Treeview(frame,columns=('at','trace','count'),show='headings',height=6)
        for name,title in (('at','마지막 기록 시각'),('trace','거래 추적번호'),('count','단계 수')):
            tree.heading(name,text=title)
            tree.column(name,width=300)
        tree.pack(fill='x',pady=6)
        controls=ttk.Frame(frame)
        controls.pack(fill='x')
        pane=ttk.Frame(frame)
        pane.pack(fill='both',expand=True,pady=6)
        text=tk.Text(pane,wrap='word',font=('맑은 고딕',10))
        bar=ttk.Scrollbar(pane,command=text.yview)
        text.configure(yscrollcommand=bar.set)
        text.pack(side='left',fill='both',expand=True)
        bar.pack(side='right',fill='y')
        def show(event=None):
            selected=tree.selection()
            rows=self.journal.audit_rows(selected[0]) if selected else []
            text.configure(state='normal')
            text.delete('1.0','end')
            text.insert('end','\n'.join(readable(r) for r in rows) or '이전 버전 거래에는 새 진단 기록이 없습니다. 수정본에서 실행한 기록부터 표시합니다.')
            text.configure(state='disabled')
        def refresh():
            old=tree.selection()
            for ident in tree.get_children():
                tree.delete(ident)
            for trace,at,count in self.journal.audit_traces():
                tree.insert('','end',iid=trace,values=(at,trace,count))
            children=tree.get_children()
            if children:
                tree.selection_set(old[0] if old and old[0] in children else children[0])
            show()
        def export(all_rows=False):
            selected=tree.selection()
            rows=self.journal.audit_rows(None if all_rows or not selected else selected[0])
            path=filedialog.asksaveasfilename(parent=window,defaultextension='.json',
                    initialfile='거래진단.json',filetypes=[('JSON','*.json')])
            if not path:
                return
            try:
                Path(path).write_text(json.dumps({'schema':1,'exported_at':datetime.now(KST).isoformat(),
                    'scope':'최근 최대 10000개 단계' if all_rows else '선택 추적번호',
                    'records':rows},ensure_ascii=False,indent=2),encoding='utf-8')
                messagebox.showinfo('진단 저장','거래 금액/주문번호가 포함됩니다. 공유 전 내용을 확인하세요.',parent=window)
            except Exception:
                messagebox.showerror('저장 실패','진단 파일을 저장하지 못했습니다.',parent=window)
        ttk.Button(controls,text='새로고침',command=refresh).pack(side='left')
        ttk.Button(controls,text='선택 거래 JSON 저장',command=export).pack(side='left',padx=8)
        ttk.Button(controls,text='전체 진단 JSON 저장',command=lambda:export(True)).pack(side='left')
        tree.bind('<<TreeviewSelect>>',show)
        refresh()

    def reset_strategy(self):
        try:
            credentials, cfg = self.credentials(), self.config()
            if self.worker and self.worker.is_alive():
                raise Halt('자동매매를 먼저 중지하세요.')
            if not messagebox.askyesno('B 기준 초기화', '저장된 B 체결 기준가와 대기 방향을 지우고, 다음 시작 때 첫 기준 설정을 적용할까요? 일일 횟수는 유지합니다.'):
                return
            self.journal.reset_state(credentials['account'], cfg.rules[0].code, cfg.rules[1].code)
            self.strategy_status.set('B 기준 초기화 완료 · 다음 시작 시 첫 기준가/방향 적용')
        except Halt as exc:
            messagebox.showerror('기준 초기화', str(exc))

    def notification_dialog(self):
        window = tk.Toplevel(self)
        self.telegram_window = window
        window.title('휴대폰 알림 · 텔레그램 기본 / 이메일 선택')
        window.transient(self)
        frame = ttk.Frame(window, padding=18)
        frame.pack(fill='both', expand=True)
        enabled = tk.BooleanVar(value=self.telegram_settings.enabled or not self.telegram_settings.token)
        token = tk.StringVar(value=self.telegram_settings.token)
        chat = tk.StringVar(value=self.telegram_settings.chat_id)
        self.telegram_chat_var = chat
        ttk.Checkbutton(frame, text='텔레그램 푸시 알림 사용', variable=enabled).grid(row=0, column=0, columnspan=2, sticky='w')
        ttk.Label(frame, text='BotFather에서 받은 봇 토큰').grid(row=1, column=0, pady=6)
        ttk.Entry(frame, textvariable=token, show='*', width=50).grid(row=1, column=1, padx=6)
        ttk.Label(frame, text='수신 Chat ID').grid(row=2, column=0, pady=6)
        ttk.Entry(frame, textvariable=chat, width=50).grid(row=2, column=1, padx=6)
        ttk.Label(frame, text='1. 텔레그램 @BotFather → /newbot으로 봇 생성 및 토큰 복사\n2. 생성한 봇과 대화를 열고 /start 전송 → Chat ID 찾기\n3. 설정 저장 후 테스트 알림 전송 / 휴대폰 텔레그램 알림 허용').grid(row=3, column=0, columnspan=2, pady=10, sticky='w')
        def save(test=False):
            try:
                settings = TelegramSettings(enabled.get(), token.get().strip(), chat.get().strip())
                if settings.enabled or test:
                    settings.validate()
                vault().set_password(SERVICE, 'telegram', json.dumps(settings.to_dict()))
                self.telegram_settings = settings
                self.telegram_notifier.configure(settings)
                self.log('텔레그램 설정을 Windows 보안 저장소에 저장했습니다.')
                if test:
                    self.telegram_notifier.notify('테스트 알림', '거래 진행 알림을 받을 수 있습니다. 실제 주문은 실행하지 않았습니다.', force=True)
            except Halt as exc:
                messagebox.showerror('텔레그램 설정', str(exc), parent=window)
            except Exception:
                messagebox.showerror('텔레그램 설정', 'Windows 보안 저장소 저장 실패', parent=window)
        def discover():
            current_token = token.get().strip()
            def work():
                try:
                    self.emit('telegram_ids', private_chat_ids(current_token))
                except Exception as exc:
                    self.emit('log', str(exc) if isinstance(exc, Halt) else '텔레그램 Chat ID 조회 실패')
            threading.Thread(target=work, daemon=True).start()
        ttk.Button(frame, text='Chat ID 찾기', command=discover).grid(row=4, column=0, pady=8)
        ttk.Button(frame, text='설정 저장', command=save).grid(row=4, column=1, pady=8)
        ttk.Button(frame, text='저장 후 테스트 알림 보내기', command=lambda:save(True)).grid(row=5, column=0, columnspan=2, pady=8)
        def delete():
            try:
                store = vault()
                if store.get_password(SERVICE, 'telegram'):
                    store.delete_password(SERVICE, 'telegram')
                self.telegram_settings = TelegramSettings()
                self.telegram_notifier.configure(self.telegram_settings)
                window.destroy()
                self.log('텔레그램 설정 삭제 완료. 이미 대기 중인 알림은 발송될 수 있습니다.')
            except Exception:
                messagebox.showerror('설정 삭제', '텔레그램 설정 삭제 실패', parent=window)
        ttk.Button(frame, text='텔레그램 설정 삭제', command=delete).grid(row=6, column=0, pady=8)
        ttk.Button(frame, text='이메일도 설정하기(선택)', command=self.mail_dialog).grid(row=6, column=1, pady=8)
        ttk.Label(frame, text='텔레그램/이메일을 모두 켜면 두 채널에 발송됩니다. 알림 실패는 주문을 재실행하지 않습니다.').grid(row=7, column=0, columnspan=2)

    def mail_dialog(self):
        window = tk.Toplevel(self)
        window.title('휴대폰 이메일 알림 설정')
        window.transient(self)
        window.resizable(False, False)
        frame = ttk.Frame(window, padding=18)
        frame.pack(fill='both', expand=True)
        enabled = tk.BooleanVar(value=self.mail_settings.enabled)
        ttk.Checkbutton(frame, text='거래 진행 이메일 알림 사용', variable=enabled).grid(row=0, column=0, columnspan=2, sticky='w')
        values = {}
        fields = (('host', 'SMTP 서버'), ('port', '포트(SSL 465 / STARTTLS 587)'),
                  ('security', '보안 방식 SSL / STARTTLS'), ('username', 'SMTP 사용자명'),
                  ('password', 'SMTP 비밀번호 / 앱 비밀번호'), ('sender', '발신 이메일'), ('recipient', '휴대폰에서 받을 이메일'))
        for row, (key, label) in enumerate(fields, 1):
            ttk.Label(frame, text=label).grid(row=row, column=0, sticky='w', pady=5)
            values[key] = tk.StringVar(value=str(getattr(self.mail_settings, key)))
            ttk.Entry(frame, textvariable=values[key], width=42, show='*' if key == 'password' else '').grid(row=row, column=1, padx=8)
        ttk.Label(frame, text='메일 서비스에서 SMTP와 앱 비밀번호를 설정하세요. 휴대폰 메일 앱 알림도 켜야 합니다.').grid(row=8, column=0, columnspan=2, pady=10)
        ttk.Label(frame, text='서버 인증 실패 시 로그에 표시합니다. 거래 주문과 이메일 처리는 분리됩니다.').grid(row=9, column=0, columnspan=2)
        def settings():
            try:
                data = {key: value.get().strip() for key, value in values.items()}
                data['password'] = values['password'].get()
                data['port'] = int(data['port'])
                data['security'] = data['security'].upper()
                return MailSettings(enabled=enabled.get(), **data)
            except Exception as exc:
                raise Halt('메일 포트/설정을 확인하세요.') from exc
        def save(test=False):
            try:
                mail = settings()
                if mail.enabled or test:
                    mail.validate()
                vault().set_password(SERVICE, 'email', json.dumps(mail.to_dict()))
                self.mail_settings = mail
                self.notifier.configure(mail)
                self.log('이메일 설정을 Windows 보안 저장소에 저장했습니다.')
                if test:
                    self.notifier.notify('테스트 알림', '이 메일이 도착하면 거래 진행 이메일 알림을 받을 수 있습니다. 실제 주문은 실행하지 않았습니다.', force=True)
            except Halt as exc:
                messagebox.showerror('메일 설정', str(exc), parent=window)
            except Exception:
                messagebox.showerror('메일 설정', 'Windows 보안 저장소 저장 실패. 설정을 확인하세요.', parent=window)
        ttk.Button(frame, text='설정 저장', command=save).grid(row=10, column=0, pady=12)
        ttk.Button(frame, text='저장 후 테스트 메일 보내기', command=lambda: save(True)).grid(row=10, column=1, pady=12)
        def delete():
            try:
                store = vault()
                if store.get_password(SERVICE, 'email'):
                    store.delete_password(SERVICE, 'email')
                self.mail_settings = MailSettings()
                self.notifier.configure(self.mail_settings)
                window.destroy()
                self.log('저장 이메일 설정을 삭제했습니다. 이미 대기 중인 알림은 발송될 수 있습니다.')
            except Exception:
                messagebox.showerror('메일 설정', '이메일 설정 삭제 실패', parent=window)
        ttk.Button(frame, text='저장 이메일 설정 삭제', command=delete).grid(row=11, column=0, columnspan=2)

    def stop(self):
        if hasattr(self, 'pending_stop'):
            self.pending_stop.set()
        self.status.set('중지 요청됨 · 이미 접수된 주문은 영웅문에서 확인')
        self.log('중지 요청. 접수 완료된 주문/통신 중이던 주문은 자동 취소하지 않습니다.')

    def recover(self):
        active = self.journal.active()
        if not active:
            messagebox.showinfo('미완료 거래', '미완료 거래 기록이 없습니다.')
            return
        try:
            credentials, cfg = self.credentials(), self.config()
        except Halt as exc:
            messagebox.showerror('입력 확인', str(exc))
            return
        lines = '\n'.join(f'사이클 {r[0]} / {r[1]} / {r[3]}\n{r[4]}' for r in active)
        if any(r[2] != credentials['account'] for r in active):
            messagebox.showerror('계좌 확인', '미완료 기록의 계좌와 입력 계좌가 다릅니다.')
            return
        if not messagebox.askyesno('영웅문 대조 후 해제', lines + '\n\n영웅문에서 이 기록과 해당 종목의 주문·체결·잔고를 대조하고, 남은 주문을 취소하셨습니까?\n매도만 완료된 경우 현금이 남고 추가 매수는 복구하지 않습니다.\n하루 실행 횟수는 유지합니다.'):
            return
        self.busy(True)
        self.status.set('미체결 주문 확인 중')
        def work():
            try:
                broker = Kiwoom(credentials['appkey'], credentials['secret'])
                broker.verify_account(credentials['account'])
                symbols = set(r.code for r in cfg.rules)
                for r in active:
                    detail = json.loads(r[4])
                    symbols.update((detail['source'], detail['target']))
                if broker.pending(symbols):
                    raise Halt('미체결 주문이 남아 있습니다. 영웅문에서 취소하고 다시 확인하세요.')
                for row in active:
                    self.journal.reconcile(row[0])
                self.emit('log', '사용자의 영웅문 대조 및 미체결 없음 확인 후 기록 해제. 자동 재주문 없음.')
            except Exception as exc:
                self.emit('log', str(exc) if isinstance(exc, Halt) else f'해제 실패({type(exc).__name__})')
            finally:
                self.emit('stopped', None)
        self.worker = threading.Thread(target=work, daemon=True)
        self.worker.start()

    def log(self, text):
        self.logs.configure(state='normal')
        self.logs.insert('end', datetime.now(KST).strftime('%H:%M:%S') + '  ' + text + '\n')
        self.logs.see('end')
        if int(self.logs.index('end-1c').split('.')[0]) > 1000:
            self.logs.delete('1.0', '200.0')
        self.logs.configure(state='disabled')

    def drain(self):
        history_dirty=False
        try:
            while True:
                event, payload = self.messages.get_nowait()
                if event == 'audit':
                    history_dirty = history_dirty or payload['stage'] in ('ORDER_ACCEPTED','PARTIAL_FILL','FILL_CONFIRMED','ORDER_REJECTED','ORDER_UNKNOWN')
                    self.audit_status.set(f'[{payload["trace"]}] {payload["stage"]}: {payload["explanation"]}')
                    if payload['stage'] != 'WAITING':
                        self.log(f'[{payload["trace"]}] {payload["explanation"]}')
                elif event == 'feasibility':
                    self.feasibility_status.set(' / '.join(f'{direction}: 예상 매도 {info["sell_qty"]}주 → 매수 {info["estimated_buy_qty"]}주 ({"가능 예상" if info["possible"] else "금액/수량 부족"})' for direction,info in payload.items()) + ' · 현재가 추정, 현금/체결 재확인 필요')
                elif event == 'symbol_lookup':
                    prefix, symbol, name, error = payload
                    self.lookup_pending.discard((prefix, symbol))
                    try:
                        current = code(self.values[prefix + '_code'].get())
                    except Halt:
                        current = None
                    if current != symbol or (self.worker and self.worker.is_alive()):
                        continue
                    if error:
                        self.lookup_status.set(f'{prefix.upper()} 조회 실패: {error}')
                    else:
                        self.values[prefix + '_name'].set(name)
                        self.lookup_done[prefix] = (symbol, name)
                        self.lookup_status.set(f'{prefix.upper()} 확인: {symbol} / {name}')
                elif event == 'strategy':
                    self.strategy_status.set(payload)
                elif event == 'telegram_ids':
                    ids = payload
                    window = getattr(self, 'telegram_window', None)
                    if window and window.winfo_exists():
                        if len(ids) == 1:
                            self.telegram_chat_var.set(ids[0])
                        else:
                            messagebox.showinfo('수신 대화 선택', '찾은 Chat ID: ' + (', '.join(ids) or '없음') + '\n봇에 /start를 보낸 후 다시 조회하고 원하는 ID를 입력하세요.', parent=window)
                elif event == 'notice':
                    self.telegram_notifier.notify(payload['subject'], payload['body'])
                    self.notifier.notify(payload['subject'], payload['body'])
                elif event == 'log':
                    self.log(payload)
                    self.journal.event(payload)
                elif event in ('balance', 'balance_error'):
                    generation, data = payload
                    if generation != self.monitor_generation:
                        continue
                    if event == 'balance':
                        self.render_balance(data)
                    else:
                        self.balance_error = data
                        last = self.last_balance['updated_at'] if self.last_balance else '성공 이력 없음'
                        self.balance_status.set('조회 실패 · 마지막 성공 ' + last + ' · ' + data)
                elif event == 'holdings':
                    pass  # The independent account monitor owns the balance display.
                elif event == 'stopped':
                    self.busy(False)
                    self.live.set(False)
                    self.status.set('중지됨 · 미완료 거래 있음' if self.journal.active() else '중지됨 · 시작 버튼으로 재개')
        except queue.Empty:
            pass
        if history_dirty:
            self.render_history()
        if self.last_balance and not self.balance_error:
            age = time.monotonic() - self.last_balance['received_at']
            if age > 15:
                self.balance_status.set(f'갱신 지연 · 마지막 성공 {self.last_balance["updated_at"]} · {int(age)}초 경과')
        self.after(100, self.drain)

    def close(self):
        if self.worker and self.worker.is_alive():
            self.stop()
            messagebox.showinfo('중지 처리 중', '중지 처리가 끝난 후 창을 닫으세요. 영웅문에서 접수 주문도 확인하세요.')
            return
        if self.notifier.pending or self.telegram_notifier.pending:
            messagebox.showinfo('알림 전송 중', '대기 중인 휴대폰 알림 발송이 끝난 후 창을 닫으세요.')
            return
        self.notifier.close()
        self.telegram_notifier.close()
        if self.monitor_stop:
            self.monitor_stop.set()
        self.destroy()


def main():
    import msvcrt
    root = data_directory()
    lock = open(root / 'instance.lock', 'a+b')
    lock.seek(0)
    if not lock.read(1):
        lock.write(b'0')
        lock.flush()
    lock.seek(0)
    try:
        msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
    except OSError:
        window = tk.Tk()
        window.withdraw()
        messagebox.showerror('중복 실행', '이 PC에서 프로그램이 이미 실행 중입니다.')
        window.destroy()
        return
    try:
        App(root).mainloop()
    finally:
        lock.close()


def smoke_test():
    import tempfile
    Path('smoke-test.json').unlink(missing_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        app = App(Path(tmp))
        def verify():
            try:
                assert not app.live.get()
                assert app.engine is None
                for key, value in {'a_code': '005930', 'a_name': 'A종목', 'b_code': '000660', 'b_name': 'B종목', 'daily': '10'}.items():
                    app.values[key].set(value)
                app.values['a_code'].set(' A005930 ')
                app.values['b_code'].set('０００６６０')
                cfg = app.config()
                assert cfg.rules[0].code == '005930' and cfg.rules[1].code == '000660'
                assert app.values['a_code'].get() == '005930'
                assert cfg.cap >= 1000000 and cfg.drop_percent == 5 and cfg.a_sell_percent == 10
                assert len(app.table['columns']) == 11
                assert app.monitor_worker is None
                sample = {'positions': [], 'account_label': '******7890', 'updated_at': '10:00:00',
                          'received_at': time.monotonic(), 'deposit': D(100000),
                          'cash_available': D(80000), 'd2_deposit': D(90000),
                          'total_cost': 0, 'total_value': 0, 'total_profit': D(0), 'total_profit_rate': D(0)}
                app.render_balance(sample)
                assert '100,000' in app.summary_values['deposit'].get()
                assert '7890' in app.balance_status.get()
                sample['positions'] = [{'code': '005930', 'name': '삼성전자', 'kind': '현금',
                    'qty': 10, 'available': 8, 'average': D(10000), 'current': D(9000),
                    'cost': 100000, 'value': 90000, 'profit': D(-10000), 'profit_rate': D(-10)}]
                app.render_balance(sample)
                assert len(app.table.get_children()) == 1
                assert app.table.item('0')['values'][1] == '삼성전자'
                sample['positions'] = []
                app.render_balance(sample)
                assert len(app.table.get_children()) == 0
                app.disconnect_monitor()
                app.geometry('1024x720')
                app.pages.select(app.dashboard)
                app.update()
                assert app.table.winfo_height() >= 80, ('balance table clipped',app.table.winfo_height())
                assert app.history_table.winfo_height() >= 80, ('history clipped',app.history_table.winfo_height())
                app.history_pages.select(1)
                app.update()
                assert app.logs.winfo_height() >= 80
                app.history_pages.select(0)
                app.journal.audit('SMOKE','SIGNAL','UI 진단 표시 테스트',{'threshold':'9975'})
                app.diagnostic_dialog()
                app.notification_dialog()
                app.mail_dialog()
                app.notifier.close()
                app.telegram_notifier.close()
                assert app.summary_values['deposit'].get() == '—'
                assert app.start_button['state'] != 'disabled'
                app.update_idletasks()
                report = {'status': 'ok', 'trading_started': False,
                          'platform': sys.platform, 'frozen': bool(getattr(sys, 'frozen', False))}
                Path('smoke-test.json').write_text(json.dumps(report), encoding='utf-8')
            finally:
                app.destroy()
        app.after(300, verify)
        app.mainloop()
        if not Path('smoke-test.json').exists():
            raise RuntimeError('UI smoke test did not finish')


if __name__ == '__main__':
    if '--smoke-test' in sys.argv:
        smoke_test()
    else:
        main()
