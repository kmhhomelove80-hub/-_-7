"""Loopback-only web launcher. Browser UI, local trading engine, native secret vault."""
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import urlsplit
import argparse
import hmac
import json
import os
import secrets
import sys
import tempfile
import threading
import time
import webbrowser
from core import Halt, KST
from datetime import datetime
from web_runtime import Controller, MemoryVault

ROOT=Path(getattr(sys,'_MEIPASS',Path(__file__).resolve().parent))/'web'


class LocalServer(ThreadingHTTPServer):
    daemon_threads=True
    def __init__(self,controller,port=0):
        super().__init__(('127.0.0.1',port),Handler)
        self.controller=controller;self.local_token=secrets.token_urlsafe(32)
        self.origin=f'http://127.0.0.1:{self.server_port}'


class Handler(BaseHTTPRequestHandler):
    def log_message(self,*args):pass
    def headers_safe(self,code,kind='application/json; charset=utf-8'):
        self.send_response(code)
        for key,value in {'Content-Type':kind,'Cache-Control':'no-store','X-Content-Type-Options':'nosniff',
            'X-Frame-Options':'DENY','Referrer-Policy':'no-referrer',
            'Content-Security-Policy':"default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"}.items():self.send_header(key,value)
        self.end_headers()
    def reply(self,status,data):
        self.headers_safe(status);self.wfile.write(json.dumps(data,ensure_ascii=False,default=str).encode())
    def host_valid(self):return self.headers.get('Host')==urlsplit(self.server.origin).netloc
    def authorized(self):return self.host_valid() and hmac.compare_digest(self.headers.get('X-Local-Token',''),self.server.local_token)
    def do_GET(self):
        if not self.host_valid():self.reply(403,{'error':'잘못된 로컬 호스트'});return
        path=urlsplit(self.path).path
        if path.startswith('/api/'):
            if not self.authorized():self.reply(403,{'error':'앱에서 연 브라우저로 접속하세요.'});return
            try:
                if path=='/api/ping':data={'app':'kiwoom-local-web-v1','pid':os.getpid()}
                elif path=='/api/state':data=self.server.controller.snapshot()
                elif path=='/api/diagnostics':data={'schema':2,'exported_at':datetime.now(KST).isoformat(),'records':self.server.controller.journal.audit_rows()}
                else:self.reply(404,{'error':'경로 없음'});return
                self.reply(200,data)
            except Exception:self.reply(500,{'error':'로컬 상태 조회 실패. 앱 기록을 확인하세요.'})
            return
        files={'/':('index.html','text/html; charset=utf-8'),'/app.js':('app.js','application/javascript; charset=utf-8'),'/style.css':('style.css','text/css; charset=utf-8')}
        if path not in files:self.reply(404,{'error':'경로 없음'});return
        filename,kind=files[path];self.headers_safe(200,kind);self.wfile.write((ROOT/filename).read_bytes())
    def do_POST(self):
        if not self.authorized() or self.headers.get('Origin')!=self.server.origin:
            self.reply(403,{'error':'로컬 앱 인증/출처 확인 실패'});return
        try:
            size=int(self.headers.get('Content-Length','0'))
            if size<0 or size>32768:raise Halt('요청 크기 초과')
            data=json.loads(self.rfile.read(size) or b'{}')
            if not isinstance(data,dict):raise Halt('JSON 객체가 필요합니다.')
            route=urlsplit(self.path).path
            c=self.server.controller
            if route=='/api/stop':c.stop();self.reply(200,{'ok':True});return
            if route=='/api/shutdown':
                if c.running():raise Halt('자동매매를 먼저 중지하고 접수 주문을 확인하세요.')
                if c.telegram.pending or c.email.pending:raise Halt('알림 발송이 끝난 후 종료하세요.')
                c.stop()
                self.reply(200,{'ok':True});threading.Thread(target=self.server.shutdown,daemon=True).start();return
            with c.operation:
                if route=='/api/credentials':c.save_credentials(data);result=None
                elif route=='/api/config':c.save_config(data);result=None
                elif route=='/api/schedule':c.configure_schedule(data);result=None
                elif route=='/api/connect':c.connect();result=None
                elif route=='/api/disconnect':c.disconnect();result=None
                elif route=='/api/lookup':result=c.lookup(data)
                elif route=='/api/inspect':result=c.inspect()
                elif route=='/api/start':c.start(data);result=None
                elif route=='/api/reconcile':c.reconcile(data);result=None
                elif route=='/api/reset':c.reset(data);result=None
                elif route=='/api/notifications':c.notification_config(data);result=None
                elif route=='/api/telegram-chats':
                    c.mutation_allowed();from notifications import private_chat_ids
                    token=data.get('token') or (c.vault.get('telegram') or {}).get('token','')
                    result={'ids':private_chat_ids(token)}
                else:self.reply(404,{'error':'경로 없음'});return
            self.reply(200,{'ok':True,'result':result})
        except Halt as exc:self.reply(409,{'error':str(exc)})
        except (ValueError,KeyError,TypeError):self.reply(400,{'error':'입력 형식과 필수 항목을 확인하세요.'})
        except Exception:self.reply(500,{'error':'로컬 처리 오류. 거래를 반복 실행하지 말고 진단을 확인하세요.'})


