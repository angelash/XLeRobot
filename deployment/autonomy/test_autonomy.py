"""Synthetic-data tests: geometry accuracy, routing/fallback/leases, learning ledger."""
import base64
import json
import math

import cv2
import numpy as np
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from deployment.autonomy import capabilities as caps, protocols, stereo
from deployment.autonomy.compute import AuthError, ComputeFabric
from deployment.autonomy.integration import install
from deployment.autonomy.learning import Ledger, linfit

K = np.array([[330., 0, 322], [0, 328., 238], [0, 0, 1]])
DIST = np.array([-0.21, 0.05, 0.001, -0.0005, 0.0])


def poses(count, seed=3):
    rng = np.random.default_rng(seed)
    for _ in range(count):
        yield (rng.uniform(-0.35, 0.35, 3), np.array([rng.uniform(-0.12, 0.02), rng.uniform(-0.1, 0.0), rng.uniform(0.35, 0.6)]))


def project(points, rvec, tvec, K=K, dist=DIST, noise=0.1, seed=0):
    pixels, _ = cv2.projectPoints(points, rvec, tvec, K, dist)
    return pixels.reshape(-1, 2)+np.random.default_rng(seed).normal(0, noise, (len(points), 2))


def test_intrinsics_recover_known_camera():
    obj = stereo.board_points(9, 6, 0.025)
    views = [project(obj, r, t, seed=i) for i, (r, t) in enumerate(poses(15))]
    result = stereo.calibrate_intrinsics([obj]*15, views, (640, 480))
    fitted = np.array(result['K'])
    assert result['rms_px'] < 0.2
    assert abs(fitted[0, 0]-K[0, 0])/K[0, 0] < 0.01 and abs(fitted[1, 2]-K[1, 2]) < 3


def stereo_setup(baseline=0.24, toe=np.radians(8)):
    R, _ = cv2.Rodrigues(np.array([0, -toe, 0]))
    T = np.array([-baseline, 0, 0.01])
    obj = stereo.board_points(9, 6, 0.025)
    left, right = [], []
    for i, (rvec, tvec) in enumerate(poses(14, seed=9)):
        tvec = tvec+np.array([0.1, 0, 0])
        left.append(project(obj, rvec, tvec, seed=i))
        Rb, _ = cv2.Rodrigues(rvec)
        rr, _ = cv2.Rodrigues(R @ Rb)
        right.append(project(obj, rr, R @ tvec+T, seed=100+i))
    intr = {'K': K.tolist(), 'dist': DIST.tolist()}
    return obj, left, right, intr, R, T


def test_stereo_calibration_and_triangulation_accuracy():
    obj, left, right, intr, R, T = stereo_setup()
    calib = stereo.stereo_calibrate([obj]*len(left), left, right, intr, intr, (640, 480))
    assert abs(calib['baseline_m']-np.linalg.norm(T)) < 0.003 and calib['rms_px'] < 0.3
    target = np.array([[0.05, 0.03, 0.45], [-0.08, -0.02, 0.62]])
    lp, _ = cv2.projectPoints(target, np.zeros(3), np.zeros(3), K, DIST)
    rp, _ = cv2.projectPoints(target, cv2.Rodrigues(R)[0], T, K, DIST)
    measured = stereo.triangulate(calib, lp.reshape(-1, 2), rp.reshape(-1, 2))
    for point, truth in zip(measured['points'], target):
        assert np.linalg.norm(np.array(point['xyz_m'])-truth) < 0.004
        assert point['in_front'] and max(point['reprojection_px']) < 0.5 and 0 < point['depth_sigma_m'] < 0.005


def test_depth_uncertainty_matches_formula():
    assert stereo.depth_sigma(0.4, 300, 0.25, 0.5) == pytest.approx(0.16/75*0.5)
    with pytest.raises(ValueError):
        stereo.depth_sigma(1, 0, 0.2)


