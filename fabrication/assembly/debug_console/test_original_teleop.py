"""Original control semantics, fake-bus execution and hardware-free API tests."""
import ast
import json
import math
import time
from pathlib import Path
import pytest
from fabrication.assembly.debug_console.upstream_teleop import UpstreamTeleop,forward,inverse,intentions,neutral,wheel_raw,encode_velocity
from fabrication.assembly.debug_console.original_runtime import OriginalRuntime,PreviewRuntime
from fabrication.assembly.debug_console import original_runtime as orig
from fabrication.assembly.debug_console import control_runtime as mod
from fabrication.assembly.debug_console.test_control_runtime import Session


def pad(axes=None,buttons=(),hat=(0,0)):
    b=[False]*17
    for i in buttons:b[i]=True
    return {'connected':True,'buttons':b,'axes':list(axes or [0]*6),'hat':list(hat)}


@pytest.mark.parametrize('x,z',[(.1629,.1131),(.15,.1),(.18,.08),(.05,.15),(.2,.1)])
def test_inverse_forward_are_consistent(x,z):
    assert math.dist((x,z),forward(*inverse(x,z)))<1e-10


def test_upstream_corrected_methods_match_portable_kernel():
    root=Path(__file__).resolve().parents[3]
    tree=ast.parse((root/'software/src/model/SO101Robot.py').read_text(encoding='utf-8'))
    cls=next(n for n in tree.body if isinstance(n,ast.ClassDef) and n.name=='SO101Kinematics')
    cls.body=[n for n in cls.body if isinstance(n,ast.FunctionDef) and n.name in ('__init__','inverse_kinematics','forward_kinematics')]
    ns={'math':math};exec(compile(ast.Module(body=[cls],type_ignores=[]),'pure upstream methods','exec'),ns)
    k=ns['SO101Kinematics']()
    for x,z in [(.1629,.1131),(.15,.1),(.18,.08)]:
        assert math.dist((x,z),k.forward_kinematics(*k.inverse_kinematics(x,z)))<1e-10
        assert math.dist(k.inverse_kinematics(x,z),inverse(x,z))<1e-8


def test_radial_and_height_original_mapping_and_both_arms():
    e=UpstreamTeleop();before=e.snapshot()
    e.step(pad([0,-1,0,-1,0,0]),.1)
    for side in ('left','right'):
        assert e.snapshot()['arms'][side]['plane_m'][0]>before['arms'][side]['plane_m'][0]
    before=e.snapshot();e.step(pad([1,0,1,0,0,0]),.1)
    for side in ('left','right'):
        assert e.snapshot()['arms'][side]['plane_m'][1]>before['arms'][side]['plane_m'][1]


def test_pan_and_wrist_modifiers_have_priority():
    p=pad([1,-1,1,-1,0,0],buttons=(8,9))
    assert intentions(p)['left']['mode']==intentions(p)['right']['mode']=='pan'
    p=pad([1,-1,1,-1,0,0],buttons=(4,5,8,9))
    assert intentions(p)['left']['mode']==intentions(p)['right']['mode']=='wrist'
    e=UpstreamTeleop();e.step(p,.1)
    assert e.q['left']['roll']>0 and e.q['right']['roll']>0
    assert e.q['left']['wrist']>-2 and e.q['left']['pan']==0


def test_upstream_abxy_head_and_trigger_release():
    e=UpstreamTeleop();e.step(pad([0,0,0,0,1,1],buttons=(0,2)),.1)
    assert e.head==[.5,.5]
    assert e.q['left']['grip']<90 and e.q['right']['grip']<90
    e.step(pad(buttons=(1,3)),.1)
    assert e.head==[0,0] and e.q['left']['grip']==90
    assert not intentions(pad(buttons=(1,)))['stop']  # B remains head control


