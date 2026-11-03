"""Fast grasp: arm model, tracker, wrist-view checks, the state machine on the simulated robot, service and routes."""
import json
import threading
import time

import cv2
import numpy as np
import pytest

from deployment.grasp import arm
from deployment.grasp.controller import DEFAULTS, GraspController
from deployment.grasp.sim import SimExecutor, SimObserver, SimWorld, calibrate
from deployment.grasp.tracker import TemplateTracker
from deployment.grasp.vision import in_jaws, propose, ref_x


# ---- arm model -------------------------------------------------------------
def test_ik_fk_round_trip_matches_the_2026_11_03_poses():
    assert arm.ik(23.0, -6.6, 105) == (2700, 2232, 1675), 'The level approach pose used on the robot'
    for x, z, t3 in [(23, -2, 108), (29, -6.6, 105), (27, 0, 148)]:
        tip = arm.fk(*arm.ik(x, z, t3))['tip']
        assert abs(tip[0] - x) < 0.15 and abs(tip[1] - z) < 0.15
    with pytest.raises(arm.Unreachable):
        arm.ik(36, -6.6, 105)
    assert arm.lateral_to_pan(1.0, 27) < 0, 'Moving left lowers the pan raw'


def test_planned_paths_never_dip_below_the_desk_margin():
    w = SimWorld()
    for start in [(2037, 2211, 955, 2245, 2124, 1340), (2041, 1162, 2921, 2366, 2124, 1340)]:
        w.q.update(zip(('pan', 'lift', 'elbow', 'pitch', 'roll', 'grip'), start))
        c = GraspController(SimExecutor(w), None, config={'desk_z': -8.0})
        c._update(w.joints())
        path = c._plan(c.survey_joints())
        here = (c.q['lift'], c.q['elbow'], c.q['pitch'])
        for a, b in zip([here] + path, path):
            assert arm.segment_min_z(a, b)[0] >= -8.0 + DEFAULTS['path_clearance'] - 0.05
    assert len(path) >= 3, 'From folded the elbow unfolds first, then upright and the high pose'


# ---- tracker / vision -------------------------------------------------------
def test_tracker_follows_the_object_through_an_approach():
    w = SimWorld()
    q0 = arm.ik(23.0, -6.6, 105)
    w.q.update(lift=q0[0], elbow=q0[1], pitch=q0[2], grip=2045)
    ob = SimObserver(w)
    ob.select_exact()
    worst = worst_top = 0.0
    for x in np.arange(23.0, 30.6, 0.5):
        q = arm.ik(float(x), -6.6, 105)
        w.apply({'left:2': q[0], 'left:3': q[1], 'left:4': q[2]})
        state = ob.observe()
        true = w.bbox()
        assert state['ok'], f'Lost at x={x}'
        worst = max(worst, abs(state['bbox'][0] + state['bbox'][2]/2 - (true[0] + true[2]/2)))
        if true[1] + true[3] < 400:
            worst_top = max(worst_top, abs(state['bbox'][1] + state['bbox'][3] - true[1] - true[3]))
    assert worst < 12, f'Centre error {worst:.1f}px'
    assert worst_top < 25, f'Near-edge error {worst_top:.1f}px before the tips (used for the at-tips event)'
    assert true[1] + true[3] > 460, 'Tracked up to the frame bottom'


def test_tracker_refuses_flat_boxes_and_flags_loss():
    t = TemplateTracker()
    flat = np.full((480, 640), 200, np.uint8)
    with pytest.raises(ValueError, match='纹理'):
        t.init(flat, [100, 100, 80, 80])
    textured = (np.random.default_rng(1).random((480, 640))*255).astype(np.uint8)
    t.init(textured, [200, 150, 90, 90])
    for _ in range(8):
        state = t.update(flat)
    assert not state['ok'] and state['lost'] >= 5


def test_proposal_and_wrist_view_rules():
    w = SimWorld(texture=0.25)          # the real desk is plain white
    q = arm.ik(23.0, -2.0, 108)
    w.q.update(lift=q[0], elbow=q[1], pitch=q[2], grip=2045)
    gray = cv2.cvtColor(w.render(), cv2.COLOR_BGR2GRAY)
    box, true = propose(cv2.GaussianBlur(gray, (0, 0), 1.2)), w.bbox()
    assert box is not None
    assert abs(box[0] + box[2]/2 - (true[0] + true[2]/2)) < 25 and abs(box[1] + box[3]/2 - (true[1] + true[3]/2)) < 25
    cfg = dict(DEFAULTS)
    assert ref_x(300, cfg) == 395 and ref_x(470, cfg) == 440 and ref_x(385, cfg) == pytest.approx(417.5)
    assert in_jaws([355, 225, 245, 255], cfg) and not in_jaws([345, 150, 180, 225], cfg), 'Real 2026-11-03 boxes: in / at the tips'


