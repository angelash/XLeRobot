"""Silent contract/state tests. No audio-device APIs, recordings or playback."""
import asyncio
import json
import struct
import time

from fastapi.testclient import TestClient
import pytest
from starlette.websockets import WebSocketDisconnect

from presentation.api import create_app
from presentation.live import LiveFeed, PublicReplies
from presentation.state import Profile, VoiceInput
from presentation.voice_engine import strip_wake


class FakeEngine:
    ready = False
    wake_ready = False
    wake_names = []
    unsupported = []
    state = 'off'

    def __init__(self):
        self.frames = []
        self.preparations = 0

    def available(self): return True

    def prepare(self, profile):
        self.preparations += 1
        self.ready = self.wake_ready = True
        self.wake_names = [profile.name]
        self.reset()

    def reset(self): self.state = 'armed'

    def control(self, command):
        if command == 'begin': self.state = 'listening'
        if command in ('cancel', 'resume'): self.state = 'armed'
        if command == 'pause': self.state = 'playback-paused'
        if command == 'end':
            self.state = 'armed'
            return [{'kind': 'asr.final', 'text': '生成的测试文字', 'requires_confirmation': True}]
        return []

    def feed(self, pcm):
        self.frames.append(pcm)
        return []


@pytest.fixture
def setup(tmp_path):
    app = create_app(tmp_path)
    service = app.state.presence.voice_input
    engine = FakeEngine()
    service.engine = engine
    sent = []

    async def send(identifier, text, profile):
        sent.append((identifier, text, profile.name))
        return {'id': identifier, 'status': 'queued'}

    service.adapter.send = send
    with TestClient(app) as client:
        yield client, service, engine, sent


def enable(client, **changes):
    profile = client.get('/api/presence/config').json()
    profile['voice_input'].update(enabled=True, **changes)
    assert client.put('/api/presence/config', json=profile).status_code == 200
    return profile


def claim(client, owner='generated-browser', takeover=False):
    value = client.post('/api/presence/voice-input/claim', json={'owner_id': owner, 'takeover': takeover})
    assert value.status_code == 200, value.text
    return {k: value.json()[k] for k in ('owner_id', 'capture_epoch', 'token')}


def until(predicate):
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        if predicate(): return
        time.sleep(.01)
    pytest.fail('State transition did not complete')


def test_old_config_defaults_off_does_not_prepare_or_rewrite(tmp_path):
    original = b'{"name":"MOSS","speech":{"enabled":true}}'
    (tmp_path/'profile.json').write_bytes(original)
    with TestClient(create_app(tmp_path)) as client:
        assert client.get('/api/presence/config').json()['voice_input']['enabled'] is False
        status = client.get('/api/presence/voice-input/status').json()
        assert status['state'] == 'off' and not status['ready'] and not status['active']
        assert client.post('/api/presence/voice-input/claim', json={'owner_id': 'generated-browser'}).status_code == 409
    assert (tmp_path/'profile.json').read_bytes() == original


def test_lease_conflict_explicit_takeover_and_no_token_in_status(setup):
    client, service, engine, sent = setup
    enable(client)
    first = claim(client)
    assert client.post('/api/presence/voice-input/claim', json={'owner_id': 'second-browser'}).status_code == 409
    with client.websocket_connect('/api/presence/voice-input/stream') as ws:
        ws.send_json(first)
        assert ws.receive_json()['kind'] == 'voice.ready'
        second = claim(client, 'second-browser', True)
        assert second['capture_epoch'] != first['capture_epoch']
        assert first['token'] not in json.dumps(client.get('/api/presence/voice-input/status').json())
    assert sent == []


@pytest.mark.parametrize('payload', [b'bad', struct.pack('<I', 1)+b'\0'*640, struct.pack('<I', 0)+b'\0'*642])
def test_malformed_or_missing_pcm_frame_invalidates_capture_without_submission(setup, payload):
    client, service, engine, sent = setup
    enable(client)
    lease = claim(client)
    with client.websocket_connect('/api/presence/voice-input/stream') as ws:
        ws.send_json(lease);ws.receive_json()
        ws.send_json({'op': 'begin'})
        ws.send_bytes(payload)
        with pytest.raises(WebSocketDisconnect): ws.receive_json()
    assert not service.lease and not service.pending and not sent


