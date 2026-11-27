"""VR session tests: real STS packets on the fake bus, never hardware."""
import math
import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from fabrication.assembly.debug_console import vr_kinematics as vk
from fabrication.assembly.debug_console.test_preset_control import runtime
from fabrication.assembly.debug_console.test_original_teleop import pad
from fabrication.assembly.debug_console.torque_control import word
from fabrication.assembly.debug_console.web_teleop import WebTeleop, LEASE_S, VR_MAX_RATE_DEG_S
from fabrication.assembly.debug_console.web_teleop_api import install
from fabrication.assembly.debug_console import vr_policy

ANGLES = {2: 0., 3: 40., 4: -30.}


@pytest.fixture
def vr(runtime, monkeypatch, tmp_path):
    clock = [100.]
    runtime.gamepad = {'connected': False}
    control = runtime.web_control = WebTeleop(runtime, clock=lambda: clock[0], threaded=False)
    control.test_clock = clock
    monkeypatch.setattr(vr_policy, 'PATH', tmp_path / 'vr-policy.json')
    return control


def references(r, port='COM3', angles=ANGLES, grip=True):
    r.kinematic_profile = {'joints': {}}
    for i, angle in list(angles.items()) + ([(6, 0.)] if grip else []):
        motor = r.motors()[(port, i)]
        r.kinematic_profile['joints'][f'{port}:{i}'] = {
            **{k: motor[k] for k in ('min', 'max', 'homing_raw')}, 'verified': True,
            'reference_raw': 2050, 'reference_deg': angle, 'direction': 1,
            'open_raw': 2250, 'closed_raw': 1850}


def start(vr, refs=True):
    token = vr.start(True, 'vr')['session_id']
    if refs:
        references(vr.runtime)
    return token


class Feed:
    def __init__(self, vr, token):
        self.vr, self.token, self.seq = vr, token, 0

    def __call__(self, ticks=1, **state):
        for _ in range(ticks):
            self.vr.vr_input(self.token, self.seq, **state); self.seq += 1
            self.vr.test_clock[0] += .1
            self.vr.tick()
        return self.vr.snapshot()


def hand(engage, pos=(0., 0., 0.), pitch=0., roll=0., orient=True):
    return {'engage': engage, 'pos': pos, 'pitch': pitch, 'roll': roll, 'orient': orient}


def goal(r, i, port='COM3'):
    return word(r.session.ports[port].regs[i], 42)


def angles(r, port='COM3'):
    return {i: a + (goal(r, i, port) - 2050) * 360 / 4096 for i, a in ANGLES.items()}


def writes(r):
    return [len(p.writes) for p in r.session.ports.values()]


def test_vr_session_is_its_own_mode_and_starts_without_moving(vr):
    token = start(vr)
    assert vr.snapshot()['mode'] == 'vr' and vr.snapshot()['vr']['arms']['left']['state'] == 'idle'
    with pytest.raises(ValueError, match='VR'):
        vr.input(token, 0, ['joint.1.plus'])
    before = writes(vr.runtime)
    Feed(vr, token)(3)
    assert writes(vr.runtime) == before
    vr.stop(session_id=token)
    keys = vr.start(True)['session_id']
    with pytest.raises(ValueError, match='不是VR'):
        vr.vr_input(keys, 0)


def test_hand_forward_moves_jaw_forward_by_ratio_and_keeps_tool_pitch(vr):
    token = start(vr); feed = Feed(vr, token); r = vr.runtime
    feed(arms={'left': hand(1)}, scale=1)
    assert vr.snapshot()['vr']['arms']['left']['state'] == 'following'
    feed(30, arms={'left': hand(1, (.04, 0, 0))}, scale=1, ratio=.5)
    a = angles(r)
    start_jaw = vk.jaw_point(*ANGLES.values()); jaw = vk.jaw_point(a[2], a[3], a[4])
    assert abs(jaw[0] - start_jaw[0] - .02) < .002 and abs(jaw[1] - start_jaw[1]) < .002
    assert abs(sum(a.values()) - sum(ANGLES.values())) < .3
    assert goal(r, 1) == 2050 and goal(r, 5) == 2050


