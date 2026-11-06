"""Generated receipts exercise actual integration, provenance and loss boundaries."""
from io import BytesIO
import json
from pathlib import Path
import zipfile

from fastapi import FastAPI
from fastapi.testclient import TestClient
from PIL import Image
import pytest

from deployment.fieldmap.history import History
from deployment.fieldmap.history_import import timestamp, key
from deployment.fieldmap.integration import API, install
from deployment.fieldmap.store import atomic_json


START = timestamp('2026-11-05T23:59:58+08:00')


def jpeg():
    output = BytesIO()
    Image.new('RGB', (640, 480), '#496b50').save(output, 'JPEG')
    return output.getvalue()


def row(at, first, delta, **extra):
    return {'op': 'travel', 'at': at / 1000, 'duration_s': 1., 'command_sign': -1,
            'direction_source': 'camera_verified', 'wheel_start_raw': first,
            'wheel_delta_raw': delta,
            'stop_readback': {i: (first[i] + delta[i]) % 4096 for i in first},
            'command_raw': {i: 150 if delta[i] > 0 else -150 for i in delta}, **extra}


def fixture(root):
    """Two continuous opposite legs, one partial failure, one discontinuity."""
    evidence = root/'evidence'
    run = evidence/'20261105/agent-runs/20261105-235958'
    run.mkdir(parents=True)
    rows = [row(START + 1000, {'9': 4000, '10': 96}, {'9': 512, '10': -512}),
            row(START + 3000, {'9': 416, '10': 3680}, {'9': -256, '10': 256}, status='failed', message='generated contact stop'),
            row(START + 5000, {'9': 2000, '10': 2000}, {'9': 100, '10': 100}, op='rotate'),
            {'op': 'snapshot', 'at': (START+5500)/1000, 'capture_started_at': (START+5400)/1000,
             'shots': [{'camera': c, 'file': f'frame-cam{c}.jpg'} for c in (0, 1, 2)]}]
    result = {'label': 'Generated round trip', 'started_at': START/1000,
              'finished_at': (START+6000)/1000, 'results': rows, 'error': 'partial movement'}
    atomic_json(run/'result.json', result)
    for c in (0, 1, 2):
        (run/f'frame-cam{c}.jpg').write_bytes(jpeg())
    exports = root/'FieldTests/20261105/generated'
    exports.mkdir(parents=True)
    atomic_json(exports/'export.json', {'run_id': run.name})
    with zipfile.ZipFile(exports/'export.zip', 'w') as bundle:
        bundle.writestr('result.json', json.dumps(result))
        for c in (0, 1, 2):
            bundle.writestr(f'frame-cam{c}.jpg', jpeg())
    index = History(root/'map/history', evidence, root/'FieldTests', -1)
    return index, run, exports


def test_native_export_dedup_and_incremental_scan(tmp_path):
    history, _, _ = fixture(tmp_path)
    history.scan()
    assert history.status()['runs'] == 1
    assert history.status()['events'] == 4
    assert history.status()['photos'] == 3
    assert history.status()['latest_motion_ms'] == START + 5000
    assert history.scan()['unchanged'] == 2
    result = history.query(START, START+6000)
    assert result['summary']['translations'] == 2
    assert result['summary']['rotations'] == 1
    assert result['summary']['failed_events'] == 1
    assert result['motion_enabled'] is False
    assert 'authorization' not in json.dumps(result)
    # Full opposite raw counts across 4096 wrap, followed by a partial return.
    assert len(result['tracks']) == 2
    assert result['tracks'][0]['points'][-1]['x_m'] == pytest.approx(.024936, abs=.00001)
    assert result['tracks'][1]['points'][0]['x_m'] == 0
    assert result['tracks'][1]['points'][-1]['yaw_rad'] > 0


def test_narrow_window_keeps_origin_and_interpolates_boundaries(tmp_path):
    history, _, _ = fixture(tmp_path)
    history.scan()
    result = history.query(START+2200, START+2400)
    points = result['tracks'][0]['points']
    assert [p['ms'] for p in points] == [START+2200, START+2400]
    assert all(p['interpolated'] for p in points)
    assert points[0]['x_m'] > points[-1]['x_m'] > 0
    assert points[0]['event_id'] == result['events'][0]['id']


def test_broken_changed_source_retains_last_good_index_and_retries(tmp_path):
    history, run, _ = fixture(tmp_path)
    history.scan()
    (run/'result.json').write_text('{', encoding='utf-8')
    assert history.scan()['errors'] == 1
    assert history.status()['events'] == 4
    assert history.status()['coverage_issues']
    assert history.scan()['errors'] == 1


