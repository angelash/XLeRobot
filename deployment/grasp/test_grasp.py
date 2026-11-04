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
    with pytest.raises(arm.Unreachable):
        arm.ik(9, -6.6, 105, min_bend=22)     # close to the body the single branch leaves the joint limits
    assert arm.ik(12, -6.6, 105, min_bend=22) == (2674, 2787, 1145)
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


def test_survey_from_the_live_folded_pose_splits_moves_under_the_step_limit():
    w = SimWorld()
    w.q.update(zip(('pan', 'lift', 'elbow', 'pitch', 'roll', 'grip'), (1995, 1154, 2919, 2246, 2124, 1348)))
    ex = SimExecutor(w)
    c = GraspController(ex, SimObserver(w), config={'desk_z': -8.0})
    c.run_survey()
    assert c.result['ok'], c.result
    assert any('（1/' in s['label'] for s in ex.steps), 'The 1062-count lift to upright is split'
    assert c.phase == 'ready'


@pytest.mark.parametrize('start', [(2007, 2216, 959, 2244, 2124, 1347), (1995, 1154, 2919, 2246, 2124, 1348),
                                   (2007, 2451, 2262, 1949, 2124, 2040)])
def test_survey_is_entered_from_behind_and_above_a_standing_object(start):
    """2026-11-03: a forward arc down to a 6 cm survey knocked over a plug standing just ahead of the tips."""
    w = SimWorld()
    w.q.update(zip(('pan', 'lift', 'elbow', 'pitch', 'roll', 'grip'), start))
    ex = SimExecutor(w)
    c = GraspController(ex, SimObserver(w), config={'desk_z': -8.0})
    tips, step = [], ex.step
    def recording(targets, **k):
        entry = step(targets, **k)
        tips.extend(arm.fk(q['lift'], q['elbow'], q['pitch'])['tip'] for q in w.trajectory)
        return entry
    ex.step = recording
    c.run_survey()
    assert c.result['ok'], c.result
    x, z = DEFAULTS['survey_x'], -8.0 + DEFAULTS['survey_height']
    assert not [t for t in tips if t[0] > x + 0.3 and t[1] < z - 0.2], 'Never below the survey height ahead of the tips'
    assert arm.fk(c.q['lift'], c.q['elbow'], c.q['pitch'])['tip'][1] >= -8.0 + 8.5


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
    assert json.loads((service.root/'config.json').read_text(encoding='utf-8')) == {'overrides': {'desk_z': -7.5}}
    from deployment.grasp.service import GraspService
    legacy = {**{k: DEFAULTS[k] for k in ('desk_z', 'insert_cm')}, 'survey_height': 6.0, 'clear_bottom': 335.0, 'approach_clearance': 2.0}
    (service.root/'config.json').write_text(json.dumps(legacy), encoding='utf-8')
    again = GraspService(service.root, executor=service.executor, feeds={})
    assert again.config['survey_height'] == DEFAULTS['survey_height'] and again.config['clear_bottom'] == DEFAULTS['clear_bottom']
    assert again.config['approach_clearance'] == 2.0, 'A real operator change in an old file is kept'
    again.track_stop.set()
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


# ---- v2: chassis reposition, make room, knowledge and local finder ------------------------------
AUTH = '2026-11-03 用户：目前机器人向后移动10-20厘米完全没问题'


def test_reposition_backs_the_chassis_away_in_legs():
    w = SimWorld(obj=(21.0, 0.0))
    ex = SimExecutor(w)
    c = GraspController(ex, SimObserver(w), config={'reverse_authorization': AUTH})
    c._update(w.joints())
    result = c.run_reposition(12)
    assert result['ok'] and [l['requested_cm'] for l in result['legs']] == [5.0, 5.0, 2.0]
    assert w.obj[0] == pytest.approx(33.0)
    with pytest.raises(ValueError, match='额度'):
        ex.start(); [ex.base(-0.05, AUTH) for _ in range(2)]


def test_reposition_needs_the_users_words_and_stops_on_a_stall():
    w = SimWorld(obj=(21.0, 0.0))
    c = GraspController(SimExecutor(w), SimObserver(w), config={})
    c._update(w.joints())
    assert not c.run_reposition(5)['ok'] and w.obj[0] == 21.0
    w.base_block_cm = 2.0
    c = GraspController(SimExecutor(w), SimObserver(w), config={'reverse_authorization': AUTH})
    c._update(w.joints())
    result = c.run_reposition(10)
    assert not result['ok'] and '受阻' in result['error'] and len(result['legs']) == 1


