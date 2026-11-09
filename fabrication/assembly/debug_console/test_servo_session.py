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


def test_stationary_session_keeps_arms_held_parks_wheels_and_refuses_base(runtime,servo):
    sid=servo.start('stationary',True,stationary_base=True)['session_id']
    assert servo.snapshot()['stationary_base'] and servo.snapshot()['wheel_park']['verified']
    for i in (9,10):
        assert reg(runtime,'COM4',i)[40]==0 and word(reg(runtime,'COM4',i),46)==0
    servo.step(sid,{'left:2':2100},torque=450,speed_deg=12)
    with pytest.raises(ValueError,match='静止底盘会话禁止'):
        servo.base(sid,-.05,'explicit task movement')
    servo.end(sid)
    assert reg(runtime,'COM3',2)[40]==1
    assert all(reg(runtime,'COM4',i)[40]==0 for i in (9,10))
    assert (servo.dir/'wheel-park.json').is_file()


def test_stationary_drift_or_unexpected_wheel_torque_stops_session(runtime,servo):
    servo.start('stationary drift',True,stationary_base=True)
    r=reg(runtime,'COM4',10);p=word(r,56)+3;r[56]=p&255;r[57]=p>>8
    assert '编码器移动' in servo.watchdog_tick() and not servo.active
    servo.start('unexpected torque',True,stationary_base=True)
    reg(runtime,'COM4',9)[40]=1
    assert '状态变化' in servo.watchdog_tick() and not servo.active
    assert all(reg(runtime,'COM4',i)[40]==0 for i in (9,10))


def test_stationary_parking_attempts_both_wheels_after_one_write_failure(runtime,servo,monkeypatch):
    from fabrication.assembly.debug_console import servo_session
    original=servo_session.write_register;calls=[]
    def fail_one(port,i,address,value,size):
        calls.append((i,address))
        if i==9 and address==46:raise OSError('lost zero acknowledgement')
        return original(port,i,address,value,size)
    monkeypatch.setattr(servo_session,'write_register',fail_one)
    with pytest.raises(ValueError,match='停车写入失败'):
        servo.start('failed parking',True,stationary_base=True)
    assert (10,46) in calls and (9,40) in calls and (10,40) in calls
    assert not servo.active and not runtime.limit_test.running


def test_stationary_wheel_temperature_glitch_uses_new_same_motor_read(runtime,servo,monkeypatch):
    servo.start('stationary temperature',True,stationary_base=True)
    original=servo.lt._read;injected=[]
    def once(port,i):
        value=original(port,i)
        if (port,i)==('COM4',10) and not injected:
            injected.append(True);value=list(value);value[63]=92
        return value
    monkeypatch.setattr(servo.lt,'_read',once)
    assert servo.watchdog_tick() is None and servo.active
    assert (servo.dir/'wheel-temperature-checks.jsonl').is_file()
    servo.end(servo.session_id)


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
    parked=client.post('/api/servo/session',json={'label':'stationary api','confirmed':True,'stationary_base':True}).json()
    assert parked['stationary_base'] and parked['wheel_park']['verified']
    assert all(reg(runtime,'COM4',i)[40]==0 for i in (9,10))
    client.post('/api/servo/stop',json={})
    auth='user grants one metre from the same observed start'
    assert client.post('/api/servo/base-authorization',json={'authorization':auth,'distance_m':.5,'confirmed':True}).status_code==200
    assert client.post('/api/servo/base-authorization',json={'authorization':auth,'distance_m':1.,'confirmed':True}).status_code==409
    extended=client.post('/api/servo/base-authorization',json={'authorization':auth,'distance_m':1.,'confirmed':True,
        'previous_distance_m':.5,'extension_authorization':'User explicitly permits one metre behind original anchor'})
    assert extended.status_code==200 and extended.json()['budget_m']==1.


AUTH = '2026-11-03 用户：目前机器人向后移动10-20厘米完全没问题'


def test_explicit_fifty_cm_grant_is_scoped_durable_and_does_not_reset_usage(runtime, servo, tmp_path, verified_direction):
    from fabrication.assembly.debug_console.test_bounded_travel import moving_wheels
    auth = '2026-11-05 用户：移动可以向后移动50CM的，足够你后退转身'
    assert servo.reverse_budget(AUTH)['budget_m'] == .2
    for distance in (1.01, float('nan'), True):
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


