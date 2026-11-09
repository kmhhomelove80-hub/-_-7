"""Persistent, fail-closed session automation and per-user Windows task registration."""
from datetime import datetime, timedelta
from pathlib import Path
import copy
import json
import os
import subprocess
import sys
import threading
import time
from xml.etree import ElementTree as ET
from core import Halt, KST, ApiFailure

DEFAULT_SCHEDULE={'enabled':False,'suspended':False,'trade_notifications':False,'consent':'','blocked':'','paused_day':'',
                  'attempt_day':'','preflight_day':'','finished_day':'','process_open':False,'alert_key':''}
TASK_NAME='KiwoomWebTrader.Session'


def atomic_json(path,value):
    path=Path(path);temp=path.with_suffix('.tmp')
    temp.write_text(json.dumps(value,ensure_ascii=False,indent=2),encoding='utf-8');temp.replace(path)


def load_schedule(root):
    path=Path(root)/'schedule.json'
    if not path.exists():return copy.deepcopy(DEFAULT_SCHEDULE)
    try:
        value=json.loads(path.read_text(encoding='utf-8'))
        if not isinstance(value,dict):raise ValueError()
        if any(k in value and not isinstance(value[k],type(default)) for k,default in DEFAULT_SCHEDULE.items()):raise ValueError()
        return dict(DEFAULT_SCHEDULE,**{k:v for k,v in value.items() if k in DEFAULT_SCHEDULE})
    except Exception:raise Halt('예약 설정 파일을 읽을 수 없습니다. 예약 설정을 다시 확인하세요.') from None


def task_xml(executable,arguments,user,now=None):
    """Interactive user only: no password or account/API secret in task XML."""
    ns='http://schemas.microsoft.com/windows/2004/02/mit/task'
    ET.register_namespace('',ns)
    def node(parent,name,text=None,**attrs):
        item=ET.SubElement(parent,'{'+ns+'}'+name,attrs)
        if text is not None:item.text=str(text)
        return item
    root=ET.Element('{'+ns+'}Task',{'version':'1.4'})
    triggers=node(root,'Triggers');daily=node(triggers,'CalendarTrigger')
    repetition=node(daily,'Repetition');node(repetition,'Interval','PT5M');node(repetition,'Duration','PT12H');node(repetition,'StopAtDurationEnd','false')
    today=(now or datetime.now(KST)).strftime('%Y-%m-%d')
    node(daily,'StartBoundary',today+'T08:00:00+09:00');node(daily,'Enabled','true')
    node(node(daily,'ScheduleByDay'),'DaysInterval','1')
    logon=node(triggers,'LogonTrigger');node(logon,'Enabled','true');node(logon,'UserId',user)
    principal=node(node(root,'Principals'),'Principal',id='Author');node(principal,'UserId',user)
    node(principal,'LogonType','InteractiveToken');node(principal,'RunLevel','LeastPrivilege')
    settings=node(root,'Settings')
    for name,value in [('MultipleInstancesPolicy','IgnoreNew'),('DisallowStartIfOnBatteries','false'),('StopIfGoingOnBatteries','false'),('StartWhenAvailable','true'),('WakeToRun','true'),('ExecutionTimeLimit','PT0S'),('Enabled','true')]:node(settings,name,value)
    action=node(node(root,'Actions',Context='Author'),'Exec');node(action,'Command',executable);node(action,'Arguments',arguments);node(action,'WorkingDirectory',str(Path(executable).parent))
    return ET.tostring(root,encoding='unicode',xml_declaration=False)


