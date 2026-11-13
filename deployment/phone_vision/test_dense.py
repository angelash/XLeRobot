"""Independent plane ground truth, source boundaries and artifact readback."""
import hashlib
import json
from pathlib import Path
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from .dense import atomic, load_frame, reconstruct
from .dense_store import DenseStore
from .integration import install
from .service import PhoneVision


def plane_sequence(folder, frames=5):
    import cv2
    import numpy as np
    folder.mkdir()
    h, w, focal = 80, 96, 85.
    y, x = np.mgrid[:h, :w]
    # z = 1 + .15*x_camera + .1*y_camera, independently ray-cast.
    depth = 1/(1-.15*(x-w/2)/focal-.1*(y-h/2)/focal)
    rgb = np.stack([(x*17+y*7)%255, (x*5+y*19)%255, (x*13+y*3)%255], -1).astype('uint8')
    rows = []
    for i in range(frames):
        measured = (depth+np.random.default_rng(i).normal(0, .0005, depth.shape)).astype('<f4')
        measured[27:53, 35:61] = 0  # Permanent missing window: must remain open.
        raw = measured.tobytes()
        encoded = cv2.imencode('.jpg', rgb)[1].tobytes()
        row = {'index': i, 'sequence': i+1, 'session': 'synthetic-fixed', 'at': '2026-11-13T00:00:00Z',
            'meta': {'width': w, 'height': h, 'intrinsics': {'fx': focal, 'fy': focal, 'tx': w/2, 'ty': h/2},
                     'depth_source': 'synthetic_ground_truth', 'sensor_raw_depth': False,
                     'host_acquired_monotonic': 10+i*.2},
            'rgb_file': f'{i:04d}.jpg', 'depth_file': f'{i:04d}.f32',
            'rgb_sha256': hashlib.sha256(encoded).hexdigest(), 'depth_sha256': hashlib.sha256(raw).hexdigest()}
        (folder/row['rgb_file']).write_bytes(encoded)
        (folder/row['depth_file']).write_bytes(raw)
        rows.append(row)
    (folder/'frames.json').write_text(json.dumps({'frames': rows}), encoding='utf8')
    return rows


def test_fixed_plane_matches_ground_truth_and_does_not_fill_missing_window(tmp_path):
    o3d = pytest.importorskip('open3d')
    import numpy as np
    inputs, result = tmp_path/'input', tmp_path/'result'
    plane_sequence(inputs)
    report = reconstruct(inputs, result, pose_mode='fixed')
    assert len(report['accepted']) == 5 and not report['rejected']
    assert report['trajectory_extent_m'] == [0, 0, 0]
    data = json.loads((result/'surface.json').read_text())
    p = np.array(data['positions']).reshape(-1, 3)*[1, -1, -1]
    assert np.abs(p[:, 2]-.15*p[:, 0]-.1*p[:, 1]-1).max() < .012
    px, py = p[:, 0]/p[:, 2]*85+48, p[:, 1]/p[:, 2]*85+40
    assert not ((px > 39) & (px < 57) & (py > 31) & (py < 49)).any()
    ply = o3d.io.read_triangle_mesh(str(result/'surface.ply'))
    assert np.max(np.abs(np.asarray(ply.vertices)-np.array(data['positions']).reshape(-1, 3))) < .000006
    assert max(data['indices']) < data['count'] <= 100000
    assert report['temporal_filter']['unseen_pixels_filled'] == 0


