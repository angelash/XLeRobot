"""Platform tests use a fake provider/HTTP transport: no serial or live controller."""
import asyncio
import io
import json
import zipfile
from copy import deepcopy

import httpx
import pytest
from fastapi.testclient import TestClient
from fabrication.assembly.debug_console import platform_app as mod
from fabrication.assembly.debug_console.platform_catalog import catalog,REPO,GUIDES,CAPABILITIES
from fabrication.assembly.debug_console.upstream_teleop import UpstreamTeleop


def state():
    return {'control_profile':'original_preview','controller':{'controller':'online','paused':True,'pause_reason':'user'},
            'original':UpstreamTeleop().snapshot(),'gamepad':{'connected':False},'ports':[]}


@pytest.fixture
def client(tmp_path,monkeypatch):
    async def provider():return deepcopy(state())
    service=mod.Workbench(tmp_path,provider)
    monkeypatch.setattr(mod,'service',service)
    with TestClient(mod.app) as c:yield c


def test_lan_origin_allows_tools_and_proxy_without_changing_takeover(client,tmp_path,monkeypatch):
    config=tmp_path/'studio-network.json'
    config.write_text(json.dumps({'allowed_origins':['http://192.0.2.20:8770']}),encoding='utf-8')
    monkeypatch.setattr(mod,'ALLOWED_ORIGINS',mod.studio_origins(config))
    before=client.get('/api/platform/status').json()['runtime']
    allowed={'Origin':'http://192.0.2.20:8770'}
    assert client.post('/api/platform/sandbox/reset',json={},headers=allowed).status_code==200
    requests=[]
    def reply(request):
        requests.append(request)
        return httpx.Response(200,json={'controller':'offline'})
    mod.service.client=httpx.AsyncClient(transport=httpx.MockTransport(reply))
    assert client.post('/api/controller/heartbeat',json={},headers=allowed).status_code==200
    assert requests[0].headers['origin']==mod.UPSTREAM
    for origin in ('http://192.0.2.21:8770','http://192.0.2.20:8765','https://foreign.example'):
        assert client.post('/api/platform/sandbox/reset',json={},headers={'Origin':origin}).status_code==403
    assert client.get('/api/platform/status').json()['runtime']==before
    for name in ('original','diagnostic'):
        html=client.get('/runtime/'+name).text
        assert 'href="http://127.0.0.1:8770/' not in html


@pytest.mark.parametrize('origin',['*','http://0.0.0.0:8770','file:///tmp/studio','http://user:password@192.0.2.20:8770',
                                 'http://192.0.2.20:8770/other','http://192.0.2.20:8770?x=1','http://192.0.2.20:8765',None])
def test_invalid_lan_origin_never_expands_workbench_access(tmp_path,origin):
    config=tmp_path/'studio-network.json'
    config.write_text(json.dumps({'allowed_origins':[origin]}),encoding='utf-8')
    with pytest.raises((ValueError,TypeError)):mod.studio_origins(config)


def test_catalog_resolves_all_sources_and_media(client):
    c=client.get('/api/platform/catalog').json()
    assert len(c['capabilities'])==20 and len(c['guides'])==14
    assert all(p in c['source_ids'] for x in GUIDES+CAPABILITIES for p in x['sources'])
    for id in c['media']:assert client.get(c['media'][id]).status_code==200
    assert client.get('/api/platform/sources/'+c['sources'][0]['id']).json()['content']
    assert client.get('/api/platform/sources/secret').status_code==404
    assert client.get('/static/../platform_app.py').status_code==404


def test_record_poll_preserves_paused_takeover_and_exports_manifest(client):
    before=client.get('/api/platform/status').json()['runtime']
    rec=client.post('/api/platform/recordings/start',json={'name':'test','fps':10}).json()
    assert client.post('/api/platform/recordings/start',json={'name':'duplicate'}).status_code==409
    # Synchronize on captured frames instead of assuming a particular machine speed.
    import time
    deadline=time.monotonic()+2
    while time.monotonic()<deadline:
        if client.get('/api/platform/recordings/'+rec['id']).json()['meta']['frame_count']>=2:break
        time.sleep(.02)
    stopped=client.post('/api/platform/recordings/stop').json()
    assert stopped['status']=='stopped' and stopped['frame_count']>=2
    pack=client.get('/api/platform/recordings/'+rec['id']).json()
    assert all(f['profile']=='original_preview' and f['controller']['paused'] for f in pack['frames'])
    assert not pack['meta']['contains_images'] and not pack['meta']['native_lerobot_dataset']
    archive=zipfile.ZipFile(io.BytesIO(client.get('/api/platform/recordings/'+rec['id']+'/export').content))
    assert json.loads(archive.read('manifest.json'))['frame_count']==len(pack['frames'])
    assert len(archive.read('frames.jsonl').decode().splitlines())==len(pack['frames'])
    assert client.get('/api/platform/status').json()['runtime']==before
    assert client.post('/api/platform/recordings/stop').status_code==409