def test_steps_respect_axis_speed_and_follow_window(vr):
    token = start(vr); feed = Feed(vr, token); r = vr.runtime
    feed(arms={'left': hand(1)}, scale=1)
    feed(arms={'left': hand(1, (0, 0, .05))}, scale=1)
    step = 12 * .1 * 4096 / 360
    assert all(abs(goal(r, i) - 2050) <= step + 1 for i in (2, 3, 4))
    assert any(abs(goal(r, i) - 2050) > step - 2 for i in (2, 3, 4))
    # Speed register follows the per-axis setting; VR caps but never raises it.
    assert word(r.session.ports["COM3"].regs[2], 46) == int(12 * 4096 / 360)


def test_axis_setting_above_vr_cap_is_capped(vr):
    r = vr.runtime
    r.settings.data['joint_rate_deg_s'] = 90.
    token = start(vr); feed = Feed(vr, token)
    feed(arms={'left': hand(1)}, scale=1)
    feed(arms={'left': hand(1, (0, 0, .05))}, scale=1)
    assert word(r.session.ports['COM3'].regs[2], 46) == int(VR_MAX_RATE_DEG_S * 4096 / 360)


def test_regrip_reanchors_without_jump(vr):
    token = start(vr); feed = Feed(vr, token); r = vr.runtime
    feed(arms={'left': hand(1)}, scale=1)
    feed(10, arms={'left': hand(1, (0, 0, .03))}, scale=1)
    held = {i: goal(r, i) for i in range(1, 6)}
    feed(arms={'left': None}, scale=1)
    assert vr.snapshot()['vr']['arms']['left']['state'] == 'idle'
    before = writes(r)
    # The hand is now far from the first anchor; a new grip must not jump.
    feed(3, arms={'left': hand(2, (.3, -.2, .1), 40, 30)}, scale=1)
    assert {i: goal(r, i) for i in range(1, 6)} == held
    assert writes(r) == before


def test_lateral_hand_turns_pan_and_roll_follows_axis_direction(vr):
    token = start(vr); feed = Feed(vr, token); r = vr.runtime
    feed(arms={'left': hand(1)}, scale=1)
    feed(20, arms={'left': hand(1, (0, .03, 0), roll=10)}, scale=1)
    anchor = vk.anchor(*ANGLES.values())
    pan = math.degrees(math.atan2(.03, anchor['reach']))
    assert abs((goal(r, 1) - 2050) * 360 / 4096 + pan) < .3
    assert abs((goal(r, 5) - 2050) * 360 / 4096 - 10) < .3
    r.settings.data.setdefault('axis_overrides', {})
    vr.stop(session_id=token)


def test_orientation_can_be_ignored(vr):
    token = start(vr); feed = Feed(vr, token); r = vr.runtime
    feed(arms={'left': hand(1, orient=False)}, scale=1)
    feed(10, arms={'left': hand(1, pitch=30, roll=30, orient=False)}, scale=1)
    assert all(goal(r, i) == 2050 for i in range(1, 6))


def test_tracking_jump_and_workspace_edge_hold_last_target(vr):
    token = start(vr); feed = Feed(vr, token); r = vr.runtime
    feed(arms={'left': hand(1)}, scale=1)
    before = writes(r)
    status = feed(arms={'left': hand(1, (.9, 0, 0))}, scale=1)
    assert status['active'] and status['vr']['arms']['left']['state'] == 'boundary'
    assert writes(r) == before
    status = feed(5, arms={'left': hand(1, (0, 0, .5))}, scale=1)
    assert status['vr']['arms']['left']['state'] == 'boundary' and status['active']


