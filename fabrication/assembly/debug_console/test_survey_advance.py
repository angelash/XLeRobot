"""Forward surveying cannot chain, reuse stale images, or leave wheel drive on."""
from datetime import datetime,timedelta
import json
import time
import pytest
from fabrication.assembly.debug_console.test_whole_calibration import runtime,set_word  # noqa: F401
from fabrication.assembly.debug_console.rotation_scan import bounded_advance
from fabrication.assembly.debug_console.torque_control import word


def wheels(runtime,wrong_direction=False,uneven=False):
    port=runtime.session.ports['COM4'];original=port.write
    def write(packet):
        result=original(packet);i=packet[2]
        if i in (9,10) and packet[4]==3 and packet[5]==46 and word(port.regs[i],46):
            velocity=word(port.regs[i],46)
            delta=(1 if wrong_direction or not velocity&0x8000 else -1)*(35 if uneven and i==10 else 12)
            set_word(port.regs[i],56,(word(port.regs[i],56)+delta)%4096)
        return result
    port.write=write
    return port


def evidence(agent,runtime,age=0,cameras=(0,2)):
    runtime.scan()
    rid='20261102-000001';folder=agent.root/rid;folder.mkdir(parents=True,exist_ok=True)
    shots=[]
    for camera in cameras:
        name=f'frame{camera}.jpg';(folder/name).write_bytes(b'jpg');shots.append({'camera':camera,'file':name})
    positions={f'{p}:{i}':m['position'] for (p,i),m in runtime.motors().items()}
    row={'op':'snapshot','status':'done','at':(datetime.now().astimezone()-timedelta(seconds=age)).isoformat(),
         'shots':shots,'positions':{k:v for k,v in positions.items() if k not in ('COM4:9','COM4:10')},
         'wheel_positions':{k:positions[k] for k in ('COM4:9','COM4:10')}}
    (folder/'result.json').write_text(json.dumps({'error':None,'results':[row]}),encoding='utf-8')
    agent.run_id=rid
    return {'op':'advance','duration':.04,'max_raw':80,'observation_run_id':rid,'clearance_confirmed':True}


def test_forward_pulse_has_opposite_signs_and_verified_stop(runtime):
    p=wheels(runtime)
    result=bounded_advance(runtime,lambda:None,duration=.04)
    assert result['wheel_delta_raw']=={9:-12,10:12}
    assert result['rotation_command_rad_s']==0 and result['direction']=='forward'
    assert all(word(p.regs[i],46)==0 and p.regs[i][40]==0 for i in (9,10))
    assert result['stop_trace'][-1]['after_zero_ms']>=200


@pytest.mark.parametrize('kwargs',[{'duration':.651},{'duration':True},{'duration':float('nan')},{'max_raw':121},{'max_raw':True}])
def test_advance_bounds_reject_before_writes(runtime,kwargs):
    p=wheels(runtime);p.writes.clear()
    with pytest.raises(ValueError):bounded_advance(runtime,lambda:None,**kwargs)
    assert not p.writes


@pytest.mark.parametrize('wrong,uneven',[(True,False),(False,True)])
def test_wrong_or_uneven_travel_fails_with_drive_off(runtime,wrong,uneven):
    p=wheels(runtime,wrong,uneven)
    with pytest.raises(RuntimeError,match='不对称、反向或超限'):
        bounded_advance(runtime,lambda:None,duration=.04)
    assert all(word(p.regs[i],46)==0 and p.regs[i][40]==0 for i in (9,10))


def test_advance_expiry_is_independent_of_delayed_action_loop(runtime):
    p=wheels(runtime);checked=False
    def check():
        nonlocal checked
        if not runtime.io._is_owned() and any(word(p.regs[i],46) for i in (9,10)) and not checked:
            time.sleep(.15)
            assert all(word(p.regs[i],46)==0 for i in (9,10));checked=True
    bounded_advance(runtime,check,duration=.04)
    assert checked


def test_advance_partial_write_failure_stops_both_wheels(runtime):
    p=wheels(runtime);original=p.write
    def write(packet):
        if packet[2]==10 and packet[4]==3 and packet[5]==46 and any(packet[6:8]):raise OSError('lost ACK')
        return original(packet)
    p.write=write
    with pytest.raises(OSError,match='lost ACK'):bounded_advance(runtime,lambda:None,duration=.04)
    assert all(word(p.regs[i],46)==0 and p.regs[i][40]==0 for i in (9,10))


def test_each_advance_needs_recent_floor_views_and_cannot_chain(runtime):
    agent=runtime.agent;step=evidence(agent,runtime)
    agent._validate([step,{'op':'snapshot'}])
    for invalid in ([step,step],[{'op':'move','targets':{'head:7':2000}},step],[step,{'op':'wait'}]):
        with pytest.raises(ValueError,match='每次只允许'):agent._validate(invalid)
    for changes in ({'clearance_confirmed':False},{'duration':.66},{'direction':-1},{'omega':1}):
        with pytest.raises(ValueError):agent._validate([{**step,**changes}])
    agent.consumed_drive_evidence.add(step['observation_run_id'])
    with pytest.raises(ValueError,match='已用过'):agent._validate([step])


@pytest.mark.parametrize('age,cameras',[(61,(0,2)),(-5,(0,2)),(0,(0,)),(0,(1,2))])
def test_stale_future_or_incomplete_evidence_blocks_hardware(runtime,age,cameras):
    agent=runtime.agent;step=evidence(agent,runtime,age,cameras)
    before={p:len(s.writes) for p,s in runtime.session.ports.items()}
    with pytest.raises(ValueError):agent.start([step],True,background=False)
    assert before=={p:len(s.writes) for p,s in runtime.session.ports.items()}


def test_pose_change_after_photo_requires_new_observation(runtime):
    agent=runtime.agent;step=evidence(agent,runtime)
    runtime.report['ports'][0]['motors'][0]['position']+=11
    with pytest.raises(ValueError,match='姿态或轮组位置已变'):agent._validate([step])


def test_evidence_failure_after_preparation_still_releases_wheels(runtime,monkeypatch):
    agent=runtime.agent;step=evidence(agent,runtime);count=0
    validate=agent._advance_evidence
    def changed(value):
        nonlocal count
        count+=1
        if count==2:raise ValueError('photo expired during preparation')
        return validate(value)
    monkeypatch.setattr(agent,'_advance_evidence',changed)
    agent.start([step],True,background=False)
    assert agent.error and len(agent.results)==1
    p=runtime.session.ports['COM4']
    assert all(p.regs[i][40]==0 and word(p.regs[i],46)==0 for i in (9,10))
