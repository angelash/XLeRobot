"""Persisted preferences must survive a display-only server update."""
import json

from fastapi.testclient import TestClient
import pytest

from presentation.api import create_app
from presentation.state import VoiceInput


def test_speech_profile_loads_and_survives_an_unrelated_settings_edit(tmp_path):
    original = {
        'version': 1, 'name': 'Archive test', 'subtitle': 'Generated fixture',
        'greeting': 'Ready.', 'persona': 'Answer briefly.', 'accent': '#325a7c',
        'avatar': {'kind': 'licensed-moss', 'detail': 'detailed',
                   'asset_id': None, 'credit': 'Test attribution'},
        'layout': {'log_open': False, 'split': .65, 'animate': False},
        'speech': {'enabled': True, 'voice_id': 'generated_test_voice',
                   'speed': 1.25, 'pitch': -3, 'volume': .45, 'emotion': 'calm'},
    }
    path = tmp_path / 'profile.json'
    before = json.dumps(original).encode('utf-8')
    path.write_bytes(before)
    with TestClient(create_app(tmp_path)) as client:
        response = client.get('/api/presence/config')
        assert response.status_code == 200
        assert response.json() == {**original, 'voice_input': VoiceInput().model_dump()}
        state = client.get('/api/presence/state')
        assert state.status_code == 200 and state.json()['profile'] == response.json()
        assert path.read_bytes() == before

        changed = response.json()
        changed['name'] = 'Edited display name'
        assert client.put('/api/presence/config', json=changed).status_code == 200

    with TestClient(create_app(tmp_path)) as restarted:
        assert restarted.get('/api/presence/config').json() == changed
    assert json.loads(path.read_bytes()) == changed


def test_profile_without_speech_loads_with_disabled_voice_without_rewriting(tmp_path):
    path = tmp_path / 'profile.json'
    before = b'{"version":1,"name":"Legacy display"}'
    path.write_bytes(before)
    with TestClient(create_app(tmp_path)) as client:
        response = client.get('/api/presence/config')
        assert response.status_code == 200
        assert response.json()['name'] == 'Legacy display'
        assert response.json()['speech']['enabled'] is False
    assert path.read_bytes() == before


@pytest.mark.parametrize('invalid', [
    {'speech': {'speed': 20}},
    {'speech': {'voice_id': 'invalid\nvoice'}},
    {'speech': {'unknown_setting': True}},
    {'unknown_section': {}},
])
def test_real_schema_errors_still_preserve_original_file(tmp_path, invalid):
    path = tmp_path / 'profile.json'
    before = json.dumps({'version': 1, **invalid}).encode('utf-8')
    path.write_bytes(before)
    with TestClient(create_app(tmp_path)) as client:
        assert client.get('/api/presence/config').status_code == 503
        assert client.get('/api/presence/state').status_code == 503
    assert path.read_bytes() == before
