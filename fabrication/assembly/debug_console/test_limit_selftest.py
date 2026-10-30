"""Powered limit self-test on the fake STS bus with simulated hard stops."""
import json
import time
import pytest
from fabrication.assembly.debug_console import limit_selftest as lt
from fabrication.assembly.debug_console.test_original_teleop import pad
from fabrication.assembly.debug_console.test_whole_calibration import runtime,set_word  # noqa: F401 (fixture)
from fabrication.assembly.debug_console.torque_control import word


def add_stops(r,stops,after=None):
    """Clamp positions at mechanical stops after each goal write."""
    count=[0]
    for name,port in r.session.ports.items():
        original=port.write
        def write(packet,p=port,name=name,original=original):
            result=original(packet)
            i=packet[2]
            if packet[4]==3 and packet[5]==42:
                count[0]+=1
                if (name,i) in stops:
                    lo,hi=stops[(name,i)];set_word(p.regs[i],56,max(lo,min(hi,word(p.regs[i],56))))
                if after and count[0]==after[0]:after[1]()
            return result
        port.write=write


@pytest.fixture
def st(runtime):
    s=runtime.limit_test;s.tick=0;s.require_pad=False
    runtime.pad_time=time.monotonic()
    return s


def reg(r,port,i):return r.session.ports[port].regs[i]


def test_probe_finds_both_stops_returns_to_start_and_writes_margined_limits(runtime,st):
    add_stops(runtime,{('COM3',2):(1700,2400)})
    st.start(['left:lift'],True,background=False)
    res=st.results['left:lift']
    assert res['status']=='done' and res['min_kind']==res['max_kind']=='stall'
    assert (res['min'],res['max'])==(1700+lt.MARGIN_RAW,2400-lt.MARGIN_RAW)
    r2=reg(runtime,'COM3',2)
    assert (word(r2,9),word(r2,11))==(res['min'],res['max']) and word(r2,56)==2050
    assert word(r2,48)==st.torque_backup['COM3:2'],'Torque ceiling is restored'
    profile=json.loads(st.profile_path.read_text(encoding='utf-8'))
    assert profile['joints']['COM3:2']['range_verified'] and profile['joints']['COM3:2']['selftest']['min_kind']=='stall'
    assert 'COM3:2' in runtime.saved_ranges(),'The runtime sees the new limits immediately'
    assert not st.running and st.error is None


def test_joint_without_hard_stop_is_bounded_by_exploration_cap(runtime,st):
    st.start(['left:roll'],True,background=False)
    res=st.results['left:roll'];extend=round(60*4096/360)
    assert res['min_kind']==res['max_kind']=='bound'
    assert (res['min'],res['max'])==(1800-extend,2300+extend)


def test_cap_reached_a_few_counts_short_is_not_a_timeout(runtime,st):
    extend=round(30*4096/360)
    add_stops(runtime,{('COM3',8):(0,2300+extend-8)})
    st.start(['head:tilt'],True,background=False)
    res=st.results['head:tilt']
    assert res['status']=='done' and res['max_kind']=='bound' and res['max']==2300+extend-8


def test_preposed_joint_resting_short_of_target_counts_as_arrived(runtime,st):
    # Wrist pitch sags 20 counts below its mid target under gravity.
    port=runtime.session.ports['COM3'];original=port.write
    def sag(packet):
        result=original(packet)
        if packet[2]==4 and packet[4]==3 and packet[5]==42:set_word(port.regs[4],56,word(port.regs[4],56)-20)
        return result
    port.write=sag;set_word(port.regs[4],9,1500);set_word(port.regs[4],11,2900)
    st.start(['left:elbow'],True,background=False)
    assert st.results['left:elbow']['status']=='done',st.results['left:elbow']


def test_blocked_prepose_stays_put_and_the_probe_still_runs(runtime,st):
    # The folded forearm cannot unfold past 2150 while the shoulder is down.
    add_stops(runtime,{('COM3',3):(2150,2400),('COM3',2):(1700,2400)})
    set_word(reg(runtime,'COM3',3),56,2300);runtime.scan()
    st.start(['left:lift'],True,background=False)
    assert st.results['left:lift']['status']=='done',st.results['left:lift']
    assert any('预置未到中位' in e['message'] for e in st.events)