def test_photos_offsets_pagination_verified_thumbnail_and_id_only(tmp_path):
    history, _, _ = fixture(tmp_path)
    history.scan()
    result = history.photos(START, START+6000, limit=2)
    assert result['total'] == 3 and result['next_offset'] == 2
    assert len(history.photos(START, START+6000, offset=2)['items']) == 1
    nearest = history.photos(START, START+6000, camera='1', at=START+5000)['items'][0]
    assert nearest['offset_ms'] == 400 and nearest['time_basis'] == 'capture_group_start_estimate'
    assert 'container' not in nearest
    raw, media = history.image(nearest['id'], True)
    assert Image.open(BytesIO(raw)).size == (320, 240) and media == 'image/jpeg'
    assert history.image(nearest['id'], True)[0] == raw
    for bad in ('../private', 'a'*32, '0'*500):
        with pytest.raises(FileNotFoundError):
            history.image(bad)
    with pytest.raises(ValueError):
        history.safe_path(tmp_path/'private.jpg')


def test_control_zip_exact_photos_and_native_servo_dedup(tmp_path):
    history, _, exports = fixture(tmp_path)
    folder = Path(history.evidence_root)/'20261105/servo-sessions/abc123def456'
    folder.mkdir(parents=True)
    session = {'session_id': folder.name, 'started_at': START/1000, 'label': 'Generated servo'}
    step = {'n': 1, 'at': (START+1000)/1000, 'base': {},
            'base_full': row(START+1000, {'9': 0, '10': 0}, {'9': 512, '10': -512})}
    atomic_json(folder/'session.json', session)
    (folder/'steps.jsonl').write_text(json.dumps(step)+'\n', encoding='utf-8')
    with zipfile.ZipFile(exports/'capture-control.zip', 'w') as z:
        z.writestr('session.json', json.dumps(session))
        z.writestr('steps.jsonl', json.dumps(step))
    with zipfile.ZipFile(exports/'capture.zip', 'w') as z:
        z.writestr('log.json', json.dumps({'captures': [{'name': 'end', 'frames': [{'camera': 0, 'captured_at': (START+1234)/1000}]}]}))
        z.writestr('end-cam0.jpg', jpeg())
    history.scan()
    assert history.status()['runs'] == 2 and history.status()['events'] == 5
    p = history.photos(START, START+2000)['items'][0]
    assert p['at_ms'] == START+1234 and p['time_basis'] == 'frame_capture'
    assert history.image(p['id'])[0] == jpeg()


def test_api_range_errors_readonly_snapshots_and_photo_route(tmp_path):
    history, _, _ = fixture(tmp_path)
    root = history.root.parent
    atomic_json(root/'map.json', {'id': 'current', 'features': [], 'observations': []})
    atomic_json(root/'history/old-map.json', {'id': 'old', 'features': [], 'observations': []})
    app = FastAPI()
    install(app, data_dir=root, history_evidence_root=history.evidence_root, history_fieldtests_root=history.fieldtests_root,
            history_forward_sign=-1, allowed_origins=('http://localhost:8770',))
    app.state.fieldmap_history.scan()
    with TestClient(app) as client:
        p = {'start_ms': START, 'end_ms': START+6000}
        assert client.get(API+'/history/query', params=p).json()['summary']['motion_events'] == 3
        assert client.get(API+'/history/query', params={'start_ms': START+1, 'end_ms': START}).status_code == 422
        assert client.get(API+'/history/photos', params={**p, 'offset': -1}).status_code == 422
        result = client.get(API+'/history/nearest', params={**p, 'at_ms': START+5000}).json()
        photo = result['cameras']['0'][0]
        assert client.get(photo['thumbnail_url']).status_code == 200
        assert client.get('/fieldmap/history/photos/'+'f'*32).status_code == 404
        assert client.post(API+'/history/refresh', json={}, headers={'Origin': 'http://evil.example'}).status_code == 403
        sid = next(s['id'] for s in client.get(API+'/snapshots').json()['items'] if s['readonly'])
        assert client.get(API+'/map', params={'snapshot': sid}).json()['readonly'] is True
        assert client.get(API+'/map', params={'snapshot': '../private'}).status_code == 404
        assert client.post(API+'/review', json={'survey_id': 'old'}).status_code == 409


def test_missing_direction_retains_event_but_does_not_invent_pose(tmp_path):
    history, run, _ = fixture(tmp_path)
    result = json.loads((run/'result.json').read_text())
    for r in result['results']:
        r.pop('command_sign', None)
    atomic_json(run/'result.json', result)
    history.forward_sign = None
    history.scan()
    result = history.query(START, START+6000)
    assert not result['tracks'] and result['total_events'] == 4
    assert result['events'][0]['pose_unavailable_reason']


def test_unlinked_and_corrupt_archives_are_visible_without_losing_good_runs(tmp_path):
    history, _, exports = fixture(tmp_path)
    (exports/'unlinked.zip').write_bytes(b'unknown archive retained')
    (exports/'broken-control.zip').write_bytes(b'not a zip')
    scan = history.scan()
    assert scan['errors'] == 1 and scan['unlinked_archives'] == 1
    status = history.status()
    assert status['runs'] == 1 and status['events'] == 4
    assert {issue['state'] for issue in status['coverage_issues']} == {'error', 'unlinked'}
    assert history.scan()['errors'] == 1
