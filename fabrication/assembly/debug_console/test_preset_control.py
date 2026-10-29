"""Preset trial is open-loop and never masquerades as precise IK."""
from copy import deepcopy
import time
import pytest
from fabrication.assembly.debug_console import original_runtime as orig
from fabrication.assembly.debug_console import control_runtime as control
from fabrication.assembly.debug_console.original_runtime import OriginalRuntime
from fabrication.assembly.debug_console.test_control_runtime import Session
from fabrication.assembly.debug_console.test_original_teleop import pad,step
from fabrication.assembly.debug_console.control_settings import DEFAULTS
from fabrication.assembly.debug_console.torque_control import word


@pytest.fixture
def runtime(tmp_path,monkeypatch):
    monkeypatch.setattr(control,'LOG_DIR',tmp_path);monkeypatch.setattr(control,'PROFILE',tmp_path/'devices.json')
    monkeypatch.setattr(orig,'KINEMATIC_PROFILE',tmp_path/'k.json');monkeypatch.setattr(orig,'SERVICE_PROFILE',tmp_path/'service.json')
    monkeypatch.setattr(orig,'CONTROLLER_SETTINGS',tmp_path/'settings.json');monkeypatch.setattr(orig,'LOG_DIR',tmp_path)
    r=OriginalRuntime(Session());r._check_ports=lambda:None;r.gamepad=pad();r.pad_time=time.monotonic()
    for i in (9,10):r.session.ports['COM4'].regs[i][33]=1
    return r


def test_preset_starts_with_real_ranges_without_reference_poses(runtime):
    runtime.begin_preset(True)
    assert runtime.control_profile=='original_preset' and runtime.controller['controller']=='online'
    assert not runtime.kinematic_profile.get('joints')
    assert all(m['torque']==1 for m in runtime.motors().values())
    assert runtime.envelopes[('COM3',2)]['min']==1800 and runtime.envelopes[('COM3',2)]['max']==2300


def test_preset_requires_explicit_confirmation_and_neutral_pad(runtime):
    with pytest.raises(ValueError,match='明确确认'):runtime.begin_preset(False)
    runtime.gamepad=pad([.5,0,0,0,0,0])
    with pytest.raises(ValueError,match='回中'):runtime.begin_preset(True)
    assert not any(p.writes for p in runtime.session.ports.values())


def test_preset_direct_axis_uses_per_axis_direction_speed_and_tolerance(runtime):
    runtime.update_controller_settings({**DEFAULTS,'axis_overrides':{'COM3:2':{'direction':-1,'joint_rate_deg_s':20,'follow_delta_raw':128}}})
    runtime.begin_preset(True)
    p=runtime.session.ports['COM3'];before=word(p.regs[2],56)
    # Horizontal stick drives lift directly; upstream vertical stick drives elbow.
    step(runtime,pad([1,0,0,0,0,0]))
    target=runtime.moving.get(('COM3',2))
    assert target is not None and target<before
    speed=word(p.regs[2],46);assert abs(speed-round(20*4096/360))<=1
    assert runtime.engine.arm_ports=={'left':'COM3','right':'COM4'}
    assert runtime.moving, 'A direct axis must write a real target'
    step(runtime,pad())
    # Releasing a non-wheel axis holds the last position; the zero-motion
    # guarantee applies to the command target, not by turning torque off.
    assert runtime.last_command.get('target') is not None


def test_preset_does_not_claim_precise_endpoint_pose():
    # Upstream preview values remain illustrative only.
    from fabrication.assembly.debug_console.upstream_teleop import UpstreamTeleop
    assert UpstreamTeleop().snapshot()['kind']=='kinematic_preview_not_physics'


def test_preset_axis_settings_are_persisted_without_register_changes(runtime):
    before=[deepcopy(p.regs) for p in runtime.session.ports.values()]
    result=runtime.update_controller_settings({**DEFAULTS,'axis_overrides':{'COM4:3':{'direction':-1,'joint_rate_deg_s':8,'follow_delta_raw':32}}})
    assert result['axis_overrides']['COM4:3']['direction']==-1
    assert runtime.settings.axis('COM4',3)=={'direction':-1,'joint_rate_deg_s':8,'grip_raw_s':120,'follow_delta_raw':32}
    assert [p.regs for p in runtime.session.ports.values()]==before


def test_preset_api_is_explicit_and_requires_confirmation(runtime,monkeypatch):
    from fastapi.testclient import TestClient
    from fabrication.assembly.debug_console import app as web
    monkeypatch.setattr(web,'runtime',runtime);client=TestClient(web.app);runtime.pad_time=time.monotonic()
    assert client.post('/api/original/start',json={'target':'preset','confirmed':False}).status_code==409
    response=client.post('/api/original/start',json={'target':'preset','confirmed':True})
    assert response.status_code==200 and runtime.control_profile=='original_preset'