def test_immediate_stop_finalizes_task_even_before_capture(tmp_path):
    async def run():
        async def provider():return state()
        s=mod.Workbench(tmp_path,provider)
        rec=await s.start_record(mod.RecordRequest(name='immediate'))
        stopped=await s.stop_record()
        assert s.recording is None and stopped['status']=='stopped'
        assert s.recording_meta(rec['id'])['ended_at']
    asyncio.run(run())


def test_windows_temporary_replace_conflict_recovers(tmp_path,monkeypatch):
    from pathlib import Path
    original=Path.replace;attempts=[]
    def replace(path,target):
        attempts.append(1)
        if len(attempts)<3:raise PermissionError('Windows open read handle')
        return original(path,target)
    monkeypatch.setattr(Path,'replace',replace)
    mod.atomic(tmp_path/'meta.json',{'frames':3})
    assert mod.read_json(tmp_path/'meta.json')=={'frames':3}


def test_storage_failure_does_not_leave_a_stuck_recording(tmp_path,monkeypatch):
    async def run():
        async def provider():return state()
        s=mod.Workbench(tmp_path,provider)
        await s.start_record(mod.RecordRequest(name='disk fault'))
        def fail(*args):raise PermissionError('disk unavailable')
        monkeypatch.setattr(mod,'atomic',fail)
        await s.record_task
        assert s.recording is None and '封口失败' in s.recording_error
        assert s.list_items('recordings')[0]['status']=='interrupted'
    asyncio.run(run())


def test_sandbox_and_demo_never_call_hardware_or_change_runtime(client):
    before=client.get('/api/platform/status').json()['runtime']
    initial=client.get('/api/platform/sandbox').json()
    after=client.post('/api/platform/sandbox/step',json={'axes':[0,-1,0,-1,1,1]}).json()
    assert after['arms']['left']['plane_m'][0]>initial['arms']['left']['plane_m'][0]
    assert after['arms']['left']['joints']['grip']<90
    demo=client.get('/api/platform/sandbox/demo/drive').json()
    assert len(demo['frames'])>50 and demo['frames'][-1]['original']['base_pose']!=[0,0,0]
    assert client.get('/api/platform/sandbox').json()==after
    assert client.get('/api/platform/status').json()['runtime']==before
    assert client.post('/api/platform/sandbox/step',json={'axes':[9,0,0,0,0,0]}).status_code==422
    assert client.post('/api/platform/sandbox/step',json={'buttons':[24]}).status_code==422
    assert client.post('/api/platform/sandbox/step',json={'dt':1}).status_code==422
    assert client.post('/api/platform/sandbox/reset',json={}).json()==initial


def test_pose_load_is_preview_only_and_path_safe(client):
    pose=client.post('/api/platform/poses',json={'name':'sandbox pose','source':'sandbox'}).json()
    client.post('/api/platform/sandbox/step',json={'axes':[1,0,0,0,0,0]})
    assert client.post('/api/platform/sandbox/pose/'+pose['id']).json()==pose['original']
    assert pose['hardware_move_supported'] is False
    p=mod.service.root/'poses'/(pose['id']+'.json')
    data=json.loads(p.read_text(encoding='utf-8'));data['profile']='original_hardware';p.write_text(json.dumps(data),encoding='utf-8')
    assert client.post('/api/platform/sandbox/pose/'+pose['id']).status_code==409
    assert client.post('/api/platform/sandbox/pose/invalid').status_code==404
    assert client.post('/api/platform/poses',json={'name':'bad','source':'filesystem'}).status_code==422