def test_joint_limit_stops_at_envelope_and_reports_boundary(vr):
    token = start(vr); feed = Feed(vr, token); r = vr.runtime
    feed(arms={'left': hand(1)}, scale=1)
    status = feed(40, arms={'left': hand(1, (0, .3, 0))}, scale=1)
    assert status['vr']['arms']['left']['state'] == 'boundary' and '限位' in status['vr']['arms']['left']['reason']
    assert goal(r, 1) == 1800


def test_small_ratio_does_not_hide_a_physical_tracking_jump(vr):
    token = start(vr); feed = Feed(vr, token); r = vr.runtime
    feed(arms={'left': hand(1)}, scale=1, ratio=.25)
    before = writes(r)
    status = feed(arms={'left': hand(1, (.9, 0, 0))}, scale=1, ratio=.25)
    assert status['vr']['arms']['left']['state'] == 'boundary'
    assert '追踪跳变' in status['vr']['arms']['left']['reason']
    assert writes(r) == before


def test_arm_without_reference_or_with_blocked_joint_is_unavailable_not_fatal(vr):
    token = start(vr, refs=False); feed = Feed(vr, token); r = vr.runtime
    before = writes(r)
    status = feed(3, arms={'left': hand(1, (.02, 0, 0))}, scale=1)
    assert status['active'] and status['vr']['arms']['left']['state'] == 'unavailable'
    assert '参考' in status['vr']['arms']['left']['reason'] and writes(r) == before
    references(r)
    r.service_blocked['COM3:2'] = '右肩物理hold'
    status = feed(arms={'left': hand(2, (.02, 0, 0))}, scale=1)
    assert status['active'] and 'hold' in status['vr']['arms']['left']['reason']
    assert writes(r) == before


def test_folded_pose_outside_model_is_refused(vr):
    token = vr.start(True, 'vr')['session_id']; r = vr.runtime
    references(r, angles={2: 0., 3: 175., 4: 0.})
    status = Feed(vr, token)(arms={'left': hand(1)}, scale=1)
    assert status['vr']['arms']['left']['state'] == 'unavailable'


def test_trigger_takes_gripper_only_after_first_press_and_squeezes_on_contact(vr):
    token = start(vr); feed = Feed(vr, token); r = vr.runtime
    before = writes(r)
    feed(3, grip={'left': 0.}, scale=1)
    assert writes(r) == before and not vr.snapshot()['vr']['grip']['left']['active']
    feed(5, grip={'left': 1.}, scale=1)
    closing = goal(r, 6)
    assert closing < 2050 and vr.snapshot()['vr']['grip']['left']['active']
    r._stall(('COM3', 6), 2000, closing)
    status = vr.snapshot()
    assert status['active'] and status['vr']['grip']['left']['holding']
    squeeze = goal(r, 6)
    assert squeeze == 2000 - min(40, 2000 - closing)
    feed(5, grip={'left': 1.}, scale=1)
    assert goal(r, 6) == squeeze
    feed(5, grip={'left': 0.}, scale=1)
    assert goal(r, 6) > squeeze and not vr.snapshot()['vr']['grip']['left']['holding']


def test_gripper_without_endpoints_reports_reason(vr):
    token = vr.start(True, 'vr')['session_id']; r = vr.runtime
    references(r, grip=False)
    status = Feed(vr, token)(grip={'left': 1.}, scale=1)
    assert status['active'] and '端点' in status['vr']['grip']['left']['reason']


def test_arm_stall_ends_vr_session(vr):
    token = start(vr); feed = Feed(vr, token); r = vr.runtime
    feed(arms={'left': hand(1)}, scale=1)
    feed(arms={'left': hand(1, (0, 0, .02))}, scale=1)
    r._stall(('COM3', 2), 2050, 2100)
    assert not vr.active and '受阻' in vr.reason