def data_directory():
    if sys.platform!='win32':raise Halt('실전 배포본은 Windows 10/11용입니다. 데모는 --demo를 사용하세요.')
    directory=Path(os.environ['LOCALAPPDATA'])/'KiwoomTwoStockTrader'
    directory.mkdir(parents=True,exist_ok=True)
    return directory


def smoke_test():
    import urllib.request,urllib.error
    with tempfile.TemporaryDirectory() as temp:
        controller=Controller(temp,MemoryVault(),demo=True)
        server=LocalServer(controller);thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        try:
            assert urllib.request.urlopen(server.origin).status==200
            headers={'X-Local-Token':server.local_token}
            request=urllib.request.Request(server.origin+'/api/state',headers=headers)
            state=json.load(urllib.request.urlopen(request))
            assert state['demo'] and state['stop_card']['pending'][0]['remaining']=='6'
            assert not state['running']
            try:urllib.request.urlopen(server.origin+'/api/state');raise AssertionError('unauthorized state allowed')
            except urllib.error.HTTPError as exc:assert exc.code==403
            request=urllib.request.Request(server.origin+'/api/start',data=b'{}',headers={**headers,'Origin':server.origin,'Content-Type':'application/json'})
            try:urllib.request.urlopen(request);raise AssertionError('demo trade allowed')
            except urllib.error.HTTPError as exc:assert exc.code==409
            Path('web-smoke-test.json').write_text(json.dumps({'status':'ok','trading_started':False,'loopback_only':True,
                'frozen':bool(getattr(sys,'frozen',False))}),encoding='utf-8')
        finally:server.shutdown();server.server_close();controller.close()


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--demo',action='store_true');parser.add_argument('--no-browser',action='store_true');parser.add_argument('--port',type=int,default=0);parser.add_argument('--smoke-test',action='store_true');parser.add_argument('--scheduled',action='store_true')
    args=parser.parse_args()
    if args.smoke_test:smoke_test();return
    lock=None;temp=None;calendar=None
    if args.scheduled:
        if args.demo:raise Halt('데모는 예약 실전 실행을 지원하지 않습니다.')
        from scheduling import load_schedule
        from broker import MarketClock
        from datetime import timedelta
        root=data_directory();schedule=load_schedule(root)
        if not schedule['enabled'] or schedule['paused_day']==datetime.now(KST).strftime('%Y%m%d'):return
        if not 8<=datetime.now(KST).hour<20:return

    if args.demo:
        temp=tempfile.TemporaryDirectory();root=Path(temp.name);vault=MemoryVault()
    else:
        import msvcrt
        root=data_directory();vault=None
        # Share the desktop app's lock so both cannot trade the same PC account concurrently.
        lock=open(root/'instance.lock','a+b');lock.seek(0)
        if not lock.read(1):lock.write(b'0');lock.flush()
        lock.seek(0)
        try:msvcrt.locking(lock.fileno(),msvcrt.LK_NBLCK,1)
        except OSError:
            if args.scheduled:lock.close();return
            from web_runtime import WindowsVault
            import urllib.request
            session=WindowsVault().get('web_session')
            try:
                port=int(session['port']);token=str(session['token'])
                if not 1<=port<=65535:raise ValueError()
                origin=f'http://127.0.0.1:{port}'
                request=urllib.request.Request(origin+'/api/ping',headers={'X-Local-Token':token})
                response=json.load(urllib.request.build_opener(urllib.request.ProxyHandler({})).open(request,timeout=2))
                if response.get('app')!='kiwoom-local-web-v1' or response.get('pid')!=session['pid']:raise ValueError()
                webbrowser.open(origin+'/#token='+token)
                lock.close()
                return
            except Exception:raise Halt('기존 데스크톱/웹 프로그램이 실행 중입니다. 기존 창에서 중지·종료하세요.') from None
    calendar_error=False
    if args.scheduled:
        try:
            calendar=MarketClock();now=datetime.now(KST);session=calendar.session(now)
            if session is None or now>=session[1]+timedelta(minutes=10):
                if lock:lock.close()
                return
        except Exception:calendar_error=True
    controller=Controller(root,vault,args.demo,autoconnect=not args.scheduled);server=LocalServer(controller,args.port)
    if calendar_error:
        controller.schedule_issue('거래일 달력 초기화 실패. 새 주문을 차단했습니다. 프로그램 버전과 달력을 확인한 뒤 다시 승인하세요.')
        def finish_error():
            for _ in range(120):
                if not controller.telegram.pending and not controller.email.pending:break
                time.sleep(1)
            controller.shutdown_requested=True
        threading.Thread(target=finish_error,daemon=True).start()

    if not args.demo:controller.vault.set('web_session',{'port':server.server_port,'token':server.local_token,'pid':os.getpid()})
    url=server.origin+'/#token='+server.local_token
    if not args.demo and not calendar_error:
        try:
            from broker import MarketClock
            if args.scheduled and session and now>session[0]+timedelta(minutes=6) and not controller.schedule['blocked'] and controller.schedule['attempt_day']!=now.strftime('%Y%m%d'):
                controller.schedule_issue('예정 자동 시작보다 5분 이상 늦게 실행됐습니다. PC 전원·로그인·절전을 확인하고 예약을 다시 승인하세요.')
            controller.begin_scheduler(calendar or MarketClock(),auto_exit=True)
        except Exception as exc:controller.schedule_issue('거래일 달력 초기화 실패. 예약을 중단했습니다. 달력과 프로그램 버전을 확인하세요.')
    if args.no_browser or args.scheduled:
        if not getattr(sys,'frozen',False):print('LOCAL_WEB_READY '+url,flush=True)
    else:webbrowser.open(url)
    def auto_shutdown():
        while not controller.shutdown_requested:
            time.sleep(1)
        server.shutdown()
    threading.Thread(target=auto_shutdown,daemon=True).start()
    try:server.serve_forever(poll_interval=.2)
    finally:
        controller.close();server.server_close()
        if not args.demo:
            try:controller.vault.delete('web_session')
            except Exception:pass
        if lock:lock.close()
        if temp:temp.cleanup()


if __name__=='__main__':
    try:main()
    except Exception as exc:
        # Frozen windowed startup errors must be visible, without printing credentials.
        text=str(exc) if isinstance(exc,Halt) else f'웹 앱 시작 실패({type(exc).__name__})'
        if sys.platform=='win32':
            import ctypes
            ctypes.windll.user32.MessageBoxW(None,text,'키움 로컬 웹 앱',0x10)
        else:print(text,file=sys.stderr)
        sys.exit(1)