def test_interrupted_recordings_and_partial_last_frame_are_readable(client):
    id='a'*32;folder=mod.service.root/'recordings'/id;folder.mkdir(parents=True)
    (folder/'meta.json').write_text(json.dumps({'id':id,'status':'recording','created_at':'2026'}))
    (folder/'frames.jsonl').write_text('{"t":0}\n{"unfinished"')
    listing=client.get('/api/platform/library').json()
    assert listing['recordings'][0]['status']=='interrupted'
    assert client.get('/api/platform/recordings/'+id).json()['frames']==[{'t':0}]


def test_experiments_are_saved_not_executed_and_cannot_inject_shell(client):
    base={'name':'experiment','dataset':'user/toys','steps':100,'batch_size':2}
    for policy in ('act','smolvla'):
        x=client.post('/api/platform/experiments',json={**base,'policy':policy}).json()
        assert x['executed'] is False and x['status']=='configuration_only'
        assert x['args'][0]=='lerobot-train' and '--wandb.enable=false' in x['args']
        assert '--policy.push_to_hub=false' in x['args']
        assert '\n' in x['command']
    for dataset in ('user/data;whoami','user/$(whoami)','user/..\\secrets','bad'):
        assert client.post('/api/platform/experiments',json={**base,'dataset':dataset}).status_code==422
    assert client.post('/api/platform/experiments',json={**base,'policy':'pi05'}).status_code==422


def test_image_quality_returns_measured_statistics_without_saving(client):
    from PIL import Image
    image=Image.new('RGB',(120,80),(0,0,0));buf=io.BytesIO();image.save(buf,format='PNG')
    q=client.post('/api/platform/vision/quality',content=buf.getvalue()).json()
    assert q['width']==120 and q['height']==80 and q['brightness']==0
    assert q['dark_fraction']==1 and q['laplacian_variance']==0 and not q['saved']
    assert client.post('/api/platform/vision/quality',content=b'not an image').status_code==422
    assert not list(mod.service.root.rglob('*.png'))


def test_fixed_proxy_uses_same_controller_origin_and_rejects_extra_routes(client):
    requests=[]
    def upstream(request):
        requests.append(request)
        if request.url.path=='/api/log/download':return httpx.Response(200,content=b'log',headers={'content-disposition':'attachment; filename="events.jsonl"'})
        return httpx.Response(409,json={'detail':'takeover preserved'})
    mod.service.client=httpx.AsyncClient(transport=httpx.MockTransport(upstream))
    r=client.post('/api/original/start',json={'target':'preview'})
    assert r.status_code==409 and requests[0].headers['origin']==mod.UPSTREAM
    assert str(requests[0].url)=='http://127.0.0.1:8765/api/original/start'
    assert client.post('/api/anything',json={}).status_code==404
    assert 'attachment' in client.get('/api/log/download').headers['content-disposition']
    assert client.post('/api/platform/sandbox/reset',json={},headers={'Origin':'https://foreign.example'}).status_code==403
    assert client.get('/runtime/original').status_code==200
    assert b'/assets/embed.css' in client.get('/runtime/original').content
    assert client.get('/runtime/something').status_code==404


def test_main_service_failure_still_allows_manuals_and_sandbox(client):
    async def unavailable():raise mod.HTTPException(503,'controller absent')
    mod.service.provider=unavailable
    assert client.get('/api/platform/status').json()['upstream_connected'] is False
    assert client.post('/api/platform/recordings/start',json={'name':'no data'}).status_code==503
    assert client.get('/api/platform/catalog').status_code==200
    assert client.post('/api/platform/sandbox/step',json={}).status_code==200


def test_guided_calibration_proxy_only_exposes_explicit_actions(client):
    requests=[]
    def reply(request):
        requests.append(request)
        return httpx.Response(200,json={'active':True,'step':'ranges'})
    mod.service.client=httpx.AsyncClient(transport=httpx.MockTransport(reply))
    for action in ('start','recover','capture','capture-item','delete-item','rewind','base-test','base-confirm','wheels','apply','cancel'):
        assert client.post('/api/whole-calibration/'+action,json={'confirmed':True}).status_code==200
        assert requests[-1].headers['origin']==mod.UPSTREAM
        assert requests[-1].extensions['timeout']['read']==45
    assert client.post('/api/whole-calibration/exec',json={}).status_code==404
    assert client.get('/static/whole-calibration.js').status_code==200
    assert '/static/whole-calibration.js?v=37' in client.get('/runtime/original').text
    assert client.post('/api/control-settings',json={}).status_code==200
    assert requests[-1].url.path=='/api/control-settings'
