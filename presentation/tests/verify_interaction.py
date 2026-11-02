"""Real browser regression for LAN-style messaging and ceiling joint animation.

Uses an isolated loopback service, a fake reply provider, and generated messages.
No device, account credential, actual model call, camera or actuator is used.
"""
from __future__ import annotations
import argparse
from http.server import ThreadingHTTPServer
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import threading
import time
from urllib.request import urlopen

from playwright.sync_api import sync_playwright,expect

REPO=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(REPO))
from deployment.onboard.service import Mailbox,handler_for


class ReplyProvider:
    def __init__(self):self.calls=[];self.cancelled=threading.Event()
    def session(self):return {}
    def ask(self,message,request_id):
        self.calls.append((message,request_id));return {'text':'收到，消息链路已验证。'}
    def cancel(self):self.cancelled.set()


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--out',type=Path,default=REPO/'.local-deploy/presence-qa-v4')
    out=parser.parse_args().out;out.mkdir(parents=True,exist_ok=True)
    brain=ReplyProvider();mailbox=Mailbox(brain,out/'fake-mailbox')
    threading.Thread(target=mailbox.work,daemon=True).start()
    bridge=ThreadingHTTPServer(('127.0.0.1',0),handler_for(mailbox))
    threading.Thread(target=bridge.serve_forever,daemon=True).start()
    with socket.socket() as probe:probe.bind(('127.0.0.1',0));port=probe.getsockname()[1]
    url=f'http://127.0.0.1:{port}'
    log=(out/'interaction-server.log').open('w',encoding='utf-8')
    process=subprocess.Popen([sys.executable,'-m','presentation.server','--port',str(port),'--data-dir',str(out/'runtime'),
                    '--onboard-url',f'http://127.0.0.1:{bridge.server_port}'],cwd=REPO,stdout=log,stderr=log,
                    creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0)
    try:
        deadline=time.monotonic()+12
        while True:
            try:urlopen(url+'/api/presence/state',timeout=1);break
            except Exception:
                if time.monotonic()>deadline:raise
                time.sleep(.1)
        with sync_playwright() as p:
            executable=Path(p.chromium.executable_path)
            if not executable.is_file():
                executable=max((Path(os.environ['LOCALAPPDATA'])/'ms-playwright').glob('chromium-*/chrome-win*/chrome.exe'),key=lambda p:int(p.parents[1].name.split('-')[-1]))
            browser=p.chromium.launch(executable_path=str(executable),headless=True,args=['--use-angle=swiftshader','--enable-unsafe-swiftshader'])
            page=browser.new_page(viewport={'width':1366,'height':768},device_scale_factor=1)
            page.add_init_script("Object.defineProperty(crypto,'randomUUID',{value:undefined,configurable:true});")
            # Only the test response exposes the renderer for matrix assertions.
            page.route('**/app.js',lambda route:route.fulfill(body=(REPO/'presentation/static/app.js').read_text(encoding='utf-8')+'\nwindow.testAvatar=avatar;',content_type='text/javascript'))
            errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
            page.on('console',lambda m:errors.append(m.text) if m.type=='error' and any(s in m.text for s in ['GLTFLoader','WebGL','Content Security Policy']) else None)
            page.goto(url+'/presence/',wait_until='domcontentloaded')
            try:expect(page.locator('#model-type')).to_have_text('MOSS 3D',timeout=30000)
            except Exception:
                print('Renderer:',page.locator('#renderer-note').text_content(),'Errors:',errors,flush=True)
                raise
            expect(page.locator('#send-message')).to_be_enabled()
            assert page.evaluate('typeof crypto.randomUUID')=='undefined'
            page.locator('#message-input').fill('界面发送测试')
            page.locator('#send-message').click()
            expect(page.locator('#robot-response')).to_have_text('收到，消息链路已验证。',timeout=10000)
            expect(page.locator('#message-input')).to_have_value('')
            expect(page.locator('#send-message')).to_be_enabled(timeout=10000)
            assert len(brain.calls)==1 and brain.calls[0][1].startswith('presence-')
            page.screenshot(path=str(out/'chat-reply.png'))
            snapshot="""()=>{const a=testAvatar;a.scene.updateMatrixWorld(true);const fixed=a.rig.fixed.matrixWorld.toArray(),head=a.rig.body.matrixWorld.toArray();
              const point=new a.rig.fixed.position.constructor(0,1.575,0).project(a.camera);
              return {fixed,head,yaw:a.rig.yaw,root:a.pivot.matrixWorld.toArray(),ceilingScreenY:(1-point.y)/2,hostTop:a.host.getBoundingClientRect().top,stageTop:document.getElementById('stage').getBoundingClientRect().top};}"""
            page.emulate_media(reduced_motion='no-preference')
            box=page.locator('#avatar-host').bounding_box()
            page.mouse.move(box['x']+box['width']*.15,box['y']+box['height']*.45)
            page.wait_for_function('()=>testAvatar.rig.yaw < -.20',timeout=15000)
            left=page.evaluate(snapshot);page.screenshot(path=str(out/'suspended-left.png'))
            page.mouse.move(box['x']+box['width']*.85,box['y']+box['height']*.60)
            page.wait_for_function('()=>testAvatar.rig.yaw > .20',timeout=15000)
            right=page.evaluate(snapshot);page.screenshot(path=str(out/'suspended-right.png'))
            assert left['fixed']==right['fixed'] and left['root']==right['root']
            assert right['yaw']-left['yaw']>.35 and left['head']!=right['head']
            for view in [left,right]:
                assert abs(view['ceilingScreenY'])<.02,view
                assert abs(view['hostTop']-view['stageTop'])<2,view
            page.locator('#process-toggle').click();page.wait_for_timeout(200)
            centered=page.evaluate(snapshot)
            assert centered['fixed']==right['fixed'] and abs(centered['ceilingScreenY'])<.02
            page.screenshot(path=str(out/'suspended-centered.png'))
            page.emulate_media(reduced_motion='reduce');page.wait_for_timeout(200)
            assert page.evaluate('testAvatar.rig.yaw')==0
            inspector=browser.new_page(viewport={'width':1600,'height':1100})
            inspector.goto(url+'/presence/assets/model-inspector.html')
            expect(inspector.locator('#model-label')).to_have_text('550W / JOM',timeout=30000)
            for angle,label in [(-30,'left'),(30,'right')]:
                inspector.locator('#joint-yaw').fill(str(angle));inspector.locator('#joint-yaw').dispatch_event('input')
                expect(inspector.locator('#joint-yaw-value')).to_have_text(str(angle)+'°')
                inspector.screenshot(path=str(out/f'rig-inspector-{label}.png'))
            assert not errors,errors
            (out/'interaction.json').write_text(json.dumps({'passed':True,'chat_calls':len(brain.calls),'randomUUID':'undefined',
              'left':left,'right':right,'centered':centered,'errors':errors},indent=2),encoding='utf-8')
            browser.close()
        print('PASS: HTTP-compatible message send/reply, fixed ceiling, independent joints, responsive ceiling anchor')
    finally:
        process.terminate()
        try:process.wait(timeout=5)
        except subprocess.TimeoutExpired:process.kill();process.wait()
        bridge.shutdown();bridge.server_close();log.close()


if __name__=='__main__':main()
