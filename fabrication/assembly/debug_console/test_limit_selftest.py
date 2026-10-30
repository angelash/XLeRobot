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
    assert not st.running and st.error is None


def test_joint_without_hard_stop_is_bounded_by_exploration_cap(runtime,st):
    st.start(['left:roll'],True,background=False)
    res=st.results['left:roll'];extend=round(60*4096/360)
    assert res['min_kind']==res['max_kind']=='bound'
    assert (res['min'],res['max'])==(1800-extend,2300+extend)


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
    assert word(r8,9)==min(1800,pos) and word(r8,11)==max(2300,pos)
    assert word(r8,48)==st.torque_backup['COM3:8'] and word(r8,42)==pos


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
