import asyncio
from io import BytesIO
import json
from pathlib import Path
import struct

from fastapi.testclient import TestClient
import httpx
from PIL import Image
import pytest
from pydantic import ValidationError

from presentation.adapters import OnboardAdapter
from presentation.api import create_app, local_peer
from presentation.state import Event, Journal, Store, validate_glb


@pytest.fixture
def client(tmp_path):
    with TestClient(create_app(tmp_path)) as client:
        yield client


def picture(color='white', size=(160, 90)):
    output = BytesIO()
    Image.new('RGB', size, color).save(output, 'PNG')
    return output.getvalue()


def glb(document=None):
    document = document or {'asset': {'version': '2.0'}, 'scene': 0, 'scenes': [{'nodes': []}]}
    payload = json.dumps(document).encode()
    payload += b' ' * (-len(payload) % 4)
    return struct.pack('<4sIII4s', b'glTF', 2, len(payload) + 20, len(payload), b'JSON') + payload


def post_event(client, **patch):
    event = {'event_id': 'tool-1', 'kind': 'tool', 'title': '读取状态', 'text': 'done', **patch}
    return client.post('/api/presence/events', json=event, headers={'X-Presence-Producer': '1'})


def test_configuration_is_persistent_and_cross_origin_mutation_is_rejected(client, tmp_path):
    profile = client.get('/api/presence/config').json()
    profile.update(name='LUNA', persona='平静而直接地回答。')
    profile['layout'].update(log_open=False, split=.65)
    assert client.put('/api/presence/config', json=profile).status_code == 200
    assert Store(tmp_path).profile().name == 'LUNA'
    assert client.get('/api/presence/state').json()['profile']['layout']['log_open'] is False
    assert client.put('/api/presence/config', json=profile, headers={'Origin':'https://foreign.example'}).status_code == 403
    profile['avatar'] = {'kind':'glb', 'asset_id':'a' * 32 + '.glb'}
    assert client.put('/api/presence/config', json=profile).status_code == 422


def test_corrupt_configuration_is_preserved_and_reported(tmp_path):
    path = tmp_path / 'profile.json'
    path.write_text('broken original', encoding='utf-8')
    with TestClient(create_app(tmp_path)) as client:
        assert client.get('/api/presence/config').status_code == 503
    assert path.read_text(encoding='utf-8') == 'broken original'


def test_legacy_profile_reads_detailed_default_and_preserves_file_until_user_saves(tmp_path):
    from presentation.state import Profile
    legacy=Profile().model_dump()
    legacy['avatar']['kind']='procedural-moss'
    legacy['avatar'].pop('detail')
    path=tmp_path/'profile.json'
    original=json.dumps(legacy)
    path.write_text(original,encoding='utf-8')
    with TestClient(create_app(tmp_path)) as client:
        profile=client.get('/api/presence/config').json()
        assert profile['avatar']['kind']=='procedural-moss'
        assert profile['avatar']['detail']=='detailed'
        assert path.read_text(encoding='utf-8')==original
        profile['avatar']['detail']='simple'
        assert client.put('/api/presence/config',json=profile).status_code==200
        assert Store(tmp_path).profile().avatar.detail=='simple'
        profile['avatar']['detail']='arbitrary-code'
        assert client.put('/api/presence/config',json=profile).status_code==422


def test_events_require_local_explicit_publisher_and_are_idempotent(client):
    assert client.post('/api/presence/events', json={'kind':'tool','title':'test'}).status_code == 403
    first = post_event(client).json()
    assert first['seq'] == 1
    assert post_event(client).json() == first
    assert post_event(client, text='different content').status_code == 409
    assert post_event(client, kind='reasoning', text='hidden').status_code == 422
    assert post_event(client, overlays=[{'label':'box','x':0,'y':0,'w':.2,'h':.3}]).status_code == 422
    assert len(client.get('/api/presence/state').json()['events']) == 1


def test_history_gap_restart_and_duplicate_window_are_explicit():
    journal = Journal(limit=3)
    for i in range(6):
        journal.add(Event(event_id=str(i), kind='tool', title=str(i)))
    batch = journal.read(after=1, epoch=journal.epoch)
    assert batch['reset'] and [v['seq'] for v in batch['events']] == [4,5,6]
    assert journal.read(after=4, epoch=journal.epoch)['reset'] is False
    assert [v['seq'] for v in journal.read(4)['events']] == [5,6]
    assert journal.read(6, 'previous-server')['reset']
    assert Journal().read(after=6, epoch=journal.epoch)['reset']


