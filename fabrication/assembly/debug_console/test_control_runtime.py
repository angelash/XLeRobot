"""Behavioral tests: real packet bytes with a fake STS bus, never hardware."""
import time
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


def frame(lb=False,hat=(0,0)):
    b=[False]*16;b[4]=lb
    return {'connected':True,'buttons':b,'hat':list(hat)}


def test_start_never_enables_or_moves(runtime):
    assert all(not p.writes for p in runtime.session.ports.values())


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
    assert runtime.controller['controller']=='offline'
    assert any(e['kind']=='no_motion' for e in runtime.events)


def test_disconnect_holds_and_disarms(runtime):
    runtime.process_input(frame(True));runtime.process_input(frame(True,(0,1)))
    runtime.process_input({'connected':False})
    assert runtime.controller['controller']=='offline'
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
