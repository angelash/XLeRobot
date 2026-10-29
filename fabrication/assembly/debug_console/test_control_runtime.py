"""Behavioral tests: real packet bytes with a fake STS bus, never hardware."""
import time
import threading
import pytest
from fabrication.assembly.debug_console import control_runtime as mod
from fabrication.assembly.debug_console.torque_control import EXPECTED


class ServoPort:
    def __init__(self, ids):
        self.is_open=True;self.reply=b'';self.writes=[];self.follow=True
        self.regs={}
        for i in ids:
            r=[0]*70
            for address,value in [(3,777),(9,1800),(11,2300),(42,2050),(48,1000),(56,2050)]:
                r[address]=value&255;r[address+1]=value>>8
            r[40]=1;r[62]=121;r[63]=30
            self.regs[i]=r

    @property
    def in_waiting(self):return len(self.reply)
    def read(self,n):
        b=self.reply[:n];self.reply=self.reply[n:];return b
    def reset_input_buffer(self):self.reply=b''
    def write(self,p):
        assert sum(p[2:])&255==255
        i,_,op=p[2:5];data=[];r=self.regs[i]
        if op==2:data=r[p[5]:p[5]+p[6]]
        elif op==3:
            addr=p[5];values=p[6:-1];r[addr:addr+len(values)]=values;self.writes.append((i,addr,list(values)))
            if addr==42 and self.follow and r[40] and (r[46]+256*r[47])>0:r[56:58]=r[42:44]
        body=[i,len(data)+2,0,*data]
        self.reply=bytes([255,255,*body,(~sum(body))&255])
    def close(self):self.is_open=False


class Session:
    def __init__(self):self.ports={n:ServoPort(ids) for n,ids in EXPECTED.items()}
    def open(self):pass
    def close(self):pass


@pytest.fixture
def runtime(tmp_path,monkeypatch):
    monkeypatch.setattr(mod,'LOG_DIR',tmp_path)
    monkeypatch.setattr(mod,'PROFILE',tmp_path/'cal.json')
    r=mod.ControlRuntime(Session());r._check_ports=lambda:None
    r.gamepad={'connected':True,'buttons':[False]*16,'hat':[0,0],'devices':['test']}
    r.pad_time=time.monotonic()
    r.begin(True)
    return r


def frame(lb=False,hat=(0,0),b=False,view=False):
    keys=[False]*17;keys[4]=lb;keys[1]=b;keys[6]=view
    b=keys
    return {'connected':True,'buttons':b,'hat':list(hat)}


def test_start_never_enables_or_moves(runtime):
    assert all(not p.writes for p in runtime.session.ports.values())


def test_native_input_stays_fresh_while_offline_preparation_holds_serial_lock(runtime,monkeypatch):
    from fabrication.assembly.debug_console import gamepad_input
    runtime.stop();second_sample=threading.Event();calls=[]
    class Reader:
        def read(self):
            calls.append(time.monotonic())
            if len(calls)>=2:second_sample.set()
            return {'connected':True,'buttons':[False]*17,'hat':[0,0],'axes':[0]*6}
    monkeypatch.setattr(gamepad_input,'XboxReader',Reader)
    thread=threading.Thread(target=runtime._pad_loop)
    try:
        with runtime.io:
            thread.start()
            assert second_sample.wait(.5),'The native reader must not wait for the serial preparation lock while offline'
            assert time.monotonic()-runtime.pad_time<.2
    finally:
        runtime.stop_event.set();thread.join(timeout=1)
    assert not thread.is_alive()


def test_real_input_edges_drive_same_path_and_stop(runtime):
    p=runtime.session.ports['COM4']
    runtime.process_input(frame(False,(0,1)))
    assert not p.writes
    runtime.process_input(frame(True))
    runtime.process_input(frame(True,(0,1)))
    writes=len(p.writes)
    assert p.regs[1][46]==100
    assert p.regs[1][56]+256*p.regs[1][57]==2066
    runtime.process_input(frame(True,(0,1)))
    assert len(p.writes)==writes  # hold dpad does not repeatedly advance
    runtime.pending['at']-=1;runtime._check_feedback()
    assert any(e['kind']=='feedback' and e['moved'] for e in runtime.events)
    runtime.process_input(frame(False))
    assert not runtime.controller['armed']
    runtime.process_input(frame(False,(0,-1)))
    assert len(p.writes)==writes+1 # only hold command from release