def test_dpad_up_is_forward_and_wheels_stay_mirrored():
    e=UpstreamTeleop();e.step(pad(hat=(0,1)),.1)
    assert e.base[0]>0 and e.wheels[0]<0 and e.wheels[1]>0
    e.step(pad(hat=(0,-1)),.1)
    assert e.wheels[0]>0 and e.wheels[1]<0
    e.step(pad(hat=(-1,0)),.1)
    assert e.wheels[0]>0 and e.wheels[1]>0
    assert encode_velocity(-15)==0x800f


def test_dt_cap_deadzone_speed_edges_and_no_input_drift():
    a=UpstreamTeleop();b=UpstreamTeleop()
    a.step(pad([0,-1,0,0,0,0]),100)
    b.step(pad([0,-1,0,0,0,0]),.1)
    assert a.q==b.q
    before=a.snapshot();a.step(pad([.1,-.12,.03,.04,0,0]),.1)
    assert a.q['left']==before['arms']['left']['joints']
    for _ in range(10):a.step(pad(buttons=(5,),hat=(0,-1)),.1)
    assert a.speed_level==2


def test_limits_do_not_accumulate_unreachable_target():
    e=UpstreamTeleop();e.bounds['left']['lift']=(0,0)
    before=e.q['left'].copy()
    for _ in range(20):e.step(pad([0,-1,0,0,0,0]),.1)
    assert e.q['left']==before and e.blockers
    e.bounds['left']['lift']=(-120,120)
    e.step(pad([0,1,0,0,0,0]),.1)
    assert e.q['left']!=before


@pytest.fixture
def runtime(tmp_path,monkeypatch):
    monkeypatch.setattr(mod,'LOG_DIR',tmp_path)
    monkeypatch.setattr(mod,'PROFILE',tmp_path/'device.json')
    monkeypatch.setattr(orig,'KINEMATIC_PROFILE',tmp_path/'kinematic.json')
    monkeypatch.setattr(orig,'SERVICE_PROFILE',tmp_path/'hardware-service.json')
    monkeypatch.setattr(orig,'CONTROLLER_SETTINGS',tmp_path/'controller-settings.json')
    r=OriginalRuntime(Session());r._check_ports=lambda:None
    r.gamepad=pad();r.pad_time=time.monotonic()
    return r


def step(r,p):
    r.gamepad=p;r.pad_time=time.monotonic();r.ui_heartbeat=time.monotonic()
    r.tick_time-=.06;r.process_input(p)


def whole_profile(r):
    r.capture_profile()
    data={'serials':mod.EXPECTED_SERIALS,'full_manual_range_verified':True,
          'wheels':{'verified':True,'radius_m':.05,'track_m':.25},'joints':{}}
    for port in ('COM3','COM4'):
        for i in (2,3,4,6):
            motor=r.motors()[(port,i)]
            ref={'verified':True,**{k:motor[k] for k in ('min','max','homing_raw')}}
            if i==6:ref.update(open_raw=2300,closed_raw=1800)
            else:ref.update(reference_raw=2050,reference_deg={2:0,3:2,4:-2}[i],direction=1)
            data['joints'][f'{port}:{i}']=ref
    orig.KINEMATIC_PROFILE.write_text(json.dumps(data),encoding='utf-8')
    for i in (9,10):r.session.ports['COM4'].regs[i][33]=1


def test_whole_robot_entry_rejects_partial_setup_before_any_torque_or_motion(runtime):
    with pytest.raises(ValueError,match='整机初始化未完成'):runtime.begin_whole_robot(True)
    assert runtime.controller['controller']=='offline'
    assert all(not port.writes for port in runtime.session.ports.values())
    assert not runtime.whole_robot_setup()['ready']


def test_trial_entry_automatically_prepares_all_without_invented_ik_references(runtime):
    for port in runtime.session.ports.values():
        for r in port.regs.values():r[40]=0
    for i in (9,10):runtime.session.ports['COM4'].regs[i][33]=1
    runtime.begin_trial(True)
    assert runtime.control_profile=='original_joint' and runtime.controller['controller']=='online'
    assert all(m['torque']==1 for m in runtime.motors().values())
    assert all(v['ready'] for v in runtime.readiness('original_joint').values())
    assert not runtime.kinematic_profile and not runtime.whole_robot_setup()['ready']
    assert runtime.envelopes[('COM3',2)]['max']-runtime.envelopes[('COM3',2)]['min']==64


