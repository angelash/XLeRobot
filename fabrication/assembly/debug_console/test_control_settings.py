"""Operator-side arm reversal and speed controls without EEPROM changes."""
from copy import deepcopy
import time
import pytest
from fabrication.assembly.debug_console.control_settings import ControllerSettings,DEFAULTS
from fabrication.assembly.debug_console.test_control_runtime import Session
from fabrication.assembly.debug_console.test_original_teleop import pad,step
from fabrication.assembly.debug_console import original_runtime as orig
from fabrication.assembly.debug_console import control_runtime as control


@pytest.fixture
def runtime(tmp_path,monkeypatch):
    monkeypatch.setattr(control,'LOG_DIR',tmp_path);monkeypatch.setattr(control,'PROFILE',tmp_path/'device.json')
    monkeypatch.setattr(orig,'KINEMATIC_PROFILE',tmp_path/'k.json');monkeypatch.setattr(orig,'SERVICE_PROFILE',tmp_path/'s.json')
    monkeypatch.setattr(orig,'CONTROLLER_SETTINGS',tmp_path/'settings.json');monkeypatch.setattr(orig,'LOG_DIR',tmp_path)
    r=orig.OriginalRuntime(Session());r._check_ports=lambda:None;r.gamepad=pad();r.pad_time=time.monotonic()
    return r


def test_settings_validate_persist_and_reject_unsafe_values(tmp_path):
    s=ControllerSettings(tmp_path/'settings.json')
    result=s.save({'arm_mapping':'swapped','joint_rate_deg_s':15,'grip_raw_s':200,'wheel_max_raw':900,'follow_delta_raw':128})
    assert result['logical_ports']=={'left':'COM4','right':'COM3'}
    assert ControllerSettings(s.path).data['arm_mapping']=='swapped'
    for bad in ({'arm_mapping':'automatic'},{'joint_rate_deg_s':100},{'follow_delta_raw':1}):
        with pytest.raises(ValueError):s.save(bad)


def test_swapped_arm_mapping_changes_logical_bus_without_motor_ids_or_config(runtime):
    runtime.capture_profile();before=[deepcopy(p.regs) for p in runtime.session.ports.values()]
    runtime.update_controller_settings({**DEFAULTS,'arm_mapping':'swapped'})
    assert runtime.arm_ports()=={'left':'COM4','right':'COM3'}
    assert runtime.motors()[('COM3',1)]['label'].startswith('右臂')
    assert runtime.motors()[('COM4',1)]['label'].startswith('左臂')
    assert [p.regs for p in runtime.session.ports.values()]==before
    assert not any(p.writes for p in runtime.session.ports.values())


def test_swapped_pose_cards_follow_physical_bus(runtime):
    for i in (9,10):runtime.session.ports['COM4'].regs[i][33]=1
    c=runtime.manual_calibration;c.start(True)
    positions={key:s.current for key,s in c.samples.items()}
    c.captures['left_a']={'positions':positions,'port':'COM3'};runtime.manual_calibration.active=False
    runtime.update_controller_settings({**DEFAULTS,'arm_mapping':'swapped'})
    assert c.captures['right_a']['port']=='COM3'
    assert 'left_a' not in c.captures


def test_wheel_mapping_swaps_logical_ids_without_relabeling_motors(runtime):
    runtime.capture_profile();before=[deepcopy(p.regs) for p in runtime.session.ports.values()]
    runtime.update_controller_settings({**DEFAULTS,'wheel_mapping':'swapped'})
    assert runtime.wheel_ids()=={'left':10,'right':9}
    assert [p.regs for p in runtime.session.ports.values()]==before


def test_takeover_allows_live_tuning_but_not_mapping_changes(runtime):
    runtime.capture_profile();runtime.controller['controller']='online'
    with pytest.raises(ValueError,match='结束接管'):runtime.update_controller_settings({'arm_mapping':'swapped'})
    with pytest.raises(ValueError,match='结束接管'):runtime.update_controller_settings({'base_turn':'reverse'})
    runtime.envelopes[('COM3',2)]['min']=5
    result=runtime.update_controller_settings({'joint_rate_deg_s':40,'wheel_max_raw':1500})
    assert result['joint_rate_deg_s']==40 and result['arm_mapping']=='normal'
    # Live tuning keeps the active profile's widened envelope.
    assert runtime.envelopes[('COM3',2)]['min']==5 and runtime.engine.max_raw==1500


def test_changed_rates_apply_to_next_joint_command(runtime):
    runtime.update_controller_settings({**DEFAULTS,'joint_rate_deg_s':20,'follow_delta_raw':96})
    runtime.prepare_hardware(True);runtime.pad_time=time.monotonic();runtime.begin_original('joint',True)
    step(runtime,pad([0,-1,0,0,0,0]))
    assert runtime.settings.data['joint_rate_deg_s']==20
    reg=runtime.session.ports['COM3'].regs[2];speed=reg[46]+256*reg[47]
    assert speed>=60


def test_calibration_time_settings_change_is_explicitly_allowed(runtime):
    for i in (9,10):runtime.session.ports['COM4'].regs[i][33]=1
    runtime.manual_calibration.start(True)
    runtime._settings_from_calibration=True
    try:runtime.update_controller_settings({**DEFAULTS,'wheel_mapping':'swapped'})
    finally:runtime._settings_from_calibration=False
    assert runtime.wheel_ids()=={'left':10,'right':9}
