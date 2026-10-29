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


def test_original_dpad_and_mirrored_wheels():
    e=UpstreamTeleop();e.step(pad(hat=(0,1)),.1)
    assert e.base[0]<0 and e.wheels[0]>0 and e.wheels[1]<0
    e.step(pad(hat=(0,-1)),.1)
    assert e.wheels[0]<0 and e.wheels[1]>0
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
    r=OriginalRuntime(Session());r._check_ports=lambda:None
    r.gamepad=pad();r.pad_time=time.monotonic()
    return r


def step(r,p):
    r.gamepad=p;r.pad_time=time.monotonic();r.ui_heartbeat=time.monotonic()
    r.tick_time-=.06;r.process_input(p)


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


def test_verified_wheels_use_sign_magnitude_then_stop_on_release_and_permission_loss(runtime):
    for i in (9,10):runtime.session.ports['COM4'].regs[i][33]=1
    runtime.begin_original('hardware',True)
    runtime.kinematic_profile={'wheels':{'verified':True,'radius_m':.05,'track_m':.25}}
    assert runtime.readiness()['base']['ready']
    step(runtime,pad(hat=(0,-1)))
    p=runtime.session.ports['COM4']
    raw=lambda i:p.regs[i][46]+256*p.regs[i][47]
    assert raw(9)&0x8000 and 0<raw(10)<0x8000
    assert runtime.wheels_commanded
    step(runtime,pad())
    assert raw(9)==raw(10)==0 and not runtime.wheels_commanded
    step(runtime,pad(hat=(0,-1)))
    runtime.kinematic_profile={}
    step(runtime,pad(hat=(0,-1)))
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