# ---- state machine on the simulated robot ------------------------------------
def rehearse(**world):
    cfg = {**calibrate({k: v for k, v in world.items() if k in ('size', 'desk_z')}), 'hold_s': 0}
    w = SimWorld(**world)
    ex, ob = SimExecutor(w), SimObserver(w)
    c = GraspController(ex, ob, config=cfg, sleep=lambda s: None)
    assert c.run_survey()['ok']
    ob.select_exact()
    return w, ex, ob, c, cfg


def test_pick_and_place_returns_the_object_and_ends_upright():
    w, ex, ob, c, _ = rehearse()
    start = w.obj.copy()
    result = c.run_pick_place()
    assert result['ok'] and result['attempts'] == 1, c.events[-3:]
    phases = [p['phase'] for p in c.phases]
    for name in ('align', 'lower', 'advance', 'close', 'squeeze', 'lift', 'hold', 'place', 'release', 'retreat', 'home', 'done'):
        assert name in phases
    lifted = [e for e in c.events if e['kind'] == 'verify']
    assert lifted[-1]['held'] is True
    assert np.linalg.norm(w.obj[:2] - start[:2]) < 1.5 and w.held is None and abs(w.obj[2] - w.desk_z) < 1e-6
    assert (w.q['lift'], w.q['elbow'], w.q['pitch']) == pytest.approx(arm.UPRIGHT) and w.q['grip'] == 1340
    assert not ex.active and ex.ended == '完成'
    assert all(not (s['contact'] or {}) for s in ex.steps), 'No obstruction on the nominal path'


def test_sagging_shoulder_and_a_placement_offset():
    w, ex, ob, c, _ = rehearse(sag=30)
    c.cfg['place_lateral_cm'] = 4.0
    start = w.obj.copy()
    result = c.run_pick_place()
    assert result['ok'], c.events[-4:]
    assert 2.5 < w.obj[1] - start[1] < 5.5, 'Placed about 4 cm to the left'


def test_an_object_out_of_reach_stops_the_advance_and_lifts_clear():
    w, ex, ob, c, _ = rehearse(obj=(35.0, 0.0))
    result = c.run_pick_place()
    assert not result['ok'] and ('够取' in result['error'] or '推进次数' in result['error'])
    assert w.held is None and arm.fk(w.q['lift'], w.q['elbow'], w.q['pitch'])['tip'][1] >= -8.0 + 1.0
    assert not ex.active


def test_a_low_object_below_the_jaws_ends_as_an_empty_grip():
    w, ex, ob, c, _ = rehearse(size=(3.0, 3.0, 1.2))
    result = c.run_pick_place()
    assert not result['ok'] and '空夹' in result['error']
    assert w.q['grip'] > DEFAULTS['grip_empty'] - 1 and w.held is None


def test_a_stop_request_aborts_through_the_executor():
    w, ex, ob, c, _ = rehearse()
    stop = threading.Event()
    c.stop_event = stop
    original = ex.step
    def step(*a, **k):
        if len(ex.steps) >= 6:
            stop.set()
        return original(*a, **k)
    ex.step = step
    result = c.run_pick_place()
    assert not result['ok'] and c.phase == 'stopped' and ex.stopped == '收到停止请求'


def test_rehearsal_inserts_without_closing_and_goes_home():
    w, ex, ob, c, _ = rehearse()
    c.cfg['stop_after'] = 'inserted'
    start = w.obj.copy()
    result = c.run_pick_place()
    assert result['ok'] and result['rehearsal']
    assert not any(s['targets'].get('left:6', 9999) < DEFAULTS['grip_open'] - 5 for s in ex.steps[:-1]), 'No closing before home'
    assert any(e['kind'] == 'inserted' for e in c.events) and np.allclose(w.obj, start) and w.held is None
    assert (w.q['lift'], w.q['elbow'], w.q['pitch']) == pytest.approx(arm.UPRIGHT)