class WindowsTask:
    def __init__(self,root):self.root=Path(root)
    def configure(self,enabled):
        if sys.platform!='win32':raise Halt('Windows 실행파일에서 예약 작업을 설정하세요.')
        flags=getattr(subprocess,'CREATE_NO_WINDOW',0)
        def run(args):return subprocess.run(args,capture_output=True,timeout=25,creationflags=flags)
        if not enabled:
            result=run(['schtasks','/Query','/TN',TASK_NAME])
            if result.returncode==0 and run(['schtasks','/Delete','/TN',TASK_NAME,'/F']).returncode:raise Halt('Windows 예약 작업 해제 실패. 작업 스케줄러에서 '+TASK_NAME+'을 확인하세요.')
            return
        identity=run(['whoami','/user','/fo','csv','/nh'])
        if identity.returncode:raise Halt('Windows 사용자 확인 실패')
        import csv
        user=list(csv.reader(identity.stdout.decode('utf-8',errors='replace').splitlines()))[-1][-1]
        if not user.startswith('S-1-'):raise Halt('Windows 사용자 SID 확인 실패')
        command=sys.executable
        args='--scheduled --no-browser' if getattr(sys,'frozen',False) else subprocess.list2cmdline([str(Path(__file__).resolve().parent/'web_app.py'),'--scheduled','--no-browser'])
        path=self.root/'scheduled-task.xml'
        path.write_text(task_xml(command,args,user),encoding='utf-16')
        try:
            result=run(['schtasks','/Create','/TN',TASK_NAME,'/XML',str(path),'/F'])
            if result.returncode:raise Halt('Windows 예약 등록 실패. 로그인 사용자·작업 스케줄러 권한을 확인하세요.')
        finally:path.unlink(missing_ok=True)