def test_trial_preflight_blocks_bad_window_before_enabling_any_motor(runtime):
    for port in runtime.session.ports.values():
        for r in port.regs.values():r[40]=0
    for i in (9,10):runtime.session.ports['COM4'].regs[i][33]=1
    runtime.session.ports['COM4'].regs[4][56:58]=[0,0]
    with pytest.raises(ValueError,match='试控窗口'):runtime.begin_trial(True)
    assert all(not p.writes for p in runtime.session.ports.values())
    assert runtime.controller['controller']=='offline'


def test_api_trial_starts_from_torque_off_without_writing_calibration(runtime,monkeypatch):
    from fastapi.testclient import TestClient
    from fabrication.assembly.debug_console import app as web
    monkeypatch.setattr(web,'runtime',runtime);client=TestClient(web.app)
    for port in runtime.session.ports.values():
        for r in port.regs.values():r[40]=0
    for i in (9,10):runtime.session.ports['COM4'].regs[i][33]=1
    runtime.pad_time=time.monotonic()
    response=client.post('/api/original/start',json={'target':'joint','confirmed':True})
    assert response.status_code==200
    assert runtime.control_profile=='original_joint'
    assert all(address>=40 for p in runtime.session.ports.values() for _,address,_ in p.writes)
    status=client.get('/api/original/status').json()
    assert all(v['ready'] for v in status['trial_readiness'].values())
    assert '肩抬升' in next(m['role'] for m in status['trial_control_map'] if m['key']=='lx')


def test_whole_robot_entry_prepares_all_and_uses_measured_full_range(runtime):
    whole_profile(runtime)
    for port in runtime.session.ports.values():
        for regs in port.regs.values():regs[40]=0
    runtime.begin_whole_robot(True)
    assert runtime.control_profile=='original_hardware' and runtime.controller['controller']=='online'
    assert runtime.whole_robot_setup()['ready']
    assert runtime.envelopes[('COM3',2)]['min']==1800 and runtime.envelopes[('COM3',2)]['max']==2300
    assert all(regs[40]==1 for port in runtime.session.ports.values() for regs in port.regs.values())
    assert all(mod.word(runtime.session.ports['COM4'].regs[i],46)==0 for i in (9,10))
    step(runtime,pad([0,-.5,0,-.5,0,0]))
    assert any(i in (2,3) and addr==42 for p in runtime.session.ports.values() for i,addr,_ in p.writes)


def test_default_api_means_complete_real_robot_and_never_silently_partial(runtime,monkeypatch):
    from fastapi.testclient import TestClient
    from fabrication.assembly.debug_console import app as web
    monkeypatch.setattr(web,'runtime',runtime);client=TestClient(web.app)
    runtime.pad_time=time.monotonic()
    response=client.post('/api/original/start',json={'confirmed':True})
    assert response.status_code==409 and '整机初始化' in response.json()['detail']
    assert runtime.controller['controller']=='offline'
    # Until the whole mapping is ready, the page opens on direct preset control.
    assert client.get('/api/original/status').json()['preferred_target']=='preset'


def test_preview_never_writes_any_hardware(runtime):
    runtime.begin_original('preview',True)
    for p in [pad([1,-1,1,-1,1,1],buttons=(0,2),hat=(1,1)),pad(buttons=(4,5,8,9)),pad()]:step(runtime,p)
    runtime.stop()
    assert all(not p.writes for p in runtime.session.ports.values())


