"""Browser control tests use actual STS packets on a fake bus, never hardware."""
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from fabrication.assembly.debug_console.test_preset_control import runtime
from fabrication.assembly.debug_console.test_original_teleop import pad
from fabrication.assembly.debug_console.torque_control import word
from fabrication.assembly.debug_console.web_teleop import WebTeleop, LEASE_S
from fabrication.assembly.debug_console.web_teleop_api import install


@pytest.fixture
def web(runtime):
    clock = [100.]
    runtime.gamepad = {'connected': False}
    control = runtime.web_control = WebTeleop(runtime, clock=lambda: clock[0], threaded=False)
    control.test_clock = clock
    return control


def advance(web, seconds=.1):
    web.test_clock[0] += seconds
    web.tick()


def test_start_needs_confirmation_but_no_physical_gamepad_and_only_latches_current_positions(web):
    with pytest.raises(ValueError, match='确认'):
        web.start(False)
    assert not any(p.writes for p in web.runtime.session.ports.values())
    started = web.start(True)
    assert started['active'] and len(started['session_id']) == 32
    assert 'session_id' not in web.snapshot()
    assert all(word(p.regs[i], 42) == 2050 for p in web.runtime.session.ports.values() for i in p.regs if i <= 8)
    assert all(word(web.runtime.session.ports['COM4'].regs[i], 46) == 0 for i in (9, 10))


def test_no_motion_until_input_then_release_holds_without_opening_gripper(web):
    token = web.start(True)['session_id']; r = web.runtime
    before = [len(p.writes) for p in r.session.ports.values()]
    advance(web)
    assert before == [len(p.writes) for p in r.session.ports.values()]
    web.input(token, 0, ['joint.2.plus'])
    advance(web)
    assert word(r.session.ports['COM3'].regs[2], 42) > 2050
    assert word(r.session.ports['COM3'].regs[6], 42) == 2050
    web.input(token, 1, [])
    assert not r.moving and web.active


def test_sequence_replay_does_not_renew_and_lease_expiry_invalidates_session(web):
    token = web.start(True)['session_id']
    web.input(token, 3, ['joint.1.plus']); advance(web)
    renewed = web.last_input
    with pytest.raises(ValueError, match='序号'):
        web.input(token, 2, ['joint.1.plus'])
    assert web.last_input == renewed
    advance(web, LEASE_S)
    assert not web.active and web.runtime.controller['controller'] == 'offline'
    with pytest.raises(ValueError, match='结束'):
        web.input(token, 4, ['joint.1.plus'])


def test_delayed_release_beats_older_held_packet_and_old_stop_cannot_end_new_session(web):
    first = web.start(True)['session_id']
    web.input(first, 2, [])
    with pytest.raises(ValueError):
        web.input(first, 1, ['base.forward'])
    advance(web)
    assert not web.runtime.wheels_commanded
    web.stop(session_id=first)
    second = web.start(True)['session_id']
    with pytest.raises(ValueError, match='不符'):
        web.stop(session_id=first)
    assert web.active and second != first


@pytest.mark.parametrize('busy', ['controller', 'selftest', 'manual_pose', 'agent', 'pad'])
def test_exclusive_with_existing_control_and_tasks(web, busy):
    r = web.runtime
    if busy == 'controller': r.controller['controller'] = 'online'
    if busy == 'selftest': r.limit_test.running = True
    if busy == 'manual_pose': r.manual_pose_active = True
    if busy == 'agent': r.agent.active = True
    if busy == 'pad': r.gamepad = pad(buttons=(0,))
    with pytest.raises(ValueError): web.start(True)
    assert not any(p.writes for p in r.session.ports.values())


def test_native_pad_input_stops_browser_without_taking_over(web):
    token = web.start(True)['session_id']
    web.input(token, 0, ['joint.1.plus']); advance(web)
    assert web.runtime.native_pad_event(pad(buttons=(7,)))
    assert not web.active and web.runtime.controller['controller'] == 'offline'
    assert not web.runtime.native_menu.pending


def test_base_up_matches_verified_upstream_forward_and_cap_then_release_zeroes_both_wheels(web):
    token = web.start(True)['session_id']; r = web.runtime
    web.input(token, 0, ['base.forward'], scale=1)
    advance(web)
    expected = r.wheel_command(-.04, 0)
    assert all(a * b > 0 for a, b in zip(r.wheel_goals, expected.values()))
    assert max(abs(n) for n in r.wheel_goals) <= 160
    web.input(token, 1, [])
    assert all(word(r.session.ports['COM4'].regs[i], 46) == 0 for i in (9, 10))