@pytest.mark.parametrize('authorized', [True, False])
def test_an_object_too_close_for_the_arm_makes_room_with_the_chassis(authorized):
    # One arm back-off is not enough here; the chassis makes up the rest, so the object stays within reach.
    cfg = {**calibrate({}), 'hold_s': 0, 'arm_backoff_max': 1, 'stop_after': 'inserted',
           **({'reverse_authorization': AUTH} if authorized else {})}
    w = SimWorld(obj=(26.0, 0.0))
    ex, ob = SimExecutor(w), SimObserver(w)
    c = GraspController(ex, ob, config=cfg, sleep=lambda s: None)
    assert c.run_survey()['ok']
    ob.select_exact()
    result = c.run_pick_place()
    bases = [s for s in ex.steps if 'base' in s]
    if authorized:
        assert result['ok'] and result['rehearsal'], c.events[-4:]
        kinds = [e['kind'] for e in c.events]
        assert bases and kinds.index('base') < kinds.index('inserted'), 'Arm back-off, then the chassis, then the insertion'
        assert w.obj[0] == pytest.approx(29.0)
    else:
        assert not result['ok'] and '后退授权' in result['error'] and not bases


def test_knowledge_learns_a_view_and_finds_it_again_after_the_object_moves(tmp_path):
    from deployment.grasp.knowledge import Knowledge, iou
    w = SimWorld()
    q = arm.ik(23.0, 1.0, 108)
    w.q.update(lift=q[0], elbow=q[1], pitch=q[2], grip=2045)
    ob = SimObserver(w)
    k = Knowledge(tmp_path)
    assert not k.locate(ob.gray())['ok'], 'Nothing known yet: abstain'
    box = w.bbox()
    k.learn('plug', ob.gray(), box, 'operator', {'approach_clearance': 2.0}, run='r1')
    w.obj = w.obj + np.array([1.0, 1.5, 0.0])
    hit = k.locate(ob.gray())
    assert hit['ok'] and hit['object'] == 'plug' and iou(hit['bbox'], w.bbox()) > 0.6, hit
    k.record('plug', True, 'operator', run='r1', llm_calls=1)
    k.record('plug', True, 'local:plug', run='r2', agreement=0.8, llm_calls=0)
    again = Knowledge(tmp_path).summary()['objects']['plug']
    assert again['templates'] == 1 and again['by_source'] == {'llm': {'attempts': 1, 'successes': 1}, 'local': {'attempts': 1, 'successes': 1}}
    assert again['params']['approach_clearance'] == 2.0 and again['agreement_ok_rate'] == 1.0
    with pytest.raises(ValueError):
        k.learn('Plug!', ob.gray(), box, 'operator')


def test_service_learns_after_a_large_model_box_and_then_selects_locally(service):
    service.start_cameras()
    box = [round(v) for v in service.world.bbox()]
    with pytest.raises(ValueError, match='知识库'):
        service.locate('plug')
    service.select(box)
    assert service.selection['source'] == 'operator'
    service.run_meta = {'object': 'plug', 'llm_calls': 1, 'agreement': None, 'selection': dict(service.selection)}
    from deployment.grasp.service import RunRecorder
    service.recorder = RunRecorder(service.root/'runs', service, 'pick-place')
    out = service._learn('pick-place', {'ok': True, 'steps': 30}, 42.0)
    assert out['learned']['templates'] == 1 and out['llm_calls'] == 1
    found = service.locate('plug')
    assert found['located']['ok'] and service.selection['source'] == 'local:template:plug'
    assert service.status()['knowledge']['objects']['plug']['by_source']['llm']['successes'] == 1


