"""Commissioning tests exercise STS packet writes, wraparound and batch rollback."""
from copy import deepcopy
import json
import time
import pytest
from fastapi.testclient import TestClient
from fabrication.assembly.debug_console import original_runtime as orig,control_runtime as control,whole_calibration as cal,commissioning
from fabrication.assembly.debug_console.test_control_runtime import Session
from fabrication.assembly.debug_console.test_original_teleop import pad,step
from fabrication.assembly.debug_console.torque_control import word


def set_word(reg,address,value):reg[address:address+2]=[value&255,value>>8]


@pytest.fixture
def runtime(tmp_path,monkeypatch):
    monkeypatch.setattr(control,'LOG_DIR',tmp_path)
    monkeypatch.setattr(control,'PROFILE',tmp_path/'devices.json')
    monkeypatch.setattr(orig,'KINEMATIC_PROFILE',tmp_path/'kinematic.json')
    monkeypatch.setattr(orig,'SERVICE_PROFILE',tmp_path/'service.json')
    monkeypatch.setattr(orig,'LOG_DIR',tmp_path)
    r=orig.OriginalRuntime(Session());r._check_ports=lambda:None
    r.gamepad=pad();r.pad_time=time.monotonic()
    for i in (9,10):r.session.ports['COM4'].regs[i][33]=1
    for port in r.session.ports.values():
        original=port.write
        def offset_write(packet,p=port,write=original):
            reg=p.regs[packet[2]]
            old_pos,old_homing=word(reg,56),cal.signed_homing(word(reg,31))
            if packet[4]==3 and packet[5]==42:
                assert word(reg,48)==0 or not r.manual_calibration.active,'Commissioning goals must have zero output'
            result=write(packet)
            if packet[4]==3 and packet[5]==31:
                set_word(reg,56,(old_pos+old_homing-cal.signed_homing(word(reg,31)))%4096)
            return result
        port.write=offset_write
    return r


def completed_measurements(c):
    c.confirmed_keys=list(cal.KEYS)
    for sample in c.samples.values():sample.low=1000;sample.high=3000
    for side,port in [('left','COM3'),('right','COM4')]:
        base={k:s.current for k,s in c.samples.items()}
        a={**base,f'{port}:2':1600,f'{port}:3':2000,f'{port}:4':2000}
        b={**a,f'{port}:2':2112,f'{port}:3':1488}
        wrist_c=1488 if side=='left' else 2512
        cc={**b,f'{port}:4':wrist_c}
        for name,positions in [('a',a),('b',b),('c',cc),('open',{**cc,f'{port}:6':2300}),('closed',{**cc,f'{port}:6':1800})]:
            c.captures[side+'_'+name]={'positions':positions,'captured_at':'test'}
    c.wheels={**cal.DESIGN_GEOMETRY,'verified':True,'left_sign':-1,'right_sign':1,
              'direction_observed':True,'stop_verified':True}
    c.step='review'
    stamp=time.monotonic()
    for sample in c.samples.values():
        sample.recent.clear()
        for delta in (-.15,-.1,-.05):sample.recent.append((stamp+delta,sample.current))


def test_continuous_encoder_wrap_and_reversal():
    s=cal.RangeSample(4080,1.)
    for raw,at in [(4095,1.1),(15,1.2),(25,1.3),(2,1.4),(4085,1.5)]:s.update(raw,at)
    assert s.low==4080 and s.high==4121 and s.current==4085
    assert s.snapshot()['span_raw']==41


@pytest.mark.parametrize('raw,at',[(10,4.),(2100,1.1),(4096,1.1)])
def test_missing_or_ambiguous_samples_cannot_become_verified(raw,at):
    with pytest.raises(ValueError):cal.RangeSample(100,1.).update(raw,at)


@pytest.mark.parametrize('old',[-2000,-1,0,500,2047])
def test_centered_homing_preserves_physical_encoder_across_zero(old):
    s=cal.RangeSample(4050,1.);s.low=3900;s.high=4400;s.current=4250
    entry=cal.recentered(s,cal.encode_homing(old))
    physical=(s.current+old)%4096
    assert (physical-cal.signed_homing(entry['homing_raw']))%4096==entry['position']
    assert entry['min']<entry['position']<entry['max']
    assert entry['max']-entry['min']==484


def test_start_persists_all_original_bytes_and_releases_without_goal(runtime):
    c=runtime.manual_calibration;c.start(True)
    backup=json.loads(c.path.read_text(encoding='utf-8'))['register_backup']
    assert len(backup)==16 and set(c.samples)==set(cal.KEYS)
    assert all(m['torque']==0 for m in runtime.motors().values())
    assert all(address in (40,46) for p in runtime.session.ports.values() for _,address,_ in p.writes)
    assert all(reg[40]==1 for reg in backup.values()),'Original torque state must be retained in evidence'


def test_start_backup_failure_and_existing_session_do_not_release(runtime,monkeypatch):
    c=runtime.manual_calibration
    runtime.controller['controller']='online'
    with pytest.raises(ValueError):c.start(True)
    runtime.controller['controller']='offline'
    monkeypatch.setattr(c,'persist',lambda:(_ for _ in ()).throw(OSError('disk full')))
    with pytest.raises(OSError):c.start(True)
    assert all(not p.writes for p in runtime.session.ports.values())


