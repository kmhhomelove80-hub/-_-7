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
from tkinter import ttk, messagebox
from datetime import datetime
from core import Config, Rule, D, Halt, Journal, Engine, KST
from broker import Kiwoom, MarketClock
from monitor import AccountMonitor

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
        self.geometry('1240x980')
        self.minsize(1100, 850)
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
        self.inputs = []
        self.live = tk.BooleanVar(value=False)
        self.status = tk.StringVar(value='중지됨 · 실제 주문 없음')
        style = ttk.Style(self)
        style.theme_use('clam')
        style.configure('TLabel', font=('맑은 고딕', 10))
        style.configure('TButton', font=('맑은 고딕', 10), padding=7)
        style.configure('Title.TLabel', font=('맑은 고딕', 20, 'bold'))
        base = ttk.Frame(self, padding=20)
        base.pack(fill='both', expand=True)
        ttk.Label(base, text='두 종목 순환매매', style='Title.TLabel').pack(anchor='w')
        ttk.Label(base, text='실전 전용 · 평균 매입가 기준 · 매도 → 체결 확인 → 추가 매수').pack(anchor='w', pady=(4, 12))
        auth = ttk.LabelFrame(base, text='실전 계좌 연결 / Windows 보안 저장소', padding=10)
        auth.pack(fill='x')
        self.field(auth, 'appkey', 'App Key', 0, 0, secret=True)
        self.field(auth, 'secret', 'Secret Key', 0, 2, secret=True)
        self.field(auth, 'account', '연결 계좌번호', 1, 0, secret=True)
        self.save_button = ttk.Button(auth, text='인증정보 1회 저장', command=self.save_credentials)
        self.save_button.grid(row=1, column=2, padx=6)
        self.delete_button = ttk.Button(auth, text='저장 인증정보 삭제', command=self.delete_credentials)
        self.delete_button.grid(row=1, column=3, padx=6)
        ttk.Label(auth, text='영웅문 ID·비밀번호는 필요하지 않습니다. 실전 REST API 키에 연결된 계좌를 확인합니다.').grid(row=2, column=0, columnspan=4, sticky='w', pady=6)
        params = ttk.LabelFrame(base, text='전략 설정', padding=10)
        params.pack(fill='x', pady=10)
        for j, label in enumerate(('구분', '종목코드', '정확한 종목명', '하락 기준 %', '상대 종목 매도 %')):
            ttk.Label(params, text=label).grid(row=0, column=j, padx=8, sticky='w')
        for i, prefix in enumerate(('a', 'b'), 1):
            ttk.Label(params, text=prefix.upper()).grid(row=i, column=0)
            for j, suffix in enumerate(('code', 'name', 'drop', 'sell'), 1):
                self.entry(params, prefix + '_' + suffix, i, j, width=23 if suffix == 'name' else 15)
        limits = ttk.Frame(params)
        limits.grid(row=3, column=0, columnspan=5, sticky='w', pady=10)
        self.field(limits, 'cap', '합산 매입원금 한도(원)', 0, 0)
        self.field(limits, 'daily', '하루 최대 사이클', 0, 2)
        self.field(limits, 'reserve', '비용 예비율 %', 1, 0, default='1')
        self.field(limits, 'poll', '조건 대기 조회 간격(초)', 1, 2, default='3')
        ttk.Label(params, text='한도 500,000~1,000,000,000원 · 기존 보유 원금 포함 · 동률이면 A 우선 · 매도 수량 소수점 버림').grid(row=4, column=0, columnspan=5, sticky='w')
        ttk.Label(params, text='지정가: 주문 직전 현재가 / 60초 내 전량 미체결 시 정지 / 장 마감 10분 전부터 새 주문 차단').grid(row=5, column=0, columnspan=5, sticky='w')
        self.live_check = ttk.Checkbutton(base, text='실전 계좌에서 실제 매도·매수 주문을 실행합니다.', variable=self.live)
        self.live_check.pack(anchor='w')
        actions = ttk.Frame(base)
        actions.pack(fill='x', pady=8)
        self.read_button = ttk.Button(actions, text='계좌 연결 / 잔고 자동갱신', command=lambda: self.begin(False))
        self.read_button.pack(side='left')
        self.start_button = ttk.Button(actions, text='실전 자동매매 시작', command=lambda: self.begin(True))
        self.start_button.pack(side='left', padx=8)
        ttk.Button(actions, text='자동매매 중지', command=self.stop).pack(side='left')
        self.recover_button = ttk.Button(actions, text='미완료 거래 확인 / 해제', command=self.recover)
        self.recover_button.pack(side='right')
        ttk.Label(base, textvariable=self.status, foreground='#ad2626').pack(anchor='w', pady=5)
        balance_box = ttk.LabelFrame(base, text='계좌 전체 잔고 · 5초 간격 재조회', padding=8)
        balance_box.pack(fill='x', pady=5)
        self.balance_status = tk.StringVar(value='미연결 · 계좌 연결 버튼을 누르세요.')
        self.summary_values = {}
        summary = ttk.Frame(balance_box)
        summary.pack(fill='x')
        for i, (key, title) in enumerate((('deposit', '예수금'), ('cash_available', '주문가능현금(100%)'),
                ('d2_deposit', 'D+2 추정예수금'), ('total_cost', '총매입금액'),
                ('total_value', '총평가금액'), ('total_profit', '평가손익 / 수익률'))):
            cell = ttk.Frame(summary, padding=6)
            cell.grid(row=i // 3, column=i % 3, sticky='w', padx=15)
            ttk.Label(cell, text=title).pack(anchor='w')
            value = tk.StringVar(value='—')
            self.summary_values[key] = value
            ttk.Label(cell, textvariable=value, font=('맑은 고딕', 11, 'bold')).pack(anchor='w')
        toolbar = ttk.Frame(balance_box)
        toolbar.pack(fill='x')
        ttk.Label(toolbar, textvariable=self.balance_status).pack(side='left')
        ttk.Button(toolbar, text='잔고 조회 연결 해제', command=self.disconnect_monitor).pack(side='right')
        columns = ('code', 'name', 'kind', 'qty', 'available', 'average', 'current', 'cost', 'value', 'profit', 'rate')
        grid = ttk.Frame(balance_box)
        grid.pack(fill='x', pady=5)
        self.table = ttk.Treeview(grid, columns=columns, show='headings', height=5)
        for key, title in zip(columns, ('코드', '종목명', '구분', '보유', '매도가능', '평균 매입가', '현재가', '매입금액', '평가금액', '평가손익', '수익률 %')):
            self.table.heading(key, text=title)
            self.table.column(key, width=95, minwidth=65, anchor='e')
        scroll = ttk.Scrollbar(grid, orient='vertical', command=self.table.yview)
        self.table.configure(yscrollcommand=scroll.set)
        self.table.pack(side='left', fill='x', expand=True)
        scroll.pack(side='right', fill='y')
        ttk.Label(base, text='거래 기록 · 중지 버튼은 이미 접수된 주문을 취소하지 않습니다. 미체결 주문은 영웅문에서 확인하세요.').pack(anchor='w')
        self.logs = tk.Text(base, height=10, bg='#142337', fg='#e1eaf5', font=('맑은 고딕', 10), state='disabled')
        self.logs.pack(fill='both', expand=True, pady=5)
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

    def field(self, parent, name, title, row, col, secret=False, default=''):
        ttk.Label(parent, text=title).grid(row=row, column=col, sticky='w', padx=5)
        self.entry(parent, name, row, col + 1, secret=secret, default=default)

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
            rules = tuple(Rule(get(p + '_code'), get(p + '_name'), D(get(p + '_drop')),
                               D(get(p + '_sell'))) for p in ('a', 'b'))
            cfg = Config(rules, int(get('cap').replace(',', '')), int(get('daily')),
                         D(get('reserve')), int(get('poll')))
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
                                     self.read_button, self.recover_button, self.live_check]:
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
                self.emit('log', str(exc) if isinstance(exc, Halt) else f'연결 오류({type(exc).__name__}). 의존성/응답 규격을 확인하세요.')
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
        try:
            while True:
                event, payload = self.messages.get_nowait()
                if event == 'log':
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