def test_missing_trigger_holds_and_reconnection_requires_a_new_press(vr):
    token = start(vr); feed = Feed(vr, token); r = vr.runtime
    feed(4, grip={'left': 1.}, scale=1)
    held = goal(r, 6)
    status = feed(grip={'left': None}, scale=1)
    assert not status['vr']['grip']['left']['active']
    feed(4, grip={'left': 0.}, scale=1)
    assert goal(r, 6) == held
    feed(2, grip={'left': 1.}, scale=1)
    assert vr.snapshot()['vr']['grip']['left']['active']


def test_drive_stick_matches_keyboard_forward_and_release_zeroes_immediately(vr):
    token = start(vr); r = vr.runtime
    vr.vr_input(token, 0, base=(.9, 0.), scale=1)
    vr.test_clock[0] += .1; vr.tick()
    expected = r.wheel_command(-.04, 0)
    assert all(a * b > 0 for a, b in zip(r.wheel_goals, expected.values()))
    assert max(abs(n) for n in r.wheel_goals) <= 160
    vr.vr_input(token, 1, base=(0., 0.), scale=1)
    assert not r.wheels_commanded
    assert all(word(r.session.ports['COM4'].regs[i], 46) == 0 for i in (9, 10))


def test_head_stick_turns_and_releases_to_hold(vr):
    token = start(vr); feed = Feed(vr, token); r = vr.runtime
    feed(3, head=(1., 0.), scale=1)
    assert goal(r, 7) < 2050  # VR right input uses the corrected physical pan sign.
    feed(head=(0., 0.), scale=1)
    assert ('COM3', 7) not in r.moving
    feed(3, head=(0., 1.), scale=1)
    assert goal(r, 8) < 2050


def test_lease_expiry_and_pad_input_end_vr_session(vr):
    token = start(vr); feed = Feed(vr, token)
    feed(arms={'left': hand(1)}, scale=1)
    vr.test_clock[0] += LEASE_S + .05; vr.tick()
    assert not vr.active and vr.runtime.controller['controller'] == 'offline'
    token = start(vr)
    Feed(vr, token)(head=(1., 0.), scale=1)
    assert vr.runtime.native_pad_event(pad(buttons=(0,)))
    assert not vr.active


def test_sequence_and_range_validation(vr):
    token = start(vr)
    vr.vr_input(token, 5)
    with pytest.raises(ValueError, match='序号'):
        vr.vr_input(token, 5)
    for bad in ({'base': (float('nan'), 0)}, {'head': (2, 0)}, {'ratio': 3}, {'grip': {'left': 1.5}},
                {'arms': {'left': hand(-1)}}, {'arms': {'left': hand(True)}}, {'grip': {'middle': .5}}):
        with pytest.raises(ValueError):
            vr.vr_input(token, 6, **bad)
    assert vr.seq == 5


def test_api_routes_validate_vr_payloads(vr):
    app = FastAPI(); install(app, lambda: vr.runtime); client = TestClient(app)
    token = client.post('/api/web/start', json={'confirmed': True, 'mode': 'vr'}).json()['session_id']
    ok = {'session_id': token, 'seq': 0, 'base': [0, 0], 'head': [0, 0],
          'arms': {'left': {'engage': 1, 'pos': [0, 0, 0]}}, 'grip': {'right': .5}, 'scale': .5, 'ratio': 1}
    response = client.post('/api/web/vr-input', json=ok)
    assert response.status_code == 200 and response.json()['mode'] == 'vr'
    for patch in ({'seq': 1, 'base': [2, 0]}, {'seq': 1, 'arms': {'left': {'engage': 1, 'pos': [0, 0]}}},
                  {'seq': 1, 'arms': {'left': {'engage': 1, 'pos': [0, 0, 0], 'raw': 1}}},
                  {'seq': 1, 'grip': {'left': 'NaN'}}, {'seq': 1, 'extra': 1}, {'seq': 1, 'ratio': 0}):
        assert client.post('/api/web/vr-input', json={**ok, **patch}).status_code == 422
    assert client.post('/api/web/start', json={'confirmed': True, 'mode': 'gamepad'}).status_code == 422
    status = client.get('/api/web/status').json()
    assert status['mode'] == 'vr' and 'session_id' not in status