def test_narrow_and_session_limits_cannot_be_bypassed(runtime):
    runtime.session.ports['COM3'].regs[1][9:13]=[252,7,3,8] # 2044..2051
    runtime.scan();runtime._make_envelopes()
    with pytest.raises(ValueError):runtime.select('COM3',1)
    runtime.process_input(frame(True))
    runtime.envelopes[('COM4',1)]['max']=2050
    runtime.process_input(frame(True,(0,1)))
    assert not runtime.session.ports['COM4'].writes


def test_encoder_failure_is_not_success(runtime):
    runtime.session.ports['COM4'].follow=False
    runtime.process_input(frame(True));runtime.process_input(frame(True,(0,1)))
    runtime.pending['at']-=1;runtime._check_feedback()
    assert runtime.controller['controller']=='online'
    assert runtime.controller['paused']
    assert any(e['kind']=='no_motion' for e in runtime.events)


def test_disconnect_holds_and_disarms(runtime):
    runtime.process_input(frame(True));runtime.process_input(frame(True,(0,1)))
    runtime.process_input({'connected':False})
    assert runtime.controller['controller']=='online'
    assert runtime.controller['paused']
    assert not runtime.controller['armed']


def test_start_requires_fresh_input_and_new_lb(runtime):
    runtime.stop();runtime.pad_time=0
    with pytest.raises(ValueError):runtime.begin(True)
    runtime.pad_time=time.monotonic();runtime.gamepad=frame(True)
    with pytest.raises(ValueError):runtime.begin(True)


def test_web_start_stop_settings_errors_use_runtime(runtime,monkeypatch):
    from fastapi.testclient import TestClient
    from fabrication.assembly.debug_console import app as web
    monkeypatch.setattr(web,'runtime',runtime)
    client=TestClient(web.app) # No lifespan: the injected bus is fully fake.
    runtime.stop()
    assert client.post('/api/controller/start',json={'confirmed':False}).status_code==409
    runtime.pad_time=time.monotonic()
    response=client.post('/api/controller/start',json={'confirmed':True})
    assert response.status_code==200,response.text
    assert client.get('/api/status').json()['controller']['controller']=='online'
    assert client.post('/api/controller/settings',json={'step_raw':100,'speed_raw':100}).status_code==422
    assert client.post('/api/controller/stop',json={}).status_code==200
    assert client.post('/api/torque',json={'enabled':True,'scope':'single','port':'COM9','id':1}).status_code==409
    assert client.post('/api/controller/start',json={'confirmed':True},headers={'Origin':'https://other.example'}).status_code==403


def test_limit_keeps_takeover_and_allows_reverse(runtime):
    runtime.envelopes[('COM4',1)]['max']=2050
    runtime.process_input(frame(True));runtime.process_input(frame(True,(0,1)))
    assert runtime.notice['code']=='limit'
    assert runtime.controller['armed'] and not runtime.controller['paused']
    runtime.process_input(frame(True));runtime.process_input(frame(True,(0,-1)))
    assert runtime.pending['target']==2034
    assert runtime.controller['controller']=='online'


def test_clipped_step_explains_boundary(runtime):
    runtime.envelopes[('COM4',1)]['max']=2058
    runtime.process_input(frame(True));runtime.process_input(frame(True,(0,1)))
    assert runtime.pending['target']==2058
    assert runtime.notice['code']=='limit'
    assert runtime.controller['armed']


def test_b_stops_only_action_and_needs_new_lb(runtime):
    runtime.process_input(frame(True));runtime.process_input(frame(True,(0,1)))
    runtime.process_input(frame(True,b=True))
    assert not runtime.pending and not runtime.controller['armed']
    assert runtime.controller['controller']=='online' and not runtime.controller['paused']
    writes=len(runtime.session.ports['COM4'].writes)
    runtime.process_input(frame(True,(0,-1)))
    assert len(runtime.session.ports['COM4'].writes)==writes
    runtime.process_input(frame(False,b=True));runtime.process_input(frame(True,b=True))
    assert not runtime.controller['armed']
    runtime.process_input(frame(False));runtime.process_input(frame(True))
    runtime.process_input(frame(True,(0,-1)))
    assert runtime.pending is not None


