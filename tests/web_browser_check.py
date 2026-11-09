"""Real browser QA with a synthetic, read-only local server. Never calls Kiwoom."""
from pathlib import Path
import json
import os
import sys
import tempfile
import threading
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from playwright.sync_api import sync_playwright, expect
from web_app import LocalServer
from web_runtime import Controller,MemoryVault


def main():
    with tempfile.TemporaryDirectory() as directory:
        controller=Controller(directory,MemoryVault(),demo=True)
        server=LocalServer(controller)
        worker=threading.Thread(target=server.serve_forever,daemon=True);worker.start()
        errors=[]
        try:
            with sync_playwright() as p:
                options={'headless':True}
                if os.environ.get('WEB_BROWSER_EXECUTABLE'):options['executable_path']=os.environ['WEB_BROWSER_EXECUTABLE']
                elif sys.platform=='win32':options['channel']='msedge'
                else:options['executable_path']='/usr/bin/chromium'
                browser=p.chromium.launch(**options)
                page=browser.new_page(viewport={'width':1440,'height':1050})
                page.on('pageerror',lambda error:errors.append(str(error)))
                page.goto(server.origin+'/#token='+server.local_token)
                page.get_by_role('heading',name='미체결 주문 발견 · 중지').wait_for()
                assert '토큰' not in page.locator('#stop-reason').inner_text()
                assert page.locator('#holdings tr').count()==2
                assert not page.locator('#start').is_enabled()
                assert page.locator('#pending-count').inner_text()=='1'
                Path('dist').mkdir(exist_ok=True)
                page.screenshot(path='dist/web-preview.png',full_page=True)
                page.get_by_role('button',name='미체결 주문 상세 보기').click()
                assert page.locator('#pending-body').inner_text().find('DEMO-01')>=0
                assert page.locator('#pending-body').inner_text().find('외부 주문')>=0
                page.locator('nav [data-page="strategy"]').click()
                assert page.locator('[name="drop"]').input_value()=='5'
                page.locator('nav [data-page="schedule"]').click()
                assert page.get_by_role('heading',name='예약 자동 운영',exact=True,level=2).is_visible()
                assert not page.locator('#schedule-form button').is_enabled()
                assert '예약 꺼짐' in page.locator('#schedule-status').inner_text()
                page.screenshot(path='dist/schedule-preview.png',full_page=True)
                page.locator('nav [data-page="account"]').click()
                assert page.locator('[name="secret"]').input_value()==''
                page.locator('nav [data-page="overview"]').click()
                for width,height in ((1024,720),(390,844)):
                    page.set_viewport_size({'width':width,'height':height})
                    assert page.locator('#holdings').is_visible()
                    assert page.locator('#show-history').is_visible()
                    overflow=page.evaluate('document.documentElement.scrollWidth > innerWidth+1')
                    assert not overflow, f'Horizontal page overflow at {width}'
                assert not errors,errors
                # Check the explicit preview -> confirmation -> waiting -> stop flow.
                from test_web_runtime import WebFakeBroker
                from web_runtime import DEFAULT_CONFIG
                from unittest.mock import patch,MagicMock
                from notifications import TelegramSettings
                live_temp=tempfile.TemporaryDirectory()
                fake_controller=Controller(live_temp.name,MemoryVault(),broker_factory=lambda *args:WebFakeBroker(),clock_factory=lambda:lambda:True)
                fake_controller.save_credentials({'appkey':'fake-key','secret':'fake-secret','account':'1234567890'})
                fake_controller.save_config(dict(DEFAULT_CONFIG,a_code='005930',a_name='A종목',b_code='000660',b_name='B종목',cap='5000000'))
                fake_server=LocalServer(fake_controller)
                fake_thread=threading.Thread(target=fake_server.serve_forever,daemon=True);fake_thread.start()
                try:
                    page.set_viewport_size({'width':1280,'height':900})
                    page.goto(fake_server.origin+'/#token='+fake_server.local_token)
                    page.get_by_role('button',name='재개 전 점검',exact=True).click()
                    page.locator('#inspection-dialog').wait_for(state='visible')
                    assert 'A종목' in page.locator('#inspection-body').inner_text()
                    page.locator('#confirm-start').click()
                    page.locator('#notice').filter(has_text='점검 내용을 확인').wait_for()
                    assert not fake_controller.running()
                    page.locator('#live-ack').check();page.locator('#preview-ack').check()
                    page.locator('#confirm-start').click()
                    page.locator('#inspection-dialog').wait_for(state='hidden')
                    expect(page.locator('#mode-label')).to_contain_text('정상 대기',timeout=10000)
                    page.locator('#stop').click()
                    expect(page.locator('#mode-label')).to_contain_text('중지',timeout=10000)
                    if fake_controller.trade_thread:fake_controller.trade_thread.join(3)
                    fake_controller.telegram.configure(TelegramSettings(True,'123:'+'x'*25,'123'))
                    page.locator('nav [data-page="schedule"]').click()
                    page.locator('#schedule-form [name="enabled"]').check()
                    with patch('web_runtime.WindowsTask') as task_class:
                        task_class.return_value=MagicMock()
                        page.locator('#schedule-form button').click()
                        expect(page.locator('#notice')).to_contain_text('동의')
                        assert not fake_controller.schedule['enabled']
                        page.locator('#schedule-form [name="live_consent"]').check()
                        page.locator('#schedule-form button').click()
                        expect(page.locator('#schedule-status')).to_contain_text('예약 켜짐',timeout=10000)
                        assert fake_controller.schedule['enabled']
                        assert not fake_controller.running()
                        assert not fake_controller.schedule['trade_notifications']
                        task_class.return_value.configure.assert_called_once_with(True)

                finally:
                    fake_controller.close();fake_server.shutdown();fake_server.server_close()
                    if fake_controller.trade_thread:fake_controller.trade_thread.join(3)
                    if fake_controller.monitor_thread:fake_controller.monitor_thread.join(3)
                    fake_controller.telegram.thread.join(1);fake_controller.email.thread.join(1)
                    live_temp.cleanup()
                assert not errors,errors
                browser.close()
            Path('dist/web-browser-report.json').write_text(json.dumps({'status':'ok','demo_only':True,
                'sizes':['1440x1050','1024x720','390x844'],'errors':errors},ensure_ascii=False,indent=2),encoding='utf-8')
        finally:server.shutdown();server.server_close();controller.close()


if __name__=='__main__':main()
