"""Vision-skill executor on the fake STS bus: bounded steps, persistent squeeze, refusals, stop, lease, hazards, API."""
import time
import pytest
from fabrication.assembly.debug_console.servo_session import ServoSession
from fabrication.assembly.debug_console.test_limit_selftest import add_stops, reg
from fabrication.assembly.debug_console.test_whole_calibration import runtime  # noqa: F401 (fixture)
from fabrication.assembly.debug_console.torque_control import word


class Clock:
    def __init__(self):
        self.t = 100.0

    def __call__(self):
        return self.t


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def servo(runtime, tmp_path, clock):
    runtime.limit_test.tick = 0; runtime.limit_test.require_pad = False; runtime.limit_test.recover_wait = 0
    runtime.pad_time = time.monotonic()
    return ServoSession(runtime, tmp_path/'servo', clock=clock, watchdog=False)


def test_steps_move_within_limits_and_a_squeeze_survives_later_steps(runtime, servo):
    sid = servo.start('测试', True)['session_id']
    entry = servo.step(sid, {'left:2': 2200, 'left:3': 1900}, speed_deg=20)
    assert entry['contact'] is None and entry['joints']['left:2']['position'] == 2200
    assert word(reg(runtime, 'COM3', 3), 56) == 1900 and runtime.limit_test.running
    # A gripper pressing on an object stops short of its goal; the goal stays ahead (squeeze).
    add_stops(runtime, {('COM3', 6): (1900, 2300)})
    squeeze = servo.step(sid, {'left:6': 1850}, torque=700, speed_deg=10)
    assert squeeze['contact'] is None, 'A 50-count lead is an arrived squeeze, not a stall'
    assert word(reg(runtime, 'COM3', 6), 42) == 1850 and word(reg(runtime, 'COM3', 6), 48) == 700
    servo.step(sid, {'left:2': 2100}, speed_deg=20)
    assert word(reg(runtime, 'COM3', 6), 42) == 1850, 'Later steps keep the squeeze goal'
    servo.end(sid, '完成')
    assert not servo.active and not runtime.limit_test.running
    assert word(reg(runtime, 'COM3', 6), 42) == 1900, 'Ending holds the gripper where it is'
    assert word(reg(runtime, 'COM3', 6), 48) == 1000, 'Torque ceiling restored'
    assert (servo.dir/'steps.jsonl').read_text(encoding='utf-8').count('\n') == 3 and (servo.dir/'result.json').is_file()


def test_refusals_leave_the_robot_untouched(runtime, servo, monkeypatch):
    from fabrication.assembly.debug_console import servo_session
    monkeypatch.setattr(servo_session, 'MAX_STEP_S', 5.0)
    with pytest.raises(ValueError, match='确认'):
        servo.start('x', False)
    runtime.limit_test.running = True
    with pytest.raises(ValueError, match='正在进行'):
        servo.start('x', True)
    runtime.limit_test.running = False
    sid = servo.start('x', True)['session_id']
    with pytest.raises(ValueError, match='超出当前限位'):
        servo.step(sid, {'left:2': 2400})
    with pytest.raises(ValueError, match='底盘|助手运行不驱动'):
        servo.step(sid, {'COM4:9': 2000})
    with pytest.raises(ValueError, match='会话编号'):
        servo.step('other', {'left:2': 2100})
    with pytest.raises(ValueError, match='预计超过'):
        servo.step(sid, {'left:2': 2290, 'left:3': 1810}, speed_deg=3)
    assert word(reg(runtime, 'COM3', 2), 56) == 2050 and servo.active
    with pytest.raises(ValueError, match='已有'):
        servo.start('again', True)


def test_a_blocked_step_is_held_reported_and_the_session_continues(runtime, servo):
    sid = servo.start('x', True)['session_id']
    add_stops(runtime, {('COM3', 2): (1800, 2100)})
    entry = servo.step(sid, {'left:2': 2250}, torque=450, speed_deg=10)
    c = entry['contact']
    assert c['joint'] == 'left:2' and c['position'] == 2100 and c['from'] == 2050
    assert word(reg(runtime, 'COM3', 2), 42) == 2100, 'Held where it stopped'
    assert servo.active and servo.snapshot()['contacts'][0]['joint'] == 'left:2'
    assert servo.step(sid, {'left:2': 2000}, speed_deg=10)['contact'] is None


def test_stop_ends_the_session_and_further_steps_fail(runtime, servo):
    sid = servo.start('x', True)['session_id']
    servo.stop('测试停止')
    assert not servo.active and servo.ended_reason == '测试停止' and not runtime.limit_test.running
    with pytest.raises(ValueError, match='未开始或已结束'):
        servo.step(sid, {'left:2': 2100})