def test_session_hash_and_acquisition_gap_are_never_silently_fused(tmp_path):
    pytest.importorskip('open3d')
    folder = tmp_path/'input'
    rows = plane_sequence(folder)
    (folder/'0000.f32').write_bytes(b'changed')
    with pytest.raises(ValueError):
        load_frame(folder, rows[0])
    rows[-1]['session'] = 'new-session'
    (folder/'frames.json').write_text(json.dumps({'frames': rows}))
    with pytest.raises(ValueError, match='session changed'):
        reconstruct(folder, tmp_path/'cross-session')
    rows[-1]['session'] = rows[0]['session']
    rows[2]['meta']['host_acquired_monotonic'] = 20
    (folder/'frames.json').write_text(json.dumps({'frames': rows}))
    with pytest.raises(ValueError, match='Fewer than three'):
        reconstruct(folder, tmp_path/'gap', pose_mode='fixed')
    rejected = json.loads((tmp_path/'gap/report.json').read_text())['rejected']
    assert any('gap exceeds' in r['reason'] for r in rejected)


def test_api_rejects_foreign_writes_arbitrary_paths_and_modified_artifacts(tmp_path):
    service = PhoneVision(tmp_path, tmp_path/'data')
    app = FastAPI()
    install(app, repo=tmp_path, data_dir=tmp_path/'data', service=service,
            allowed_origins=['http://localhost:8770'])
    client = TestClient(app)
    api = '/api/platform/phone-vision/dense'
    assert client.post(api+'/jobs', json={'device': 'iphone', 'fixed': True},
                       headers={'Origin': 'https://outside.example'}).status_code == 403
    assert client.post(api+'/jobs', json={'device': 'iphone', 'fixed': True, 'path': 'arbitrary'}).status_code == 422
    assert client.post(api+'/jobs', json={'device': 'iphone', 'fixed': False}).status_code == 422
    assert client.get(api+'/unknown/report.json').status_code == 404
    identifier = 'a'*32
    folder = tmp_path/'data/dense'/identifier/'result'
    folder.mkdir(parents=True)
    path = folder/'surface.json'
    path.write_text('{"schema":"test"}')
    value = path.read_bytes()
    (folder/'manifest.json').write_text(json.dumps({'surface.json': {'bytes': len(value), 'sha256': hashlib.sha256(value).hexdigest()}}))
    assert client.get(api+'/'+identifier+'/surface.json').status_code == 200
    assert client.get(api+'/'+identifier+'/job.json').status_code == 404
    path.write_text('{"schema":"evil"}')
    assert client.get(api+'/'+identifier+'/surface.json').status_code == 422
    assert client.get(api).json()['motion_enabled'] is False


def test_windows_transient_replace_denial_keeps_previous_json_until_success(tmp_path, monkeypatch):
    import os
    path = tmp_path/'frames.json'
    atomic(path, {'previous': True})
    original, calls = os.replace, []
    def briefly_locked(source, destination):
        calls.append(destination)
        if len(calls) <= 2:
            assert json.loads(path.read_text()) == {'previous': True}
            raise PermissionError('Windows reader or indexer holds the old file')
        return original(source, destination)
    monkeypatch.setattr('deployment.phone_vision.dense.os.replace', briefly_locked)
    atomic(path, {'new': True})
    assert json.loads(path.read_text()) == {'new': True}
    assert len(calls) == 3 and not list(tmp_path.glob('*.tmp'))


def test_fieldmap_live_selects_iphone_with_the_original_freshness_guard(tmp_path):
    from deployment.fieldmap.integration import install as fieldmap
    from .test_integration import message
    clock = [100.]
    service = PhoneVision(tmp_path, tmp_path/'data', clock=lambda: clock[0])
    service.states['iphone'].update(enabled=True, session='first', phase='starting')
    service._publish('iphone', 'first', message())
    app = FastAPI()
    install(app, repo=tmp_path, data_dir=tmp_path/'data', service=service)
    fieldmap(app, data_dir=tmp_path/'map')
    client = TestClient(app)
    path = '/api/platform/fieldmap/phone-space/live?device=iphone&session=first&seq=1'
    value = client.get(path)
    assert value.status_code == 200 and value.json()['coverage']['in_range'] == 2
    clock[0] += 1.01
    assert client.get(path).status_code == 409
    assert client.get(path.replace('device=iphone', 'device=unknown')).status_code == 422
