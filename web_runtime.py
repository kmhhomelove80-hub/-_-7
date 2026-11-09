"""Local web application controller. No credentials leave the local machine."""
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from decimal import Decimal
import copy
import hashlib
import json
import threading
import time
import secrets
from core import Config, Rule, D, Halt, ApiFailure, Journal, Engine, KST, code, signal, sale_qty, buy_qty, pending_details
from broker import Kiwoom, MarketClock
from monitor import AccountMonitor
from notifications import TelegramSettings, MailSettings, TelegramNotifier, EmailNotifier, private_chat_ids
from diagnostics import trade_history
from scheduling import load_schedule, atomic_json, WindowsTask

DEFAULT_CONFIG={'a_code':'','a_name':'','b_code':'','b_name':'','drop':'5','rise':'5','a_sell':'10','b_sell':'5',
                'cap':'1000000','daily':'10','reserve':'1','poll':'3','initial_price':'0','initial_mode':'BOTH'}


def parse_config(data):
    try:
        get=lambda k:str(data[k]).strip()
        cfg=Config((Rule(get('a_code'),get('a_name')),Rule(get('b_code'),get('b_name'))),
            int(get('cap').replace(',','')),int(get('daily')),D(get('reserve')),int(get('poll')),
            D(get('drop')),D(get('rise')),D(get('a_sell')),D(get('b_sell')),D(get('initial_price')),get('initial_mode').upper())
        cfg.validate()
        return cfg
    except Halt:
        raise
    except Exception:
        raise Halt('전략 숫자와 필수 항목을 확인하세요.') from None


class WindowsVault:
    def __init__(self):
        from keyring.backends.Windows import WinVaultKeyring
        self.store=WinVaultKeyring()
    def get(self,key):
        value=self.store.get_password('KiwoomTwoStockTrader.v1',key)
        return json.loads(value) if value else None
    def set(self,key,value):
        self.store.set_password('KiwoomTwoStockTrader.v1',key,json.dumps(value))
    def delete(self,key):
        if self.store.get_password('KiwoomTwoStockTrader.v1',key):
            self.store.delete_password('KiwoomTwoStockTrader.v1',key)


class MemoryVault:
    def __init__(self):self.data={}
    def get(self,key):return copy.deepcopy(self.data.get(key))
    def set(self,key,value):self.data[key]=copy.deepcopy(value)
    def delete(self,key):self.data.pop(key,None)


class ReadRetryBroker:
    """Retry only explicitly transient READ errors. order() is never retried."""
    READS={'verify_account','holdings','quote','stock_info','pending','cash','cash_order_limit','fills','account_snapshot'}
    def __init__(self,broker,emit,actor,stop=None):
        self.delegate,self.emit,self.actor,self.stop=broker,emit,actor,stop or threading.Event()
    @property
    def audit_callback(self):return self.delegate.audit_callback
    @audit_callback.setter
    def audit_callback(self,value):self.delegate.audit_callback=value
    def __getattr__(self,name):
        method=getattr(self.delegate,name)
        if name not in self.READS:return method
        def call(*args,**kwargs):
            for attempt in range(3):
                try:
                    value=method(*args,**kwargs)
                    if attempt:self.emit('query_restored',{'actor':self.actor,'method':name})
                    return value
                except ApiFailure as exc:
                    transient=exc.outcome=='QUERY_FAILED' and (exc.error_code is None or str(exc.error_code) in ('429','500','502','503','504'))
                    if not transient or attempt==2:raise
                    self.emit('query_retry',{'actor':self.actor,'method':name,'attempt':attempt+1,'maximum':2,'delay':2})
                    if self.stop.wait(2):raise Halt('조회 재시도 중 사용자가 중지했습니다.')
        return call
    def order(self,*args,**kwargs):
        symbol=args[1] if len(args)>1 else kwargs['symbol']
        snapshot=getattr(self.delegate,'quote_snapshots',{}).get(symbol)
        try:age=(datetime.now(KST)-datetime.fromisoformat(snapshot['received_at'])).total_seconds()
        except Exception:age=9999
        if not snapshot or age>30 or age<0:
            raise ApiFailure('시세 수신 후 30초 초과 또는 신선도 확인 불가. 주문 미전송','NOT_SENT_STALE')
        return self.delegate.order(*args,**kwargs)