def test_rendered_board_detection_feeds_calibration_capability():
    images, Ks = [], np.array([[320., 0, 320], [0, 320., 240], [0, 0, 1]])
    for rvec, tvec in poses(8, seed=21):
        image = stereo.render_chessboard(9, 6, 0.025, Ks, rvec*0.6, tvec)
        images.append(base64.b64encode(cv2.imencode('.png', image)[1].tobytes()).decode())
    detected = caps.run('vision.chessboard', {'images': images[:2], 'pattern': [9, 6]})
    assert detected['found'] == 2 and len(detected['views'][0]['corners']) == 54
    corners = np.array(detected['views'][0]['corners'])
    assert corners[0].sum() <= corners[-1].sum()
    result = caps.run('vision.calibrate_intrinsics', {'images': images, 'pattern': [9, 6], 'square_m': 0.025})
    assert len(result['used_views']) >= 6 and abs(result['K'][0][0]-320)/320 < 0.03 and result['rms_px'] < 0.5


def test_ground_homography_maps_pixels_to_floor():
    # Head camera 0.6 m high, pitched down 50 degrees, looking along robot +x.
    pitch = np.radians(50)
    R_wc = np.array([[0, -1, 0], [-np.sin(pitch), 0, -np.cos(pitch)], [np.cos(pitch), 0, -np.sin(pitch)]])
    cam = np.array([0, 0, 0.6])
    floor = np.array([[x, y, 0] for x in (0.3, 0.5, 0.8) for y in (-0.2, 0, 0.2)])
    pixels, _ = cv2.projectPoints(floor, cv2.Rodrigues(R_wc)[0], -R_wc @ cam, K, np.zeros(5))
    fit = caps.run('vision.ground_homography', {'image_px': pixels.reshape(-1, 2).tolist(), 'floor_m': floor[:, :2].tolist()})
    assert fit['rms_m'] < 1e-6
    probe = np.array([[0.65, -0.1, 0]])
    pp, _ = cv2.projectPoints(probe, cv2.Rodrigues(R_wc)[0], -R_wc @ cam, K, np.zeros(5))
    out = caps.run('vision.ground_project', {'H': fit['H'], 'pixels': pp.reshape(-1, 2).tolist()})['floor_m'][0]
    assert math.dist(out, probe[0, :2]) < 1e-4