@pytest.mark.parametrize('action',[lambda r:r.begin(True),lambda r:r.begin_trial(True),
 lambda r:r.begin_whole_robot(True),lambda r:r.begin_original('preview',True),lambda r:r.torque(True),lambda r:r.prepare_hardware(True)])
def test_manual_collection_excludes_all_torque_and_takeover_entries(runtime,action):
    runtime.manual_calibration.start(True)
    before=sum(len(p.writes) for p in runtime.session.ports.values())
    with pytest.raises(ValueError,match='手动标定'):action(runtime)
    assert sum(len(p.writes) for p in runtime.session.ports.values())==before


def test_range_confirmation_and_pose_steps_require_real_samples(runtime):
    c=runtime.manual_calibration;c.start(True)
    with pytest.raises(ValueError,match='逐项'):c.capture('ranges',[])
    with pytest.raises(ValueError,match='有效行程'):c.capture('ranges',list(cal.KEYS))
    with pytest.raises(ValueError,match='步骤'):c.capture('left_a')
    for s in c.samples.values():s.low-=100;s.high+=100
    c.capture('ranges',list(cal.KEYS));assert c.step=='left_a'
    for sample in c.samples.values():
        sample.recent.clear();sample.recent.append((sample.at,sample.current))
    with pytest.raises(ValueError,match='稳定'):c.capture('left_a')
    completed_measurements(c);c.step='left_b'
    with pytest.raises(ValueError,match='45'):c.capture('left_b')


def test_pose_geometry_sets_absolute_model_angle_and_observed_directions(runtime):
    c=runtime.manual_calibration;c.start(True);completed_measurements(c)
    data=cal.build_profile(c.samples,c.captures,c.backup,c.wheels,c.confirmed_keys)
    assert data['joints']['COM3:2']['reference_deg']==pytest.approx(-cal.math.degrees(cal.O1))
    assert data['joints']['COM3:3']['reference_deg']==pytest.approx(cal.math.degrees(cal.O2))
    assert data['joints']['COM3:4']['direction']==1 and data['joints']['COM4:4']['direction']==-1
    assert data['wheels']['geometry_source']=='published_design'
    assert data['wheels']['radius_m']==.0635
    c.captures['left_c']['positions']['COM3:2']+=200
    with pytest.raises(ValueError,match='只抬'):cal.build_profile(c.samples,c.captures,c.backup,c.wheels,c.confirmed_keys)


def test_apply_observed_ranges_enables_whole_mode_but_keeps_torque_off(runtime):
    c=runtime.manual_calibration;c.start(True);completed_measurements(c)
    c.apply(True)
    assert c.applied and not c.active and c.step=='complete'
    assert runtime.whole_robot_setup()['ready']
    data=json.loads(c.profile_path.read_text(encoding='utf-8'))
    assert data['full_manual_range_verified'] and len(data['joints'])==14
    assert all(m['torque']==0 for m in runtime.motors().values())
    runtime.pad_time=time.monotonic();runtime.begin_whole_robot(True)
    assert runtime.control_profile=='original_hardware'
    assert all(v['ready'] for v in runtime.readiness().values())
    assert all(e['min']==e['device_min'] and e['max']==e['device_max'] for e in runtime.envelopes.values())


def test_batch_failure_after_homing_ack_loss_restores_every_touched_servo(runtime,monkeypatch):
    c=runtime.manual_calibration;c.start(True);completed_measurements(c)
    original=commissioning.write_register;hits=[]
    def write(p,i,address,value,length):
        original(p,i,address,value,length)
        if address==31 and len(hits)<3:
            hits.append(i)
            if len(hits)==3:raise RuntimeError('homing applied; ACK lost')
    monkeypatch.setattr(commissioning,'write_register',write)
    with pytest.raises(RuntimeError,match='ACK lost'):c.apply(True)
    assert c.active and c.error and not c.applied and not c.profile_path.exists()
    for key,before in c.backup.items():
        port,i=cal.split_key(key);reg=runtime.session.ports[port].regs[i]
        assert reg[:40]==before[:40] and reg[40]==0
        assert word(reg,48)==word(before,48)


def test_live_sample_failure_prevents_apply_and_never_writes_a_goal(runtime):
    c=runtime.manual_calibration;c.start(True);completed_measurements(c)
    c.samples['COM3:2'].at-=3
    runtime.scan()
    assert c.error and '2秒' in c.error
    with pytest.raises(ValueError):c.apply(True)
    assert all(address!=42 for p in runtime.session.ports.values() for _,address,_ in p.writes)
    c.cancel();assert not c.active


class ManualTimer:
    def __init__(self,delay,callback,args=()):self.callback=callback;self.args=args;self.daemon=False
    def start(self):pass
    def cancel(self):pass
    def fire(self):self.callback(*self.args)