def test_stop_inside_saved_range_keeps_the_reachable_saved_end(runtime,st):
    add_stops(runtime,{('COM3',3):(1900,2450)})
    st.start(['left:elbow'],True,background=False)
    res=st.results['left:elbow']
    assert res['min_kind']=='inside' and res['min']==1800,'A hand-reached end is never narrowed'
    assert res['max_kind']=='stall' and res['max']==2450-lt.MARGIN_RAW


def test_abort_keeps_only_the_explored_range_and_restores_torque_ceiling(runtime,st):
    add_stops(runtime,{('COM3',8):(1500,2600)},after=(15,lambda:st.stop('测试停止')))
    st.start(['head:tilt'],True,background=False)
    r8=reg(runtime,'COM3',8);pos=word(r8,56)
    assert st.results['head:tilt']['status']=='aborted' and st.error=='测试停止'
    assert word(r8,9)==min(1800,pos-8) and word(r8,11)==max(2300,pos+8)
    assert word(r8,48)==st.torque_backup['COM3:8'] and word(r8,42)==pos


def test_joint_resting_just_past_its_limit_is_reincluded_before_holding(runtime,st):
    r1=reg(runtime,'COM3',1);set_word(r1,56,1795);runtime.scan()
    joints={f'COM3:{i}':{'position':p,'homing_raw':word(reg(runtime,'COM3',i),31),'mid':2047} for i,p in ((2,2050),(3,2050),(4,2050))}
    st.profile_path.write_text(json.dumps({'serials':lt.EXPECTED_SERIALS,'joints':joints}),encoding='utf-8')
    st.start(['left:pan'],True,background=False)
    assert st.results['left:pan']['status']=='done' and word(r1,9)<=1795-8


def test_shoulder_pan_requires_a_folded_arm_or_an_automatic_fold(runtime,st):
    with pytest.raises(ValueError,match='全收起'):st.start(['left:pan'],True,background=False)
    joints={f'COM3:{i}':{'position':p,'homing_raw':word(reg(runtime,'COM3',i),31),'mid':2047,'min':1800,'max':2300}
            for i,p in ((2,1850),(3,2250),(4,2000))}
    st.profile_path.write_text(json.dumps({'serials':lt.EXPECTED_SERIALS,'joints':joints}),encoding='utf-8')
    runtime.scan();assert st.folded('left') is False
    st.start(['left:fold','left:pan'],True,background=False)
    assert st.results['left:fold']['status']=='done' and st.results['left:pan']['status']=='done'
    assert word(reg(runtime,'COM3',2),56)==1850 and st.folded('left')


def test_selftest_blocks_takeover_and_needs_confirmation(runtime,st):
    with pytest.raises(ValueError,match='空间'):st.start(['head:pan'],False,background=False)
    st.running=True
    with pytest.raises(ValueError,match='自检'):runtime.begin_preset(True)
    st.running=False


def test_api_and_pad_dead_man(runtime,st,monkeypatch):
    from fastapi.testclient import TestClient
    from fabrication.assembly.debug_console import app as web
    monkeypatch.setattr(web,'runtime',runtime);client=TestClient(web.app)
    st.require_pad=True;runtime.gamepad=pad([0,.6,0,0,0,0]);runtime.pad_time=time.monotonic()
    response=client.post('/api/limit-test/start',json={'items':['head:pan'],'confirmed':True})
    assert response.status_code==409 and '回中' in response.json()['detail']
    # A sleeping (disconnected) gamepad does not block a delegated run.
    runtime.gamepad={'connected':False,'buttons':[],'axes':[],'hat':[0,0]}
    assert not st._pad_active()
    assert client.post('/api/limit-test/stop').status_code==200
    assert 'limit_selftest' in client.get('/api/original/status').json()


def test_upright_pose_is_the_start_for_pitch_and_the_arm_returns_afterwards(runtime,st):
    goals={2:[],3:[],4:[]}
    port=runtime.session.ports['COM3'];original=port.write
    def spy(packet):
        if packet[4]==3 and packet[5]==42 and packet[2] in goals:goals[packet[2]].append(packet[6]+256*packet[7])
        return original(packet)
    port.write=spy
    st.set_upright('left','set',{'2':2250,'3':1900,'4':2100})
    assert st.upright_pose('left')=={2:2250,3:1900,4:2100}
    rows={x['id']:x for x in st.items()}
    assert rows['left:upright']['upright']=={'2':2250,'3':1900,'4':2100} and rows['left:pitch']['from_upright']
    assert 'left:upright' not in lt.RECOMMENDED,'Only run when the user ticks it'
    st.start(['left:pitch'],True,background=False)
    res=st.results['left:pitch']
    assert res['status']=='done' and res['from_upright'],res
    assert 2250 in goals[2] and 1900 in goals[3],'The arm is raised to the upright pose first'
    assert all(word(reg(runtime,'COM3',i),56)==2050 for i in (2,3,4)),'The arm returns to where the test found it'


