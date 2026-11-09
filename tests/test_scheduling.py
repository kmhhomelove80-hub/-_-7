from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch
import tempfile
import unittest
from pathlib import Path
from xml.etree import ElementTree as ET
from scheduling import SessionScheduler, task_xml, load_schedule
from web_runtime import Controller, MemoryVault, DEFAULT_CONFIG
from core import KST, Halt
from notifications import TelegramSettings
from test_web_runtime import WebFakeBroker


class SchedulerTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.vault=MemoryVault()
        self.c=Controller(self.temp.name,self.vault,broker_factory=lambda *a:WebFakeBroker(),clock_factory=lambda:lambda:True)
        self.c.save_credentials({'appkey':'x','secret':'y','account':'1234567890'})
        self.c.save_config(dict(DEFAULT_CONFIG,a_code='005930',a_name='A종목',b_code='000660',b_name='B종목',cap='5000000'))
        self.c.telegram.configure(TelegramSettings(True,'123:'+'x'*25,'123'))
        self.c.telegram.notify=MagicMock();self.c.email.notify=MagicMock()
        self.now=datetime(2026,11,9,8,45,tzinfo=KST)
        self.opened=self.now.replace(hour=9,minute=0);self.closed=self.now.replace(hour=15,minute=30)
        self.calendar=MagicMock();self.calendar.session.side_effect=lambda now:(self.opened,self.closed)
        self.scheduler=SessionScheduler(self.c,self.calendar,now=lambda:self.now,auto_exit=True)
        self.task=MagicMock()
        self.c.configure_schedule({'enabled':True,'live_consent':True,'trade_notifications':False},self.task)
    def tearDown(self):
        self.c.close()
        for t in (self.c.trade_thread,self.c.monitor_thread):
            if t:t.join(3)
        self.c.telegram.thread.join(1);self.c.email.thread.join(1);self.temp.cleanup()
    def test_preopen_read_only_then_open_starts_once(self):
        self.scheduler.tick();self.assertFalse(self.c.running());self.assertIn('자동 시작 대기',self.c.schedule_status)
        self.assertIsNone(self.c.journal.state('1234567890','005930','000660'))
        self.now=self.now.replace(hour=9,minute=1)
        with patch.object(self.c,'start') as start:
            self.scheduler.tick();self.scheduler.tick()
            self.assertEqual(start.call_count,1);self.assertTrue(start.call_args.kwargs['scheduled'])
        self.assertEqual(load_schedule(self.temp.name)['attempt_day'],'20261109')
    def test_holiday_does_not_inspect_or_start(self):
        self.calendar.session.side_effect=lambda now:None
        with patch.object(self.c,'inspect') as inspect:
            self.scheduler.tick();inspect.assert_not_called()
        self.assertTrue(self.c.shutdown_requested)
    def test_delayed_open_uses_calendar(self):
        self.opened=self.opened.replace(hour=10);self.closed=self.closed.replace(hour=16)
        self.now=self.now.replace(hour=9,minute=20)
        with patch.object(self.c,'inspect') as inspect:
            self.scheduler.tick();inspect.assert_not_called()
        self.now=self.now.replace(hour=10,minute=1)
        with patch.object(self.c,'start') as start:self.scheduler.tick();start.assert_called_once()
    def test_manual_stop_pauses_today_persistently(self):
        self.c.stop();self.now=self.now.replace(hour=9,minute=1)
        with patch.object(self.c,'start') as start:self.scheduler.tick();start.assert_not_called()
        self.assertEqual(load_schedule(self.temp.name)['paused_day'],'20261109')
    def test_changed_config_blocks_and_deduplicates_alert(self):
        self.c.save_config(dict(self.c.config,drop='3'))
        self.scheduler.tick();self.scheduler.tick()
        self.assertIn('변경',self.c.schedule['blocked']);self.c.telegram.notify.assert_called_once()
    def test_preflight_failure_persists_and_never_restarts_next_day(self):
        self.c.journal.reserve('20261109','1234567890',10,{})
        self.scheduler.tick();self.assertIn('미완료',self.c.schedule['blocked'])
        self.now+=timedelta(days=1)
        with patch.object(self.c,'start') as start:self.scheduler.tick();start.assert_not_called()
    def test_end_checks_pending_and_exits_without_selling(self):
        self.now=self.closed
        self.scheduler.tick()
        self.assertTrue(self.c.shutdown_requested);self.assertEqual(self.c.schedule['finished_day'],'20261109')
        self.c.telegram.notify.assert_not_called()
    def test_end_query_failure_alerts_once_and_blocks(self):
        self.now=self.closed
        with patch.object(self.c,'broker',side_effect=Halt('조회 오류')):
            self.scheduler.tick();self.scheduler.tick()
        self.c.telegram.notify.assert_called_once();self.assertEqual(self.c.schedule['blocked'],'조회 오류')
    def test_no_trade_notifications_by_default_but_errors_notify(self):
        self.c.event('notice',{'subject':'자동매매 시작','body':'정상'})
        self.c.event('notice',{'subject':'매수 체결 000660','body':'정상'})
        self.c.telegram.notify.assert_not_called()
        self.c.event('notice',{'subject':'자동매매 중지 / 확인 필요','body':'접수 불명'})
        self.c.telegram.notify.assert_called_once()
    def test_approval_requires_explicit_boolean_and_notification_channel(self):
        with self.assertRaises(Halt):self.c.configure_schedule({'enabled':True,'live_consent':'true'},self.task)
        self.c.telegram.configure(TelegramSettings())
        with self.assertRaises(Halt):self.c.configure_schedule({'enabled':True,'live_consent':True},self.task)
    def test_task_failure_does_not_claim_enabled(self):
        self.c.schedule['enabled']=False;self.task.configure.side_effect=Halt('권한 오류')
        with self.assertRaises(Halt):self.c.configure_schedule({'enabled':True,'live_consent':True},self.task)
        self.assertFalse(self.c.schedule['enabled'])
    def test_task_xml_is_per_user_wake_enabled_no_password(self):
        xml=task_xml(r'C:\Users\사용자\My App\KiwoomWebTrader.exe','--scheduled --no-browser','S-1-5-21-123',self.now)
        tree=ET.fromstring(xml);ns={'t':'http://schemas.microsoft.com/windows/2004/02/mit/task'}
        self.assertEqual(tree.find('.//t:LogonType',ns).text,'InteractiveToken')
        self.assertEqual(tree.find('.//t:WakeToRun',ns).text,'true')
        self.assertEqual(tree.find('.//t:Interval',ns).text,'PT5M')
        self.assertIn('+09:00',tree.find('.//t:StartBoundary',ns).text)
        self.assertNotIn('Password',xml)
    def test_crash_marker_blocks_next_launch(self):
        self.c.schedule['process_open']=True;self.c.persist_schedule()
        with patch('scheduling.SessionScheduler.launch'):
            self.c.begin_scheduler(self.calendar)
        self.assertIn('정상 종료되지',self.c.schedule['blocked']);self.c.telegram.notify.assert_called_once()
    def test_user_stop_during_scheduled_inspection_cannot_start(self):
        self.now=self.now.replace(hour=9,minute=1)
        original=self.c.inspect
        def inspect():
            result=original();self.c.stop();return result
        with patch.object(self.c,'inspect',side_effect=inspect):
            self.scheduler.tick()
        self.assertFalse(self.c.running())


