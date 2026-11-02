"""Real SSE/DOM checks with generated controller events; no device commands."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import argparse
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import threading
import time
from urllib.request import urlopen

from playwright.sync_api import sync_playwright, expect

REPO=Path(__file__).resolve().parents[2]


class Events:
    def __init__(self):
        self.condition=threading.Condition();self.seq=0;self.requests=[];self.closed=False
        self.value={'version':1,'epoch':'generated-test','connected':True,'input_fresh':True,'phase':'offline',
                    'active':False,'paused':False,'mode':None,'whole_ready':True,'reason':'','fallback_reason':None}
    def publish(self,**patch):
        with self.condition:
            self.value.update(patch);self.seq+=1;self.condition.notify_all()


def handler(events):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*args):pass
        def do_POST(self):
            events.requests.append(('POST',self.path));self.send_error(405)
        def do_GET(self):
            events.requests.append(('GET',self.path))
            if self.path!='/api/operator/stream':self.send_error(404);return
            self.send_response(200);self.send_header('Content-Type','text/event-stream; charset=utf-8');self.end_headers()
            previous=-1
            try:
                while not events.closed:
                    with events.condition:
                        events.condition.wait_for(lambda:events.seq!=previous or events.closed,.3)
                        current=events.seq;value={**events.value,'seq':current}
                    kind='operator' if current!=previous else 'heartbeat';previous=current
                    self.wfile.write(('event: '+kind+'\ndata: '+json.dumps(value,ensure_ascii=False)+'\n\n').encode())
                    self.wfile.flush()
            except (BrokenPipeError,ConnectionResetError,ConnectionAbortedError):pass
    return Handler


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--out',type=Path,default=REPO/'.local-deploy/presence-gamepad-qa')
    out=parser.parse_args().out;out.mkdir(parents=True,exist_ok=True)
    events=Events();upstream=ThreadingHTTPServer(('127.0.0.1',0),handler(events))
    threading.Thread(target=upstream.serve_forever,daemon=True).start()
    with socket.socket() as probe:probe.bind(('127.0.0.1',0));port=probe.getsockname()[1]
    origin=f'http://127.0.0.1:{port}';log=(out/'server.log').open('w',encoding='utf-8')
    process=subprocess.Popen([sys.executable,'-m','presentation.server','--port',str(port),'--data-dir',str(out/'runtime'),
        '--controller-url',f'http://127.0.0.1:{upstream.server_port}'],cwd=REPO,stdout=log,stderr=log,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0)
    try:
        deadline=time.monotonic()+15
        while True:
            try:
                before=json.load(urlopen(origin+'/api/presence/config',timeout=1));break
            except Exception:
                if time.monotonic()>deadline:raise
                time.sleep(.1)
        with sync_playwright() as p:
            exe=Path(p.chromium.executable_path)
            if not exe.is_file():exe=max((Path(os.environ['LOCALAPPDATA'])/'ms-playwright').glob('chromium-*/chrome-win*/chrome.exe'),key=lambda value:int(value.parents[1].name.split('-')[-1]))
            browser=p.chromium.launch(executable_path=str(exe),headless=True,args=['--use-angle=swiftshader','--enable-unsafe-swiftshader'])
            page=browser.new_page(viewport={'width':1366,'height':768});errors=[];writes=[]
            page.on('pageerror',lambda error:errors.append(str(error)))
            page.on('request',lambda request:writes.append(request.url) if request.method not in ('GET','HEAD','OPTIONS') else None)
            page.add_init_script("navigator.getGamepads=()=>{throw new Error('Must use laptop native events, never workstation pads')}")
            page.goto(origin+'/presence/',wait_until='domcontentloaded')
            expect(page.locator('[data-operator=device]')).to_have_text('手柄已连接')
            expect(page.locator('[data-operator=session]')).to_have_text('未接管')
            expect(page.locator('[data-operator=mode]')).to_have_text('整机联动优先')
            assert len(events.requests)==1
            events.publish(phase='waiting-release')
            expect(page.locator('[data-operator=session]')).to_have_text('等待松开')
            events.publish(phase='starting')
            expect(page.locator('[data-operator=session]')).to_have_text('准备中')
            events.publish(phase='active',active=True,mode='original_hardware')
            expect(page.locator('[data-operator=mode]')).to_have_text('整机联动')
            expect(page.locator('[data-operator=hint]')).to_have_text('Menu 取消')
            page.screenshot(path=str(out/'whole-control.png'))
            events.publish(mode='original_preset',whole_ready=False,fallback_reason='生成测试：缺少双臂姿态卡')
            expect(page.locator('[data-operator=mode]')).to_contain_text('预设直控')
            expect(page.locator('#operator-status')).to_have_attribute('data-state','warning')
            assert '姿态卡' in page.locator('#operator-status').get_attribute('title')
            page.screenshot(path=str(out/'preset-fallback.png'))
            events.publish(phase='paused',paused=True,reason='生成测试：通信超时')
            expect(page.locator('[data-operator=session]')).to_have_text('已暂停')
            events.publish(phase='unknown',connected=None)
            expect(page.locator('[data-operator=session]')).to_have_text('接管未知')
            events.publish(phase='offline',connected=False,input_fresh=False,active=False,paused=False,mode=None)
            expect(page.locator('[data-operator=device]')).to_have_text('手柄未连接')
            assert len(events.requests)==1,events.requests
            assert page.locator('.topbar #voices-open').count()==0
            page.locator('#settings-open').click()
            page.locator('#setting-name').fill('Generated unsaved name')
            page.locator('#voices-open').click()
            expect(page.locator('#voices-dialog')).to_be_visible()
            expect(page.locator('.voice-row')).to_have_count(332,timeout=15000)
            page.locator('#voices-close').click()
            expect(page.locator('#settings')).to_be_visible()
            expect(page.locator('#setting-name')).to_have_value('Generated unsaved name')
            page.screenshot(path=str(out/'voices-in-settings.png'))
            page.locator('#settings-close').click()
            for width,height in [(1280,800),(1024,768),(390,844)]:
                page.set_viewport_size({'width':width,'height':height})
                assert page.evaluate('document.documentElement.scrollWidth<=innerWidth+1')
                page.screenshot(path=str(out/f'header-{width}.png'))
            assert not writes and not errors,(writes,errors)
            assert all(method=='GET' and path=='/api/operator/stream' for method,path in events.requests)
            browser.close()
        assert json.load(urlopen(origin+'/api/presence/config',timeout=5))==before
        receipt={'passed':True,'event_connections':len(events.requests),'state_changes_without_polling':True,
                 'voices_in_settings':True,'unsaved_form_preserved':True,'profile_unchanged':True,
                 'writes':writes,'errors':errors,'viewports':[1366,1280,1024,390]}
        (out/'operator-verification.json').write_text(json.dumps(receipt,ensure_ascii=False,indent=2),encoding='utf-8')
        print(json.dumps(receipt,ensure_ascii=False),flush=True)
    finally:
        events.closed=True
        with events.condition:events.condition.notify_all()
        process.terminate()
        try:process.wait(timeout=5)
        except subprocess.TimeoutExpired:process.kill();process.wait()
        upstream.shutdown();upstream.server_close();log.close()


if __name__=='__main__':main()
