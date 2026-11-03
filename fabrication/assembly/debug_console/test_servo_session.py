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