class CalendarTests(unittest.TestCase):
    def test_real_calendar_holiday_and_year_refresh(self):
        try:from broker import MarketClock;clock=MarketClock()
        except ImportError:self.skipTest('calendar dependency unavailable')
        self.assertIsNone(clock.session(datetime(2026,11,14,tzinfo=KST)))
        opened,closed=clock.session(datetime(2026,11,9,tzinfo=KST))
        self.assertEqual((opened.hour,closed.hour,closed.minute),(9,15,30))
        self.assertIsNotNone(clock.session(datetime(2027,11,9,tzinfo=KST)))
    def test_malformed_schedule_fails_closed(self):
        with tempfile.TemporaryDirectory() as root:
            (Path(root)/'schedule.json').write_text('{"enabled": "true"}')
            with self.assertRaises(Halt):load_schedule(root)


class WindowsRegistrationTests(unittest.TestCase):
    @unittest.skipUnless(__import__('sys').platform=='win32','Windows only registration validation')
    def test_disabled_task_schema_registers_without_running(self):
        import subprocess,sys,csv
        result=subprocess.run(['whoami','/user','/fo','csv','/nh'],capture_output=True,check=True)
        sid=list(csv.reader(result.stdout.decode('utf-8',errors='replace').splitlines()))[-1][-1]
        tree=ET.fromstring(task_xml(sys.executable,'-c "pass"',sid))
        ns={'t':'http://schemas.microsoft.com/windows/2004/02/mit/task'}
        for e in tree.findall('.//t:Enabled',ns):e.text='false'
        name='KiwoomWebTrader.CI.DisabledValidation'
        with tempfile.TemporaryDirectory() as root:
            path=Path(root)/'task.xml';ET.ElementTree(tree).write(path,encoding='utf-16',xml_declaration=True)
            try:
                result=subprocess.run(['schtasks','/Create','/TN',name,'/XML',str(path),'/F'],capture_output=True)
                self.assertEqual(result.returncode,0,result.stderr.decode(errors='replace'))
                query=subprocess.run(['schtasks','/Query','/TN',name,'/XML'],capture_output=True)
                self.assertEqual(query.returncode,0)
            finally:subprocess.run(['schtasks','/Delete','/TN',name,'/F'],capture_output=True)
