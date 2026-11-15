"""Use the actual Studio module routes; controller access must remain absent."""
import json

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from .integration import install
from .metric_api import PREFIX
from .store import Survey
from .test_metric import config, packet, vacuum_map


@pytest.fixture
def client(tmp_path, monkeypatch):
    (tmp_path/'map.json').write_text('{"id":"old-survey","features":[],"observations":[]}')
    original = (tmp_path/'map.json').read_bytes()
    def forbidden(*_, **__):
        raise AssertionError('mapping must not contact robot controller')
    monkeypatch.setattr(Survey, 'controller', forbidden)
    app = FastAPI(); install(app, data_dir=tmp_path, allowed_origins=('http://127.0.0.1:8770',))
    with TestClient(app) as value:
        yield value
    assert (tmp_path/'map.json').read_bytes() == original


def test_mapping_uses_original_app_routes_and_rejects_controls(client):
    assert client.get(PREFIX+'/status').json()['localization_backend'] == 'not_connected'
    assert client.get('/fieldmap/metric/view').status_code == 200
    assert client.get('/fieldmap/assets/metric.js').status_code == 200
    assert client.post(PREFIX+'/drive', json={}).status_code == 404
    assert client.post(PREFIX+'/sessions', json={**config(), 'confirmed': True}).status_code == 422
    assert client.post(PREFIX+'/sessions', json=config(), headers={'Origin': 'https://foreign.example'}).status_code == 403
    assert client.post(PREFIX+'/sessions', content='{}', headers={'Content-Type': 'text/plain'}).status_code == 415
    assert client.post(PREFIX+'/demo', json={'start_robot': True}).status_code == 422


def test_scan_revisions_prior_isolation_exports_and_restart_contract(client):
    created = client.post(PREFIX+'/sessions', json=config())
    assert created.status_code == 201
    sid = created.json()['id']; base = PREFIX+'/sessions/'+sid
    response = client.post(base+'/scan', json={'expected_revision': 0, 'scan': packet()})
    assert response.status_code == 200 and response.json()['revision'] == 1
    assert client.post(base+'/scan', json={'expected_revision': 0, 'scan': packet(1)}).status_code == 409
    assert client.post(base+'/plan', json={'expected_revision': 0}).status_code == 409
    assert client.post(base+'/plan', json={'expected_revision': 1, 'robot_radius_m': .1}).status_code == 422
    imported = client.post(base+'/priors', json={'expected_revision': 1, 'format': 'valetudo_v2', 'map': vacuum_map()})
    assert imported.status_code == 201
    assert not client.get(base+'/priors/'+imported.json()['id']).json()['navigation_use']
    data = client.get(base).json(); assert data['scan_count'] == 1 and len(data['priors']) == 1
    archive = client.get(base+'/export')
    assert archive.json()['scans'] == [packet()] and 'attachment' in archive.headers['Content-Disposition']
    assert client.get(base+'/occupancy.pgm').content.startswith(b'P5\n')
    yaml = client.get(base+'/occupancy.yaml').text
    assert 'resolution: 0.1' in yaml and 'origin: [-2.0, -2.0, 0.0]' in yaml
    assert not data['motion_enabled']


def test_request_budgets_and_nonfinite_values_do_not_create_partial_frames(client):
    sid = client.post(PREFIX+'/sessions', json=config()).json()['id']
    path = PREFIX+'/sessions/'+sid
    assert client.post(path+'/scan', content=b' '*256001, headers={'Content-Type': 'application/json'}).status_code == 413
    bad = packet(); bad['pose']['x_m'] = float('nan')
    response = client.post(path+'/scan', content=json.dumps({'expected_revision': 0, 'scan': bad}), headers={'Content-Type': 'application/json'})
    assert response.status_code == 422
    assert client.get(path).json()['revision'] == 0


def test_demo_plan_is_local_and_honestly_labelled(client):
    response = client.post(PREFIX+'/demo', json={})
    assert response.status_code == 201
    data = response.json(); assert data['config']['mode'] == 'synthetic_demo'
    assert data['localization_backend'] == 'provided_pose_only' and data['scan_count'] == 24
    preview = client.post(PREFIX+'/sessions/'+data['id']+'/plan', json={'expected_revision': data['revision']}).json()
    assert preview['status'] == 'frontier_preview' and not preview['motion_enabled']
