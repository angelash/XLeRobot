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
    monkeypatch.setattr(orig,'CONTROLLER_SETTINGS',tmp_path/'controller-settings.json')
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
    assert c.step=='cards','A new session opens free collection, not the old wizard'
    c.step='ranges'  # The legacy step API remains guarded.
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
        c.base_test(kind,True)
        with pytest.raises(ValueError,match='停车'):c.confirm_base(True,True)
        p=runtime.session.ports['COM4']
        for i in (9,10):set_word(p.regs[i],56,word(p.regs[i],56)+10)
        c.timer.fire()
    assert set(c.base_tests)=={'forward','left'}
    with pytest.raises(ValueError,match='现场'):c.confirm_base(False,True)
    c.confirm_base(True,True)
    assert c.step=='review' and c.wheels['stop_verified']


def test_change_wheel_direction_discards_tests_and_drives_with_new_setting(runtime,monkeypatch):
    monkeypatch.setattr(cal.threading,'Timer',ManualTimer)
    c=runtime.manual_calibration;c.start(True);c.step='base'
    c.wheels={**cal.DESIGN_GEOMETRY,'base_forward':'a','base_turn':'normal'};c.base_tests={'forward':{'results':[]}}
    runtime.calibration_command('base-test',{'kind':'left','confirmed':True,'base_turn':'reverse'})
    assert not c.base_tests and runtime.settings.data['base_turn']=='reverse'
    p=runtime.session.ports['COM4']
    # Normal left turn commands both wheels positive; reversed turn is negative.
    assert word(p.regs[9],46)&0x8000 and word(p.regs[10],46)&0x8000
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
    assert 0<before-after<=12,'Normalized grip changes must respect the configured physical raw rate'


def test_forward_setting_drives_continuously_and_release_stops(runtime):
    c=runtime.manual_calibration;c.start(True);completed_measurements(c)
    c.apply(True)
    runtime.pad_time=time.monotonic();runtime.begin_whole_robot(True)
    step(runtime,pad(hat=(0,1)))
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


def test_supported_manual_swing_does_not_abort_other_joint_ranges():
    sample=cal.RangeSample(2000,1.)
    sample.update(2300,1.1)  # 26 degrees in 100ms, well below a half-turn.
    assert sample.high-sample.low==300


def test_checkpoint_recovery_keeps_known_arcs_and_resets_only_unknown_joint(runtime):
    c=runtime.manual_calibration;c.start(True)
    for sample in c.samples.values():sample.low-=150;sample.high+=150
    c.persist();c.error='interrupted';before=sum(len(p.writes) for p in runtime.session.ports.values())
    set_word(runtime.session.ports['COM3'].regs[4],56,3000)
    c.recover(c.id,True)
    assert c.error is None and c.active and c.step=='cards'
    assert c.samples['COM3:2'].high-c.samples['COM3:2'].low==300
    assert c.samples['COM3:4'].high==c.samples['COM3:4'].low==3000
    assert c.recovery_reset==[{'key':'COM3:4','label':'左臂腕俯仰'}]
    assert sum(len(p.writes) for p in runtime.session.ports.values())==before,'Recovery must not write any register'


def test_checkpoint_recovery_after_restart_anchors_a_recorded_cross_zero_arc(runtime):
    c=runtime.manual_calibration;c.start(True)
    sample=c.samples['COM3:5'];sample.low=3900;sample.high=4400;sample.current=4200;sample.raw=104
    set_word(runtime.session.ports['COM3'].regs[5],56,104);c.persist()
    restored=cal.WholeCalibration(runtime,c.path.parent/'new.json',c.profile_path)
    runtime.manual_calibration=restored
    assert not restored.active and restored.find_recoverable()['id']==c.id
    restored.recover(c.id,True)
    assert restored.samples['COM3:5'].current==4200 and restored.samples['COM3:5'].low==3900


@pytest.mark.parametrize('changed',['homing','mode','torque','pid','output'])
def test_checkpoint_cannot_resume_with_changed_hardware_configuration(runtime,changed):
    c=runtime.manual_calibration;c.start(True);c.error='interrupted';c.persist()
    reg=runtime.session.ports['COM3'].regs[2]
    if changed=='homing':set_word(reg,31,1)
    elif changed=='mode':reg[33]=1
    elif changed=='torque':reg[40]=1
    elif changed=='pid':reg[21]+=1
    elif changed=='output':set_word(reg,48,0)
    with pytest.raises(ValueError,match='不能恢复'):c.recover(c.id,True)