def test_ptt_keeps_frame_order_final_confirm_is_idempotent_and_keeps_role(setup):
    client, service, engine, sent = setup
    enable(client)
    lease = claim(client)
    with client.websocket_connect('/api/presence/voice-input/stream') as ws:
        ws.send_json(lease);ws.receive_json()
        ws.send_json({'op': 'begin'})
        for seq in range(3): ws.send_bytes(struct.pack('<I', seq)+bytes([seq])*640)
        ws.send_json({'op': 'end'})
        until(lambda: bool(service.pending))
        assert engine.frames == [bytes([i])*640 for i in range(3)]
        assert not sent
        identifier = next(iter(service.pending))
        body = {**lease, 'request_id': identifier, 'text': '用户确认的测试文字'}
        assert client.post('/api/presence/voice-input/confirm', json=body).status_code == 200
        assert client.post('/api/presence/voice-input/confirm', json=body).json()['status'] == 'accepted'
        assert sent == [(identifier, '用户确认的测试文字', 'MOSS')]
    until(lambda: not service.lease)


def test_name_switch_cancels_only_unsubmitted_input_and_preserves_output(setup):
    client, service, engine, sent = setup
    profile = enable(client)
    profile['speech']['enabled'] = True
    client.put('/api/presence/config', json=profile)
    lease = claim(client)
    with client.websocket_connect('/api/presence/voice-input/stream') as ws:
        ws.send_json(lease);ws.receive_json();ws.send_json({'op': 'begin'});ws.send_json({'op': 'end'})
        until(lambda: bool(service.pending))
        identifier = next(iter(service.pending))
        profile['name'] = 'LUNA'
        assert client.put('/api/presence/config', json=profile).status_code == 200
        assert not service.pending and not service.lease
        assert client.post('/api/presence/voice-input/confirm', json={**lease,'request_id':identifier,'text':'迟到结果'}).status_code == 409
    assert client.get('/api/presence/config').json()['speech']['enabled'] is True
    assert not sent


def test_only_exact_local_display_commands_are_executed(setup):
    client, service, engine, sent = setup
    enable(client)
    lease = claim(client)
    with client.websocket_connect('/api/presence/voice-input/stream') as ws:
        ws.send_json(lease);ws.receive_json();ws.send_json({'op':'begin'});ws.send_json({'op':'end'})
        until(lambda: bool(service.pending))
        identifier = next(iter(service.pending))
        result = client.post('/api/presence/voice-input/confirm', json={**lease,'request_id':identifier,'text':'收起过程。'})
        assert result.json()['status'] == 'local'
        assert not client.get('/api/presence/config').json()['layout']['log_open'] and not sent


def test_loopback_and_websocket_origin_are_required(setup):
    client, service, engine, sent = setup
    enable(client)
    assert client.post('http://192.168.137.150:8780/api/presence/voice-input/claim', json={'owner_id':'generated-browser'}).status_code == 403
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect('/api/presence/voice-input/stream', headers={'origin':'https://untrusted.example'}): pass
    assert not engine.preparations


def test_expired_lease_and_disallowed_capture_preferences(setup):
    client, service, engine, sent = setup
    enable(client)
    lease = claim(client)
    service.lease['heartbeat'] -= 4
    assert not service.valid(type('Lease', (), lease)())
    with client.websocket_connect('/api/presence/voice-input/stream') as ws:
        ws.send_json(lease)
        with pytest.raises(WebSocketDisconnect): ws.receive_json()
    for change in ({'keep_raw_audio':True}, {'duplex':'full'}, {'wake':{'aliases':['invalid@word']}}):
        with pytest.raises(ValueError): VoiceInput.model_validate(change)


def test_stable_sentences_and_terminal_replies_never_rewrite_prior_parts():
    values = PublicReplies(LiveFeed())
    first = values.update('generated','第一句。还没说完')
    assert first['parts'] == ['第一句。']
    second = values.update('generated','第一句。还没说完的第二句。')
    assert second['parts'] == ['第一句。', '还没说完的第二句。']
    terminal = values.update('generated',second['text'],status='cancelled')
    assert values.update('generated','迟到的完成',status='completed') == terminal


def test_strip_only_leading_confirmed_wake():
    assert strip_wake('莫斯，告诉我莫斯的状态', ['MOSS','莫斯']) == '告诉我莫斯的状态'
    assert strip_wake('告诉我莫斯的状态', ['莫斯']) == '告诉我莫斯的状态'
    assert strip_wake('MOSS', ['MOSS']) == ''