# ---- service and routes -------------------------------------------------------
class Feed:
    def __init__(self, world, camera):
        self.world, self.camera, self.seq, self.t = world, camera, 0, None
        self.jpeg = None

    def tick(self):
        ok, data = cv2.imencode('.jpg', self.world.render())
        self.jpeg, self.t = data.tobytes(), time.monotonic(); self.seq += 1

    def start(self):
        self.tick()

    def stop(self):
        pass

    def latest(self, max_age=1.5):
        return None if self.jpeg is None else (self.jpeg, self.t, self.seq)

    def status(self):
        return {'camera': self.camera, 'mode': 'test', 'seq': self.seq, 'age_ms': 0, 'error': None}


@pytest.fixture
def service(tmp_path):
    from deployment.grasp.service import GraspService
    w = SimWorld()
    q = arm.ik(23.0, -2.0, 108)
    w.q.update(lift=q[0], elbow=q[1], pitch=q[2], grip=2045)
    ex = SimExecutor(w)
    ex.status = lambda: {'active': ex.active}
    s = GraspService(tmp_path, executor=ex, feeds={2: Feed(w, 2), 1: Feed(w, 1)})
    s.world = w
    yield s
    s.track_stop.set()


def test_service_validates_requests_and_records_keyframes(service):
    with pytest.raises(ValueError, match='腕部画面'):
        service.select([300, 220, 90, 110])
    service.start_cameras()
    with pytest.raises(ValueError, match='框'):
        service.select([1, 2, 3])
    with pytest.raises(ValueError, match='先在腕部画面中框选'):
        service.run(True)
    box = [round(v) for v in service.world.bbox()]
    assert service.select(box)['selected']['ok']
    with pytest.raises(ValueError, match='确认'):
        service.run(False)
    with pytest.raises(ValueError, match='place_lateral_cm'):
        service.run(True, {'place_lateral_cm': 30})
    with pytest.raises(ValueError, match='不可修改'):
        service.update_config({'grip_empty': 1000})
    assert service.update_config({'desk_z': -7.5})['desk_z'] == -7.5
    assert json.loads((service.root/'config.json').read_text(encoding='utf-8'))['desk_z'] == -7.5
    from deployment.grasp.service import RunRecorder
    rec = RunRecorder(service.root/'runs', service, 'survey')
    rec.keyframe('start'); rec.event({'kind': 'x'}); rec.finish({'ok': True}, {'phase': 'done'})
    run = service.runs()[0]
    files = run['keyframes'][0]['files']
    assert run['result'] == {'ok': True} and files[:2] == ['01-start-cam2.jpg', '01-start-cam1.jpg']
    assert service.run_file(run['id'], '01-start-cam2.jpg').is_file()
    with pytest.raises(ValueError):
        service.run_file('../x', 'a.jpg')
    status = service.status()
    assert status['cameras_on'] and status['config']['desk_z'] == -7.5 and 'gray' not in status['track']


def test_routes_are_local_only_and_never_proxy_motion():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from deployment.grasp.integration import install

    class Stub:
        overlay = None
        cameras_on = False
        def status(self):
            return {'ok': True}
        def run(self, confirmed, options):
            if confirmed is not True:
                raise ValueError('请确认')
            return {'started': options}
        def stop(self):
            return {'stopped': True}

    app = FastAPI()
    install(app, data_dir=None, allowed_origins=('http://studio',), service=Stub())
    client = TestClient(app)
    assert client.get('/api/platform/grasp/status').json() == {'ok': True}
    assert client.post('/api/platform/grasp/run', json={'confirmed': True}, headers={'Origin': 'http://evil'}).status_code == 403
    assert client.post('/api/platform/grasp/run', json={}).status_code == 409
    assert client.post('/api/platform/grasp/run', json={'confirmed': True, 'options': {'end_pose': 'survey'}}).json() == {'started': {'end_pose': 'survey'}}
    assert client.post('/api/platform/grasp/run', content='x', headers={'Content-Type': 'text/plain'}).status_code == 415
    assert client.post('/api/platform/grasp/stop', json={}).json() == {'stopped': True}
    assert 'Content-Security-Policy' in client.get('/grasp/view').headers
    assert client.get('/grasp', follow_redirects=False).headers['location'] == '/#grasp'
    paths = {r.path for r in app.routes}
    assert not any('servo' in p or p.startswith('/api/servo') for p in paths), 'Motion only via the service, never a proxy'