def stop_card(record,pending):
    d=record.get('data',{});reason=record.get('explanation','확인 필요')
    card={'title':'확인 필요 · 중지','reason':reason,'trace':record.get('trace'),
          'executed':d.get('orders',{}),'completed':d.get('completed_legs',[]),'pending':pending,
          'actions':['새 주문을 차단했습니다.','기존 주문을 자동 취소하거나 재전송하지 않습니다.'],
          'remedies':['영웅문에서 주문·체결·잔고를 대조하세요.','문제 처리 후 재개 전 점검을 다시 실행하세요.'],
          'resume':'확인과 점검 후 사용자가 시작해야 합니다. 누락된 매수는 자동 복구하지 않습니다.'}
    if '미체결 주문' in reason:
        card['title']='미체결 주문 발견 · 중지'
        card['remedies']=['아래 주문번호·잔량을 영웅문과 대조하세요.','주문을 유지할지 취소할지 판단한 후 미체결이 해소되면 점검하세요.']
    elif '사용자' in reason and '중지' in reason:
        card['title']='사용자 중지'
    elif '실행 횟수' in reason:
        card['title']='일일 횟수 도달 · 거래 종료'
        card['remedies']=['예약된 시도 횟수와 설정 한도를 확인하세요. 실패 시도도 횟수에 포함됩니다.']
    elif '시간 밖' in reason:
        card['title']='거래시간 종료 / 거래시간 밖'
        card['remedies']=['허용 거래시간에 기준가와 잔고를 점검한 후 시작하세요.']
    elif d.get('order_outcome')=='UNKNOWN':
        card['title']='주문 접수 여부 불명 · 중지'
        card['remedies']=['실제 주문이 접수됐을 수 있습니다. 영웅문 주문·체결을 확인하세요.','같은 주문을 수동으로 중복 제출하지 마세요.']
    elif d.get('completed_legs')==['SELL']:
        card['title']='매도 후 후속 매수 미완료 · 중지'
        card['remedies']=['매수 주문 전송 여부와 부분체결 여부를 확인하세요.','확보 현금과 부족 조건을 진단에서 확인하세요.','기록 해제 후 시작하면 자금 공급 종목을 다시 매도할 수 있습니다.']
    elif '60초' in reason and '체결' in reason:
        card['title']='체결 확인 시간 초과 · 중지'
        card['remedies']=['영웅문에서 실제 체결수량과 남은 주문을 대조하세요.','자동 취소나 재주문은 실행하지 않았습니다. 잔량 처리 후 다시 점검하세요.']
    elif d.get('api') and d.get('api') not in ('kt10000','kt10001') and d.get('order_outcome')!='NOT_SENT':
        card['title']='주문 후 조회 실패 · 체결 대조 필요'
        card['remedies']=['주문은 이미 접수됐을 수 있습니다. 체결조회 오류와 주문 실패는 다릅니다.','기존 주문번호·체결·잔고를 영웅문에서 확인하고 반복 주문하지 마세요.']
    elif d.get('api') and d.get('order_outcome')=='NOT_SENT':
        card['title']='계좌/시세 조회 실패 · 중지'
        card['remedies']=['API 연결과 마지막 조회 시각을 확인하세요.','조회 오류 설명과 연결 상태를 확인하고, 복구 후 다시 점검하세요.']
    return card