def test_real_pixel_analysis_binds_its_input_without_inventing_boxes(client):
    upload = client.post('/api/presence/media', content=picture()).json()
    result = client.post('/api/presence/analyze', json={'media_id':upload['id']})
    assert result.status_code == 200
    body = result.json()
    assert body['metrics']['brightness'] == 255
    assert body['metrics']['bright_fraction'] == 1
    assert body['metrics']['laplacian_variance'] == 0
    event = body['event']
    assert event['media_id'] == upload['id'] and event['frame_id']
    assert event['evidence'] == 'computed' and event['overlays'] == []
    events = client.get('/api/presence/state').json()['events']
    assert [e['phase'] for e in events] == ['started','completed']
    assert events[0]['frame_id'] == events[1]['frame_id']
    assert client.get(upload['url']).headers['content-type'] == 'image/png'


def test_image_normalization_removes_original_exif(client):
    output = BytesIO()
    exif = Image.Exif()
    exif[271] = 'generated-private-test-metadata'
    Image.new('RGB',(30,20),'red').save(output,'JPEG',exif=exif)
    result = client.post('/api/presence/media',content=output.getvalue()).json()
    content = client.get(result['url']).content
    with Image.open(BytesIO(content)) as image:
        assert not image.getexif()
    assert b'generated-private-test-metadata' not in content


def test_public_mailbox_roundtrip_carries_persona_without_touching_an_actuator(tmp_path):
    import threading
    import time
    from http.server import ThreadingHTTPServer
    from deployment.onboard.service import Mailbox, handler_for
    class FakeBrain:
        def __init__(self):
            self.calls = []
            self.cancelled = threading.Event()
        def session(self): return {}
        def ask(self, message, request_id):
            self.calls.append((message,request_id))
            return {'text':'公开回复：观察完成。'}
        def cancel(self): self.cancelled.set()
    brain = FakeBrain()
    mailbox = Mailbox(brain,tmp_path/'mailbox')
    threading.Thread(target=mailbox.work,daemon=True).start()
    server = ThreadingHTTPServer(('127.0.0.1',0),handler_for(mailbox))
    threading.Thread(target=server.serve_forever,daemon=True).start()
    try:
        with TestClient(create_app(tmp_path/'presence',onboard_url=f'http://127.0.0.1:{server.server_port}')) as client:
            response = client.post('/api/presence/messages',json={'id':'roundtrip','message':'看一下环境'})
            assert response.status_code == 200
            deadline = time.monotonic()+3
            while True:
                state = client.get('/api/presence/state').json()
                if any(e['phase']=='completed' for e in state['events']):break
                assert time.monotonic()<deadline
                time.sleep(.05)
            assert state['connection']['state']=='online'
            assert state['events'][-1]['text']=='公开回复：观察完成。'
            assert len(brain.calls)==1 and 'MOSS' in brain.calls[0][0] and '看一下环境' in brain.calls[0][0]
            assert all('诊断' not in e['text'] for e in state['events'])
    finally:
        server.shutdown(); server.server_close()


def test_upload_rejects_scripts_paths_and_external_glb_resources(client):
    assert client.post('/api/presence/media', content=b'<svg onload="alert(1)"></svg>').status_code == 422
    assert client.get('/api/presence/media/../../profile.json').status_code == 404
    bad = glb({'asset':{'version':'2.0'},'images':[{'uri':'https://foreign.example/secret'}]})
    assert client.post('/api/presence/avatar', content=bad).status_code == 422
    extension = glb({'asset':{'version':'2.0'},'extensions':{'custom':{'uri':'file:///secret'}}})
    with pytest.raises(ValueError, match='外部'):
        validate_glb(extension)
    uploaded = client.post('/api/presence/avatar', content=glb())
    assert uploaded.status_code == 200 and uploaded.json()['kind'] == 'glb'
    assert client.post('/api/presence/media', content=glb()).status_code == 422


def test_media_quota_rejects_new_asset_without_breaking_old_references(tmp_path):
    store = Store(tmp_path, quota=500)
    first = store.put(picture())
    store.quota = first['size']
    with pytest.raises(ValueError, match='上限'):
        store.put(picture('red'))
    assert store.asset(first['id']).read_bytes() == picture()


