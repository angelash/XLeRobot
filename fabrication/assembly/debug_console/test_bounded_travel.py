"""Packet-level fake-bus tests for distance stops and guarded returns."""
from datetime import datetime
import json
import pytest
from fabrication.assembly.debug_console.test_whole_calibration import runtime,set_word  # noqa: F401
from fabrication.assembly.debug_console.test_survey_advance import evidence
from fabrication.assembly.debug_console.rotation_scan import bounded_travel
from fabrication.assembly.debug_console.torque_control import word


def moving_wheels(runtime,increment=40,uneven=False,wrong=False,coast=0,cap=None):
    p=runtime.session.ports['COM4'];original=p.write
    for i in (9,10):set_word(p.regs[i],56,4090)
    def write(packet):
        i=packet[2];reg=p.regs[i]
        if i in (9,10) and packet[4]==2 and reg[40] and word(reg,46):
            sign=-1 if word(reg,46)&0x8000 else 1
            if wrong:sign=-sign
            delta=(increment*3 if uneven and i==10 else increment)*sign
            if cap is not None and abs((word(reg,56)-4090+2048)%4096-2048)>=cap:
                delta=0                        # blocked: the wheel no longer gains
            set_word(reg,56,(word(reg,56)+delta)%4096)
        if i in (9,10) and packet[4]==3 and packet[5]==40 and packet[6]==0 and word(reg,46)==0:
            set_word(reg,56,(word(reg,56)+(coast if i==10 else -coast))%4096)
        return original(packet)
    p.write=write
    return p


def stopped(p):
    assert all(word(p.regs[i],46)==0 and p.regs[i][40]==0 for i in (9,10))


@pytest.mark.parametrize('distance',[.05,-.05])
def test_count_stop_wraps_both_directions_and_reports_estimate(runtime,distance):
    p=moving_wheels(runtime)
    result=bounded_travel(runtime,lambda:None,distance)
    assert result['target_counts']==513
    assert 503<=min(abs(v) for v in result['wheel_delta_raw'].values())<=565
    assert result['wheel_delta_raw'][9]*distance<0<result['wheel_delta_raw'][10]*distance
    assert result['duration_s']<result['max_duration_s']==6
    assert result['wheel_distance_m_estimate']*distance>0
    assert result['stop_trace'][-1]['after_zero_ms']>=200
    stopped(p)


@pytest.mark.parametrize('kwargs',[{'distance_m':True},{'distance_m':.051},{'distance_m':0},
    {'distance_m':float('nan')},{'distance_m':float('inf')},{'max_raw':221},{'max_raw':True}])
def test_invalid_distance_or_speed_never_writes(runtime,kwargs):
    p=moving_wheels(runtime)
    with pytest.raises(ValueError):bounded_travel(runtime,lambda:None,**kwargs)
    assert not p.writes


@pytest.mark.parametrize('kwargs',[{'wrong':True},{'uneven':True},{'increment':0},{'coast':100}])
def test_failed_motion_stops_and_does_not_report_success(runtime,kwargs):
    p=moving_wheels(runtime,**kwargs)
    with pytest.raises(RuntimeError):bounded_travel(runtime,lambda:None)
    stopped(p)


def test_abort_preserves_trace_and_stops(runtime):
    p=moving_wheels(runtime)
    def check():
        if not runtime.io._is_owned() and any(word(p.regs[i],46) for i in (9,10)):
            raise RuntimeError('operator input')
    with pytest.raises(RuntimeError,match='operator input') as caught:bounded_travel(runtime,check)
    assert 'readbacks' in caught.value.rotation
    stopped(p)


def test_a_blocked_wheel_mid_leg_stops_as_contact_not_fault(runtime):
    import time
    p=moving_wheels(runtime,increment=20,cap=200)
    began=time.monotonic()
    result=bounded_travel(runtime,lambda:None,-.05)
    assert result['stopped_by']=='stall' and result['stall']['reason']=='stall'
    assert time.monotonic()-began<2.5
    assert 190<=min(abs(v) for v in result['wheel_delta_raw'].values())<=230
    stopped(p)


def test_partial_velocity_write_still_stops_both(runtime):
    p=moving_wheels(runtime);original=p.write
    def fail(packet):
        if packet[2]==10 and packet[4]==3 and packet[5]==46 and any(packet[6:8]):raise OSError('lost ACK')
        return original(packet)
    p.write=fail
    with pytest.raises(OSError,match='lost ACK'):bounded_travel(runtime,lambda:None)
    stopped(p)