def test_backoff_stuck_in_overload_protection_waits_and_retries(runtime,st):
    port=runtime.session.ports['COM3'];original=port.write;state={'stuck':False,'seen':False}
    def write(packet):
        result=original(packet)
        if packet[2]==8 and packet[4]==3 and packet[5]==42:
            r=port.regs[8]
            if word(r,56)<=1600:
                set_word(r,56,1600)
                if not state['seen']:state.update(seen=True,stuck=True)
            if state['stuck']:set_word(r,56,1600);r[65]=0x20
            else:r[65]=0
        return result
    port.write=write
    def recover(seconds):state['stuck']=False
    st._wait=recover
    st.start(['head:tilt'],True,background=False)
    res=st.results['head:tilt']
    assert res['status']=='done' and res['min']==1600+lt.MARGIN_RAW,res
    assert any('退离止挡' in e['message'] and '过载' in e['message'] and '重试' in e['message'] for e in st.events)


def test_backoff_that_never_moves_is_reported_with_the_servo_state(runtime,st):
    port=runtime.session.ports['COM3'];original=port.write
    def write(packet):
        result=original(packet)
        if packet[2]==8 and packet[4]==3 and packet[5]==42 and word(port.regs[8],56)<=1650:
            set_word(port.regs[8],56,1600);port.regs[8][65]=0x20
        return result
    port.write=write;st.recover_wait=0
    st.start(['head:tilt'],True,background=False)
    res=st.results['head:tilt']
    assert res['status']=='failed' and '退离止挡' in res['message'] and '过载' in res['message'],res
    assert sum('重试' in e['message'] for e in st.events)==lt.RECOVER_TRIES-1


def test_probe_torque_stays_under_the_overload_threshold(runtime,st):
    port=runtime.session.ports['COM3'];port.regs[3][36]=80;written=[];original=port.write
    def spy(packet):
        if packet[2]==3 and packet[4]==3 and packet[5]==48:written.append(packet[6]+256*packet[7])
        return original(packet)
    port.write=spy
    st.start(['left:elbow'],True,background=False)
    assert st.results['left:elbow']['status']=='done'
    assert 800-lt.PROBE_TORQUE_MARGIN in written and lt.ARM_TESTS['elbow']['torque'] not in written


def test_cleanup_restores_exact_limits_when_the_joint_stopped_inside(runtime,st):
    r8=reg(runtime,'COM3',8)
    for pos,expected in ((2296,(1800,2300)),(2340,(1800,2348))):
        set_word(r8,9,1500);set_word(r8,11,2600);set_word(r8,56,pos)
        st.limit_backup={'COM3:8':(1800,2300)};st.finalized=set();st.torque_backup={}
        st._cleanup()
        assert (word(r8,9),word(r8,11))==expected,'Widen only when the joint is really outside'


def test_upright_item_needs_a_record_and_the_api_records_it(runtime,st,monkeypatch):
    with pytest.raises(ValueError,match='竖直'):st.start(['left:upright'],True,background=False)
    from fastapi.testclient import TestClient
    from fabrication.assembly.debug_console import app as web
    monkeypatch.setattr(web,'runtime',runtime);client=TestClient(web.app)
    assert client.post('/api/limit-test/upright',json={'side':'left','action':'record'}).status_code==200
    assert st.upright_pose('left')=={2:2050,3:2050,4:2050}
    bad=client.post('/api/limit-test/upright',json={'side':'left','action':'set','positions':{'2':4000}})
    assert bad.status_code==409 and '超出' in bad.json()['detail']
    st.start(['left:upright'],True,background=False)
    assert st.results['left:upright']['status']=='done'
    assert client.post('/api/limit-test/upright',json={'side':'left','action':'clear'}).status_code==200
    assert st.upright_pose('left') is None
