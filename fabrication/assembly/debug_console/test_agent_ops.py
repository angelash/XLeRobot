"""Assistant runs on the fake STS bus: motion, refusal, abort, capture, sampling, back-off trials."""
import json
import time
import pytest
from fabrication.assembly.debug_console.test_limit_selftest import add_stops,reg
from fabrication.assembly.debug_console.test_whole_calibration import runtime,set_word  # noqa: F401 (fixture)
from fabrication.assembly.debug_console.torque_control import word


@pytest.fixture
def agent(runtime):
    runtime.limit_test.tick=0;runtime.limit_test.require_pad=False;runtime.limit_test.recover_wait=0
    runtime.pad_time=time.monotonic()
    return runtime.agent


def test_move_reaches_targets_and_writes_a_run_folder(runtime,agent):
    agent.start([{'op':'move','targets':{'left:2':2200,'head:7':1900},'label':'抬肩'}],True,'测试',background=False)
    assert agent.error is None and agent.results[0]['status']=='done'
    assert word(reg(runtime,'COM3',2),56)==2200 and word(reg(runtime,'COM3',7),56)==1900
    assert (agent.dir/'plan.json').is_file() and (agent.dir/'result.json').is_file()
    assert json.loads((agent.dir/'steps.jsonl').read_text(encoding='utf-8').splitlines()[0])['positions']['COM3:2']==2200
    assert not runtime.limit_test.running and not agent.active


def test_targets_outside_limits_are_refused_and_stop_the_run(runtime,agent):
    agent.start([{'op':'move','targets':{'left:2':2500}},{'op':'move','targets':{'left:3':2200}}],True,background=False)
    assert agent.results[0]['status']=='failed' and '越限只能用探测' in agent.results[0]['message']
    assert len(agent.results)==1 and '运行停止' in agent.error
    assert word(reg(runtime,'COM3',3),56)==2050,'Nothing after a failed move runs'


def test_exploratory_move_uses_a_low_torque_ceiling_and_stops_at_the_first_obstruction(runtime,agent):
    port=runtime.session.ports['COM3'];written=[];original=port.write
    def spy(packet):
        if packet[2]==1 and packet[4]==3 and packet[5]==48:written.append(packet[6]+256*packet[7])
        return original(packet)
    port.write=spy
    add_stops(runtime,{('COM3',1):(1800,2100)})
    agent.start([{'op':'move','targets':{'left:1':2250},'torque':400,'tries':1,'speed_deg':8}],True,background=False)
    res=agent.results[0]
    assert res['status']=='failed' and '受阻' in res['message'] and 400 in written
    assert not any('重试' in e['message'] for e in runtime.limit_test.events),'No retry pushes into an unknown obstacle'
    assert word(reg(runtime,'COM3',1),48)==runtime.limit_test.torque_backup['COM3:1'],'Torque ceiling restored'


def test_a_blocked_move_is_held_and_recorded_as_a_suspected_contact(runtime,agent):
    add_stops(runtime,{('COM3',1):(1800,2100)})
    agent.start([{'op':'move','targets':{'left:1':2250},'torque':900,'tries':1,'speed_deg':8}],True,background=False)
    res=agent.results[0];c=res['contact']
    assert res['status']=='failed' and c['joint']=='COM3:1' and c['verdict']=='pending'
    assert c['position']==2100 and c['target']==2250 and c['direction']==1 and c['from']==2050
    assert c['requested_torque']==c['ceiling']==750,'An explicit torque stays under the overload threshold'
    assert c['kind']=='pushing' and c['peak_load']==900 and isinstance(c['settle_ms'],int)
    assert c['pose']['left:1']==2100 and set(c['pose'])>={'left:6','right:1','head:7','head:8'} and len(c['pose'])==14
    assert c['to_limit']==word(reg(runtime,'COM3',1),11)-2100
    lines=(agent.dir/'contacts.jsonl').read_text(encoding='utf-8').splitlines()
    assert len(lines)==1 and json.loads(lines[0])['position']==2100
    assert json.loads((agent.dir/'result.json').read_text(encoding='utf-8'))['results'][0]['contact']['joint']=='COM3:1'
    assert word(reg(runtime,'COM3',1),42)==2100,'The joint stays where it stopped, not pushed and not backed off'
    assert any('疑似碰阻' in e['message'] for e in runtime.limit_test.events)


def test_a_low_load_stall_is_flagged_as_a_pose_contact(runtime,agent):
    port=runtime.session.ports['COM3'];original=port.write
    def write(packet):
        result=original(packet)
        if packet[2]==1 and packet[4]==3 and packet[5]==42:
            wanted=word(port.regs[1],56);held=min(2080,wanted)
            set_word(port.regs[1],56,held);set_word(port.regs[1],60,120 if held!=wanted else 0)
        return result
    port.write=write
    agent.start([{'op':'move','targets':{'left:1':2250},'torque':450,'tries':1}],True,background=False)
    c=agent.results[0]['contact']
    assert c['kind']=='low_load' and c['peak_load']==120 and c['position']==2080 and '姿态相关接触' in c['hint']


def test_wheels_and_unknown_ops_are_rejected_up_front(runtime,agent):
    with pytest.raises(ValueError,match='底盘'):agent.start([{'op':'move','targets':{'COM4:9':10}}],True,background=False)
    with pytest.raises(ValueError,match='未知'):agent.start([{'op':'drive'}],True,background=False)
    with pytest.raises(ValueError,match='空间'):agent.start([{'op':'wait','seconds':0}],False,background=False)