class Controller:
    def __init__(self,root,vault=None,demo=False,broker_factory=Kiwoom,clock_factory=MarketClock,autoconnect=True):
        self.root=Path(root);self.root.mkdir(parents=True,exist_ok=True)
        self.vault=vault if vault is not None else WindowsVault()
        self.demo=demo;self.broker_factory=broker_factory;self.clock_factory=clock_factory
        self.lock=threading.RLock();self.operation=threading.RLock();self.journal=Journal(self.root/'journal.sqlite3')
        self.config=copy.deepcopy(DEFAULT_CONFIG)
        self.config_path=self.root/'settings.json'
        config_warning=False
        if self.config_path.exists():
            try:
                old=json.loads(self.config_path.read_text(encoding='utf-8'))
                self.config.update({k:str(v) for k,v in old.items() if k in DEFAULT_CONFIG})
                if int(self.config['cap'].replace(',',''))<1000000:self.config['cap']='1000000'
            except Exception:config_warning=True
        self.engine=None;self.trade_thread=None;self.preparing=False;self.active_stop=threading.Event();self.monitor_thread=None;self.monitor_stop=threading.Event()
        self.schedule_path=self.root/'schedule.json'
        try:self.schedule=load_schedule(self.root)
        except Halt:
            from scheduling import DEFAULT_SCHEDULE
            self.schedule=dict(DEFAULT_SCHEDULE);config_warning=True
        self.schedule_status='예약 꺼짐';self.shutdown_requested=False;self.scheduler=None;self.scheduled_stop=False
        self.monitor_generation=0;self.inspection=None;self.last_phase=''
        self.state={'mode':'IDLE','mode_label':'중지 · 시작 전 점검 필요','query':'미연결','balance':None,
            'pending':[],'stop_card':None,'logs':[],'audit':None,'strategy':'기준가 확인 전',
            'wait':None,'feasibility':{},'notification':{},'inspection':None}
        self.telegram=TelegramNotifier(TelegramSettings(),lambda message:self.notice_status('telegram',message))
        self.email=EmailNotifier(MailSettings(),lambda message:self.notice_status('email',message))
        try:
            if self.vault.get('telegram'):self.telegram.configure(TelegramSettings(**self.vault.get('telegram')))
            if self.vault.get('email'):self.email.configure(MailSettings(**self.vault.get('email')))
        except Exception:self.log('저장 알림 설정을 읽지 못했습니다. 다시 설정하세요.')
        if config_warning:self.log('설정 파일을 읽지 못했습니다. 전략 설정을 확인하고 저장하세요.')
        if demo:self.seed_demo()
        else:
            try:
                if autoconnect and self.vault.get('credentials'):self.connect()
            except Exception:self.log('계좌 보안 저장소를 읽지 못했습니다. 연결 설정을 확인하세요.')
    def log(self,text):
        with self.lock:
            self.state['logs'].append({'at':datetime.now(KST).strftime('%H:%M:%S'),'message':text})
            self.state['logs']=self.state['logs'][-300:]
    def notice_status(self,channel,message):
        with self.lock:self.state['notification'][channel]={'at':datetime.now(KST).isoformat(),'message':message,'success':'완료' in message}
        self.log(message)
    def running(self):return bool(self.preparing or (self.trade_thread and self.trade_thread.is_alive()))
    def credentials(self):
        result=self.vault.get('credentials')
        if not result or not result.get('appkey') or not result.get('secret'):
            raise Halt('계좌 연결 설정에서 App Key와 Secret을 저장하세요.')
        account=str(result.get('account','')).replace('-','').strip()
        if len(account)!=10 or not account.isdigit():raise Halt('연결 계좌번호 10자리를 확인하세요.')
        return dict(appkey=result['appkey'],secret=result['secret'],account=account)
    def broker(self,actor='inspection',stop=None):
        c=self.credentials()
        return ReadRetryBroker(self.broker_factory(c['appkey'],c['secret']),self.event,actor,stop),c['account']
    def event(self,event,payload):
        with self.lock:
            if event=='log':self.log(payload)
            elif event=='balance':
                self.balance_failures=0;self.state['balance']=payload;self.state['query']='연결됨'
            elif event=='balance_error':
                self.state['query']='조회 실패 · 이전 값';self.log(payload)
                self.balance_failures=getattr(self,'balance_failures',0)+1
                if self.balance_failures>=3 and self.schedule['enabled'] and self.running():
                    self.active_stop.set()
                    self.schedule_issue('계좌 조회가 3회 연속 실패했습니다. 새 주문을 보류했습니다. API 연결을 확인하고 다시 승인하세요.')
            elif event=='pending':self.state['pending']=payload
            elif event=='strategy':self.state['strategy']=payload
            elif event=='feasibility':self.state['feasibility']=payload
            elif event=='trade_wait':
                self.state.update(mode='WAITING',mode_label='실행 중 · 정상 대기',wait=payload)
            elif event=='trade_phase':
                self.last_phase=payload['stage']
                if payload['stage'] not in ('WAITING','HALTED'):
                    self.state.update(mode='TRADING',mode_label='실행 중 · '+payload['explanation'])
            elif event=='query_retry':
                self.log(f'{payload["method"]} 조회 재시도 {payload["attempt"]}/{payload["maximum"]} · 새 주문 보류')
                if payload['actor']=='monitor':self.state['query']='조회 재시도 중 · 이전 값'
                elif payload['actor']=='trade':self.state.update(mode='RETRYING',mode_label='조회 재시도 중 · 새 주문 보류')
            elif event=='query_restored':
                if payload['actor']=='monitor':self.state['query']='연결됨'
                elif payload['actor']=='trade':self.state.update(mode='RUNNING',mode_label='실행 중 · 조회 복구')
            elif event=='audit':
                self.state['audit']=payload
                if payload['stage']=='HALTED':
                    prior_card=self.state['stop_card']
                    card=stop_card(payload,self.state['pending'])
                    self.state.update(mode='HALTED',mode_label=card['title'],stop_card=card)
                    if self.schedule['enabled']:
                        user_cancel='사용자 중지' in payload['explanation']
                        normal=(('시간 밖' in payload['explanation'] or '실행 횟수' in payload['explanation'] or self.scheduled_stop or
                                 user_cancel and self.schedule['paused_day']==datetime.now(KST).strftime('%Y%m%d')) and
                                not self.journal.active() and not self.schedule['blocked'])
                        if normal:
                            self.schedule['paused_day']=datetime.now(KST).strftime('%Y%m%d');self.persist_schedule()
                            self.state.update(mode='SESSION_ENDED',mode_label='정상 종료 · '+payload['explanation'],stop_card=None)
                        elif user_cancel and self.schedule['blocked']:
                            self.state.update(mode='BLOCKED',mode_label='예약 운영 확인 필요',stop_card=prior_card or card)
                        else:self.schedule_issue(card['reason'],notify=False)
                if payload['stage']=='PENDING_ORDERS':self.state['pending']=payload['data']['pending_orders']
            elif event=='notice':
                title=payload['subject']
                if '중지 / 확인 필요' in title:
                    if self.schedule['blocked'] and self.schedule['alert_key']==self.schedule['blocked'] and '사용자 중지' in payload['body']:return
                    if not self.schedule.get('blocked') and (self.scheduled_stop or '시간 밖' in payload['body'] or '실행 횟수' in payload['body'] or '사용자 중지' in payload['body'] and self.schedule['paused_day']==datetime.now(KST).strftime('%Y%m%d')) and not self.journal.active():return
                    if self.schedule['enabled']:
                        self.schedule['alert_key']=self.schedule['blocked'];self.persist_schedule()
                elif any(t in title for t in ('자동매매 시작','자동매매 종료','순환매 신호 발생','순환매 완료',' 체결 ')) and not self.schedule['trade_notifications']:return
                body=payload['body']
                if '중지 / 확인 필요' in payload['subject'] and self.state['stop_card']:
                    card=self.state['stop_card']
                    body+='\n대응 방법: '+ ' / '.join(card['remedies'])+'\n재개 조건: '+card['resume']
                self.telegram.notify(payload['subject'],body);self.email.notify(payload['subject'],body)
            elif event=='stopped':
                if self.state['mode'] not in ('HALTED','USER_STOPPED','BLOCKED','SESSION_ENDED'):
                    self.state.update(mode='IDLE',mode_label='중지 · 재개 전 점검 필요')
    def snapshot(self):
        with self.lock:
            state=copy.deepcopy(self.state)
        state['schedule']={k:self.schedule[k] for k in ('enabled','trade_notifications','blocked','paused_day','attempt_day','finished_day')}
        state['schedule']['status']=self.schedule_status
        state['config']=copy.deepcopy(self.config)
        state['demo']=self.demo;state['running']=self.running()
        try:
            credentials=self.credentials() if not self.demo else None
        except Exception:credentials=None
        state['credential_saved']=bool(credentials)
        state['credentials_account']=('*'*6+credentials['account'][-4:]) if credentials else ''
        state['incomplete']=[{'id':r[0],'day':r[1],'phase':r[3],'detail':json.loads(r[4])} for r in self.journal.active()]
        state['history']=trade_history(self.journal.audit_rows())[:100]
        state['notifications']={'telegram':{'enabled':self.telegram.settings.enabled,'configured':bool(self.telegram.settings.token)},
                                'email':{'enabled':self.email.settings.enabled,'configured':bool(self.email.settings.host)}}
        if state['balance']:
            state['balance_age']=max(0,time.monotonic()-state['balance']['received_at'])
            if state['balance_age']>15:state['query']='更新 지연 · 이전 값'.replace('更新','갱신')
        return state
    def mutation_allowed(self):
        if self.demo:raise Halt('데모는 조회 전용입니다. 실제 주문·인증정보 저장은 하지 않습니다.')
    def save_credentials(self,data):
        self.mutation_allowed()
        if self.running():raise Halt('자동매매를 먼저 중지하세요.')
        result={k:str(data.get(k,'')).strip() for k in ('appkey','secret','account')}
        result['account']=result['account'].replace('-','')
        if not result['appkey'] or not result['secret'] or len(result['account'])!=10 or not result['account'].isdigit():raise Halt('App Key·Secret·계좌번호 10자리를 확인하세요.')
        self.disconnect();self.vault.set('credentials',result);self.inspection=None
        self.state.update(balance=None,pending=[],inspection=None,mode='IDLE',mode_label='계좌정보 변경 · 다시 점검 필요',stop_card=None)
        self.log('인증정보를 Windows 보안 저장소에 저장했습니다.')
    def save_config(self,data):
        self.mutation_allowed()
        if self.running():raise Halt('자동매매를 먼저 중지하세요.')
        cfg=parse_config(data)
        updated={k:str(data.get(k,DEFAULT_CONFIG[k])) for k in DEFAULT_CONFIG}
        updated.update(a_code=cfg.rules[0].code,b_code=cfg.rules[1].code)
        temp=self.config_path.with_suffix('.tmp');temp.write_text(json.dumps(updated,ensure_ascii=False,indent=2),encoding='utf-8');temp.replace(self.config_path)
        self.config=updated;self.inspection=None;self.state['inspection']=None
        self.state.update(mode='IDLE',mode_label='전략 저장됨 · 다시 점검 필요')
        self.log('전략 설정 저장. 다음 시작 전 점검이 필요합니다.')
    def connect(self):
        self.mutation_allowed();self.disconnect()
        stop=self.monitor_stop=threading.Event();generation=self.monitor_generation
        with self.lock:self.state['query']='연결 중 · 이전 값' if self.state['balance'] else '연결 중'
        def run():
            try:
                broker,account=self.broker('monitor',stop)
                def emit(event,data):
                    if generation==self.monitor_generation:self.event(event,data)
                AccountMonitor(broker,account,emit,stop).run()
            except Exception as exc:
                if generation==self.monitor_generation:self.event('balance_error',str(exc) if isinstance(exc,Halt) else '계좌 연결 오류')
        self.monitor_thread=threading.Thread(target=run,daemon=True);self.monitor_thread.start()
    def disconnect(self):
        self.monitor_stop.set();self.monitor_generation+=1
        with self.lock:self.state['query']='연결 해제 · 이전 값'
    def fingerprint(self):
        return hashlib.sha256(json.dumps({'config':self.config,'credentials':self.credentials()},sort_keys=True).encode()).hexdigest()
    def inspect(self,allow_closed=False):
        self.mutation_allowed()
        if self.running():raise Halt('실행 중에는 재개 점검을 할 수 없습니다.')
        cfg=parse_config(self.config);broker,account=self.broker()
        try:
            broker.verify_account(account)
            hs=broker.holdings();cash=broker.cash()
            for rule in cfg.rules:
                if rule.code in hs and hs[rule.code].name.strip()!=rule.name.strip():raise Halt('설정 이름과 계좌 종목명이 다릅니다.')
                price=broker.quote(rule.code,rule.name)
                if rule.code in hs:hs[rule.code]=__import__('dataclasses').replace(hs[rule.code],current=price)
                else:hs[rule.code]=__import__('core').Holding(rule.code,rule.name,0,0,D(0),price,0)
            pending=pending_details(broker.pending({r.code for r in cfg.rules}),self.journal.known_orders(account,datetime.now(KST).strftime('%Y%m%d')))
            active=self.journal.active();stored=self.journal.state(account,cfg.rules[0].code,cfg.rules[1].code)
            state=stored or {'reference':str(cfg.initial_price or hs[cfg.rules[1].code].average),'mode':cfg.initial_mode}
            if D(state['reference'])<=0:raise Halt('첫 기준가를 입력하세요.')
            action=signal(cfg,state,hs[cfg.rules[1].code].current)
            total=sum(h.cost for h in hs.values() if h.code in {r.code for r in cfg.rules})
            used=self.journal.count(datetime.now(KST).strftime('%Y%m%d'),account)
            market_open=self.clock_factory()()
            blockers=[]
            if pending:blockers.append('A/B 미체결 주문이 남아 있습니다.')
            if active:blockers.append('미완료 기록이 있습니다. 영웅문 대조 후 별도로 해제하세요.')
            if total>cfg.cap:blockers.append('합산 매입원금 한도 초과')
            if used>=cfg.daily_max:blockers.append('일일 최대 횟수 도달')
            if not market_open and not allow_closed:blockers.append('허용 거래시간 밖')
            immediate='현재 가격에서 즉시 발생하는 신호 없음'
            if action:
                source=hs[cfg.rules[0 if action=='BUY' else 1].code];target=hs[cfg.rules[1 if action=='BUY' else 0].code]
                qty=sale_qty(source,cfg.a_sell_percent if action=='BUY' else cfg.b_sell_percent)
                headroom=D(cfg.cap-total)+(D(source.cost)*qty/source.qty if source.qty else 0)
                expected=buy_qty(target.current,source.current*qty,cash+source.current*qty,headroom,cfg.reserve_percent)
                immediate=f'지금 시작하면 {source.name} {qty}주 매도 → {target.name} 약 {expected}주 매수 신호가 즉시 성립합니다.'
                if qty<1 or expected<1:blockers.append('현재 신호의 예상 매도/매수수량이 1주 미만')
            identifier=secrets.token_urlsafe(24)
            result={'id':identifier,'at':datetime.now(KST).isoformat(),'state':state,'immediate':immediate,
                'pending':pending,'active_ids':[r[0] for r in active],'blockers':blockers,'can_start':not blockers,
                'cash':str(cash),'daily_count':used,'daily_max':cfg.daily_max,'total_cost':total,'holdings':{k:asdict(v) for k,v in hs.items()},'valid_seconds':30,
                'config':copy.deepcopy(self.config),
                'warning':'새 사이클 점검입니다. 과거 누락 매수를 복구하지 않습니다. 실제 주문 전 다시 조회합니다.'}
            self.inspection={'result':result,'created':time.monotonic(),'fingerprint':self.fingerprint(),'account':account}
            with self.lock:
                self.state['inspection']=result;self.state['pending']=pending
                if not blockers:
                    self.state.update(mode='READY',mode_label='점검 통과 · 사용자 시작 대기')
                    if self.state['stop_card']:self.state['stop_card']['resume']='새 점검에서 현재 차단 사유 없음. 사용자 시작 확인을 기다립니다.'
                else:
                    card=stop_card({'trace':None,'explanation':' / '.join(blockers),'data':{'orders':{'SELL':'NOT_SENT','BUY':'NOT_SENT'}}},pending)
                    self.state.update(mode='BLOCKED',mode_label='점검 차단 · 확인 필요',stop_card=card)
            self.log('재개 전 점검 완료: '+immediate)
            return result
        finally:broker.session.close()
    def require_inspection(self,identifier):
        item=self.inspection
        if not item or item['result']['id']!=identifier or time.monotonic()-item['created']>30 or item['fingerprint']!=self.fingerprint():
            raise Halt('점검이 만료됐거나 설정이 변경됐습니다. 재개 전 점검을 다시 실행하세요.')
        return item
    def start(self,data,scheduled=False):
        self.mutation_allowed()
        with self.lock:
            if self.running():raise Halt('이미 실행/준비 중입니다.')
            if scheduled and (not self.schedule['enabled'] or self.schedule['blocked'] or self.schedule['paused_day']==datetime.now(KST).strftime('%Y%m%d') or self.schedule['consent']!=self.fingerprint()):raise Halt('예약 중지 또는 승인 상태 변경으로 자동 시작을 취소했습니다.')
            item=self.require_inspection(data.get('inspection_id'))
            if data.get('live_confirmed') is not True or data.get('preview_confirmed') is not True:raise Halt('실전 주문 및 점검 내용을 확인해야 합니다.')
            if not item['result']['can_start']:raise Halt('재개 점검의 차단 사유를 먼저 해결하세요.')
            if not scheduled and self.schedule['enabled']:
                self.schedule['paused_day']=datetime.now(KST).strftime('%Y%m%d');self.persist_schedule()
            self.scheduled_stop=False
            self.inspection=None;self.preparing=True
            stop=self.active_stop=threading.Event()
            self.state.update(mode='CONNECTING',mode_label='연결 중 · 주문 전 재확인',stop_card=None,pending=[])
        try:
            if not self.monitor_thread or not self.monitor_thread.is_alive():self.connect()
            engine=Engine(self.broker('trade',stop)[0],parse_config(self.config),self.journal,item['account'],self.clock_factory(),self.event,stop)
            self.engine=engine
            def run():
                try:engine.run()
                finally:engine.broker.session.close()
            with self.lock:
                if stop.is_set():
                    engine.broker.session.close()
                    self.state.update(mode='USER_STOPPED',mode_label='사용자 중지 · 시작 준비 취소')
                    return
                self.trade_thread=threading.Thread(target=run,daemon=True)
                self.trade_thread.start()
        except Exception as exc:
            reason=str(exc) if isinstance(exc,Halt) else f'실행 준비 오류({type(exc).__name__})'
            record=self.journal.audit('START-'+secrets.token_hex(6),'HALTED',reason,{'orders':{'SELL':'NOT_SENT','BUY':'NOT_SENT'},'order_outcome':'NOT_SENT'})
            self.event('audit',record);self.event('notice',{'subject':'자동매매 시작 실패','body':reason})
            raise Halt(reason) from None
        finally:
            with self.lock:self.preparing=False
    def stop(self,scheduled=False):
        self.scheduled_stop=scheduled
        if not scheduled and self.schedule['enabled']:
            self.schedule['paused_day']=datetime.now(KST).strftime('%Y%m%d');self.persist_schedule()
        with self.lock:self.active_stop.set();self.inspection=None
        if self.engine:self.engine.request_stop()
        self.inspection=None
        card=stop_card({'trace':getattr(self.engine,'trace_id',None),'explanation':'사용자가 중지 요청했습니다. 접수된 주문을 취소한 것은 아닙니다.',
            'data':{'orders':getattr(self.engine,'order_statuses',{})}},self.state['pending'])
        with self.lock:
            if scheduled and not self.journal.active():
                self.state.update(mode='SESSION_ENDED',mode_label='정상 종료 · 신규 주문 차단',stop_card=None)
            else:self.state.update(mode='USER_STOPPED',mode_label='사용자 중지 요청 · 접수 주문은 취소하지 않음',stop_card=card)
        self.log('예약 종료 요청. 신규 주문 차단, 기존 주문 취소 없음.' if scheduled else '사용자 중지 요청. 접수/전송 중인 주문은 영웅문에서 확인하세요.')
    def reconcile(self,data):
        self.mutation_allowed()
        if self.running():raise Halt('자동매매를 먼저 중지하세요.')
        item=self.require_inspection(data.get('inspection_id'))
        if data.get('confirmation')!='HTS_CHECKED':raise Halt('영웅문 대조 완료 확인이 필요합니다.')
        if item['result']['pending']:raise Halt('미체결 주문을 먼저 처리하세요.')
        broker,account=self.broker()
        try:
            broker.verify_account(account)
            if broker.pending({r.code for r in parse_config(self.config).rules}):raise Halt('미체결 주문이 남아 있습니다.')
            active=self.journal.active()
            if [r[0] for r in active]!=item['result']['active_ids'] or any(r[2]!=account for r in active):raise Halt('미완료 기록/계좌가 점검 당시와 다릅니다.')
            for row in active:self.journal.reconcile(row[0])
            self.log('미완료 차단 기록 해제. 누락 매수 복구/자동 시작은 실행하지 않았습니다.')
            self.inspection=None;self.state['inspection']=None
        finally:broker.session.close()
    def reset(self,data):
        self.mutation_allowed()
        if self.running():raise Halt('자동매매를 먼저 중지하세요.')
        item=self.require_inspection(data.get('inspection_id'))
        if data.get('confirmation')!='RESET_CONFIRMED' or item['result']['pending'] or item['result']['active_ids']:raise Halt('미체결/미완료 해소와 기준 초기화 확인이 필요합니다.')
        cfg=parse_config(self.config);self.journal.reset_state(item['account'],cfg.rules[0].code,cfg.rules[1].code)
        self.inspection=None;self.state['inspection']=None
    def lookup(self,data):
        self.mutation_allowed();broker,account=self.broker()
        try:broker.verify_account(account);return broker.stock_info(code(data.get('code','')))
        finally:broker.session.close()
    def notification_config(self,data):
        self.mutation_allowed();channel=data.get('channel')
        if channel not in ('telegram','email'):raise Halt('알림 채널 오류')
        previous=self.vault.get(channel) or {}
        fields=set(TelegramSettings.__dataclass_fields__) if channel=='telegram' else set(MailSettings.__dataclass_fields__)
        values={k:v for k,v in data.get('settings',{}).items() if k in fields}
        for key in ('token','password'):
            if key in fields and not values.get(key) and previous.get(key):values[key]=previous[key]
        if 'enabled' in values and not isinstance(values['enabled'],bool):raise Halt('알림 사용 값은 참/거짓이어야 합니다.')
        if channel=='email':values['port']=int(values.get('port',465))
        settings=TelegramSettings(**values) if channel=='telegram' else MailSettings(**values)
        if settings.enabled or data.get('test'):settings.validate()
        self.vault.set(channel,settings.to_dict());notifier=self.telegram if channel=='telegram' else self.email;notifier.configure(settings)
        if data.get('test'):notifier.notify('웹 버전 테스트 알림','실제 거래 주문은 실행하지 않았습니다.',force=True)
    def seed_demo(self):
        self.config.update(a_code='005930',a_name='예시 자금 종목',b_code='000660',b_name='예시 매매 종목')
        now=time.monotonic()
        self.state.update(mode='HALTED',mode_label='데모 · 미체결 주문 발견',query='데모 데이터',
            strategy='B 기준 10,500원 · BUY 대기 · 매수 ≤ 9,975원',
            pending=[{'order':'DEMO-01','symbol':'005930','name':'예시 자금 종목','side':'매도','ordered':'10','filled':'4','remaining':'6','price':'10000','time':'10:30:00','origin':'외부 주문 또는 출처 확인 불가'}],
            balance={'positions':[{'code':'005930','name':'예시 자금 종목','kind':'현금','qty':100,'available':94,'average':'10000','current':'10200','cost':1000000,'value':1020000,'profit':'20000','profit_rate':'2'},
                                  {'code':'000660','name':'예시 매매 종목','kind':'현금','qty':50,'available':50,'average':'10000','current':'9800','cost':500000,'value':490000,'profit':'-10000','profit_rate':'-2'}],
                     'deposit':'230000','cash_available':'180000','d2_deposit':'180000','total_cost':1500000,'total_value':1510000,'total_profit':'10000','total_profit_rate':'0.67','updated_at':'10:30:05','received_at':now,'account_label':'******1234'})
        record={'trace':'DEMO-TRACE','explanation':'A/B에 미체결 주문이 있습니다. 영웅문에서 확인하세요.','data':{'orders':{'SELL':'NOT_SENT','BUY':'NOT_SENT'}}}
        self.state['stop_card']=stop_card(record,self.state['pending'])
        self.log('데모 화면입니다. 실제 API 연결·주문·알림은 실행하지 않습니다.')
    def persist_schedule(self):
        with self.lock:atomic_json(self.schedule_path,self.schedule)
    def schedule_issue(self,reason,notify=True):
        with self.lock:
            self.active_stop.set()
            changed=self.schedule['blocked']!=reason
            self.schedule['blocked']=reason;self.schedule_status='확인 필요 · 자동 시작 차단'
            if changed:
                record=self.journal.audit('SCHEDULE-'+datetime.now(KST).strftime('%Y%m%d-%H%M%S'),'SCHEDULE_BLOCKED',reason,
                    {'actions':['새 주문 차단','자동 취소·재전송 없음'],'resume':'영웅문 대조 후 예약 재승인 필요'})
                self.state['audit']=record
            self.persist_schedule()
            self.log(reason)
            card=stop_card({'explanation':reason,'data':{}},self.state['pending'])
            card['resume']='영웅문 대조와 문제 해결 후 예약 설정에서 다시 승인하세요. 자동 복구 주문은 없습니다.'
            self.state.update(stop_card=card,mode='BLOCKED',mode_label='예약 운영 확인 필요')
            if notify and self.schedule['alert_key']!=reason:
                self.schedule['alert_key']=reason;self.persist_schedule()
                body=reason+'\n조치: 자동 시작 차단. 주문 취소·재전송 없음.\n대응: 영웅문 주문·체결·잔고 대조 및 연결 확인 후 예약 설정에서 다시 승인하세요.'
                self.telegram.notify('예약 자동매매 확인 필요',body);self.email.notify('예약 자동매매 확인 필요',body)
    def configure_schedule(self,data,task=None):
        self.mutation_allowed()
        if self.running():raise Halt('자동매매를 먼저 중지하세요.')
        if not isinstance(data.get('enabled'),bool) or not isinstance(data.get('trade_notifications',False),bool):raise Halt('예약 사용 값은 참/거짓이어야 합니다.')
        enabled=data['enabled']
        if enabled:
            if data.get('live_consent') is not True:raise Halt('매 거래일 자동 실전 주문 실행에 동의해야 합니다.')
            parse_config(self.config);fingerprint=self.fingerprint()
            if self.journal.active():raise Halt('미완료 기록을 영웅문과 대조하고 처리한 후 예약을 승인하세요.')
            if not self.telegram.settings.enabled and not self.email.settings.enabled:raise Halt('예외 알림을 받을 텔레그램 또는 이메일을 먼저 설정하세요.')
            if self.telegram.settings.enabled:self.telegram.settings.validate()
            if self.email.settings.enabled:self.email.settings.validate()
        (task or WindowsTask(self.root)).configure(enabled)
        with self.lock:
            self.schedule.update(enabled=enabled,trade_notifications=data.get('trade_notifications',False))
            if enabled:
                self.schedule.update(consent=fingerprint,blocked='',paused_day='',attempt_day='',preflight_day='',finished_day='',alert_key='',process_open=bool(self.scheduler))
            self.persist_schedule();self.inspection=None
            self.schedule_status='예약 승인됨 · 거래일·계좌 점검 대기' if enabled else '예약 꺼짐'
            if enabled:self.state.update(mode='SCHEDULED_WAIT',mode_label='예약 승인됨 · 거래일 점검 대기',stop_card=None)
        self.log('Windows 예약 작업 '+('등록 및 자동 실전 거래 승인 완료' if enabled else '해제 완료'))
    def begin_scheduler(self,calendar,auto_exit=False):
        from scheduling import SessionScheduler
        self.scheduler=SessionScheduler(self,calendar,auto_exit=auto_exit)
        if self.schedule['enabled']:
            crashed=self.schedule['process_open']
            self.schedule['process_open']=True;self.persist_schedule()
            if crashed:self.schedule_issue('이전 프로그램이 정상 종료되지 않았습니다. 영웅문에서 주문·체결을 확인한 후 예약을 다시 승인하세요.')
        self.scheduler.launch()
    def close(self):
        if self.scheduler:self.scheduler.stop.set()
        self.stop(scheduled=True);self.disconnect();self.telegram.close();self.email.close()
        if self.scheduler:
            self.schedule['process_open']=False;self.persist_schedule()