def test_temperature_spike_stops_immediately_and_preserves_raw_evidence(runtime):
    p=moving_wheels(runtime);original=p.write
    def spike(packet):
        i=packet[2]
        if i==10 and packet[4]==2 and word(p.regs[i],46):p.regs[i][63]=57
        return original(packet)
    p.write=spike
    with pytest.raises(RuntimeError,match='temperature=57') as caught:bounded_travel(runtime,lambda:None)
    assert caught.value.rotation['fault']['registers'][63]==57
    assert caught.value.rotation['fault']['motor']==10
    stopped(p)


def test_expiry_still_stops_when_count_monitor_is_delayed(runtime,monkeypatch):
    import threading,time
    from fabrication.assembly.debug_console import rotation_scan
    p=moving_wheels(runtime,increment=1);real_timer=threading.Timer;seen=[];observed=[]
    def timer(interval,callback):
        seen.append(interval)
        return real_timer(.08,callback)
    monkeypatch.setattr(rotation_scan.threading,'Timer',timer)
    def delayed():
        if not runtime.io._is_owned() and any(word(p.regs[i],46) for i in (9,10)):
            time.sleep(.35);stopped(p);observed.append(True)
    with pytest.raises(RuntimeError) as caught:bounded_travel(runtime,delayed)
    assert seen==[6.] and observed
    trace = caught.value.rotation
    assert trace['target_counts'] == 513 and trace['command_raw']
    assert trace['readbacks'][-1]['after_zero_ms'] >= 200
    assert max(abs(v) for v in trace['wheel_delta_raw'].values()) < 513
    stopped(p)


def travel_evidence(runtime):
    step=evidence(runtime.agent,runtime)
    return {k:v for k,v in {**step,'op':'travel','distance_m':.05,'boundary_id':'user-redline'}.items() if k!='duration'}


def test_travel_cannot_queue_skip_boundary_or_reuse_photos(runtime):
    a=runtime.agent;step=travel_evidence(runtime)
    a._validate([step,{'op':'snapshot'}])
    for steps in ([step,step],[step,{'op':'move','targets':{'head:7':2000}}]):
        with pytest.raises(ValueError):a._validate(steps)
    for changes in ({'boundary_id':''},{'distance_m':.1},{'distance_m':-.05},{'clearance_confirmed':False},
                    {'max_raw':221},{'return_run_id':'20261102-000002'}):
        with pytest.raises(ValueError):a._validate([{**step,**changes}])
    a.consumed_drive_evidence.add(step['observation_run_id'])
    with pytest.raises(ValueError):a._validate([step])


def return_evidence(runtime,step):
    a=runtime.agent;rid='20261102-000002';folder=a.root/rid;folder.mkdir()
    row={'op':'travel','status':'done','direction':'forward','boundary_id':step['boundary_id'],
         'at':datetime.now().astimezone().isoformat(),'requested_distance_m':.05,
         'stop_readback':{str(i):{'position':runtime.motors()[('COM4',i)]['position']} for i in (9,10)}}
    (folder/'result.json').write_text(json.dumps({'error':None,'results':[row]}),encoding='utf-8')
    return {**step,'distance_m':-.05,'return_run_id':rid},folder,row


def test_reverse_requires_same_boundary_unmoved_endpoint_and_unused_leg(runtime):
    a=runtime.agent;step=travel_evidence(runtime);back,folder,row=return_evidence(runtime,step)
    a._validate([back])
    row['boundary_id']='another-area'
    (folder/'result.json').write_text(json.dumps({'error':None,'results':[row]}),encoding='utf-8')
    with pytest.raises(ValueError,match='边界'):a._validate([back])
    row['boundary_id']=step['boundary_id'];row['stop_readback']['9']['position']+=3
    (folder/'result.json').write_text(json.dumps({'error':None,'results':[row]}),encoding='utf-8')
    with pytest.raises(ValueError,match='前进终点'):a._validate([back])
    a.consumed_return_evidence.add(back['return_run_id'])
    with pytest.raises(ValueError,match='尚未返回'):a._validate([back])


def test_travel_rechecks_expiring_evidence_after_preparation_and_releases(runtime,monkeypatch):
    a=runtime.agent;step=travel_evidence(runtime);original=a._advance_evidence;calls=0
    def expired(value):
        nonlocal calls
        calls+=1
        if calls==2:raise ValueError('expired after preparation')
        return original(value)
    monkeypatch.setattr(a,'_advance_evidence',expired)
    a.start([step],True,background=False)
    assert a.error and len(a.results)==1
    stopped(runtime.session.ports['COM4'])