def test_stalled_wheel_ends_session_without_automatic_retry(web):
    token = web.start(True)['session_id']
    for seq in range(11):
        if not web.active: break
        web.input(token, seq, ['base.forward']); advance(web)
    assert not web.active and '未按目标方向' in web.reason
    assert not web.runtime.wheels_commanded


def test_persistent_low_voltage_stops_without_latching_old_input(web):
    token = web.start(True)['session_id']; r = web.runtime
    r.session.ports['COM3'].regs[2][62] = 102
    for seq in range(3):
        web.input(token, seq, ['joint.2.plus']); advance(web)
    assert not web.active and '电压' in web.reason


def test_wheel_asymmetry_stops_even_when_both_wheels_move_forward(web):
    token = web.start(True)['session_id']; r = web.runtime
    web.input(token, 0, ['base.forward']); advance(web)
    origin = web.base_window['positions']
    commands = r.wheel_goals
    for seq in range(1, 11):
        if not web.active: break
        for i, value, rate in ((9, commands[0], 1.), (10, commands[1], .4)):
            pos = round(origin[i] + value * seq * .1 * rate) % 4096
            r.session.ports['COM4'].regs[i][56:58] = [pos & 255, pos >> 8]
        web.input(token, seq, ['base.forward']); advance(web)
    assert not web.active and '比例' in web.reason


def test_hardware_fault_and_stall_stop_all_motion(web):
    token = web.start(True)['session_id']; r = web.runtime
    web.input(token, 0, ['joint.2.plus']); advance(web)
    r._stall(('COM3', 2), 2050, 2100)
    assert not web.active and not r.moving
    token = web.start(True)['session_id']
    r.session.ports['COM3'].regs[2][65] = 32
    web.input(token, 0, ['joint.2.plus']); advance(web)
    assert not web.active and '异常' in web.reason


def test_global_stop_invalidates_browser_session_immediately(web):
    token = web.start(True)['session_id']
    web.runtime.stop()
    assert not web.active
    with pytest.raises(ValueError): web.input(token, 0, ['base.forward'])


def test_ik_plane_step_preserves_pitch_and_uses_verified_gripper_endpoint_direction(web):
    r = web.runtime
    token = web.start(True)['session_id']
    r.kinematic_profile = {'joints': {}}
    for i, angle in ((2, 0), (3, 2), (4, -2), (6, 0)):
        motor = r.motors()[('COM3', i)]
        r.kinematic_profile['joints'][f'COM3:{i}'] = {
            **{k: motor[k] for k in ('min', 'max', 'homing_raw')}, 'verified': True,
            'reference_raw': 2050, 'reference_deg': angle, 'direction': 1,
            'open_raw': 2300, 'closed_raw': 1800}
    web.input(token, 0, ['arm.extend', 'arm.close']); advance(web)
    assert web.active
    assert word(r.session.ports['COM3'].regs[6], 42) < 2050
    angles = [a + (word(r.session.ports['COM3'].regs[i], 42)-2050)*360/4096 for i, a in ((2,0),(3,2),(4,-2))]
    assert abs(sum(angles)) < .3


def test_no_guessing_ik_or_gripper_reference_and_no_cross_arm_partial_ik_write(web):
    token = web.start(True)['session_id']; r = web.runtime
    web.input(token, 0, ['arm.extend'], arm='both')
    before = [len(p.writes) for p in r.session.ports.values()]
    advance(web)
    assert not web.active and '参考' in web.reason
    assert before == [len(p.writes) for p in r.session.ports.values()]


def test_device_limits_and_configured_arm_mapping_are_respected(web):
    r = web.runtime
    r.settings.data['arm_mapping'] = 'swapped'
    token = web.start(True)['session_id']
    for seq in range(80):
        web.input(token, seq, ['joint.1.plus']); advance(web)
    assert word(r.session.ports['COM3'].regs[1], 42) == 2050
    assert 2050 < word(r.session.ports['COM4'].regs[1], 42) <= 2300


def test_api_validation_rejects_nan_unknown_fields_old_session_and_foreign_origin_stays_on_controller(web):
    app = FastAPI(); install(app, lambda: web.runtime); client = TestClient(app)
    assert client.post('/api/web/start', json={}).status_code == 409
    token = client.post('/api/web/start', json={'confirmed': True}).json()['session_id']
    for body in ({'seq': True}, {'seq': 1, 'scale': 4}, {'seq': 1, 'actions': ['arbitrary.raw']},
                 {'seq': 1, 'raw': 1}, {'seq': 1, 'arm': 'COM4'}):
        assert client.post('/api/web/input', json={'session_id': token, **body}).status_code in (409, 422)
    assert client.get('/api/web/status').json()['active']
    assert 'session_id' not in client.get('/api/web/status').json()