def test_start_neutral_gate_and_no_auto_zero(runtime):
    runtime.gamepad=pad([.2,0,0,0,0,0])
    with pytest.raises(ValueError):runtime.begin_original('hardware',True)
    runtime.gamepad=pad();runtime.begin_original('hardware',True)
    step(runtime,pad())
    assert all(not p.writes for p in runtime.session.ports.values())
    with pytest.raises(ValueError):runtime.begin(True) # no implicit takeover replacement


def test_readiness_distinguishes_saved_range_torque_and_mode(runtime):
    motor={'label':'左肩','min':2044,'max':2051,'position':2047,'mode':0,'torque':0}
    reasons=runtime.joint_readiness_reasons(motor,{'available':False})
    assert any('7计数' in r and '0.62°' in r for r in reasons)
    assert '左肩扭矩关闭' in reasons
    assert not any('需要位置模式' in r for r in reasons)
    motor.update(min=1000,max=2000,position=2050,mode=1,torque=1)
    reasons=runtime.joint_readiness_reasons(motor,{'available':False})
    assert any('实测2050超出设备范围1000–2000' in r for r in reasons)
    assert '左肩当前模式1，需要位置模式0' in reasons
    assert not any('扭矩关闭' in r for r in reasons)


def test_explicit_preparation_latches_current_before_torque_and_retains_mode_limits(runtime):
    for port in runtime.session.ports.values():
        for r in port.regs.values():r[40]=0
    for i in (9,10):runtime.session.ports['COM4'].regs[i][33]=1
    result=runtime.prepare_hardware(True)
    assert len(result['ready'])==16 and not result['blocked']
    for name,port in runtime.session.ports.items():
        for i in port.regs:
            writes=[(address,data) for motor_id,address,data in port.writes if motor_id==i]
            assert all(address in (40,42,46) for address,_ in writes)
            if name=='COM4' and i in (9,10):assert writes==[(46,[0,0]),(40,[1])]
            else:assert writes==[(46,[60,0]),(42,[2,8]),(40,[1])]
    runtime.begin_original('joint',True)
    with pytest.raises(ValueError):runtime.prepare_hardware(True)


def test_preparation_skips_narrow_or_outside_limits_and_preview_rejects(runtime):
    runtime.session.ports['COM3'].regs[1][9:13]=[252,7,3,8]
    result=runtime.prepare_hardware(True)
    assert any(x['port']=='COM3' and x['id']==1 for x in result['blocked'])
    assert not any(i==1 for i,_,_ in runtime.session.ports['COM3'].writes)
    with pytest.raises(ValueError):PreviewRuntime().prepare_hardware(True)


def test_hardware_start_cannot_report_success_when_all_torque_is_off(runtime):
    for port in runtime.session.ports.values():
        for r in port.regs.values():r[40]=0
    with pytest.raises(ValueError,match='准备实机'):runtime.begin_original('joint',True)
    with pytest.raises(ValueError,match='准备实机'):runtime.begin_original('hardware',True)
    assert runtime.controller['controller']=='offline'
    assert all(not port.writes for port in runtime.session.ports.values())


def test_maintenance_exclusions_survive_restart_and_cover_diagnostic_torque_and_joint(runtime):
    orig.SERVICE_PROFILE.write_text(json.dumps({'serials':mod.EXPECTED_SERIALS,
        'blocked_motors':{'COM4:1':'机械检修待验收'}}),encoding='utf-8')
    # No preparation call: this is the first capture after a service restart.
    runtime.begin(True)
    assert not runtime.envelopes[('COM4',1)]['available']
    assert runtime.envelopes[('COM4',1)]['maintenance_blocked']
    assert not any('行程不足' in reason for reason in runtime.readiness()['right_pan']['reasons'])
    assert runtime.selected!=('COM4',1)
    with pytest.raises(ValueError,match='机械检修'):runtime.select('COM4',1)
    runtime.stop()
    with pytest.raises(ValueError,match='检修关节'):runtime.torque(True,{'COM4':[1]})
    with pytest.raises(ValueError,match='检修关节'):runtime.torque(True)
    assert all(not port.writes for port in runtime.session.ports.values())
    runtime.begin_original('joint',True)
    step(runtime,pad([0,0,1,0,0,0],buttons=(9,)))
    assert not runtime.session.ports['COM4'].writes
    with pytest.raises(ValueError,match='机械检修'):runtime._goal('COM4',1,2060)
    runtime.stop();runtime.torque(False,{'COM4':[1]})
    assert runtime.session.ports['COM4'].regs[1][40]==0