def policy(disabled):
    vr_policy.PATH.write_text(json.dumps({'schema': 1, 'disabled': disabled}), encoding='utf-8')


def test_readiness_is_read_only_and_shows_channel_exclusions(vr):
    policy({'right': 'right shoulder physical hold', 'base': 'uncommanded_pose_change'})
    vr.runtime.scan()
    before = writes(vr.runtime)
    app = FastAPI(); install(app, lambda: vr.runtime)
    status = TestClient(app).get('/api/web/vr-readiness').json()
    assert status['read_only'] and status['can_start']
    assert status['channels']['head']['enabled']
    assert not status['channels']['right']['enabled'] and not status['channels']['base']['enabled']
    assert writes(vr.runtime) == before


def test_real_folded_pose_is_ready_without_preparing_or_changing_branch(vr):
    r = vr.runtime; r.scan()
    references(r, angles={2: -105.02264757982617, 3: 97.91373294197406, 4: 3.427734375})
    before = writes(r)
    c = vr.vr_readiness()['channels']['left']
    assert c['enabled'] and c['follow_ready'] and c['follow_reason'] is None
    assert c['grip_ready'] and all(j['enabled'] for j in c['joints'].values())
    assert writes(r) == before


def test_real_folded_pose_grip_is_jump_free_and_can_follow_forward(vr):
    r = vr.runtime
    # This test verifies the solver over 1 cm, independently of the generic
    # fake fixture's narrow +/-22 degree joint limits. Production uses the
    # existing measured device envelopes, unchanged by the VR solver.
    for i in (2, 3, 4):
        for addr, value in ((9, 1000), (11, 3500)):
            r.session.ports['COM3'].regs[i][addr:addr + 2] = [value & 255, value >> 8]
    token = start(vr); feed = Feed(vr, token)
    folded = {2: -105.02264757982617, 3: 97.91373294197406, 4: 3.427734375}
    references(r, angles=folded)
    before = writes(r)
    feed(3, arms={'left': hand(1)}, scale=1)
    assert writes(r) == before
    assert vr.vr_arms['left']['state'] == 'following'
    feed(35, arms={'left': hand(1, (.01, 0, 0))}, scale=1)
    actual = [folded[i] + (goal(r, i) - 2050) * 360 / 4096 for i in (2, 3, 4)]
    old_jaw, new_jaw = vk.jaw_point(*folded.values()), vk.jaw_point(*actual)
    assert abs(new_jaw[0] - old_jaw[0] - .01) < .002
    assert abs(new_jaw[1] - old_jaw[1]) < .002
    assert all(r.envelopes[('COM3', i)]['min'] <= goal(r, i) <= r.envelopes[('COM3', i)]['max']
               for i in (2, 3, 4))


def test_feedback_uses_cached_encoders_and_distinguishes_sent_targets(vr):
    token = start(vr); r = vr.runtime; feed = Feed(vr, token)
    r.scan()
    # A stationary fake servo must remain visible at its measured pose even
    # when a different command target has been sent. No display code writes.
    r.session.ports['COM3'].follow = False
    feed(arms={'left': hand(1)}, scale=1)
    feed(arms={'left': hand(1, (.01, 0, 0))}, scale=1)
    before = writes(r)
    data = vr.snapshot()['vr']['feedback']
    assert data['source'] == 'encoder_cache' and data['sampled_at'] == r.report['updated_at']
    left = data['arms']['left']
    assert left['measured']['lift'] == ANGLES[2]
    assert left['target']['lift'] != left['measured']['lift']
    assert data['age_ms'] is not None and data['targets_are_sent_commands']
    assert writes(r) == before
    r.kinematic_profile = {'joints': {}}
    assert vr.pose_feedback()['arms']['left']['measured'] is None