def test_explicit_one_metre_extension_preserves_consumption_and_original_anchor(runtime, servo, tmp_path):
    from fabrication.assembly.debug_console.whole_calibration import atomic_json
    servo.grant_reverse(AUTH, .5, True)
    rows=servo._reverse_used()
    rows[AUTH].update(used_m=.5, forward_m=.03, legs=11)
    atomic_json(servo.root/'base-authorizations.json', rows)
    words='2026-11-09 用户：可以后退1米范围，可以转身，按箭头退出工位'
    for previous, distance, text in ((.3,1.,words),(.5,.5,words),(.5,1.,'')):
        with pytest.raises(ValueError):
            servo.grant_reverse(AUTH,distance,True,previous,text)
    assert servo._reverse_used()==rows
    extended=servo.grant_reverse(AUTH,1.,True,.5,words)
    assert extended['budget_m']==1. and extended['used_m']==.47 and extended['left_m']==.53
    again=ServoSession(runtime,tmp_path/'servo',watchdog=False)
    row=again._reverse_used()[AUTH]
    assert row['used_m']==.5 and row['forward_m']==.03 and row['legs']==11
    assert row['boundary']==rows[AUTH]['boundary'] and row['grant_at']==rows[AUTH]['grant_at']
    assert len(row['extensions'])==1 and row['extensions'][0]['authorization']==words
    assert again.grant_reverse(AUTH,1.,True)['left_m']==.53
    with pytest.raises(ValueError):
        again.grant_reverse(AUTH,1.,True,.5,words)
    with pytest.raises(ValueError):
        again.grant_reverse('new unrelated anchor',1.,True,.5,words)
    with pytest.raises(ValueError):
        again.grant_reverse(AUTH,1.01,True,1.,words)
    assert again.reverse_budget('unregistered')['budget_m']==.2


def test_authorization_consumption_survives_startup_date_change_and_corruption(runtime,tmp_path):
    from fabrication.assembly.debug_console.whole_calibration import atomic_json
    old=tmp_path/'2026-11-07'/'servo-sessions';old.mkdir(parents=True)
    new=tmp_path/'2026-11-09'/'servo-sessions';new.mkdir(parents=True)
    row={'granted_m':.5,'used_m':.72,'forward_m':.22,'legs':20}
    atomic_json(old/'base-authorizations.json',{AUTH:row})
    session=ServoSession(runtime,new,watchdog=False,authorization_root=tmp_path)
    assert session.reverse_budget(AUTH)['left_m']==0
    session.grant_reverse(AUTH,1.,True,.5,'User explicitly permits one metre behind original anchor')
    durable=session._reverse_used()[AUTH]
    assert durable['used_m']==.72 and durable['forward_m']==.22 and durable['legs']==20
    assert (tmp_path/'base-authorizations.json').exists()
    later=ServoSession(runtime,tmp_path/'2026-11-10'/'servo-sessions',watchdog=False,authorization_root=tmp_path)
    assert later.reverse_budget(AUTH)['left_m']==.5
    assert __import__('json').loads((old/'base-authorizations.json').read_text('utf8'))[AUTH]==row
    (tmp_path/'base-authorizations.json').write_text('{broken',encoding='utf8')
    with pytest.raises(ValueError,match='台账读取失败'):later.reverse_budget(AUTH)


def test_legacy_date_conflict_never_inflates_remaining_allowance(runtime,tmp_path):
    from fabrication.assembly.debug_console.whole_calibration import atomic_json
    for date,used in (('2026-11-07',.45),('2026-11-08',.1)):
        p=tmp_path/date/'servo-sessions';p.mkdir(parents=True)
        atomic_json(p/'base-authorizations.json',{AUTH:{'granted_m':.5,'used_m':used}})
    session=ServoSession(runtime,tmp_path/'2026-11-09'/'servo-sessions',authorization_root=tmp_path,watchdog=False)
    assert session.reverse_budget(AUTH)['left_m']==.05


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