def test_invalid_maintenance_identity_blocks_motion_but_allows_torque_off(runtime):
    orig.SERVICE_PROFILE.write_text(json.dumps({'serials':{'COM4':'other-board'},'blocked_motors':{}}),encoding='utf-8')
    with pytest.raises(ValueError,match='USB身份'):runtime.begin_original('joint',True)
    assert runtime.controller['controller']=='offline'
    assert all(not port.writes for port in runtime.session.ports.values())
    runtime.torque(False)
    assert all(r[40]==0 for port in runtime.session.ports.values() for r in port.regs.values())


def test_joint_mode_moves_without_fabricating_kinematic_references_and_bounds_at_32(runtime):
    runtime.begin_original('joint',True)
    for _ in range(30):step(runtime,pad([1,0,0,0,0,0]))
    assert runtime.motors()[('COM3',2)]['position']==2050 # Snapshot scans separately.
    p=runtime.session.ports['COM3']
    assert 2050<mod.word(p.regs[2],56)<=2082
    assert not runtime.kinematic_profile
    assert not any(i in (3,4) for i,_,_ in p.writes)
    runtime.stop()


def test_joint_wrist_input_updates_visible_mode_without_computing_fake_pose(runtime):
    runtime.begin_original('joint',True)
    before=runtime.engine.snapshot()['arms']['right']['joints']
    step(runtime,pad([0,0,1,0,0,0],buttons=(5,)))
    assert runtime.engine.snapshot()['arms']['right']['mode']=='wrist'
    assert runtime.engine.snapshot()['arms']['right']['joints']==before
    assert mod.word(runtime.session.ports['COM4'].regs[5],56)>2050
    assert 'right_lift' in runtime.readiness() and 'right_elbow' in runtime.readiness()


def test_joint_wheel_pulse_stops_and_cannot_repeat_until_dpad_release(runtime,monkeypatch):
    class Timer:
        def __init__(self,delay,callback):self.delay=delay;self.callback=callback
        def start(self):pass
        def cancel(self):pass
    monkeypatch.setattr(orig.threading,'Timer',Timer)
    for i in (9,10):runtime.session.ports['COM4'].regs[i][33]=1
    runtime.begin_original('joint',True)
    step(runtime,pad(hat=(0,-1)))
    timer=runtime.wheel_pulse_timer;assert timer.delay==.18
    assert runtime.wheels_commanded
    assert max(abs(v) for v in runtime.wheel_goals)==160
    timer.callback();assert not runtime.wheels_commanded
    p=runtime.session.ports['COM4'];count=len(p.writes)
    step(runtime,pad(hat=(0,-1)));assert len(p.writes)==count
    assert all(mod.word(p.regs[i],46)==0 for i in (9,10))
    step(runtime,pad());step(runtime,pad(hat=(0,-1)));assert runtime.wheels_commanded
    runtime.stop();assert not runtime.wheels_commanded


def test_joint_wheel_stop_lost_ack_retries_both_wheels_and_pauses(runtime,monkeypatch):
    for i in (9,10):runtime.session.ports['COM4'].regs[i][33]=1
    runtime.begin_original('joint',True)
    runtime.wheels_commanded=True
    original=orig.write_register;attempts=[]
    def fail_first_stop(port,i,address,value,length):
        attempts.append((i,address,value))
        if len(attempts)==1:raise RuntimeError('stop ACK lost')
        return original(port,i,address,value,length)
    monkeypatch.setattr(orig,'write_register',fail_first_stop)
    runtime._stop_wheel_pulse()
    assert runtime.controller['paused'] and runtime.controller['controller']=='online'
    assert attempts.count((9,46,0))==2 and attempts.count((10,46,0))==2
    assert not runtime.wheels_commanded


