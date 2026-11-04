"""Visual obstacle gate: freshness rules and gated forward driving on a fake bus."""
import pytest
from fabrication.assembly.debug_console.test_whole_calibration import runtime  # noqa: F401
from fabrication.assembly.debug_console.test_bounded_travel import moving_wheels, stopped, travel_evidence
from fabrication.assembly.debug_console.obstacle_gate import PerceptionGate
from fabrication.assembly.debug_console.rotation_scan import bounded_drive
from fabrication.assembly.debug_console.torque_control import word


class Clock:
    def __init__(self):self.t=100.
    def __call__(self):return self.t


def message(seq, clear=1.0, **changes):
    return {'stream':'s1','seq':seq,'valid':True,'metric':True,'head_pose_ok':True,'clear_m':clear,'frame_age_ms':0.,**changes}


def test_gate_blocks_missing_stale_invalid_uncalibrated_and_close():
    clock=Clock();gate=PerceptionGate(clock)
    assert gate.check(.2)[0] is False
    assert gate.update(message(1))['accepted'] and gate.check(.5)[0]
    assert not gate.update(message(1, clear=5))['accepted'], 'repeated sequence must not refresh freshness'
    clock.t+=.4
    ok,reason,_=gate.check(.2);assert not ok and '过期' in reason
    for seq,change,word_ in ((2,{'valid':False,'reasons':['画面丢失']},'画面丢失'),(3,{'metric':False},'标定'),
                             (4,{'head_pose_ok':False},'导航姿态'),(5,{'clear_m':.3},'净空')):
        gate.update(message(seq,**change))
        ok,reason,_=gate.check(.5)
        assert not ok and word_ in reason
    assert gate.update({'stream':'s2','seq':0,'valid':True,'metric':True,'head_pose_ok':True,'clear_m':2})['accepted']
    assert gate.snapshot()['blocks']>=5
    with pytest.raises(ValueError):gate.update({'stream':'s3','seq':1,'clear_m':float('nan')})


class ScriptedGate:
    """Clear until a given number of checks, then report an obstacle."""
    def __init__(self,block_after=None,clear=2.):
        self.calls=0;self.block_after=block_after;self.clear=clear
    def check(self,needed):
        self.calls+=1
        if self.block_after is not None and self.calls>self.block_after:
            return False,'前方净空0.10m',{'clear_m':.1}
        return True,None,{'clear_m':self.clear}


def test_clear_gate_drives_to_target(runtime):
    p=moving_wheels(runtime)
    result=bounded_drive(runtime,lambda:None,ScriptedGate(),.05,max_raw=120)
    assert result['stopped_by']=='target' and result['gate_checks']>=3 and result['min_clear_m']==2.
    stopped(p)


def test_obstacle_mid_drive_is_a_safety_stop_not_a_fault(runtime):
    p=moving_wheels(runtime,increment=10)
    result=bounded_drive(runtime,lambda:None,ScriptedGate(block_after=4),.2,max_raw=120)
    assert result['stopped_by']=='obstacle_gate' and '净空' in result['gate_block']['reason']
    assert 0<min(abs(v) for v in result['wheel_delta_raw'].values())<result['target_counts']
    assert result['stop_trace'][-1]['after_zero_ms']>=200
    stopped(p)


def test_blocked_gate_never_writes_motion(runtime):
    p=moving_wheels(runtime);p.writes.clear()
    with pytest.raises(ValueError,match='视觉避障未放行'):
        bounded_drive(runtime,lambda:None,ScriptedGate(block_after=0),.1)
    assert not any(addr==46 and any(values) for _,addr,values in p.writes)
    stopped(p)


def test_perception_going_stale_stops_the_drive(runtime):
    clock=Clock();gate=PerceptionGate(clock);gate.update(message(1,clear=3.))
    p=moving_wheels(runtime,increment=10);original=p.write;reads=[0]
    def write(packet):
        if packet[2]==9 and packet[4]==2 and word(p.regs[9],46):
            reads[0]+=1
            if reads[0]==6:clock.t+=1.  # no new message arrives for a second
        return original(packet)
    p.write=write
    result=bounded_drive(runtime,lambda:None,gate,.3,max_raw=120)
    assert result['stopped_by']=='obstacle_gate' and '过期' in result['gate_block']['reason']
    stopped(p)


@pytest.mark.parametrize('changes',[{'distance_m':-.05},{'distance_m':.7},{'max_raw':221},{'stop_margin_m':.01},{'direction':-1}])
def test_drive_step_validation(runtime,changes):
    runtime.perception_gate=PerceptionGate()
    step={**travel_evidence(runtime),'op':'drive','distance_m':.2}
    runtime.agent._validate([step,{'op':'snapshot'}])
    with pytest.raises(ValueError):runtime.agent._validate([{**step,**changes}])


def test_drive_requires_gate_and_reports_gate_state(runtime):
    moving_wheels(runtime)  # sets the wheel positions before the photo evidence is recorded
    step={**travel_evidence(runtime),'op':'drive','distance_m':.05}
    with pytest.raises(ValueError,match='闸门'):runtime.agent._validate([step])
    # Frozen clock: the fake run is slower than real perception's 0.35 s refresh.
    runtime.perception_gate=PerceptionGate(Clock());runtime.perception_gate.update(message(1,clear=2.))
    runtime.agent.start([step],True,background=False)
    result=runtime.agent.results[0]
    assert result['status']=='done',result.get('message')
    assert result['stopped_by']=='target' and result['gate_final']['present']


def test_snapshot_reuses_shared_stream_frame_when_camera_is_busy(runtime,monkeypatch):
    agent=runtime.agent
    runtime.camera_health[0]['state']='在线'
    agent.shared_frame=lambda camera:b'\xff\xd8shared\xff\xd9'
    monkeypatch.setattr(agent,'_capture',lambda camera,path:path.write_bytes(b'direct'))
    agent.start([{'op':'snapshot','cameras':[0,1]}],True,background=False)
    shots=agent.results[0]['shots']
    assert shots[0]['source']=='shared_stream' and 'source' not in shots[1]
    assert (agent.dir/shots[0]['file']).read_bytes().startswith(b'\xff\xd8')


def test_fresh_message_cannot_hide_an_old_source_image():
    clock=Clock();gate=PerceptionGate(clock)
    gate.update(message(1,frame_age_ms=300.))
    assert gate.check(.2)[0]
    clock.t += .06
    ok,reason,_=gate.check(.2)
    assert not ok and '源画面过期' in reason
    assert gate.snapshot()['effective_frame_age_ms'] == 360
    gate.update(message(2,frame_age_ms=None))
    assert not gate.check(.2)[0]
    for bad in [True,-1.,float('nan'),'0']:
        with pytest.raises(ValueError):gate.update(message(3,frame_age_ms=bad))