def test_lease_expiry_and_gamepad_input_end_the_session(runtime, servo, clock):
    sid = servo.start('x', True, lease_s=2)['session_id']
    clock.t += 1.5
    assert servo.watchdog_tick() is None
    servo.hold(sid)
    clock.t += 1.9
    assert servo.watchdog_tick() is None
    clock.t += 2.5
    assert '租约超时' in servo.watchdog_tick() and not servo.active
    servo.start('y', True)
    runtime.limit_test.require_pad = True
    runtime.limit_test._pad_active = lambda: True
    assert '手柄' in servo.watchdog_tick() and not servo.active


def test_overheating_after_a_step_ends_the_session(runtime, servo):
    sid = servo.start('x', True)['session_id']
    reg(runtime, 'COM3', 4)[63] = 51
    entry = servo.step(sid, {'left:2': 2100}, speed_deg=10)
    assert '温度' in entry['ended'] and not servo.active


def test_recovered_joint_temperature_does_not_end_the_session(runtime,servo,monkeypatch):
    sid=servo.start('temperature recovery',True)['session_id']
    original=servo.lt._read;injected=[]
    def once(port,i):
        value=original(port,i)
        if port=='COM3' and i==4 and not injected:
            injected.append(True);value=list(value);value[63]=92
        return value
    monkeypatch.setattr(servo.lt,'_read',once)
    joints=servo.hold(sid)['joints']
    assert servo._hazard(joints) is None and servo.active
    recovered=[j for j in joints.values() if j.get('temperature_check')]
    assert len(recovered)==1 and recovered[0]['temperature_check']['recovered']
    servo.end(sid)


def test_preparation_uses_fresh_temperature_after_a_bad_report(runtime,monkeypatch):
    capture=runtime.capture_profile
    def stale_report():
        result=capture()
        for port in runtime.report['ports']:
            for motor in port['motors']:motor['temperature']=92
        return result
    monkeypatch.setattr(runtime,'capture_profile',stale_report)
    prepared=runtime.prepare_hardware(True)
    assert len(prepared['ready'])==16 and not prepared['blocked']
    assert all(m['temperature_check']['reported']==92 and m['temperature_check']['fresh']<50
               for m in prepared['ready'])


def test_the_controller_api(runtime, servo, monkeypatch):
    from fastapi.testclient import TestClient
    from fabrication.assembly.debug_console import app as web
    monkeypatch.setattr(web, 'runtime', runtime)
    runtime.servo = servo
    client = TestClient(web.app)
    assert client.post('/api/servo/session', json={'label': 'api'}).status_code == 409
    sid = client.post('/api/servo/session', json={'label': 'api', 'confirmed': True}).json()['session_id']
    moved = client.post('/api/servo/step', json={'session_id': sid, 'targets': {'left:2': 2120}, 'speed_deg': 20}).json()
    assert moved['joints']['left:2']['position'] == 2120
    assert client.post('/api/servo/step', json={'session_id': sid, 'targets': {'left:2': 2999}}).status_code == 409
    assert client.get('/api/servo/status').json()['active']
    assert client.post('/api/servo/hold', json={'session_id': sid}).json()['active']
    assert client.post('/api/agent/run', json={'steps': [{'op': 'wait'}], 'confirmed': True}).status_code == 409
    assert not client.post('/api/servo/stop', json={}).json()['active']
    assert client.post('/api/servo/end', json={'session_id': sid}).status_code == 409


AUTH = '2026-11-03 用户：目前机器人向后移动10-20厘米完全没问题'


def test_explicit_fifty_cm_grant_is_scoped_durable_and_does_not_reset_usage(runtime, servo, tmp_path, verified_direction):
    from fabrication.assembly.debug_console.test_bounded_travel import moving_wheels
    auth = '2026-11-05 用户：移动可以向后移动50CM的，足够你后退转身'
    assert servo.reverse_budget(AUTH)['budget_m'] == .2
    for distance in (.6, float('nan'), True):
        with pytest.raises(ValueError):
            servo.grant_reverse(auth, distance, True)
    with pytest.raises(ValueError):
        servo.grant_reverse(auth, .5, False)
    assert servo.grant_reverse(auth, .5, True)['budget_m'] == .5
    sid = servo.start('50cm', True)['session_id']
    with pytest.raises(ValueError, match='接管'):
        servo.grant_reverse(auth, .5, True)
    moving_wheels(runtime)
    for _ in range(10):
        servo.base(sid, -.05, auth)
    with pytest.raises(ValueError, match='额度'):
        servo.base(sid, -.005, auth)
    servo.end(sid)
    again = ServoSession(runtime, tmp_path/'servo', watchdog=False)
    assert again.grant_reverse(auth, .5, True)['left_m'] == 0
    assert again.reverse_budget(AUTH)['budget_m'] == .2
    with pytest.raises(ValueError, match='不能修改'):
        again.grant_reverse(auth, .3, True)
    # Legacy rows predate granted_m. Registering again cannot inflate their old budget.
    from fabrication.assembly.debug_console.whole_calibration import atomic_json
    rows = again._reverse_used()
    rows[AUTH] = {'used_m': .05, 'legs': 1}
    atomic_json(again.root/'base-authorizations.json', rows)
    with pytest.raises(ValueError, match='不能修改'):
        again.grant_reverse(AUTH, .5, True)
    assert again.grant_reverse(AUTH, .2, True)['left_m'] == .15


