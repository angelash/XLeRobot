"""Edge UI acceptance through real FastAPI handlers, without opening a port.

All data is synthetic. Playwright routes requests into TestClient; controller,
network, real camera and phone endpoints are forbidden in this fixture.
"""
from pathlib import Path
import hashlib
import json
import sys
from tempfile import TemporaryDirectory
from time import perf_counter
from urllib.parse import urlsplit

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

from fastapi import FastAPI
from fastapi.testclient import TestClient
from playwright.sync_api import sync_playwright, expect

from deployment.fieldmap.integration import install
from deployment.fieldmap.metric_api import PREFIX
from deployment.fieldmap.test_metric import vacuum_map


def main():
    out = REPO/'.local-deploy/vacuum-mapping-20261115/browser'
    out.mkdir(parents=True, exist_ok=True)
    errors, requests, forbidden = [], [], []
    with TemporaryDirectory(prefix='fixture-', dir=out) as temporary:
        app = FastAPI()
        survey = install(app, data_dir=Path(temporary), allowed_origins=('http://127.0.0.1:8770',))
        def reject_controller(*args, **kwargs):
            forbidden.append('controller')
            raise AssertionError('controller I/O forbidden')
        survey.controller = reject_controller
        with TestClient(app) as client, sync_playwright() as runtime:
            browser = runtime.chromium.launch(channel='msedge', headless=True)
            context = browser.new_context(viewport={'width': 1360, 'height': 1000})
            context.set_default_timeout(30000)

            def dispatch(route):
                request = route.request
                url = urlsplit(request.url)
                if url.netloc != '127.0.0.1:8770' or not (url.path.startswith(PREFIX) or url.path.startswith('/fieldmap/')):
                    forbidden.append(request.url)
                    route.abort()
                    return
                entry = {'method': request.method, 'path': url.path}
                if url.path.endswith('/plan') and request.post_data:
                    entry['plan_request'] = json.loads(request.post_data)
                requests.append(entry)
                response = client.request(request.method, url.path+('?' + url.query if url.query else ''),
                                          content=request.post_data_buffer,
                                          headers={k: v for k, v in request.headers.items() if k in ('origin', 'content-type')})
                route.fulfill(status=response.status_code, body=response.content,
                              headers={k: v for k, v in response.headers.items() if k not in ('content-length', 'transfer-encoding')})

            context.route('**/*', dispatch)
            page = context.new_page(); page.on('pageerror', lambda error: errors.append(str(error)))
            page.goto('http://127.0.0.1:8770/fieldmap/metric/view')
            expect(page.locator('#metricMessage')).to_contain_text('合成办公室')
            start = perf_counter()
            page.get_by_role('button', name='生成合成环境回放').click()
            expect(page.locator('#metricFrames')).to_have_text('24')
            expect(page.locator('#metricMapEmpty')).to_be_hidden()
            page.get_by_role('button', name='寻找下一观察位').click()
            expect(page.locator('#metricPlanResult')).to_contain_text('可达观察位')
            expect(page.locator('#metricLlm')).to_have_text('0')
            assert page.locator('#metricCanvas').evaluate('(canvas)=>{const p=canvas.getContext("2d").getImageData(0,0,canvas.width,canvas.height).data;const colors=new Set();for(let i=0;i<p.length;i+=160)colors.add([p[i],p[i+1],p[i+2]].join(","));return colors.size>8;}')
            page.screenshot(path=str(out/'desktop.png'), full_page=True)
            assert page.evaluate('document.documentElement.scrollWidth <= innerWidth+1')
            desktop_seconds = perf_counter()-start
            items = client.get(PREFIX+'/sessions').json()['items']
            sid = items[0]['id']
            data = client.get(PREFIX+'/sessions/'+sid).json()
            old_hash = hashlib.sha256(json.dumps(data['cells']).encode()).hexdigest()
            imported = client.post(PREFIX+'/sessions/'+sid+'/priors', json={
                'expected_revision': data['revision'], 'format': 'valetudo_v2', 'map': vacuum_map()}).json()
            page.get_by_role('button', name='刷新记录').click()
            expect(page.locator('#metricLayer option')).to_have_count(2)
            page.locator('#metricLayer').select_option(imported['id'])
            expect(page.locator('#metricBasis')).to_have_text('外部二维先验 · 未配准')
            expect(page.locator('#metricPlan')).to_be_disabled()
            page.screenshot(path=str(out/'external-prior.png'), full_page=True)
            page.locator('#metricLayer').select_option('robot')
            expect(page.locator('#metricFrames')).to_have_text('24')
            page.set_viewport_size({'width': 390, 'height': 844})
            page.get_by_role('button', name='寻找下一观察位').click()
            expect(page.locator('#metricPlanResult')).to_contain_text('可达观察位')
            page.screenshot(path=str(out/'mobile.png'), full_page=True)
            assert page.evaluate('document.documentElement.scrollWidth <= innerWidth+1')
            bounds = page.locator('#metricCanvas').bounding_box()
            page.locator('#metricCanvas').click(position={'x': bounds['width']/2, 'y': bounds['height']/2})
            expect(page.locator('#metricPlanResult')).to_contain_text('目标路径预览')
            goal = [r['plan_request']['goal_m'] for r in requests if 'goal_m' in r.get('plan_request', {})][-1]
            # The origin lies on cell boundaries; integer CSS pointer pixels on
            # a narrow canvas may select either adjacent 10 cm cell.
            assert abs(goal[0]) <= data['grid']['resolution_m'] and abs(goal[1]) <= data['grid']['resolution_m'], goal
            # Reload exercises persisted records rather than an in-memory demo.
            page.reload()
            expect(page.locator('#metricFrames')).to_have_text('24')
            assert old_hash == hashlib.sha256(json.dumps(client.get(PREFIX+'/sessions/'+sid).json()['cells']).encode()).hexdigest()
            assert not errors and not forbidden, (errors, forbidden)
            report = {'browser': 'Microsoft Edge headless', 'transport': 'Playwright to FastAPI TestClient; no listening port',
                      'data': 'synthetic_only', 'viewports': [[1360, 1000], [390, 844]],
                      'demo_frames': 24, 'grid_sha256': old_hash, 'demo_and_plan_ui_seconds': round(desktop_seconds, 3),
                      'page_errors': errors, 'forbidden_requests': forbidden, 'requests': requests,
                      'prior_isolation': True, 'reload_preserved_map': True, 'llm_calls': 0, 'motion_commands': 0}
            (out/'receipt.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
            print(json.dumps({k: v for k, v in report.items() if k != 'requests'}, ensure_ascii=False, indent=2))
            browser.close()


if __name__ == '__main__':
    main()