def test_a_single_temperature_glitch_is_reread_and_recorded_not_a_stop(runtime):
    p=moving_wheels(runtime);original=p.write;reads={'n':0}
    def once(packet):
        i=packet[2]
        if i==9 and packet[4]==2 and word(p.regs[i],46):
            reads['n']+=1
            if reads['n']==4:
                p.regs[i][63]=49
                try:return original(packet)
                finally:p.regs[i][63]=35
        return original(packet)
    p.write=once
    result=bounded_travel(runtime,lambda:None,-.05)
    assert result['stopped_by']=='target' and len(result['read_glitches'])==1
    glitch=result['read_glitches'][0]
    assert glitch['motor']==9 and glitch['first'][63]==49 and glitch['reread'][63]==35
    stopped(p)


def test_recovered_temperature_jumps_do_not_accumulate_a_stop_budget(runtime):
    p=moving_wheels(runtime,increment=10);original=p.write;reads={9:0,10:0}
    def repeated(packet):
        i=packet[2]
        if i in reads and packet[4]==2 and word(p.regs[i],46):
            reads[i]+=1
            p.regs[i][63]=(92,52,35)[(reads[i]-1)%3]
        return original(packet)
    p.write=repeated
    result=bounded_travel(runtime,lambda:None,-.05)
    recovered=result['read_glitches']
    assert result['stopped_by']=='target' and len(recovered)>3
    assert all(g['kind']=='temperature' and g['recovered'] for g in recovered)
    assert all([r[63] for r in g['samples']]==[92,52,35] for g in recovered)
    stopped(p)


def test_recovered_voltage_faults_still_have_the_existing_count_limit(runtime):
    p=moving_wheels(runtime,increment=10);original=p.write;reads={9:0,10:0}
    def repeated(packet):
        i=packet[2]
        if i in reads and packet[4]==2 and word(p.regs[i],46):
            reads[i]+=1;p.regs[i][62]=110 if reads[i]%2 else 120
        return original(packet)
    p.write=repeated
    with pytest.raises(RuntimeError,match='读数反复异常') as caught:
        bounded_travel(runtime,lambda:None,-.05)
    assert len(caught.value.rotation['read_glitches'])==4
    assert all(g['kind']=='other' for g in caught.value.rotation['read_glitches'])
    stopped(p)


def test_a_temperature_spike_at_preflight_is_confirmed_before_motion(runtime):
    p=moving_wheels(runtime);original=p.write;injected=[]
    def once(packet):
        if packet[2]==9 and packet[4]==2 and not injected:
            injected.append(True);p.regs[9][63]=92
            try:return original(packet)
            finally:p.regs[9][63]=35
        return original(packet)
    p.write=once
    result=bounded_travel(runtime,lambda:None,-.05)
    assert result['stopped_by']=='target'
    assert result['read_glitches'][0]['phase']=='preflight'
    stopped(p)


@pytest.mark.parametrize('sign',[1,-1])
def test_translations_follow_the_camera_verified_chassis_direction(runtime,tmp_path,monkeypatch,sign):
    from fabrication.assembly.debug_console import rotation_scan
    path=tmp_path/'chassis-direction.json'
    path.write_text(json.dumps({'camera_forward_sign':sign,'evidence':['20261103-170422-reposition']}),encoding='utf-8')
    monkeypatch.setattr(rotation_scan,'CHASSIS_DIRECTION',path)
    moving_wheels(runtime)
    result=bounded_travel(runtime,lambda:None,-.05)
    assert result['direction']=='backward' and result['command_sign']==sign and result['direction_source']=='camera_verified'
    # uncalibrated "backward" turns wheel 9 positive (as on the robot, +120 at 16:58); a -1 sign flips it
    assert result['wheel_delta_raw'][9]*sign>0
    assert result['wheel_distance_m_estimate']<0


def test_without_evidence_the_direction_is_reported_unverified(runtime,tmp_path,monkeypatch):
    from fabrication.assembly.debug_console import rotation_scan
    path=tmp_path/'chassis-direction.json'
    path.write_text(json.dumps({'camera_forward_sign':-1}),encoding='utf-8')
    monkeypatch.setattr(rotation_scan,'CHASSIS_DIRECTION',path)
    assert rotation_scan.chassis_direction()==(1,'uncalibrated')