def test_hardware_blocks_uncalibrated_ik_grip_and_position_mode_wheels(runtime):
    runtime.begin_original('hardware',True)
    assert not runtime.readiness()['left_position']['ready']
    step(runtime,pad([0,-1,0,-1,1,1],hat=(0,-1)))
    assert all(not p.writes for p in runtime.session.ports.values())
    assert runtime.controller['controller']=='online' and not runtime.controller['paused']
    assert runtime.notice['code']=='blocked'


def test_hardware_original_pan_and_roll_use_real_feedback(runtime):
    runtime.begin_original('hardware',True)
    for _ in range(5):step(runtime,pad([1,0,1,0,0,0],buttons=(8,9)))
    assert runtime.session.ports['COM3'].writes and runtime.session.ports['COM4'].writes
    for p in runtime.session.ports.values():
        assert all(i==1 and addr in (42,46) for i,addr,values in p.writes)
    step(runtime,pad())
    assert not runtime.moving
    assert runtime.controller['controller']=='online'


def test_menu_pause_view_stop_and_watchdog_preserve_session(runtime):
    runtime.begin_original('preview',True)
    step(runtime,pad(buttons=(7,)))
    assert runtime.controller['paused'] and runtime.controller['controller']=='online'
    runtime.gamepad=pad([1,0,0,0,0,0])
    with pytest.raises(ValueError):runtime.resume()
    runtime.gamepad=pad();runtime.pad_time=time.monotonic();runtime.resume()
    runtime.ui_heartbeat-=4;runtime.check_watchdog()
    assert runtime.controller['paused'] and runtime.controller['controller']=='online'
    step(runtime,pad(buttons=(6,)))
    assert runtime.controller['controller']=='offline'


def test_no_encoder_motion_pauses_original_hardware(runtime):
    runtime.begin_original('hardware',True)
    runtime.session.ports['COM4'].follow=False
    for _ in range(12):step(runtime,pad([0,0,1,0,0,0],buttons=(9,)))
    for window in runtime.feedback_window.values():window['at']-=1
    runtime._check_feedback()
    assert runtime.controller['paused'] and runtime.controller['controller']=='online'


def test_reached_joint_limit_does_not_become_false_stall_and_reverse_is_still_monitored(runtime):
    runtime.begin_original('joint',True)
    port=runtime.session.ports['COM4']
    for _ in range(20):step(runtime,pad([0,0,1,0,0,0],buttons=(5,)))
    key=('COM4',5);target=runtime.moving[key]
    # Match the live failure: the servo rests four counts short of the goal.
    port.regs[5][56:58]=list((target-4).to_bytes(2,'little'))
    runtime.feedback_window[key]={'origin':target-4,'at':time.monotonic()-1}
    runtime._check_feedback()
    assert not runtime.controller['paused'] and key not in runtime.feedback_window
    assert runtime.last_command['reached']
    runtime._check_feedback();assert not runtime.controller['paused']
    # A fresh target in the opposite direction must open a new feedback window.
    port.follow=False
    for _ in range(15):step(runtime,pad([0,0,-1,0,0,0],buttons=(5,)))
    assert key in runtime.feedback_window
    runtime.feedback_window[key]['at']-=1
    runtime._check_feedback();assert runtime.controller['paused']


@pytest.mark.parametrize('profile,should_pause',[('joint',False),('hardware',True)])
def test_trial_tracking_tolerance_is_separate_from_precise_hardware_acceptance(runtime,profile,should_pause):
    runtime.begin_original(profile,True)
    runtime._goal('COM4',1,2074)
    port=runtime.session.ports['COM4'];port.follow=False
    port.regs[1][56:58]=list((2064).to_bytes(2,'little'))
    runtime.feedback_window[('COM4',1)]={'origin':2064,'at':time.monotonic()-1}
    runtime._check_feedback()
    assert runtime.controller['paused']==should_pause


