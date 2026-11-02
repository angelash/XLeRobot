"""Inspect display-only idle motion with a real browser; never operate hardware."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time
from urllib.request import urlopen

from PIL import Image, ImageDraw, ImageFont
from playwright.sync_api import sync_playwright, expect

REPO = Path(__file__).resolve().parents[2]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--url', help='Existing Studio; leave empty for an isolated local service')
    parser.add_argument('--out', type=Path, default=REPO/'.local-deploy/presence-idle-qa')
    args = parser.parse_args()
    out = args.out.resolve(); out.mkdir(parents=True, exist_ok=True)
    process = log = None
    if args.url:
        origin = args.url.rstrip('/')
    else:
        with socket.socket() as probe:
            probe.bind(('127.0.0.1', 0)); port = probe.getsockname()[1]
        origin = f'http://127.0.0.1:{port}'
        log = (out/'server.log').open('w', encoding='utf-8')
        process = subprocess.Popen([sys.executable, '-m', 'presentation.server', '--port', str(port),
            '--data-dir', str(out/'runtime')], cwd=REPO, stdout=log, stderr=log,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0)

    def fetch(path):
        with urlopen(origin+path, timeout=10) as response:
            assert response.status == 200
            return json.load(response)

    try:
        deadline = time.monotonic()+15
        while True:
            try:
                before = fetch('/api/presence/config'); break
            except Exception:
                if time.monotonic()>deadline: raise
                time.sleep(.1)
        with sync_playwright() as p:
            exe = Path(p.chromium.executable_path)
            if not exe.is_file():
                exe = max((Path(os.environ['LOCALAPPDATA'])/'ms-playwright').glob('chromium-*/chrome-win*/chrome.exe'),
                          key=lambda item:int(item.parents[1].name.split('-')[-1]))
            browser = p.chromium.launch(executable_path=str(exe), headless=True,
                args=['--use-angle=swiftshader', '--enable-unsafe-swiftshader'])
            context = browser.new_context(viewport={'width':1366,'height':768}, device_scale_factor=1)
            page = context.new_page(); errors = []; writes = []
            def observe(tab):
                tab.on('pageerror', lambda error:errors.append(str(error)))
                tab.on('console', lambda m:errors.append(m.text) if m.type=='error' and any(x in m.text for x in ('GLTFLoader','WebGL','Content Security Policy')) else None)
                tab.on('request', lambda request:writes.append(request.url) if request.method not in ('GET','HEAD','OPTIONS') else None)
            observe(page)
            def expose(route):
                response = route.fetch()
                route.fulfill(response=response, body=response.body()+b'\nwindow.testAvatar=avatar;')
            page.route('**/app.js', expose)
            page.goto(origin+'/presence/', wait_until='domcontentloaded')
            expect(page.locator('#model-type')).to_have_text('MOSS 3D', timeout=30000)
            page.wait_for_function('()=>testAvatar.idle.current?.elapsed > .5', timeout=15000)
            snapshot = """()=>{const a=testAvatar;a.scene.updateMatrixWorld(true);return {
                fixed:a.rig.fixed.matrixWorld.toArray(),root:a.pivot.matrixWorld.toArray(),body:a.rig.body.matrixWorld.toArray(),
                yaw:a.rig.yaw,pitch:a.rig.pitch,arm:a.rig.arm,clip:a.idle.current?.clip.id,elapsed:a.idle.current?.elapsed}}"""
            initial = page.evaluate(snapshot)
            page.screenshot(path=str(out/'main-idle.png'))
            box = page.locator('#avatar-host').bounding_box()
            page.mouse.move(box['x']+box['width']*.9, box['y']+box['height']*.55)
            page.wait_for_function('()=>!testAvatar.idle.current && testAvatar.rig.yaw > .2')
            pointer = page.evaluate(snapshot)
            assert pointer['fixed']==initial['fixed'] and pointer['root']==initial['root']
            page.wait_for_function('()=>testAvatar.idle.current?.elapsed > .2', timeout=8000)
            page.evaluate("testAvatar.setState('working')")
            page.wait_for_function('()=>!testAvatar.idle.current && testAvatar.rig.pitch > .03')
            page.evaluate("testAvatar.setState('idle')")
            page.wait_for_function('()=>testAvatar.idle.current?.elapsed > .1')
            page.evaluate("document.getElementById('voice-audio').dispatchEvent(new Event('playing'))")
            page.wait_for_function('()=>!testAvatar.idle.current && testAvatar.speaking')
            page.evaluate("document.getElementById('voice-audio').dispatchEvent(new Event('ended'))")
            page.wait_for_function('()=>testAvatar.idle.current?.elapsed > .1', timeout=5000)
            page.evaluate("async()=>{const profile=await (await fetch('/api/presence/config')).json();profile.layout.animate=false;await testAvatar.setProfile(profile);}")
            page.wait_for_function('()=>!testAvatar.idle.current && testAvatar.rig.yaw===0 && testAvatar.rig.pitch===0 && testAvatar.rig.arm===0')
            page.evaluate("async()=>{const profile=await (await fetch('/api/presence/config')).json();profile.layout.animate=true;await testAvatar.setProfile(profile);}")
            page.emulate_media(reduced_motion='reduce')
            page.wait_for_function('()=>!testAvatar.idle.current && testAvatar.rig.yaw===0')
            page.emulate_media(reduced_motion='no-preference')
            page.wait_for_function('()=>testAvatar.idle.current?.elapsed > .1')
            # Exercise the exact visibility handler without depending on headless
            # Chromium treating background tabs as hidden on every host version.
            page.evaluate("()=>{window.testHidden=true;Object.defineProperty(document,'hidden',{configurable:true,get:()=>testHidden});document.dispatchEvent(new Event('visibilitychange'));}")
            page.wait_for_timeout(300)
            assert page.evaluate('testAvatar.idle.current') is None
            page.evaluate("()=>{window.testHidden=false;document.dispatchEvent(new Event('visibilitychange'));}")
            page.wait_for_function('()=>testAvatar.idle.current?.elapsed > .1')
            final = page.evaluate(snapshot)
            assert final['fixed']==initial['fixed'] and final['root']==initial['root']
            print('PASS: automatic idle, pointer priority, task pause, animation preference and visibility lifecycle', flush=True)

            inspector = context.new_page(); observe(inspector)
            inspector.bring_to_front()
            def expose_inspector(route):
                response = route.fetch()
                route.fulfill(response=response, body=response.body()+b'\nwindow.testInspector={get rig(){return rig},idle,render,stopIdle};')
            inspector.route('**/model-inspector.js', expose_inspector)
            inspector.goto(origin+'/presence/assets/model-inspector.html', wait_until='domcontentloaded')
            expect(inspector.locator('#model-label')).to_have_text('550W / JOM', timeout=30000)
            expect(inspector.locator('#idle-clip option')).to_have_count(10)
            inspector.locator('#idle-clip').select_option('sneeze')
            inspector.locator('#idle-preview').click()
            inspector.wait_for_function('()=>testInspector.rig.pitch > .14', timeout=12000)
            inspector.screenshot(path=str(out/'sneeze-preview.png'), full_page=True)
            expect(inspector.locator('#idle-status')).to_have_text('这套动作播放完毕。', timeout=12000)
            inspector.locator('#idle-random').click()
            inspector.wait_for_function('()=>testInspector.idle.current?.elapsed > .3')
            inspector.locator('#joint-yaw').fill('20'); inspector.locator('#joint-yaw').dispatch_event('input')
            inspector.wait_for_function('()=>!testInspector.idle.current')
            expect(inspector.locator('#joint-yaw-value')).to_have_text('20°')
            inspector.locator('#idle-random').click()
            inspector.emulate_media(reduced_motion='reduce')
            inspector.wait_for_function('()=>!testInspector.idle.current')
            try:
                expect(inspector.locator('#idle-preview')).to_be_disabled()
            except Exception:
                print('Motion preference diagnostics:', inspector.evaluate("()=>({hidden:document.hidden,reduced:matchMedia('(prefers-reduced-motion: reduce)').matches,disabled:document.getElementById('idle-preview').disabled,status:document.getElementById('idle-status').textContent})"),flush=True)
                raise
            inspector.emulate_media(reduced_motion='no-preference')
            inspector.locator('[data-view="front"]').click()
            clips = inspector.evaluate("async()=> (await import('/presence/assets/moss-idle.js')).MOSS_IDLE_CLIPS.map(c=>({id:c.id,label:c.label}))")
            tiles = []
            for clip in clips:
                for position in ([.31,.445,.565] if clip['id']=='sneeze' else [.2,.5,.75]):
                    inspector.evaluate("""async({id,position})=>{const api=await import('/presence/assets/moss-idle.js');
                        testInspector.stopIdle();const clip=api.MOSS_IDLE_CLIPS.find(c=>c.id===id),pose=api.sampleIdle(clip,position);
                        testInspector.rig.lookAt(pose.yaw,pose.pitch,pose.arm);testInspector.render();}""", {'id':clip['id'],'position':position})
                    path = out/f"{clip['id']}-{position}.png"
                    inspector.locator('#inspection-canvas').screenshot(path=str(path))
                    tiles.append((clip,path))
            inspector.set_viewport_size({'width':390,'height':844})
            inspector.locator('#idle-controls').scroll_into_view_if_needed()
            inspector.screenshot(path=str(out/'preview-mobile.png'))
            assert inspector.evaluate('document.documentElement.scrollWidth<=innerWidth+1')
            assert not errors and not writes, (errors,writes)
            browser.close()

        font_path = Path(os.environ.get('WINDIR','C:/Windows'))/'Fonts/msyh.ttc'
        font = ImageFont.truetype(str(font_path),18) if font_path.exists() else ImageFont.load_default()
        sheet = Image.new('RGB',(1040,2400),'#101719'); draw = ImageDraw.Draw(sheet)
        for i,(clip,path) in enumerate(tiles):
            row,col = divmod(i,3)
            if col==0: draw.text((16,row*240+15),f"{row+1:02d}  {clip['label']}",fill='#e0e6e7',font=font)
            with Image.open(path) as source:
                source.thumbnail((280,225)); sheet.paste(source,(190+col*280,row*240+8))
        sheet.save(out/'idle-contact-sheet.jpg',quality=90)
        assert fetch('/api/presence/config')==before
        receipt={'passed':True,'url':origin,'clips':len(clips),'pointer_priority':True,'task_pause':True,'speech_pause':True,
                 'motion_preferences':True,'visibility_handler':True,'fixed_mount_and_root':True,'single_preview':True,
                 'manual_override':True,'mobile_preview':True,'profile_unchanged':True,'mutating_requests':writes,'errors':errors}
        (out/'idle-verification.json').write_text(json.dumps(receipt,ensure_ascii=False,indent=2),encoding='utf-8')
        print(json.dumps(receipt,ensure_ascii=False),flush=True)
    finally:
        if process:
            process.terminate()
            try: process.wait(timeout=5)
            except subprocess.TimeoutExpired: process.kill(); process.wait()
        if log: log.close()


if __name__=='__main__': main()