def test_joint_adjustment_moves_only_selected_joint_slowly_and_releases_to_hold(vr):
    token = start(vr); r = vr.runtime; feed = Feed(vr, token)
    references(r, angles={2: -105., 3: 98., 4: 3.4})
    feed(3, jog={'side': 'left', 'joint': 2, 'value': 1., 'engage': 1}, scale=1.)
    assert 2050 < goal(r, 2) <= 2050 + 6 * .3 * 4096 / 360 + 1
    assert word(r.session.ports['COM3'].regs[2], 46) <= 6 * 4096 / 360
    assert all(goal(r, i) == 2050 for i in (1, 3, 4, 5, 6))
    assert vr.snapshot()['vr']['arms']['left']['state'] == 'adjusting'
    feed(); held = goal(r, 2); before = writes(r)
    feed(3)
    assert goal(r, 2) == held and writes(r) == before and not r.wheels_commanded


def test_jog_does_not_override_arm_exclusions(vr):
    policy({'right': 'right physical hold', 'base': 'old stop latch'})
    token = start(vr); r = vr.runtime; before = len(r.session.ports['COM4'].writes)
    status = Feed(vr, token)(3, jog={'side': 'right', 'joint': 2, 'value': 1., 'engage': 1})
    assert status['active'] and status['vr']['arms']['right']['state'] == 'unavailable'
    assert len(r.session.ports['COM4'].writes) == before


@pytest.mark.parametrize('patch', [
    {'arms': {'left': hand(1)}}, {'base': (1., 0.)},
    {'jog': {'side': 'left', 'joint': 6, 'value': 1., 'engage': 1}},
    {'jog': {'side': 'left', 'joint': 2, 'value': 1., 'engage': 0}},
    {'jog': {'side': 'left', 'joint': 2, 'value': float('nan'), 'engage': 1}},
])
def test_jog_rejects_invalid_or_mixed_commands_before_renewing_lease(vr, patch):
    token = start(vr); r = vr.runtime; before = writes(r)
    state = {'jog': {'side': 'left', 'joint': 2, 'value': .5, 'engage': 1}, **patch}
    with pytest.raises(ValueError):
        vr.vr_input(token, 0, **state)
    assert vr.seq == -1 and writes(r) == before


def test_restricted_arm_and_base_receive_no_torque_or_goal_writes(vr):
    policy({'right': 'right shoulder physical hold', 'base': 'uncommanded_pose_change'})
    for i in (1, 2, 3, 4, 5, 6, 9, 10):
        vr.runtime.session.ports['COM4'].regs[i][40] = 0
    token = start(vr); r = vr.runtime
    # Fake fixture maps right to COM4, containing the wheels too.
    assert not r.session.ports['COM4'].writes
    feed = Feed(vr, token)
    feed(3, arms={'right': hand(1, (.05, 0, 0))}, grip={'right': 1.}, base=(1., 1.), head=(.5, 0.))
    assert vr.active and r.session.ports['COM3'].writes
    assert not r.session.ports['COM4'].writes
    assert all(r.session.ports['COM4'].regs[i][40] == 0 for i in (1, 2, 3, 4, 5, 6, 9, 10))
    assert vr.snapshot()['vr']['arms']['right']['state'] == 'unavailable'


def test_policy_change_during_vr_stops_instead_of_expanding_channels(vr):
    policy({'base': 'old stop latch'})
    start(vr); policy({})
    vr.test_clock[0] += .1; vr.tick()
    assert not vr.active and '通道限制已变化' in vr.reason


@pytest.mark.parametrize('bad', ['{}', '{"schema":1,"disabled":{"base":""}}', '{"schema":1,"disabled":{"unknown":"blocked"}}'])
def test_invalid_policy_refuses_before_hardware_preparation(vr, bad):
    vr_policy.PATH.write_text(bad, encoding='utf-8')
    with pytest.raises(ValueError, match='限制文件'):
        vr.start(True, 'vr')
    assert not any(writes(vr.runtime))