def test_verified_kinematic_profile_and_group_execution(runtime):
    runtime.capture_profile()
    data={'serials':mod.EXPECTED_SERIALS,'joints':{}}
    for port in ('COM3','COM4'):
        for i,angle in [(2,0),(3,2),(4,-2)]:
            m=runtime.motors()[(port,i)]
            data['joints'][f'{port}:{i}']={'verified':True,'reference_raw':2050,'reference_deg':angle,'direction':1,
                **{k:m[k] for k in ('min','max','homing_raw')}}
    orig.KINEMATIC_PROFILE.write_text(json.dumps(data),encoding='utf-8')
    runtime.begin_original('hardware',True)
    assert runtime.readiness()['left_position']['ready']
    for _ in range(8):step(runtime,pad([0,-.5,0,-.5,0,0]))
    assert any(addr==42 for p in runtime.session.ports.values() for i,addr,v in p.writes)
    assert all(i in (2,3,4) for p in runtime.session.ports.values() for i,addr,v in p.writes)


def test_stale_reference_and_fake_limits_never_unlock(runtime):
    runtime.capture_profile()
    runtime.kinematic_profile={'joints':{'COM4:2':{'verified':True,'reference_raw':2050,'reference_deg':0,'direction':1,'min':1,'max':4095,'homing_raw':0}}}
    assert runtime.reference('COM4',2) is None


def test_preview_service_cannot_access_devices(runtime,monkeypatch):
    r=PreviewRuntime(runtime.session);r.gamepad=pad();r.pad_time=time.monotonic()
    for fn in [lambda:r.begin(True),lambda:r.torque(True),r.scan,r.capture_profile,lambda:r.begin_original('hardware',True)]:
        with pytest.raises(ValueError):fn()
    r.begin_original('preview',True);step(r,pad([1,0,1,0,1,1],hat=(0,-1)));r.close()
    assert all(not p.writes for p in r.session.ports.values())


def test_original_api_mapping_and_profile_gates(runtime,monkeypatch):
    from fastapi.testclient import TestClient
    from fabrication.assembly.debug_console import app as web
    monkeypatch.setattr(web,'runtime',runtime);client=TestClient(web.app)
    assert client.get('/original').status_code==200
    mapping=client.get('/api/original/status').json()['control_map']
    assert len(mapping)==22 and next(m for m in mapping if m['key']=='b')['target']=='头部1'
    runtime.pad_time=time.monotonic()
    assert client.post('/api/original/start',json={'target':'preview','confirmed':True}).status_code==200
    assert client.post('/api/original/start',json={'target':'hardware','confirmed':True}).status_code==409
    assert client.post('/api/original/pause').status_code==200
    assert runtime.controller['paused'] and runtime.controller['controller']=='online'


def test_wheels_drive_without_field_verification_and_stop_on_release_and_torque_loss(runtime):
    for i in (9,10):runtime.session.ports['COM4'].regs[i][33]=1
    runtime.begin_original('hardware',True)
    assert runtime.readiness()['base']['ready']
    step(runtime,pad(hat=(0,1)))
    p=runtime.session.ports['COM4']
    raw=lambda i:p.regs[i][46]+256*p.regs[i][47]
    assert raw(9)&0x8000 and 0<raw(10)<0x8000
    assert runtime.wheels_commanded
    step(runtime,pad())
    assert raw(9)==raw(10)==0 and not runtime.wheels_commanded
    for i in (9,10):p.regs[i][40]=0
    runtime.scan();step(runtime,pad(hat=(0,1)))
    assert raw(9)==raw(10)==0