def test_http_executor_speaks_the_controller_contract_for_arm_and_chassis():
    import httpx
    from deployment.grasp.service import HttpExecutor
    seen = []
    def handler(request):
        body = json.loads(request.content or b'{}')
        seen.append((request.url.path, body))
        if request.url.path == '/api/servo/session':
            return httpx.Response(200, json={'session_id': 'abc'})
        if request.url.path == '/api/servo/base' and body['distance_m'] > 0:
            return httpx.Response(409, json={'detail': '底盘步骤只接受后退'})
        return httpx.Response(200, json={'joints': {}, 'base': {'stopped_by': 'target'}})
    ex = HttpExecutor('http://ctl', client=httpx.Client(transport=httpx.MockTransport(handler)))
    ex.start('t')
    assert ex.base(-0.05, AUTH, '后退')['base']['stopped_by'] == 'target'
    with pytest.raises(Exception, match='只接受后退'):
        ex.base(0.05, AUTH)
    assert seen[1] == ('/api/servo/base', {'session_id': 'abc', 'distance_m': -0.05, 'authorization': AUTH, 'label': '后退'})


def test_local_find_prefers_the_workstation_model_and_falls_back(service):
    service.start_cameras()
    box = [round(v) for v in service.world.bbox()]
    service.select(box)
    service.run_meta = {'object': 'plug', 'llm_calls': 1, 'agreement': None, 'selection': dict(service.selection)}
    from deployment.grasp.service import RunRecorder
    service.recorder = RunRecorder(service.root/'runs', service, 'pick-place')
    service._learn('pick-place', {'ok': True, 'steps': 30}, 40.0)

    class Fabric:
        def __init__(self, result=None, error=None):
            self.result, self.error, self.calls = result, error, []
        def submit(self, capability, payload, route='auto', deadline_s=30, source='api'):
            self.calls.append((capability, route, len(payload['queries_b64'])))
            if self.error:
                raise ValueError(self.error)
            return {'id': 'j1', 'status': 'done', 'result': self.result, 'node': 'ws', 'total_ms': 1400}
        def wait(self, job_id, wait_s):
            raise AssertionError('already final')
    fabric = Fabric({'ok': True, 'score': 0.83, 'bbox': [box[0] + 2, box[1], box[2], box[3]], 'query': 0, 'ms': 1300})
    service.fabric_provider = lambda: fabric
    found = service.locate('plug')
    assert fabric.calls == [('vision.find_by_example', 'worker_only', 1)]
    assert found['located']['finder'] == 'owlvit' and service.selection['source'] == 'local:owlvit:plug'
    fabric = Fabric({'ok': False, 'score': 0.21, 'bbox': [10, 10, 40, 40], 'query': 0, 'ms': 1300})
    service.fabric_provider = lambda: fabric
    with pytest.raises(ValueError, match='owlvit最高分0.21'):
        service.locate('plug')
    service.fabric_provider = lambda: Fabric(error='没有在线的工作端节点提供vision.find_by_example')
    found = service.locate('plug')                      # model unavailable: the classical matcher on the same scene
    assert found['located']['finder'] == 'template' and 'model_error' in found['located']


def test_example_finder_and_worker_register_only_with_a_model(tmp_path):
    from deployment.grasp.example_finder import ExampleFinder, in_jaws
    from deployment.autonomy.worker import Providers
    assert not ExampleFinder(tmp_path).available()
    assert in_jaws([200, 380, 40, 40]) and not in_jaws([380, 150, 120, 100])
    p = Providers(None, 'tiny-not-cached', owl_model=str(tmp_path))
    assert 'vision.find_by_example' not in p.available and p.details['vision.find_by_example']['available'] is False


def test_survey_finder_rejects_both_jaws_and_clipped_boxes():
    from deployment.grasp.example_finder import usable_box
    assert not usable_box([79.3, 304.4, 556.1, 175.3]), 'Actual three-frame false positive'
    assert not usable_box([100, 305, 510, 165]), 'Spans both jaws even without clipping'
    assert not usable_box([403, 310, 147, 88]), 'Too close: centre is inside the right jaw zone'
    assert usable_box([380, 150, 120, 100])


def test_laptop_rechecks_unsafe_worker_box_before_selection(service, monkeypatch):
    service.knowledge.data['objects']['plug'] = {'templates': []}
    monkeypatch.setattr(service, '_locate_by_model', lambda name: {
        'ok': True, 'score': 0.55, 'bbox': [100, 305, 510, 165], 'finder': 'owlvit'})
    with pytest.raises(ValueError, match='跨越双夹爪'):
        service.locate('plug')
    assert service.selection is None