def test_apriltag_pose_distance():
    f, length, depth = 330.0, 0.08, 0.5
    side = int(round(f*length/depth))
    tag = cv2.aruco.generateImageMarker(cv2.aruco.getPredefinedDictionary(cv2.aruco.DICT_APRILTAG_36h11), 2, side)
    scene = np.full((480, 640), 255, np.uint8)
    scene[240-side//2:240-side//2+side, 320-side//2:320-side//2+side] = tag
    data = base64.b64encode(cv2.imencode('.png', scene)[1].tobytes()).decode()
    found = caps.run('vision.markers', {'image': data, 'marker_length_m': length,
                                        'intrinsics': {'K': [[f, 0, 320], [0, f, 240], [0, 0, 1]], 'dist': [0]*5}})
    assert [m['id'] for m in found['markers']] == [2]
    assert found['markers'][0]['distance_m'] == pytest.approx(depth, rel=0.03)


def test_benchmark_reports_timings_and_sanity_checks():
    result = caps.run('system.benchmark', {'repeats': 1})
    assert result['checks']['chessboard_found'] and result['checks']['apriltag_ids'] == [3]
    assert set(result['timings']) >= {'sgbm_disparity_640x480', 'chessboard_detect_640x480'}


def test_disparity_depth_on_synthetic_pair():
    K_, left, right = caps._synthetic_pair()
    calib = {'left': {'K': K_.tolist(), 'dist': [0]*5}, 'right': {'K': K_.tolist(), 'dist': [0]*5},
             'R': np.eye(3).tolist(), 'T': [-0.1, 0, 0], 'image_size': [640, 480]}
    result = stereo.disparity_depth(calib, left, right, roi=[300, 230, 40, 30])
    assert result['depth_m'] is not None and 0.35 < result['depth_m'] < 0.6


# ---- compute fabric --------------------------------------------------------
class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def wait_final(fabric, job_id):
    return fabric.wait(job_id, 10)


def test_routes_local_fallback_and_worker_only_without_nodes(tmp_path):
    fabric = ComputeFabric(tmp_path, start=False)
    done = wait_final(fabric, fabric.submit('system.echo', {'message': 'hi'}, 'local')['id'])
    assert done['status'] == 'done' and done['node'] == 'local' and done['result']['message'] == 'hi'
    fallback = wait_final(fabric, fabric.submit('system.echo', {}, 'prefer_worker')['id'])
    assert fallback['status'] == 'done' and fallback['fallback'] == '没有在线的工作端节点'
    missing = fabric.submit('speech.transcribe', {}, 'auto')
    assert missing['status'] == 'failed' and missing['route'] == 'worker_only'
    with pytest.raises(ValueError):
        fabric.submit('robot.drive', {}, 'local')
    assert fabric.token_path.read_text().strip() == fabric.token and len(fabric.token) >= 32
    with pytest.raises(AuthError):
        fabric.check_token('wrong')
    fabric.close()


def test_worker_assignment_completion_and_late_results(tmp_path):
    clock = Clock()
    fabric = ComputeFabric(tmp_path, start=False, clock=clock)
    fabric.register({'node_id': 'ws', 'capabilities': ['system.echo', 'llm.chat'], 'max_parallel': 1})
    job = fabric.submit('system.echo', {'message': 'x'}, 'prefer_worker', deadline_s=5)
    assert job['status'] == 'queued'
    task = fabric.poll('ws', 0)
    assert task['job_id'] == job['id'] and task['payload'] == {'message': 'x'}
    assert fabric.complete('ws', job['id'], True, {'v': 1}, run_ms=3)['accepted']
    assert fabric.get(job['id'])['result'] == {'v': 1}
    assert not fabric.complete('ws', job['id'], True, {'v': 2})['accepted']
    late = fabric.submit('llm.chat', {}, 'auto', deadline_s=1)
    fabric.poll('ws', 0)
    clock.t += 2
    assert not fabric.complete('ws', late['id'], True, {'text': 'late'})['accepted']
    assert fabric.get(late['id'])['status'] == 'expired' and fabric.get(late['id'])['result'] is None
    fabric.close()


def test_lost_worker_and_busy_queue_fall_back_to_laptop(tmp_path):
    clock = Clock()
    fabric = ComputeFabric(tmp_path, start=False, clock=clock)
    fabric.register({'node_id': 'ws', 'capabilities': ['system.echo', 'llm.chat'], 'max_parallel': 1})
    lost = fabric.submit('system.echo', {'message': 'a'}, 'prefer_worker', deadline_s=30)
    fabric.poll('ws', 0)
    queued = fabric.submit('system.echo', {'message': 'b'}, 'prefer_worker', deadline_s=30)
    clock.t += 2.5
    fabric.sweep()
    assert wait_final(fabric, queued['id'])['fallback'] == '工作端繁忙，转笔记本执行'
    clock.t += 7
    fabric.sweep()
    result = wait_final(fabric, lost['id'])
    assert result['status'] == 'done' and result['node'] == 'local' and '失联' in result['fallback']
    only = fabric.submit('llm.chat', {}, 'worker_only', deadline_s=30)
    assert only['status'] == 'failed'
    # A late answer proves the node is alive again, but the job already moved.
    assert not fabric.complete('ws', lost['id'], True, {})['accepted']
    fabric.close()


def test_retryable_worker_failure_falls_back_and_deadline_expires(tmp_path):
    clock = Clock()
    fabric = ComputeFabric(tmp_path, start=False, clock=clock)
    fabric.register({'node_id': 'ws', 'capabilities': ['system.echo'], 'max_parallel': 2})
    job = fabric.submit('system.echo', {'message': 'r'}, 'prefer_worker', deadline_s=30)
    fabric.poll('ws', 0)
    assert fabric.complete('ws', job['id'], False, error='ImportError', retryable=True)['fallback']
    assert wait_final(fabric, job['id'])['status'] == 'done'
    stale = fabric.submit('system.echo', {}, 'worker_only', deadline_s=1)
    clock.t += 1.5
    fabric.sweep()
    assert fabric.get(stale['id'])['status'] == 'expired'
    fabric.close()


# ---- learning ---------------------------------------------------------------
def wheel_sample(distance, radius=0.065, noise=0.0):
    counts = round(abs(distance)/radius/(2*math.pi)*4096)
    sign = 1 if distance > 0 else -1
    return {'inputs': {'op': 'travel', 'requested_distance_m': distance},
            'observed': {'wheel_delta_raw': {'9': -sign*counts, '10': sign*(counts+3)}},
            'truth': {'kind': 'tape_measure', 'unit': 'm', 'value': distance+noise}}


def test_linfit_and_wheel_model_lifecycle(tmp_path):
    fit = linfit([1, 2, 3, 4], [2.1, 3.9, 6.1, 8.0])
    assert fit['slope'] == pytest.approx(1.98, abs=0.05) and fit['loo_rmse'] > 0
    ledger = Ledger(tmp_path)
    first = ledger.create_session('W2', environment={'floor': '地毯'})
    for d, n in [(0.05, 0.001), (0.03, -0.0005), (-0.05, 0.0008), (-0.03, 0), (0.05, -0.001)]:
        ledger.add_sample(first['id'], wheel_sample(d, noise=n))
    ledger.add_sample(first['id'], {'inputs': {}, 'observed': {'wheel_delta_raw': {'9': -900, '10': 900}},
                                    'truth': {'kind': 'tape_measure', 'unit': 'm', 'value': 0.2}, 'quality': 'outlier'})
    model = ledger.fit('wheel_distance', [first['id']])
    assert model['status'] == 'candidate' and len(model['samples']) == 5
    assert model['parameters']['radius_m'] == pytest.approx(0.065, rel=0.03)
    assert model['parameters']['counts_per_5cm'] == pytest.approx(0.05/0.065/(2*math.pi)*4096, rel=0.03)
    with pytest.raises(ValueError):
        ledger.validate(model['id'], [first['id']])
    second = ledger.create_session('W2')
    for d in (0.05, -0.05, 0.03):
        ledger.add_sample(second['id'], wheel_sample(d))
    with pytest.raises(ValueError):
        ledger.review(model['id'], 'adopt', 'user')
    assert ledger.validate(model['id'], [second['id']])['status'] == 'validated'
    with pytest.raises(ValueError):
        ledger.review(model['id'], 'adopt', 'assistant')
    assert ledger.review(model['id'], 'adopt', 'user')['status'] == 'adopted'
    rotation = ledger.create_session('W3')
    for yaw in (10, 20, -15, 30):
        arc = abs(math.radians(yaw))*0.26/2
        counts = round(arc/0.065/(2*math.pi)*4096)
        ledger.add_sample(rotation['id'], {'inputs': {'op': 'rotate'}, 'observed': {'wheel_delta_raw': {'9': counts, '10': counts}},
                                           'truth': {'kind': 'marker_vision', 'unit': 'deg', 'value': yaw}})
    turned = ledger.fit('wheel_rotation', [rotation['id']])
    assert turned['parameters']['track_m'] == pytest.approx(0.26, rel=0.02) and turned['parameters']['radius_source'].startswith('adopted:')


def test_truth_quality_and_closed_session_rules(tmp_path):
    ledger = Ledger(tmp_path)
    session = ledger.create_session('G1')
    with pytest.raises(ValueError):
        ledger.add_sample(session['id'], {'inputs': {}, 'observed': {}, 'truth': {'kind': 'guess', 'unit': 'm', 'value': 1}})
    sample = ledger.add_sample(session['id'], {'inputs': {}, 'observed': {'measured_distance_m': 0.41}})
    assert sample['truth'] is None
    ledger.set_truth(session['id'], sample['id'], {'kind': 'tape_measure', 'unit': 'm', 'value': 0.4})
    ledger.set_truth(session['id'], sample['id'], {'kind': 'tape_measure', 'unit': 'm', 'value': 0.405})
    stored = ledger.session(session['id'])['samples'][0]
    assert stored['truth']['value'] == 0.405 and len(stored['truth_history']) == 1
    ledger.close_session(session['id'], 'interrupted', '手柄接管')
    with pytest.raises(ValueError):
        ledger.add_sample(session['id'], {'inputs': {}, 'observed': {}})
    with pytest.raises(KeyError):
        ledger.session('../escape')


def make_run(root, run_id='20261103-101500'):
    folder = root/'2026-11-03'/'agent-runs'/run_id
    folder.mkdir(parents=True)
    (folder/'plan.json').write_text(json.dumps({'steps': [{'op': 'travel', 'distance_m': 0.05, 'max_raw': 220},
                                                           {'op': 'snapshot'}, {'op': 'travel', 'distance_m': 0.05}]}))
    trace = [{'t': 0.1*i, 'counts': {'9': 20*i, '10': 20*i}} for i in range(1, 12)]
    (folder/'result.json').write_text(json.dumps({'error': None, 'results': [
        {'step': 0, 'op': 'travel', 'status': 'done', 'wheel_delta_raw': {'9': -510, '10': 515}, 'target_counts': 513,
         'travel_trace': trace, 'stop_trace': [{'after_zero_ms': 230}], 'requested_distance_m': 0.05},
        {'step': 1, 'op': 'snapshot', 'status': 'done', 'shots': [{'camera': 1, 'file': '01-a-cam1.jpg'}, {'camera': 2, 'error': 'x'}],
         'positions': {'COM3:1': 2000}},
        {'step': 2, 'op': 'travel', 'status': 'failed', 'message': '直行中轮10状态异常', 'rotation': {'travel_trace': trace[:3], 'fault': {'temperature': 57}}}]}))
    image = cv2.imencode('.jpg', np.full((48, 64, 3), 128, np.uint8))[1].tobytes()
    (folder/'01-a-cam1.jpg').write_bytes(image)
    return run_id


def test_import_run_keeps_failures_and_photos(tmp_path):
    run_id = make_run(tmp_path/'evidence')
    ledger = Ledger(tmp_path/'learning', tmp_path/'evidence')
    session = ledger.create_session('W2')
    imported = ledger.import_run(session['id'], run_id)
    stored = ledger.session(session['id'])
    assert imported['samples'] == ['s001', 's002'] and stored['samples'][0]['observed']['counts_per_s'] == pytest.approx(200, rel=0.05)
    assert stored['samples'][1]['quality'] == 'invalid' and stored['samples'][1]['observed']['fault']['temperature'] == 57
    assert stored['photos'][0]['files'] == [{'camera': 1, 'file': '01-a-cam1.jpg'}]
    with pytest.raises(ValueError):
        ledger.import_run(session['id'], run_id)
    with pytest.raises(ValueError):
        ledger.import_run(session['id'], '../../x')


def test_protocol_catalogue_matches_document():
    from pathlib import Path
    doc = (Path(__file__).resolve().parents[2]/'docs'/'autonomy'/'LEARNING_PROTOCOLS_ZH.md').read_text(encoding='utf-8')
    ids = [p['id'] for p in protocols.PROTOCOLS]
    assert len(ids) == len(set(ids)) == 17
    assert all(f'| {i} ' in doc for i in ids)
    assert {p['model_kind'] for p in protocols.PROTOCOLS if p['model_kind']} <= set(protocols.VALIDATION)
    fixture = json.loads((Path(__file__).with_name('test_protocols.json')).read_text(encoding='utf-8'))
    assert fixture == protocols.PROTOCOLS, 'Regenerate test_protocols.json for the page test'


# ---- HTTP integration ----------------------------------------------------------
@pytest.fixture
def api(tmp_path):
    app = FastAPI()
    run_id = make_run(tmp_path/'evidence')
    fabric, ledger = install(app, data_dir=tmp_path/'data', evidence_root=tmp_path/'evidence',
                             allowed_origins=('http://192.168.137.150:8770',), start=False)
    with TestClient(app) as client:
        yield client, fabric, ledger, run_id
    fabric.close()


def test_http_jobs_worker_protocol_and_security(api):
    client, fabric, ledger, run_id = api
    r = client.post('/api/platform/autonomy/jobs', json={'capability': 'vision.quality', 'route': 'local', 'wait_s': 10,
                                                         'payload': {'image': {'run_id': run_id, 'file': '01-a-cam1.jpg'}}})
    assert r.status_code == 200 and r.json()['status'] == 'done' and r.json()['result']['width'] == 64
    bad = client.post('/api/platform/autonomy/jobs', json={'capability': 'vision.quality', 'route': 'local',
                                                           'payload': {'image': {'run_id': run_id, 'file': '../secret.jpg'}}})
    assert bad.status_code == 422
    foreign = client.post('/api/platform/autonomy/jobs', json={'capability': 'system.echo'}, headers={'Origin': 'http://evil.example'})
    assert foreign.status_code == 403
    assert client.post('/api/platform/autonomy/worker/register', json={'node_id': 'ws'}).status_code == 401
    headers = {'X-Autonomy-Token': fabric.token}
    assert client.post('/api/platform/autonomy/worker/register', headers=headers,
                       json={'node_id': 'ws', 'capabilities': ['system.echo']}).status_code == 200
    job = client.post('/api/platform/autonomy/jobs', json={'capability': 'system.echo', 'route': 'worker_only', 'payload': {'message': 'm'}}).json()
    task = client.post('/api/platform/autonomy/worker/poll', headers=headers, json={'node_id': 'ws', 'wait_s': 0}).json()['job']
    assert task['job_id'] == job['id']
    done = client.post('/api/platform/autonomy/worker/result', headers=headers,
                       json={'node_id': 'ws', 'job_id': job['id'], 'ok': True, 'result': {'echo': 'm'}, 'run_ms': 1}).json()
    assert done['accepted'] and client.get('/api/platform/autonomy/jobs/'+job['id']).json()['result'] == {'echo': 'm'}
    status = client.get('/api/platform/autonomy/status').json()
    assert status['nodes'][0]['node_id'] == 'ws' and status['local']['resources']['memory']['total_mb'] > 0
    assert client.post('/api/platform/autonomy/worker/poll', headers=headers, json={'node_id': 'ghost', 'wait_s': 0}).status_code == 404
    view = client.get('/autonomy/view')
    assert "script-src 'self'" in view.headers['content-security-policy'] and '/autonomy/assets/app.js' in view.text
    assert client.get('/autonomy/print/checkerboard-9x6-25mm.pdf').content.startswith(b'%PDF')
    assert client.get('/autonomy/print/..%2Fsecret.pdf').status_code == 404


def test_http_learning_flow_and_calibration_from_job(api):
    client, fabric, ledger, run_id = api
    session = client.post('/api/platform/autonomy/learning/sessions', json={'protocol': 'W2', 'note': '测试'}).json()
    imported = client.post(f"/api/platform/autonomy/learning/sessions/{session['id']}/import-run", json={'run_id': run_id}).json()
    assert imported['samples'] == ['s001', 's002']
    truth = client.post(f"/api/platform/autonomy/learning/sessions/{session['id']}/truth",
                        json={'sample_id': 's001', 'truth': {'kind': 'tape_measure', 'unit': 'm', 'value': 0.049}})
    assert truth.status_code == 200
    assert client.post('/api/platform/autonomy/learning/fit', json={'kind': 'wheel_distance', 'sessions': [session['id']]}).status_code == 422
    obj = stereo.board_points(9, 6, 0.025)
    views = [project(obj, r, t, seed=i).tolist() for i, (r, t) in enumerate(poses(6))]
    job = client.post('/api/platform/autonomy/jobs', json={'capability': 'vision.calibrate_intrinsics', 'route': 'local', 'wait_s': 20,
                                                           'payload': {'images_corners': views, 'image_size': [640, 480]}}).json()
    assert job['status'] == 'done'
    saved = client.post('/api/platform/autonomy/calibrations', json={'kind': 'intrinsics', 'camera': 'head', 'job_id': job['id']}).json()
    assert saved['meets_threshold'] and saved['source']['job_id'] == job['id']
    assert client.post(f"/api/platform/autonomy/calibrations/{saved['id']}/review", json={'action': 'adopt', 'reviewer': 'assistant'}).status_code == 422
    assert client.post(f"/api/platform/autonomy/calibrations/{saved['id']}/review", json={'action': 'adopt', 'reviewer': 'user'}).json()['status'] == 'adopted'
    overview = client.get('/api/platform/autonomy/learning').json()
    assert len(overview['protocols']) == 17 and overview['calibrations'][0]['status'] == 'adopted'
    assert 'checkerboard-9x6-25mm.pdf' in overview['print_assets'] and 'data' not in overview['calibrations'][0]


def test_printable_targets_have_a4_physical_size():
    from pathlib import Path
    import re
    assets = Path(__file__).resolve().parents[2]/'docs'/'autonomy'/'assets'
    for name in ('checkerboard-9x6-25mm.pdf', 'apriltag36h11-80mm.pdf'):
        data = (assets/name).read_bytes()
        box = [float(v) for v in re.search(rb'/MediaBox\s*\[\s*([\d.]+)\s+([\d.]+)\s+([\d.]+)\s+([\d.]+)', data).groups()]
        assert box[2] == pytest.approx(841.89, abs=1.5) and box[3] == pytest.approx(595.28, abs=1.5)