def test_partial_wheel_write_failure_is_tracked_and_stop_attempts_both(runtime,monkeypatch):
    for i in (9,10):runtime.session.ports['COM4'].regs[i][33]=1
    runtime.begin_original('hardware',True)
    runtime.kinematic_profile={'wheels':{'verified':True,'radius_m':.05,'track_m':.25}}
    original=orig.write_register;failed=[]
    def fail_once(port,i,address,value,length):
        if i==10 and address==46 and value and not failed:
            failed.append(True);raise RuntimeError('lost ack')
        return original(port,i,address,value,length)
    monkeypatch.setattr(orig,'write_register',fail_once)
    with pytest.raises(RuntimeError):step(runtime,pad(hat=(0,-1)))
    assert runtime.wheels_commanded
    runtime._pause('test lost ack')
    assert runtime.controller['paused'] and runtime.controller['controller']=='online'
    assert all(runtime.session.ports['COM4'].regs[i][46:48]==[0,0] for i in (9,10))


def test_final_goal_ack_failure_still_holds_that_motor(runtime,monkeypatch):
    runtime.begin_original('hardware',True)
    original=orig.write_register;failed=[]
    def fail_once(port,i,address,value,length):
        if port is runtime.session.ports['COM4'] and i==1 and address==42 and value!=2050 and not failed:
            failed.append(True);raise RuntimeError('goal ack lost')
        return original(port,i,address,value,length)
    monkeypatch.setattr(orig,'write_register',fail_once)
    with pytest.raises(RuntimeError):step(runtime,pad([0,0,1,0,0,0],buttons=(9,)))
    assert ('COM4',1) in runtime.moving
    runtime._pause('lost ack')
    assert not runtime.moving and runtime.controller['paused']


@pytest.mark.parametrize('stop_kind',['neutral','pause','stop'])
def test_original_stop_never_reenables_a_released_motor(runtime,stop_kind):
    runtime.begin_original('hardware',True)
    step(runtime,pad([0,0,1,0,0,0],buttons=(9,)))
    assert ('COM4',1) in runtime.moving
    port=runtime.session.ports['COM4'];port.regs[1][40]=0;count=len(port.writes)
    if stop_kind=='neutral':step(runtime,pad())
    elif stop_kind=='pause':runtime._pause('released during motion')
    else:runtime.stop()
    assert port.regs[1][40]==0 and len(port.writes)==count
    assert not runtime.moving and not runtime.feedback_window
    assert runtime.last_command['outcome']=='torque_off'


def test_resume_does_not_reopen_a_previously_operated_gripper(runtime):
    runtime.capture_profile()
    m=runtime.motors()[('COM4',6)]
    data={'serials':mod.EXPECTED_SERIALS,'joints':{'COM4:6':{'verified':True,'closed_raw':1800,'open_raw':2300,
          **{k:m[k] for k in ('min','max','homing_raw')}}}}
    orig.KINEMATIC_PROFILE.write_text(json.dumps(data),encoding='utf-8')
    runtime.begin_original('hardware',True)
    step(runtime,pad([0,0,0,0,0,1]))
    runtime._pause('user pause')
    runtime.gamepad=pad();runtime.pad_time=time.monotonic();runtime.resume()
    before=len(runtime.session.ports['COM4'].writes)
    step(runtime,pad())
    assert len(runtime.session.ports['COM4'].writes)==before


def test_feedback_checked_by_actual_monitor_watchdog_path(runtime):
    runtime.begin_original('hardware',True)
    runtime.session.ports['COM4'].follow=False
    for _ in range(12):step(runtime,pad([0,0,1,0,0,0],buttons=(9,)))
    for w in runtime.feedback_window.values():w['at']-=1
    runtime.check_watchdog()
    assert runtime.controller['paused']


def test_main_preview_does_not_pause_when_hardware_disconnected(runtime,monkeypatch):
    runtime.begin_original('preview',True)
    def unavailable():raise RuntimeError('COM3 missing')
    runtime.scan=unavailable
    monkeypatch.setattr(runtime.stop_event,'wait',lambda _:runtime.stop_event.set())
    runtime._monitor()
    assert runtime.controller['controller']=='online' and not runtime.controller['paused']
    assert runtime.report['error']=='COM3 missing'