def test_base_needs_motion_stop_and_human_direction_observation(runtime,monkeypatch):
    monkeypatch.setattr(cal.threading,'Timer',ManualTimer)
    c=runtime.manual_calibration;c.start(True);c.step='base'
    for kind in ('forward','left'):
        c.base_test(kind,-1,1,True)
        with pytest.raises(ValueError,match='停车'):c.confirm_base(True,True)
        p=runtime.session.ports['COM4']
        for i in (9,10):set_word(p.regs[i],56,word(p.regs[i],56)+10)
        c.timer.fire()
    assert set(c.base_tests)=={'forward','left'}
    with pytest.raises(ValueError,match='现场'):c.confirm_base(False,True)
    c.confirm_base(True,True)
    assert c.step=='review' and c.wheels['stop_verified']


def test_change_wheel_sign_discards_previous_direction_tests(runtime,monkeypatch):
    monkeypatch.setattr(cal.threading,'Timer',ManualTimer)
    c=runtime.manual_calibration;c.start(True);c.step='base'
    c.wheels={'left_sign':-1,'right_sign':1};c.base_tests={'forward':{'results':[]}}
    c.base_test('left',1,1,True)
    assert not c.base_tests
    c.cancel();assert all(reg[40]==0 for p in runtime.session.ports.values() for reg in p.regs.values())


def test_api_exposes_guided_actions_and_preview_has_no_device_access(runtime,monkeypatch):
    from fabrication.assembly.debug_console import app as web
    monkeypatch.setattr(web,'runtime',runtime);client=TestClient(web.app)
    assert client.post('/api/whole-calibration/start',json={}).status_code==409
    assert client.post('/api/whole-calibration/start',json={'confirmed':True}).status_code==200
    assert client.post('/api/whole-calibration/capture',json={'step':'ranges'}).status_code==409
    assert client.get('/api/original/status').json()['manual_calibration']['active']
    assert client.post('/api/whole-calibration/cancel',json={}).status_code==200
    monkeypatch.setattr(web,'runtime',orig.PreviewRuntime())
    assert client.post('/api/whole-calibration/start',json={'confirmed':True}).status_code==409


def test_calibrated_wrist_uses_observed_sign_when_switching_back_to_ik(runtime):
    c=runtime.manual_calibration;c.start(True);completed_measurements(c);c.apply(True)
    runtime.pad_time=time.monotonic();runtime.begin_whole_robot(True)
    before=word(runtime.session.ports['COM4'].regs[4],56)
    old=runtime.engine.q['right']['wrist']
    for _ in range(4):step(runtime,pad([0,0,0,-1,0,0],buttons=(5,)))
    after=word(runtime.session.ports['COM4'].regs[4],56)
    assert after<before and runtime.engine.q['right']['wrist']>old
    ref=runtime.reference('COM4',4)
    expected=ref['reference_deg']+(runtime.direct_targets[('COM4',4)]-ref['reference_raw'])*ref['direction']*360/4096
    assert runtime.engine.q['right']['wrist']==pytest.approx(expected)


def test_large_observed_gripper_range_obeys_raw_slew_limit(runtime):
    c=runtime.manual_calibration;c.start(True);completed_measurements(c)
    for side,port in [('left','COM3'),('right','COM4')]:
        c.captures[side+'_open']['positions'][f'{port}:6']=2950
        c.captures[side+'_closed']['positions'][f'{port}:6']=1050
    c.apply(True);runtime.pad_time=time.monotonic();runtime.begin_whole_robot(True)
    p=runtime.session.ports['COM3'];before=word(p.regs[6],56)
    step(runtime,pad([0,0,0,0,1,0]))
    after=word(p.regs[6],56)
    assert 0<before-after<=6,'Normalized grip changes must not exceed the physical 60 raw/s rate'


def test_confirmed_wheel_signs_drive_continuously_and_release_stops(runtime):
    c=runtime.manual_calibration;c.start(True);completed_measurements(c)
    c.wheels.update(left_sign=1,right_sign=-1);c.apply(True)
    runtime.pad_time=time.monotonic();runtime.begin_whole_robot(True)
    step(runtime,pad(hat=(0,-1)))
    p=runtime.session.ports['COM4']
    assert 0<word(p.regs[9],46)<32768 and word(p.regs[10],46)>32768
    assert runtime.engine.wheels[0]>0 and runtime.engine.wheels[1]<0
    step(runtime,pad())
    assert word(p.regs[9],46)==word(p.regs[10],46)==0


def test_folded_pose_is_explained_before_whole_start_enables_torque(runtime):
    c=runtime.manual_calibration;c.start(True);completed_measurements(c);c.apply(True)
    data=json.loads(c.profile_path.read_text(encoding='utf-8'))
    data['joints']['COM4:3']['reference_deg']=-170
    c.profile_path.write_text(json.dumps(data),encoding='utf-8')
    before=sum(len(p.writes) for p in runtime.session.ports.values())
    runtime.pad_time=time.monotonic()
    with pytest.raises(ValueError,match='姿态B'):runtime.begin_whole_robot(True)
    assert sum(len(p.writes) for p in runtime.session.ports.values())==before