class SessionScheduler:
    def __init__(self,controller,calendar,now=None,auto_exit=False):
        self.c=controller;self.calendar=calendar;self.now=now or (lambda:datetime.now(KST))
        self.auto_exit=auto_exit;self.ending=False;self.closed_at=None;self.stop=threading.Event();self.thread=None
        self.last_heartbeat=0;self.retry_at=0
    def launch(self):
        if self.c.demo:return
        self.thread=threading.Thread(target=self.run,daemon=True);self.thread.start()
    def run(self):
        try:
            while not self.stop.is_set():
                if sys.platform=='win32':
                    import ctypes
                    # Keep the PC awake while this scheduled session is active;
                    # does not power on a shut down PC or prevent manual sleep.
                    ctypes.windll.kernel32.SetThreadExecutionState(0x80000001 if self.c.schedule['enabled'] else 0x80000000)
                try:
                    with self.c.operation:self.tick()
                except Exception as exc:
                    safe=isinstance(exc,ApiFailure) and exc.outcome=='QUERY_FAILED' and (exc.error_code is None or str(exc.error_code) in ('429','500','502','503','504')) and not self.c.journal.active() and not self.c.running()
                    if safe:
                        self.c.schedule.update(attempt_day='',preflight_day='');self.c.persist_schedule();self.retry_at=time.monotonic()+30
                        self.c.schedule_status='조회 장애 · 주문 없이 30초 후 자동 재점검'
                        self.c.event('notice',{'subject':'조회 장애 · 자동복구 대기','body':str(exc)+'\n원인: 읽기 조회 응답 실패. 주문 전송 없음.\n조치: 30초 후 자동 재점검. 지속되면 PC 네트워크와 API 연결을 확인하세요.'})
                    elif self.c.schedule['paused_day']!=self.now().strftime('%Y%m%d'):
                        self.c.schedule_issue('예약 운영 오류: '+(str(exc) if isinstance(exc,Halt) else type(exc).__name__))
                self.stop.wait(5)
        finally:
            if sys.platform=='win32':
                import ctypes
                ctypes.windll.kernel32.SetThreadExecutionState(0x80000000)
    def tick(self):
        c=self.c;now=self.now();day=now.strftime('%Y%m%d');s=c.schedule
        if not s['enabled'] or c.demo:return
        if time.monotonic()<self.retry_at:return
        if time.monotonic()-self.last_heartbeat>=30:
            c.persist_schedule();self.last_heartbeat=time.monotonic()
        session=self.calendar.session(now)
        if session is None:
            c.schedule_status='휴장일 · 자동매매 없음'
            if self.auto_exit and not c.running():c.shutdown_requested=True
            return
        opened,closed=session;start=opened+timedelta(minutes=1);cutoff=closed-timedelta(minutes=10)
        if now>=closed:
            c.schedule_status='정규장 종료 · 주문·기록 점검'
            if c.running():
                c.stop(scheduled=True);return
            if s['finished_day']!=day:
                # Mark once before query: no endless retry or alert storm.
                s['finished_day']=day;c.persist_schedule()
                try:
                    broker,account=c.broker()
                    try:
                        broker.verify_account(account)
                        from core import pending_details
                        pending=pending_details(broker.pending({r.code for r in __import__('web_runtime').parse_config(c.config).rules}),c.journal.known_orders(account,day))
                        c.state['pending']=pending
                        if pending or c.journal.active():raise Halt('장 종료 후 미체결 또는 미완료 거래가 남았습니다. 영웅문에서 주문·체결·잔고를 대조하세요.')
                        c.log('장 종료 점검 완료. 보유종목 유지, 주문 취소·재전송 없음.')
                        c.journal.audit('SCHEDULE-'+day,'SESSION_COMPLETED','정규장 종료 후 미체결·미완료 없음 확인. 기록 보존 후 앱 종료.',{'pending_count':0,'incomplete_count':0})
                    finally:broker.session.close()
                except Exception as exc:c.schedule_issue(str(exc) if isinstance(exc,Halt) else '장 종료 계좌 조회 실패. 영웅문에서 체결 상태를 확인하세요.')
                c.disconnect();self.closed_at=time.monotonic()
            if self.auto_exit and (not c.telegram.pending and not c.email.pending or self.closed_at is not None and time.monotonic()-self.closed_at>120):c.shutdown_requested=True
            return
        if now>=cutoff:
            c.schedule_status='마감 10분 전 · 신규 주문 차단'
            # Engine uses the same calendar cutoff; stop idle loop without an alert.
            if c.running():c.stop(scheduled=True)
            return
        if s['suspended']:
            c.schedule_status='사용자 중단 · 재승인 전까지 자동 시작 안 함';return
        if s['blocked']:
            c.schedule_status='자동 시작 차단 · '+s['blocked'];return
        if s['paused_day']==day:
            c.schedule_status='오늘 자동 시작 중지 · 다음 거래일 대기';return
        if s['consent']!=c.fingerprint():
            c.schedule_issue('계좌 또는 전략이 변경되어 예약 동의가 만료됐습니다. 예약 설정에서 다시 승인하세요.');return
        if now<opened-timedelta(minutes=20):c.schedule_status='개장 전 점검 시각 대기';return
        if now<start:
            c.schedule_status='개장 전 점검 · '+start.strftime('%H:%M')+' 자동 시작 대기'
            if s['preflight_day']!=day:
                s['preflight_day']=day;c.persist_schedule()
                result=c.inspect(allow_closed=True)
                if result['blockers']:c.schedule_issue('개장 전 점검 차단: '+' / '.join(result['blockers']))
                else:
                    c.journal.audit('SCHEDULE-'+day,'PREOPEN_PASSED','개장 전 읽기 전용 점검 통과. 주문 미전송, 개장 후 재점검 예정.',{'opened':opened.isoformat(),'closed':closed.isoformat(),'daily_count':result['daily_count'],'total_cost':result['total_cost']})
                    c.state.update(mode='SCHEDULED_WAIT',mode_label='개장 전 점검 통과 · '+start.strftime('%H:%M')+' 자동 시작 대기')
            return
        if c.running():c.schedule_status='예약 자동매매 실행 중';return
        if s['attempt_day']==day:
            c.schedule_status='오늘 실행 종료 · 자동 재시작 안 함';return
        s['attempt_day']=day;c.persist_schedule()  # Durable BEFORE any start/order.
        c.schedule_status='개장 점검 후 자동 시작 준비'
        result=c.inspect()
        if not result['can_start']:
            c.schedule_issue('자동 시작 점검 차단: '+' / '.join(result['blockers']));return
        # User approval is persisted against the exact config+account fingerprint.
        if s['suspended'] or s['paused_day']==day or not s['enabled'] or s['blocked']:return
        c.start({'inspection_id':result['id'],'live_confirmed':True,'preview_confirmed':True},scheduled=True)
        c.log('저장된 예약 동의와 최신 점검 결과로 자동매매 시작')