def test_reverse_legs_stay_inside_the_user_budget_and_a_stall_is_a_contact(runtime, servo, tmp_path, verified_direction):
    from fabrication.assembly.debug_console.test_bounded_travel import moving_wheels
    sid = servo.start('让位', True)['session_id']
    for bad, match in [(dict(distance_m=0.03, authorization=AUTH), '前方'), (dict(distance_m=-0.06, authorization=AUTH), '每段'),
                       (dict(distance_m=-0.03, authorization='好'), '授权')]:
        with pytest.raises(ValueError, match=match):
            servo.base(sid, **bad)
    moving_wheels(runtime)
    entry = servo.base(sid, -0.05, AUTH)
    assert entry['base']['stopped_by'] == 'target' and entry['contact'] is None
    assert entry['base']['wheel_distance_m_estimate'] < 0 and entry['budget']['used_m'] == 0.05
    assert servo.active, 'Arms stay held; the session continues'
    for _ in range(3):
        servo.base(sid, -0.05, AUTH)
    with pytest.raises(ValueError, match='额度'):
        servo.base(sid, -0.01, AUTH)
    servo.end(sid)
    again = ServoSession(runtime, tmp_path/'servo', watchdog=False)
    assert again.reverse_budget(AUTH)['left_m'] == 0, 'The budget survives a restart'


def test_a_blocked_reverse_leg_is_reported_as_base_contact(runtime, servo, verified_direction):
    from fabrication.assembly.debug_console.test_bounded_travel import moving_wheels
    moving_wheels(runtime, increment=20, cap=200)
    sid = servo.start('让位', True)['session_id']
    entry = servo.base(sid, -0.05, AUTH)
    assert entry['base']['stopped_by'] == 'stall' and entry['contact']['joint'] == 'base'
    assert servo.active and servo.snapshot()['contacts'][-1]['joint'] == 'base'


@pytest.fixture
def verified_direction(tmp_path, monkeypatch):
    import json
    from fabrication.assembly.debug_console import rotation_scan
    path = tmp_path/'chassis-direction.json'
    path.write_text(json.dumps({'camera_forward_sign': 1, 'evidence': ['test']}), encoding='utf-8')
    monkeypatch.setattr(rotation_scan, 'CHASSIS_DIRECTION', path)
    return path


def test_reverse_needs_a_camera_verified_direction_and_counts_net_backward_travel(runtime, servo, tmp_path, monkeypatch):
    import json
    from fabrication.assembly.debug_console import rotation_scan
    from fabrication.assembly.debug_console.test_bounded_travel import moving_wheels
    monkeypatch.setattr(rotation_scan, 'CHASSIS_DIRECTION', tmp_path/'missing.json')
    sid = servo.start('让位', True)['session_id']
    with pytest.raises(ValueError, match='相机核实'):
        servo.base(sid, -0.05, AUTH)
    (tmp_path/'servo').mkdir(exist_ok=True)
    (tmp_path/'servo'/'base-authorizations.json').write_text(json.dumps({AUTH: {'used_m': 0.15, 'legs': 3, 'forward_correction_m': 0.15}}), encoding='utf-8')
    assert servo.reverse_budget(AUTH)['left_m'] == 0.2, 'Legs that went forward by a sign error do not use the backward budget'


def test_forward_legs_need_a_clear_view_and_stay_inside_the_net_range(runtime, servo, verified_direction):
    from fabrication.assembly.debug_console.test_bounded_travel import moving_wheels
    moving_wheels(runtime)
    sid = servo.start('红线内往返', True)['session_id']
    servo.base(sid, -0.05, AUTH)
    servo.base(sid, -0.05, AUTH)
    assert servo.reverse_budget(AUTH)['net_m'] == -0.1
    entry = servo.base(sid, 0.05, AUTH, forward_clear=True)
    assert entry['base']['wheel_distance_m_estimate'] > 0 and entry['budget']['net_m'] == -0.05
    servo.base(sid, 0.05, AUTH, forward_clear=True)
    assert servo.reverse_budget(AUTH)['net_m'] == 0 and servo.reverse_budget(AUTH)['left_m'] == 0.2
    assert servo.reverse_budget(AUTH)['forward_left_m'] == 0, 'Not past the place the user put the robot'
    with pytest.raises(ValueError, match='前方0cm'):
        servo.base(sid, 0.01, AUTH, forward_clear=True)