def test_recovery_requires_a_stopped_collection_and_rejects_path_input(runtime):
    c=runtime.manual_calibration;c.start(True)
    with pytest.raises(ValueError,match='无需恢复'):c.recover(c.id,True)
    c.error='interrupted'
    with pytest.raises(ValueError,match='编号'):c.recover('../devices',True)


def test_one_card_capture_and_delete_do_not_reorder_or_reset_other_cards(runtime):
    c=runtime.manual_calibration;c.start(True)
    for sample in c.samples.values():sample.low-=100;sample.high+=100
    stamp=time.monotonic()
    for sample in c.samples.values():
        sample.recent.clear()
        for delta in (-.15,-.1,-.05):sample.recent.append((stamp+delta,sample.current))
    c.capture_item('range',True);c.capture_item('left_a',True);c.capture_item('right_open',True)
    assert c.captures['left_a']['port']=='COM3' and c.captures['right_open']['port']=='COM4'
    before=deepcopy(c.captures['left_a']);c.delete_capture('right_open')
    assert c.captures['left_a']==before and 'right_open' not in c.captures and c.active


def test_disk_error_does_not_invalidate_completed_pose(tmp_path,monkeypatch):
    c=cal.WholeCalibration(object(),tmp_path/'c.json',tmp_path/'k.json')
    c.active=True;c.id='a'*32;c.samples={};
    c.captures['left_a']={'port':'COM3','positions':{}}
    monkeypatch.setattr(cal.os,'replace',lambda *_: (_ for _ in ()).throw(PermissionError('read only')))
    assert c.persist() is False and c.captures['left_a'] and c.active


def test_chassis_failure_is_isolated_and_does_not_end_manual_collection(runtime):
    c=runtime.manual_calibration;c.start(True);c.captures['left_a']={'port':'COM3','positions':{}}
    c.base_running='forward';c._chassis_failure('停车回读不符')
    assert c.active and c.error is None and 'left_a' in c.captures
    assert not c.wheels['verified'] and c.step_warnings


def test_merge_checkpoint_restores_completed_pose_cards_without_motor_io(runtime):
    c=runtime.manual_calibration;c.start(True)
    positions={key:s.current for key,s in c.samples.items()}
    c.captures={'left_a':{'captured_at':'old','port':'COM3','positions':positions}}
    c.persist();backup=c.path.with_name(c.id+'-before-recovery-12345678.json')
    atomic=cal.atomic_json;atomic(backup,json.loads(c.path.read_text(encoding='utf-8')))
    c.captures={};writes=sum(len(p.writes) for p in runtime.session.ports.values())
    result=c.merge_checkpoint(backup.stem,True)
    assert 'left_a' in c.captures and c.captures['left_a']['positions']==positions
    assert sum(len(p.writes) for p in runtime.session.ports.values())==writes


def test_clear_pose_cards_keeps_ranges_and_saves_old_cards_as_separate_evidence(runtime):
    c=runtime.manual_calibration;c.start(True)
    positions={key:s.current for key,s in c.samples.items()}
    c.captures={'left_a':{'port':'COM4','positions':positions}}
    c.persist();ranges=deepcopy(c.samples);count=len(list(c.path.parent.glob('*captures-before-rerecord*')))
    c.clear_captures(True)
    assert not c.captures and c.samples.keys()==ranges.keys()
    assert len(list(c.path.parent.glob('*captures-before-rerecord*')))==count+1


def test_latest_torque_off_checkpoint_resumes_after_service_restart(runtime):
    c=runtime.manual_calibration;c.start(True);c.persist()
    # Simulate an app restart: retain persisted evidence but replace live owner.
    runtime.manual_calibration=cal.WholeCalibration(runtime,c.path,c.profile_path)
    assert not runtime.manual_calibration.active
    resumed=runtime.manual_calibration.resume_latest()
    assert resumed['active'] and resumed['step']=='cards'


def test_recovered_pose_cards_follow_saved_physical_port_not_current_UI_side(runtime):
    c=runtime.manual_calibration;c.start(True)
    positions={key:s.current for key,s in c.samples.items()}
    c.captures={'right_a':{'port':'COM3','positions':positions},'left_a':{'port':'COM4','positions':positions}}
    c.persist();c.error='interrupt';c.persist()
    from fabrication.assembly.debug_console.control_settings import DEFAULTS
    runtime.settings.save({**DEFAULTS, 'arm_mapping':'swapped'})
    c.recover(c.id,True)
    assert c.captures['left_a']['port']=='COM4' and c.captures['right_a']['port']=='COM3'
