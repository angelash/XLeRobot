"""Local Chromium acceptance with generated photos/receipts; never calls a robot."""
from contextlib import asynccontextmanager
import hashlib
import json
import os
from pathlib import Path
import socket
import sys
import threading
import time

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from playwright.sync_api import sync_playwright, expect
import uvicorn

from deployment.fieldmap.integration import install, API
from deployment.fieldmap.store import atomic_json
from deployment.fieldmap.test_history import fixture, START, jpeg


def main():
    out = REPO/'.local-deploy/history-3d-20261106/browser'
    out.mkdir(parents=True, exist_ok=True)
    # Fresh generated state per run, kept in the ignored QA directory.
    import tempfile
    with tempfile.TemporaryDirectory(prefix='fixture-', dir=out) as folder:
        history, _, _ = fixture(Path(folder))
        run = Path(history.evidence_root)/'20261105/agent-runs/20261105-235958'
        receipt = json.loads((run/'result.json').read_text())
        receipt['results'].append({'op': 'snapshot', 'at': (START+5900)/1000,
                                  'capture_started_at': (START+5900)/1000,
                                  'shots': [{'camera': c, 'file': f'later-cam{c}.jpg'} for c in (0, 1, 2)]})
        atomic_json(run/'result.json', receipt)
        for c in (0, 1, 2):
            (run/f'later-cam{c}.jpg').write_bytes(jpeg())
        root = history.root.parent
        (root/'images').mkdir()
        (root/'images/front.jpg').write_bytes(jpeg())
        sample = {'id': 'generated-map', 'title': 'Generated environment',
                  'summary': '桌旁通路、线缆与遮挡区域仍需分别查看',
                  'map_limit': '生成场景未做尺度与历史轮估坐标配准',
                  'features': [{'id': 'desk', 'name': '示例桌子', 'kind': 'blocked', 'x': 80, 'y': -90,
                                'w': 110, 'h': 65, 'height': 75, 'description': '生成的测试物品',
                                'evidence': [{'observation': 'front', 'polygon': [[.1,.1],[.6,.1],[.6,.6]]}]},
                               {'id': 'floor', 'name': '候选地面', 'kind': 'candidate', 'shape': 'area',
                                'x': -100, 'y': -80, 'w': 60, 'h': 180, 'description': '可见地面尚未测宽', 'evidence': [{'observation': 'front'}]},
                               {'id': 'blind', 'name': '轮边盲区', 'kind': 'unknown', 'shape': 'area',
                                'x': 100, 'y': 70, 'w': 50, 'h': 40, 'description': '没有低处净空证据', 'evidence': []}],
                  'observations': [{'id': 'front', 'camera': 0, 'image': 'front.jpg', 'label': '测试视角',
                                    'captured_at': '2026-11-06T00:00:01+08:00', 'width': 640, 'height': 480}]}
        atomic_json(root/'map.json', sample)
        atomic_json(root/'history/old.json', {**sample, 'id': 'old-map'})
        atomic_json(root/'reviews.json', {'old-map': {'revision': 1, 'items': {
            'desk': {'kind': 'blocked', 'confirmed': True, 'note': '旧图复核：桌脚外伸', 'at': '2026-11-06T00:00:01+08:00'}}, 'history': []}})
        preserved = {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in (root/'map.json', root/'reviews.json', root/'history/old.json')}
        @asynccontextmanager
        async def lifespan(app):
            yield
            app.state.fieldmap_history.stop()
        app = FastAPI(lifespan=lifespan)
        survey = install(app, data_dir=root, history_evidence_root=history.evidence_root,
                         history_fieldtests_root=history.fieldtests_root, history_forward_sign=-1,
                         allowed_origins=lambda: ('http://127.0.0.1:'+str(port),))
        survey.status = lambda: {'online': False, 'motor_count': 0, 'wheels_stopped': None}
        app.state.fieldmap_history.scan()
        real_photos = app.state.fieldmap_history.photos
        def delayed_photos(*args, **kwargs):
            # An old cursor request finishes after a newer one. HTTP worker
            # threads let the second request return without waiting for it.
            if kwargs.get('at') and kwargs['at'] < START+5700:
                time.sleep(.2)
            return real_photos(*args, **kwargs)
        app.state.fieldmap_history.photos = delayed_photos
        app.mount('/presence/assets', StaticFiles(directory=REPO/'presentation/static'))
        sock = socket.socket(); sock.bind(('127.0.0.1', 0)); port = sock.getsockname()[1]
        server = uvicorn.Server(uvicorn.Config(app, log_level='error'))
        thread = threading.Thread(target=lambda: server.run(sockets=[sock]), daemon=True); thread.start()
        while not server.started:
            time.sleep(.01)
        url = f'http://127.0.0.1:{port}'
        report = {'fixture': True, 'motion_commands': [], 'errors': []}
        try:
            with sync_playwright() as p:
                executable = Path(p.chromium.executable_path)
                if not executable.is_file():
                    executable = max((Path(os.environ['LOCALAPPDATA'])/'ms-playwright').glob('chromium-*/chrome-win*/chrome.exe'), key=lambda x: int(x.parents[1].name.split('-')[-1]))
                browser = p.chromium.launch(executable_path=str(executable), headless=True,
                                          args=['--use-angle=swiftshader', '--enable-unsafe-swiftshader'])
                page = browser.new_page(viewport={'width': 1440, 'height': 1080})
                page.emulate_media(reduced_motion='reduce')
                page.on('pageerror', lambda e: report['errors'].append(str(e)))
                page.on('request', lambda r: report['motion_commands'].append(r.url) if r.method != 'GET' and not any(x in r.url for x in ('/body/plan', '/review', '/history/refresh')) else None)
                page.goto(url+'/fieldmap/view', wait_until='networkidle')
                expect(page.locator('#historyStats')).to_contain_text('2 平移 / 1 转向')
                expect(page.locator('#bodyResult')).to_contain_text('已计算')
                expect(page.locator('#observationStrip .album-photo')).to_have_count(2)
                expect(page.locator('#environmentReview')).to_be_visible()
                expect(page.locator('#contextVisuals')).to_be_visible()
                expect(page.locator('#featureList button')).to_have_count(3)
                expect(page.locator('#contextMap [data-feature]')).to_have_count(3)
                expect(page.locator('#contextSummary')).to_contain_text('桌旁通路')
                expect(page.locator('#contextTimeRelation')).to_contain_text('不代表游标当时障碍状态')
                expect(page.locator('#candidateCount')).to_have_text('1')
                expect(page.locator('#blockedCount')).to_have_text('1')
                expect(page.locator('#unknownCount')).to_have_text('1')
                expect(page.locator('#historyObstacleToggle')).to_have_attribute('aria-pressed', 'true')
                expect(page.locator('#map [data-history-feature]')).to_have_count(3)
                expect(page.locator('#historyObstacleStatus')).to_contain_text('独立示意')
                # Main history viewport has clickable obstacles; toggling them
                # leaves the cursor, source context and odometry data untouched.
                main_cursor=page.locator('#timeline').get_attribute('aria-valuenow')
                page.locator('#map [data-history-feature="desk"]').click()
                expect(page.locator('#detailTitle')).to_have_text('示例桌子')
                assert page.locator('#timeline').get_attribute('aria-valuenow')==main_cursor
                page.locator('#historyObstacleToggle').click()
                expect(page.locator('#map [data-history-feature]')).to_have_count(0)
                expect(page.locator('#map .track-path')).to_have_count(1)
                expect(page.locator('#contextMap [data-feature]')).to_have_count(3)
                page.locator('#historyObstacleToggle').click()
                expect(page.locator('#map [data-history-feature]')).to_have_count(3)
                # Pointer scrub, visible cursor and exact photo offsets.
                page.locator('#timeline').scroll_into_view_if_needed(); box = page.locator('#timeline').bounding_box()
                page.mouse.move(box['x']+box['width']*.25, box['y']+42); page.mouse.down()
                page.mouse.move(box['x']+box['width']*.78, box['y']+42, steps=12); page.mouse.up()
                assert float(page.locator('#timeline').get_attribute('aria-valuenow')) > START+4500
                page.wait_for_timeout(220)
                page.mouse.click(box['x']+box['width']*.99, box['y']+42)
                expect(page.locator('#photoName')).to_contain_text('later-cam0.jpg')
                page.wait_for_timeout(700)
                expect(page.locator('#photoName')).to_contain_text('later-cam0.jpg')
                page.locator('[data-camera="2"]').click()
                expect(page.locator('#photoName')).to_contain_text('左腕')
                page.locator('.album-photo').first.click()
                expect(page.locator('#photoBearing')).to_have_text('距游标 +0.00 s')
                photo_id = page.locator('#historyPhoto').get_attribute('data-photo-id')
                cursor = page.locator('#timeline').get_attribute('aria-valuenow')
                page.locator('#featureList button[data-id="desk"]').click()
                expect(page.locator('#contextPhoto .photo-overlay')).to_have_count(1)
                expect(page.locator('#detailDescription')).to_contain_text('生成的测试物品')
                assert page.locator('#historyPhoto').get_attribute('data-photo-id') == photo_id
                assert page.locator('#timeline').get_attribute('aria-valuenow') == cursor
                page.locator('[data-filter="blocked"]').click()
                expect(page.locator('#contextMap [data-feature="desk"]')).to_have_count(0)
                expect(page.locator('#map [data-history-feature="desk"]')).to_have_count(0)
                expect(page.locator('#contextPhoto .photo-overlay')).to_have_count(0)
                page.locator('[data-filter="blocked"]').click()
                expect(page.locator('#contextMap [data-feature="desk"]')).to_have_count(1)
                expect(page.locator('#map [data-history-feature="desk"]')).to_have_count(1)
                page.locator('#overlayToggle').click();expect(page.locator('#contextPhoto .photo-overlay')).to_have_count(0)
                page.locator('#overlayToggle').click();expect(page.locator('#contextPhoto .photo-overlay')).to_have_count(1)
                old=page.locator('#snapshotSelect option').nth(1).get_attribute('value');page.locator('#snapshotSelect').select_option(old)
                expect(page.locator('#reviewAssessment')).to_contain_text('旧图复核：桌脚外伸')
                expect(page.locator('#reviewForm')).to_be_hidden()
                page.locator('#snapshotSelect').select_option('current');expect(page.locator('#reviewForm')).to_be_visible()
                # Wheel zoom and Shift drag change the window; keyboard jumps events.
                page.locator('#timeline').hover(); page.mouse.wheel(0, -500); box=page.locator('#timeline').bounding_box()
                before = page.locator('#windowTimes').inner_text()
                page.keyboard.down('Shift');page.mouse.move(box['x']+box['width']*.55, box['y']+50);page.mouse.down();page.mouse.move(box['x']+box['width']*.75, box['y']+50, steps=8);page.mouse.up();page.keyboard.up('Shift')
                after = page.locator('#windowTimes').inner_text(); assert before != after
                page.locator('#timeFit').click();page.locator('#timeline').focus();page.keyboard.press('Home');page.keyboard.press('ArrowRight')
                page.locator('#playSpeed').select_option('1');page.locator('#playPause').click();page.wait_for_timeout(450);page.locator('#playPause').click()
                assert float(page.locator('#timeline').get_attribute('aria-valuenow')) > START+1000
                page.locator('.history-event.failed').click();expect(page.locator('#eventDetail')).to_contain_text('generated contact stop')
                # A real canvas rendered with perspective/depth meshes, orbit, zoom, pan.
                page.locator('#depthView').click();canvas=page.locator('#scene3d canvas');expect(canvas).to_be_visible(timeout=15000)
                expect(page.locator('#scene3d')).to_have_attribute('data-history-obstacle-count','3')
                canvas.scroll_into_view_if_needed();page.wait_for_timeout(150)
                with_obstacles=hashlib.sha256(canvas.screenshot()).hexdigest()
                position=page.locator('#timeline').get_attribute('aria-valuenow')
                page.locator('#historyObstacleToggle').click()
                expect(page.locator('#scene3d')).to_have_attribute('data-history-obstacle-count','0')
                canvas.scroll_into_view_if_needed();page.wait_for_timeout(150)
                assert hashlib.sha256(canvas.screenshot()).hexdigest()!=with_obstacles
                assert page.locator('#timeline').get_attribute('aria-valuenow')==position
                page.locator('#historyObstacleToggle').click()
                expect(page.locator('#scene3d')).to_have_attribute('data-history-obstacle-count','3')
                expect(page.locator('#contextVisuals')).to_be_visible();expect(page.locator('#environmentReview')).to_be_visible()
                canvas.scroll_into_view_if_needed();page.wait_for_timeout(200)
                hash_before=hashlib.sha256(canvas.screenshot()).hexdigest();b=canvas.bounding_box()
                page.mouse.move(b['x']+b['width']*.45,b['y']+b['height']*.5);page.mouse.down();page.mouse.move(b['x']+b['width']*.7,b['y']+b['height']*.65,steps=15);page.mouse.up();page.wait_for_timeout(150)
                hash_orbit=hashlib.sha256(canvas.screenshot()).hexdigest();assert hash_before!=hash_orbit
                page.mouse.wheel(0,-220);page.wait_for_timeout(150);assert hashlib.sha256(canvas.screenshot()).hexdigest()!=hash_orbit
                page.mouse.down(button='right');page.mouse.move(b['x']+b['width']*.55,b['y']+b['height']*.45,steps=8);page.mouse.up(button='right');page.locator('#resetView').click()
                page.screenshot(path=str(out/'01-history-3d.png'),full_page=True)
                page.locator('#environmentMode').click();expect(page.locator('#environmentReview')).to_be_visible();expect(page.locator('#photo')).to_be_visible()
                page.locator('#featureList button').first.click();expect(page.locator('#reviewForm')).to_be_visible()
                old=page.locator('#snapshotSelect option').nth(1).get_attribute('value');page.locator('#snapshotSelect').select_option(old)
                expect(page.locator('#saveMessage')).to_contain_text('历史快照只读');expect(page.locator('#reviewForm')).to_be_hidden()
                page.locator('#flatView').click();expect(canvas).to_be_hidden()
                page.locator('#historyMode').click();page.locator('#depthView').click();expect(canvas).to_be_visible()
                expect(page.locator('#reviewAssessment')).to_contain_text('旧图复核：桌脚外伸')
                expect(page.locator('#contextPhoto')).to_be_visible()
                # Loss must provide an actionable 2D fallback.
                canvas.evaluate("c=>c.dispatchEvent(new Event('webglcontextlost',{cancelable:true}))")
                expect(page.locator('#map')).to_be_visible();expect(page.locator('#historyMessage')).to_contain_text('WebGL')
                page.set_viewport_size({'width':390,'height':844})
                page.screenshot(path=str(out/'02-mobile.png'),full_page=True)
                assert page.evaluate('document.documentElement.scrollWidth<=innerWidth+1')
                # The preference survives reload and still applies in 2D.
                page.locator('#historyObstacleToggle').click()
                page.reload(wait_until='networkidle')
                expect(page.locator('#historyObstacleToggle')).to_have_attribute('aria-pressed','false')
                expect(page.locator('#map [data-history-feature]')).to_have_count(0)
                expect(page.locator('#contextMap [data-feature]')).to_have_count(3)
                page.locator('#historyObstacleToggle').click()
                expect(page.locator('#map [data-history-feature]')).to_have_count(3)
                # Empty range, absent image, backend query failure leave visible states.
                page.locator('#rangeStart').fill('2027-01-01T00:00');page.locator('#rangeEnd').fill('2027-01-01T01:00');page.locator('#rangeForm button[type=submit]').click()
                expect(page.locator('#historyMessage')).to_contain_text('没有动作记录')
                page.route('**/history/query?*', lambda route: route.fulfill(status=503, content_type='application/json', body='{"detail":"generated unavailable"}'))
                page.locator('#allRange').click();expect(page.locator('#historyMessage')).to_contain_text('generated unavailable')
                report.update(orbit_changed=True, time_pan_changed=True, keyboard_playback=True, snapshot_readonly=True,
                              photo_seek=True, stale_photo_response_ignored=True, webgl_loss_fallback=True, mobile_overflow=False,
                              obstacle_context_during_playback=True, independent_recognition_photo=True, old_review_note_visible=True)
                report.update(main_history_obstacles_2d=True,main_history_obstacles_3d=True,obstacle_visibility_persisted=True)
                browser.close()
            assert not report['errors'], report['errors']
            assert not report['motion_commands'], report['motion_commands']
            assert all(hashlib.sha256(p.read_bytes()).hexdigest() == digest for p, digest in preserved.items())
        finally:
            server.should_exit=True;thread.join(timeout=5)
            (out/'report.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
        print(json.dumps(report))


if __name__ == '__main__':
    main()