@pytest.mark.parametrize('fault',['disconnect','pad_timeout','page_timeout'])
def test_fault_preserves_session_until_manual_resume(runtime,fault):
    runtime.process_input(frame(True));runtime.process_input(frame(True,(0,1)))
    if fault=='disconnect':runtime.process_input({'connected':False})
    elif fault=='pad_timeout':runtime.pad_time-=1;runtime.check_watchdog()
    else:runtime.ui_heartbeat-=4;runtime.check_watchdog()
    assert runtime.controller['controller']=='online' and runtime.controller['paused']
    assert runtime.pending is None and not runtime.controller['armed']
    p=runtime.session.ports['COM4'];count=len(p.writes)
    runtime.process_input(frame(False));runtime.process_input(frame(True,(0,1)))
    assert len(p.writes)==count  # reconnect never resumes motion
    runtime.gamepad=frame(True);runtime.pad_time=time.monotonic()
    with pytest.raises(ValueError):runtime.resume()
    runtime.gamepad=frame(False,(0,1))
    with pytest.raises(ValueError):runtime.resume()
    runtime.gamepad=frame(b=True)
    with pytest.raises(ValueError):runtime.resume()
    runtime.gamepad=frame();runtime.resume()
    assert not runtime.controller['paused'] and not runtime.controller['armed']
    assert len(p.writes)==count  # no old command replayed
    runtime.process_input(frame(True));runtime.process_input(frame(True,(0,-1)))
    assert runtime.pending is not None


def test_view_explicitly_ends_even_when_paused(runtime):
    runtime._pause('test fault')
    runtime.process_input(frame(view=True))
    assert runtime.controller['controller']=='offline'
    assert not runtime.controller['paused']


def test_diagonal_does_not_select_and_move(runtime):
    selected=runtime.selected
    runtime.process_input(frame(True));runtime.process_input(frame(True,(1,1)))
    assert runtime.selected==selected and runtime.pending is None
    assert runtime.notice['code']=='blocked'


def test_unbound_and_ui_buttons_never_move(runtime):
    runtime.process_input(frame(True))
    for i in [0,2,3,5,7,8,9,10,11,16]:
        raw=frame(True);raw['buttons'][i]=True
        runtime.process_input(raw);runtime.process_input(frame(True))
    assert all(not p.writes for p in runtime.session.ports.values())


def test_web_mapping_and_resume_contract(runtime,monkeypatch):
    from fastapi.testclient import TestClient
    from fabrication.assembly.debug_console import app as web
    monkeypatch.setattr(web,'runtime',runtime)
    client=TestClient(web.app)
    data=client.get('/api/status').json()
    assert len(data['control_map'])==22
    assert len({m['key'] for m in data['control_map']})==22
    runtime._pause('simulated disconnect')
    runtime.gamepad=frame(True)
    assert client.post('/api/controller/resume',json={}).status_code==409
    runtime.gamepad=frame();runtime.pad_time=time.monotonic()
    data=client.post('/api/controller/resume',json={}).json()
    assert not data['controller']['paused'] and not data['controller']['armed']
    assert data['controller']['controller']=='online'


def test_failed_hold_requires_successful_retry_before_resume(runtime,monkeypatch):
    runtime.process_input(frame(True));runtime.process_input(frame(True,(0,1)))
    original=mod.write_register
    def fail(*args):raise RuntimeError('wire disconnected')
    monkeypatch.setattr(mod,'write_register',fail)
    runtime._pause('communication error')
    assert runtime.pending is None
    assert runtime.last_command['outcome']=='hold_unconfirmed'
    runtime.gamepad=frame();runtime.pad_time=time.monotonic()
    with pytest.raises(RuntimeError):runtime.resume()
    assert runtime.controller['paused']
    monkeypatch.setattr(mod,'write_register',original)
    runtime.resume()
    assert not runtime.controller['paused'] and runtime.active is None