def test_overlay_cannot_reference_unknown_media_or_leave_image_bounds(client):
    assert post_event(client, frame_id='f',media_id='a'*32+'.png',
        overlays=[{'label':'object','x':.8,'y':.2,'w':.4,'h':.4}]).status_code == 422
    assert post_event(client, media_id='a'*32+'.png').status_code == 404
    upload = client.post('/api/presence/media', content=picture()).json()
    assert post_event(client, frame_id='f', media_id=upload['id'],
        overlays=[{'label':'object','x':.2,'y':.2,'w':.4,'h':.4}]).status_code == 200


@pytest.mark.parametrize('url', ['https://127.0.0.1:8781','http://192.0.2.1:8781','http://user:password@127.0.0.1:8781','http://127.0.0.1:8781/command'])
def test_fixed_upstream_cannot_become_an_external_or_arbitrary_proxy(url):
    with pytest.raises(ValueError):
        local_peer(url)


def test_onboard_projection_uses_public_reply_and_preserves_failed_status(tmp_path):
    adapter = OnboardAdapter(Store(tmp_path))
    adapter.receive({'id':'one','status':'running','message':'observe'})
    adapter.receive({'id':'one','status':'completed','reply':{'text':'verified public reply','diagnostics':'private'}})
    adapter.receive({'id':'two','status':'failed','message':'private error text'})
    events = adapter.store.journal.read()['events']
    assert events[1]['text'] == 'verified public reply'
    assert events[2]['phase'] == 'failed' and 'private' not in events[2]['text']
    assert 'diagnostics' not in json.dumps(events)


@pytest.mark.parametrize('status,content,error_type', [
    (409,b'{}',ValueError), (500,b'{}',httpx.HTTPStatusError), (200,b'broken json',httpx.RemoteProtocolError),
])
def test_message_delivery_distinguishes_rejection_from_uncertainty_without_retry(tmp_path,monkeypatch,status,content,error_type):
    original = httpx.AsyncClient
    requests = []
    def reply(request):
        requests.append(request)
        return httpx.Response(status,content=content)
    monkeypatch.setattr(httpx,'AsyncClient',lambda **kwargs:original(**kwargs,transport=httpx.MockTransport(reply)))
    adapter = OnboardAdapter(Store(tmp_path),'http://127.0.0.1:8781')
    from presentation.state import Profile
    with pytest.raises(error_type):
        asyncio.run(adapter.send('stable-request','hello',Profile()))
    assert len(requests)==1
    assert requests[0].url.path=='/v1/messages'


def test_camera_proxy_closes_its_upstream_and_only_uses_the_allowlisted_get(tmp_path,monkeypatch):
    original = httpx.AsyncClient
    closed, requests = [], []
    class Stream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b'--frame\r\nContent-Type: image/jpeg\r\n\r\nsynthetic-test\r\n'
        async def aclose(self): closed.append(True)
    def reply(request):
        requests.append(request)
        return httpx.Response(200,headers={'Content-Type':'multipart/x-mixed-replace; boundary=frame'},stream=Stream())
    monkeypatch.setattr(httpx,'AsyncClient',lambda **kwargs:original(**kwargs,transport=httpx.MockTransport(reply)))
    with TestClient(create_app(tmp_path,controller_url='http://127.0.0.1:8765')) as client:
        assert client.get('/api/presence/camera/0').status_code==200
        assert client.get('/api/presence/camera/7').status_code==404
    assert closed and len(requests)==1
    assert requests[0].method=='GET' and requests[0].url.path=='/camera/0.mjpg'


def test_offline_runtime_assets_are_locally_resolvable(client):
    page = client.get('/presence/')
    assert page.status_code == 200 and 'object-src' in page.headers['content-security-policy']
    for path in ['app.js','avatar.js','model.js','demo.js','style.css','vendor/three.module.min.js','vendor/three.core.min.js',
                 'vendor/addons/loaders/GLTFLoader.js','vendor/addons/utils/BufferGeometryUtils.js']:
        assert client.get('/presence/assets/' + path).status_code == 200
    source = client.get('/presence/assets/vendor/addons/loaders/GLTFLoader.js').text
    assert "from 'three';" not in source
    assert client.get('/api/presence/torque').status_code in (404, 405)
    assert client.post('/api/presence/torque', json={'enabled':True}).status_code in (404, 405)
