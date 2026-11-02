"""Isolated browser speech UI checks; uses a generated WAV and mock provider.

No real reply or camera is sent to a provider. Real synthesis is checked separately.
"""
from __future__ import annotations
import argparse
from io import BytesIO
import json,math,os
from pathlib import Path
import socket,struct,subprocess,sys,time,wave
from urllib.request import Request,urlopen
from playwright.sync_api import sync_playwright,expect

REPO=Path(__file__).resolve().parents[2]


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--out',default='.local-deploy/presence-tts-qa');args=parser.parse_args()
    out=Path(args.out);out.mkdir(parents=True,exist_ok=True)
    with socket.socket() as s:s.bind(('127.0.0.1',0));port=s.getsockname()[1]
    origin=f'http://127.0.0.1:{port}'
    process=subprocess.Popen([sys.executable,'-m','presentation.server','--port',str(port),'--data-dir',str(out/'data')],cwd=REPO,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
    try:
        for _ in range(50):
            try:urlopen(origin+'/api/presence/config',timeout=1);break
            except Exception:time.sleep(.1)
        with urlopen(origin+'/api/presence/config') as r:profile=json.load(r)
        profile['speech']['enabled']=False;profile['avatar']['kind']='procedural-moss'
        with urlopen(Request(origin+'/api/presence/config',data=json.dumps(profile).encode(),method='PUT',headers={'Content-Type':'application/json'})):pass
        voices=json.loads((REPO/'presentation/voices.json').read_text(encoding='utf-8'))['voices']
        generated=BytesIO()
        with wave.open(generated,'wb') as wav:
            wav.setnchannels(1);wav.setsampwidth(2);wav.setframerate(16000)
            wav.writeframes(b''.join(struct.pack('<h',int(800*math.sin(i*2*math.pi*220/16000))) for i in range(6400)))
        with sync_playwright() as p:
            executable=Path(p.chromium.executable_path)
            if not executable.is_file():executable=max((Path(os.environ['LOCALAPPDATA'])/'ms-playwright').glob('chromium-*/chrome-win*/chrome.exe'),key=lambda x:int(x.parents[1].name.split('-')[-1]))
            browser=p.chromium.launch(executable_path=str(executable),headless=True,args=['--use-angle=swiftshader','--enable-unsafe-swiftshader'])
            page=browser.new_page(viewport={'width':1366,'height':900});calls=[];errors=[]
            page.on('pageerror',lambda error:errors.append(str(error)))
            page.on('console',lambda message:errors.append(message.text) if message.type=='error' and 'Content Security Policy' in message.text else None)
            page.route('**/api/presence/voices',lambda route:route.fulfill(json={'available':True,'total':len(voices),'voices':voices,'source':'provider','note':'generated test catalogue'}))
            def audio_route(route):
                calls.append({'path':route.request.url,'body':route.request.post_data_json})
                route.fulfill(body=generated.getvalue(),content_type='audio/wav',headers={'X-Speech-Parts':'1'})
            page.route('**/api/presence/speech',audio_route);page.route('**/api/presence/speech/preview',audio_route)
            page.goto(origin+'/presence/',wait_until='domcontentloaded')
            expect(page.locator('#voice-toggle')).to_have_text('语音关闭')
            def emit(identifier,text):
                with urlopen(Request(origin+'/api/presence/events',data=json.dumps({'event_id':identifier,'kind':'message','phase':'completed','title':'公开回复','text':text}).encode(),headers={'Content-Type':'application/json','X-Presence-Producer':'1'})):pass
            emit('off-text','关闭语音时，文字仍然显示。')
            expect(page.locator('#robot-response')).to_have_text('关闭语音时，文字仍然显示。')
            assert not calls
            page.locator('#voices-open').click()
            expect(page.locator('.voice-row')).to_have_count(332)
            page.locator('#voice-recommended').click();expect(page.locator('.voice-row')).to_have_count(5)
            page.locator('[data-voice="Robot_Armor"]').click()
            page.locator('#voice-preview').click();expect(page.locator('#voice-preview-status')).to_have_text('试听结束，可选择下一声线',timeout=10000)
            assert len(calls)==1 and calls[0]['body']['voice_id']=='Robot_Armor'
            with urlopen(origin+'/api/presence/config') as r:assert json.load(r)['speech']['enabled'] is False
            page.locator('#voice-use').click();expect(page.locator('#voice-preview-status')).to_contain_text('已选择')
            page.screenshot(path=str(out/'01-voice-candidates.png'))
            page.locator('#voice-recommended').click();page.locator('#voice-search').fill('Japanese')
            assert 0<page.locator('.voice-row').count()<332
            page.locator('#voice-search').fill('');page.screenshot(path=str(out/'02-all-voices.png'))
            page.set_viewport_size({'width':390,'height':844});page.screenshot(path=str(out/'03-voices-mobile.png'))
            assert page.evaluate('()=>document.documentElement.scrollWidth<=innerWidth')
            page.set_viewport_size({'width':1366,'height':900});page.locator('#voices-close').click()
            page.locator('#voice-toggle').click();expect(page.locator('#voice-toggle')).to_have_text('语音开启')
            assert len(calls)==1  # No replay of the earlier reply.
            emit('on-text','开启后，只播报新的公开回复。')
            expect(page.locator('#robot-response')).to_have_text('开启后，只播报新的公开回复。')
            page.wait_for_timeout(1000);assert len(calls)==2
            page.wait_for_timeout(1600);assert len(calls)==2
            page.locator('#voice-toggle').click();expect(page.locator('#voice-toggle')).to_have_text('语音关闭')
            emit('off-again','语音再次关闭，文字保持输出。')
            expect(page.locator('#robot-response')).to_have_text('语音再次关闭，文字保持输出。')
            assert len(calls)==2
            page.reload(wait_until='domcontentloaded');expect(page.locator('#voice-toggle')).to_have_text('语音关闭')
            assert len(calls)==2
            page.screenshot(path=str(out/'04-speech-off-text.png'))
            page.goto(origin+'/presence/?voices=1',wait_until='domcontentloaded')
            expect(page.locator('#voices-dialog')).to_be_visible()
            expect(page.locator('.voice-row')).to_have_count(332)
            assert len(calls)==2
            assert not errors,errors
            browser.close()
        (out/'verification.json').write_text(json.dumps({'passed':True,'voices':332,'speech_requests':len(calls),'page_errors':errors,'checks':['default-off-no-request','manual-preview-keeps-off','all-voices','candidate-filter','search','mobile','choose-persists','new-reply-only','no-reconnect-repeat','switch-off-text-continues']},indent=2),encoding='utf-8')
        print('Speech UI verification PASS: 332 voices, audition, off-by-default, new replies only, text preserved')
    finally:
        process.terminate()
        try:process.wait(timeout=5)
        except subprocess.TimeoutExpired:process.kill();process.wait()


if __name__=='__main__':main()
