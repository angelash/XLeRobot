"""Exercise our localhost UI with generated media; no robot or external pages.

Run from the repository: python presentation/tests/verify_browser.py
Requires the development-only Playwright Python package and a local Chromium.
"""
from __future__ import annotations

import argparse
from io import BytesIO
import json
import os
import re
from pathlib import Path
import socket
import struct
import subprocess
import sys
import time
from urllib.request import urlopen, Request

from PIL import Image, ImageDraw
from playwright.sync_api import sync_playwright, expect


REPO = Path(__file__).resolve().parents[2]


def triangle_glb():
    binary = struct.pack('<9f', -1,-1,0, 1,-1,0, 0,1,0)
    document = {'asset':{'version':'2.0'},'scene':0,'scenes':[{'nodes':[0]}],
                'nodes':[{'mesh':0}], 'meshes':[{'primitives':[{'attributes':{'POSITION':0},'material':0}]}],
                'materials':[{'doubleSided':True,'pbrMetallicRoughness':{'baseColorFactor':[.3,.7,.75,1],'metallicFactor':.2,'roughnessFactor':.6}}],
                'buffers':[{'byteLength':len(binary)}],'bufferViews':[{'buffer':0,'byteOffset':0,'byteLength':len(binary),'target':34962}],
                'accessors':[{'bufferView':0,'componentType':5126,'count':3,'type':'VEC3','min':[-1,-1,0],'max':[1,1,0]}]}
    text = json.dumps(document).encode(); text += b' '*(-len(text)%4)
    length = 12+8+len(text)+8+len(binary)
    return struct.pack('<4sII',b'glTF',2,length)+struct.pack('<I4s',len(text),b'JSON')+text+struct.pack('<I4s',len(binary),b'BIN\x00')+binary


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--out', default=str(REPO / '.local-deploy/presence-qa'))
    args = parser.parse_args()
    output = Path(args.out); output.mkdir(parents=True,exist_ok=True)
    with socket.socket() as probe:
        probe.bind(('127.0.0.1',0)); port = probe.getsockname()[1]
    url = f'http://127.0.0.1:{port}'
    log = (output/'test-server.log').open('w',encoding='utf-8')
    process = subprocess.Popen([sys.executable,'-m','presentation.server','--port',str(port),
                                '--data-dir',str(output/'runtime')],cwd=REPO,stdout=log,stderr=log,
                               creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0)
    try:
        deadline = time.monotonic()+12
        while True:
            try:
                with urlopen(url+'/api/presence/state',timeout=1) as response:
                    assert json.load(response)['version'] == 1
                break
            except Exception:
                if time.monotonic()>deadline:raise
                time.sleep(.15)
        # A prior verification's profile must not affect this run's assertions.
        with urlopen(url+'/api/presence/config') as response:
            profile=json.load(response)
        profile.update(name='MOSS',subtitle='550W · 机器人智能终端')
        profile['avatar']={'kind':'procedural-moss','asset_id':None,'credit':''}
        profile['layout']={'log_open':True,'split':.58,'animate':True}
        with urlopen(Request(url+'/api/presence/config', data=json.dumps(profile).encode(),method='PUT',headers={'Content-Type':'application/json'})) as response:
            assert response.status==200
        image = Image.new('RGB',(640,360),'#293a45'); draw = ImageDraw.Draw(image)
        draw.rectangle((100,90,280,260),fill='#dbc794'); draw.ellipse((410,130,510,230),fill='#709b9a')
        photo=output/'generated-input.png'; image.save(photo)
        model=output/'generated-triangle.glb'; model.write_bytes(triangle_glb())
        with sync_playwright() as playwright:
            executable=Path(playwright.chromium.executable_path)
            if not executable.is_file():
                available=list((Path(os.environ['LOCALAPPDATA'])/'ms-playwright').glob('chromium-*/chrome-win*/chrome.exe'))
                if not available:raise RuntimeError('No installed Chromium for local UI tests')
                executable=max(available,key=lambda p:int(p.parents[1].name.split('-')[-1]))
            browser=playwright.chromium.launch(executable_path=str(executable),headless=True,
                args=['--use-angle=swiftshader','--enable-unsafe-swiftshader'])
            page=browser.new_page(viewport={'width':1366,'height':768},device_scale_factor=1)
            page.emulate_media(reduced_motion='reduce')
            errors=[]; page.on('pageerror',lambda error:errors.append(str(error)))
            external=[]; page.on('request',lambda request:external.append(request.url) if request.url.startswith(('http:','https:')) and not request.url.startswith(url) else None)
            page.goto(url+'/presence/',wait_until='domcontentloaded')
            expect(page.locator('#avatar-host canvas')).to_be_visible()
            expect(page.locator('#avatar-fallback')).to_be_hidden()
            page.wait_for_timeout(1100)
            assert page.locator('#model-type').inner_text()=='DETAILED 3D'
            print('Default 3D renderer loaded',flush=True)
            page.screenshot(path=str(output/'01-default-1366.png'))
            page.locator('#process-toggle').click()
            assert page.locator('#process-panel').is_hidden()
            bounds=page.locator('#stage').bounding_box()
            assert abs(bounds['x']+bounds['width']/2-683)<2
            page.screenshot(path=str(output/'02-centered-1366.png'))
            pixels = Image.open(BytesIO(page.locator('#avatar-host canvas').screenshot())).convert('RGB')
            assert sum(1 for r,g,b in pixels.getdata() if min(r,g,b)>150)>100
            page.emulate_media(reduced_motion='no-preference')
            page.keyboard.press('i'); assert page.locator('#process-panel').is_visible()
            page.locator('#settings-open').click()
            page.locator('#setting-name').fill('NOVA')
            page.locator('#setting-persona').fill('简洁、温和，区分事实与推断。')
            page.locator('#save-settings').click(); expect(page.locator('#settings')).to_be_hidden()
            page.reload(wait_until='domcontentloaded')
            expect(page.locator('#robot-name')).to_have_text('NOVA')
            assert page.locator('#robot-name').inner_text()=='NOVA'
            page.locator('#demo-toggle').click()
            expect(page.locator('.overlay-box')).to_have_count(2,timeout=14000)
            assert '演示' in page.locator('#source-badge').inner_text()
            print('Settings persistence and synthetic live/analysis frame verified',flush=True)
            page.locator('[data-filter="vision"]').click()
            assert page.locator('.event:not([data-kind="vision"])').count()==0
            page.locator('#overlay-toggle').click(); assert page.locator('.overlay-box').count()==0
            page.locator('#overlay-toggle').click(); assert page.locator('.overlay-box').count()==2
            page.locator('#frame-zoom-open').click(); expect(page.locator('#frame-zoom')).to_be_visible()
            page.locator('#frame-zoom-close').click(); expect(page.locator('#frame-zoom')).to_be_hidden()
            page.set_viewport_size({'width':1920,'height':1080}); page.wait_for_timeout(300)
            page.screenshot(path=str(output/'03-demo-1920.png'))
            page.set_viewport_size({'width':2560,'height':1600}); page.wait_for_timeout(300)
            assert page.evaluate('() => document.documentElement.scrollWidth <= innerWidth && document.documentElement.scrollHeight <= innerHeight')
            page.screenshot(path=str(output/'03b-demo-2560.png'))
            page.set_viewport_size({'width':390,'height':844}); page.wait_for_timeout(300)
            assert page.evaluate('() => document.documentElement.scrollWidth <= innerWidth')
            page.screenshot(path=str(output/'04-mobile-390.png'),full_page=True)
            page.set_viewport_size({'width':1366,'height':768})
            page.locator('#demo-toggle').click()
            page.locator('#vision-toggle').click() if page.locator('#vision-workspace').is_hidden() else None
            page.locator('#image-file').set_input_files(str(photo))
            expect(page.locator('#frame-caption')).to_contain_text('Laplacian',timeout=12000)
            assert page.locator('.overlay-box').count()==0
            page.locator('[data-filter="all"]').click()
            expect(page.locator('.event').first).to_be_visible()
            page.screenshot(path=str(output/'05-real-image-analysis-1366.png'))
            print('Responsive layout and real pixel analysis verified',flush=True)
            page.locator('#settings-open').click(); page.locator('#avatar-file').set_input_files(str(model))
            expect(page.locator('#setting-avatar')).to_have_value('glb')
            page.locator('#save-settings').click()
            expect(page.locator('#model-type')).to_have_text('CUSTOM GLB')
            page.locator('#settings-open').click(); page.locator('#avatar-file').set_input_files(str(photo))
            expect(page.locator('#setting-avatar')).to_have_value('image')
            page.locator('#save-settings').click()
            expect(page.locator('#model-type')).to_have_text('IMAGE')
            print('Custom GLB and image avatar imports verified',flush=True)
            page.locator('#settings-open').click(); page.locator('#setting-avatar').select_option('procedural-moss')
            page.locator('#setting-name').fill('MOSS'); page.locator('#save-settings').click()
            expect(page.locator('#model-type')).to_have_text('DETAILED 3D')
            # Exercise real SSE delivery, including text that must never become HTML.
            event={'event_id':'ui-xss-test','kind':'tool','phase':'completed','title':'公开工具输出','text':'<img src=x onerror=alert(1)>'}
            with urlopen(Request(url+'/api/presence/events',data=json.dumps(event).encode(),headers={'Content-Type':'application/json','X-Presence-Producer':'1'})) as response:
                assert response.status==200
            expect(page.locator('#event-list')).to_contain_text('onerror=alert')
            assert page.locator('#event-list img').count()==0
            # Verify the actual reconnect path instead of only the journal reducer.
            page.context.set_offline(True); page.wait_for_timeout(1800); page.context.set_offline(False)
            expect(page.locator('#stream-label')).to_have_text('LIVE',timeout=12000)

            inspector=browser.new_page(viewport={'width':1600,'height':1100},device_scale_factor=1)
            inspector.on('pageerror',lambda error:errors.append(str(error)))
            inspector.on('request',lambda request:external.append(request.url) if request.url.startswith(('http:','https:')) and not request.url.startswith(url) else None)
            inspector.goto(url+'/presence/assets/model-inspector.html',wait_until='domcontentloaded')
            expect(inspector.locator('#inspection-canvas canvas')).to_be_visible()
            expect(inspector.locator('#model-label')).to_have_text('550W / DETAILED')
            expect(inspector.locator('#triangle-count')).to_have_text(re.compile(r'[\d,]+'))
            triangles=int(inspector.locator('#triangle-count').text_content().replace(',',''))
            meshes=int(inspector.locator('#mesh-count').text_content())
            assert 0<triangles<200000 and 0<meshes<=40,(triangles,meshes)
            for direction in ['front','quarter','side','rear']:
                inspector.locator('[data-view="'+direction+'"]').click()
                inspector.wait_for_timeout(180)
                inspector.locator('#inspection-canvas').screenshot(path=str(output/f'moss-{direction}.png'))
            inspector.locator('#yaw').focus();inspector.keyboard.press('End')
            expect(inspector.locator('#yaw-value')).to_have_text('180°')
            inspector.locator('[data-view="quarter"]').click()
            with inspector.expect_download(timeout=20000) as download_info:
                inspector.locator('#export-model').click()
            exported=output/'moss-550w-detailed-v2.glb';download_info.value.save_as(str(exported))
            sys.path.insert(0,str(REPO))
            from presentation.state import validate_glb
            document=validate_glb(exported.read_bytes())
            assert len(document.get('meshes',[]))>=20 and exported.stat().st_size<20*1024**2
            page.locator('#settings-open').click();page.locator('#avatar-file').set_input_files(str(exported))
            expect(page.locator('#setting-avatar')).to_have_value('glb')
            page.locator('#save-settings').click();expect(page.locator('#model-type')).to_have_text('CUSTOM GLB')
            page.locator('#settings-open').click();page.locator('#setting-avatar').select_option('procedural-moss')
            page.locator('#setting-detail').select_option('simple');page.locator('#save-settings').click()
            expect(page.locator('#model-type')).to_have_text('PROCEDURAL 3D')
            page.locator('#settings-open').click();page.locator('#setting-detail').select_option('detailed')
            page.locator('#save-settings').click();expect(page.locator('#model-type')).to_have_text('DETAILED 3D')
            inspector.locator('#inspector-variant').select_option('simple')
            expect(inspector.locator('#model-label')).to_have_text('550W / SIMPLE')
            inspector.locator('#inspection-canvas').screenshot(path=str(output/'moss-simple-inspector.png'))
            inspector.locator('#inspector-variant').select_option('detailed')
            expect(inspector.locator('#model-label')).to_have_text('550W / DETAILED')
            inspector.screenshot(path=str(output/'moss-inspector.png'))
            print(f'Detailed MOSS views, GLB roundtrip, legacy selection PASS: {triangles} triangles / {meshes} meshes',flush=True)
            assert not errors,errors
            assert not external,external
            browser.close()
        (output/'verification.json').write_text(json.dumps({'passed':True,'viewport_sizes':[[1366,768],[1920,1080],[2560,1600],[390,844]],
            'checks':['WebGL3D','centered-collapse','keyboard-toggle','profile-persistence','demo-frame-overlays','filter',
                      'responsive-no-horizontal-overflow','frame-zoom','real-generated-image-analysis','GLB-import','image-avatar',
                      'SSE-public-text-no-HTML','network-reconnect','offline-local-assets','reduced-motion-resize-renders',
                      'four-detailed-model-views','detailed-GLB-export-import','legacy-detail-selector'],
            'model':{'triangles':triangles,'meshes':meshes,'glb_bytes':exported.stat().st_size},
            'page_errors':errors,'external_requests':external},ensure_ascii=False,indent=2),encoding='utf-8')
        print('Presence browser verification PASS; evidence: '+str(output))
    finally:
        process.terminate()
        try:process.wait(timeout=5)
        except subprocess.TimeoutExpired:process.kill(); process.wait(timeout=5)
        log.close()


if __name__=='__main__':
    main()