def test_stop_request_aborts_and_holds(runtime,agent):
    add_stops(runtime,{},after=(16,lambda:runtime.limit_test.stop('测试停止')))
    agent.start([{'op':'move','targets':{'left:2':2290},'speed_deg':5}],True,background=False)
    pos=word(reg(runtime,'COM3',2),56)
    assert agent.error=='测试停止' and 2050<pos<2290 and word(reg(runtime,'COM3',2),42)==pos


def test_snapshot_saves_frames_and_reports_busy_cameras(runtime,agent,monkeypatch):
    monkeypatch.setattr(agent,'_capture',lambda camera,path:path.write_bytes(b'\xff\xd8jpg\xff\xd9'))
    runtime.camera_health[1]['state']='在线'
    agent.start([{'op':'snapshot','cameras':[0,1,'laptop'],'tag':'base'}],True,background=False)
    shots={s['camera']:s for s in agent.results[0]['shots']}
    assert (agent.dir/shots[0]['file']).is_file() and (agent.dir/shots['laptop']['file']).is_file()
    assert '推流' in shots[1]['error'] and 'COM4:3' in agent.results[0]['positions']
    assert set(agent.results[0]['wheel_positions'])=={'COM4:9','COM4:10'}
    assert agent.results[0]['capture_started_at']<=agent.results[0]['at']


def test_sample_logs_telemetry_rows(runtime,agent):
    agent.start([{'op':'sample','joints':['head:7','right:4'],'seconds':0.12,'hz':50}],True,background=False)
    rows=[json.loads(x) for x in (agent.dir/agent.results[0]['file']).read_text(encoding='utf-8').splitlines()]
    assert len(rows)>=4 and {r['joint'] for r in rows}=={'COM3:7','COM4:4'} and 'status' in rows[0]


def test_watch_captures_frames_while_a_motion_step_runs(runtime,agent,monkeypatch):
    monkeypatch.setattr(agent,'_capture',lambda camera,path:path.write_bytes(b'\xff\xd8jpg\xff\xd9'))
    agent.start([{'op':'move','targets':{'left:2':2250},'speed_deg':3,'watch':{'cameras':[2,'laptop'],'every':1}}],True,background=False)
    shots=agent.results[0]['watch']
    assert agent.results[0]['status']=='done' and len(shots)>=2
    assert all((agent.dir/s['file']).is_file() for s in shots) and 'COM3:2' in shots[0]['positions']
    with pytest.raises(ValueError,match='边动边拍'):agent.start([{'op':'wait','watch':{'cameras':[0]}}],True,background=False)


def test_backoff_trial_measures_latency_and_returns_to_start(runtime,agent):
    add_stops(runtime,{('COM3',8):(1600,2600)})
    agent.start([{'op':'backoff_trial','joint':'head:8','direction':-1,'variant':'B','delay':0}],True,background=False)
    res=agent.results[0]
    assert res['status']=='done' and res['edge']==1600 and res['latency_ms'] is not None and res['moved']>=24
    r8=reg(runtime,'COM3',8)
    assert word(r8,56)==2050 and (word(r8,9),word(r8,11))==(1800,2300),'Trial limits are not kept'
    assert (agent.dir/res['file']).is_file()


def test_variant_d_is_refused_on_gravity_joints(runtime,agent):
    agent.start([{'op':'backoff_trial','joint':'left:2','variant':'D','continue_on_fail':True},{'op':'wait','seconds':0}],True,background=False)
    assert agent.results[0]['status']=='failed' and '掉臂' in agent.results[0]['message'] and agent.results[1]['status']=='done'


def test_pan_probe_refuses_when_the_other_arm_is_parked_inward(runtime,agent,monkeypatch):
    import fabrication.assembly.debug_console.agent_ops as ops
    monkeypatch.setattr(ops,'PAN_CENTRE',{'left':2050,'right':2050})
    set_word(reg(runtime,'COM4',1),56,1750)   # right arm (COM4 in the fake's normal mapping) turned 26 deg inward
    agent.start([{'op':'probe','item':'left:pan'}],True,background=False)
    assert agent.results[0]['status']=='failed' and '转回中间' in agent.results[0]['message']
    assert word(reg(runtime,'COM3',1),56)==2050,'The probed arm did not move'


def test_runs_exclude_self_tests_and_the_api_works(runtime,agent,monkeypatch):
    runtime.limit_test.running=True
    with pytest.raises(ValueError,match='正在进行'):agent.start([{'op':'wait','seconds':0}],True,background=False)
    runtime.limit_test.running=False
    from fastapi.testclient import TestClient
    from fabrication.assembly.debug_console import app as web
    monkeypatch.setattr(web,'runtime',runtime);client=TestClient(web.app)
    monkeypatch.setattr(agent,'start',lambda steps,confirmed,label='':{'run_id':'x','steps':len(steps)})
    assert client.post('/api/agent/run',json={'steps':[{'op':'wait'}],'confirmed':True}).json()['run_id']=='x'
    assert client.get('/api/agent/status').status_code==200 and client.post('/api/agent/stop').status_code==200
    assert 'agent_run' in client.get('/api/original/status').json()

