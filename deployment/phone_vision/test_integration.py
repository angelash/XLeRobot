"""Lifecycle, freshness and numeric-depth contracts; no phone or robot access."""
import io
import struct
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from deployment.phone_vision.integration import install
from deployment.phone_vision.protocol import HEADER,MAGIC,read_message,write_message
from deployment.phone_vision.service import PhoneVision


def message(sequence=1):
    meta={'kind':'frame','width':2,'height':2,'sequence':sequence,'camera_type':0,'rgbd':True,'confidence_available':False}
    return meta,b'\xff\xd8generated-rgb',b'\xff\xd8generated-depth',struct.pack('<ffff',.25,0,float('nan'),.75)


@pytest.fixture
def prepared(tmp_path):
    clock=[100.]
    service=PhoneVision(tmp_path,tmp_path/'data',clock=lambda:clock[0])
    service.states['iphone'].update(enabled=True,session='first',phase='starting')
    service._publish('iphone','first',message())
    app=FastAPI();install(app,repo=tmp_path,data_dir=tmp_path/'data',service=service,allowed_origins=['http://localhost:8770'])
    return service,clock,TestClient(app)


def test_numeric_pixel_and_empty_depth_are_not_confidence(prepared):
    service,clock,client=prepared
    common='/api/platform/phone-vision/iphone/depth?session=first&seq=1&y=0&x='
    assert client.get(common+'0').json()['depth_m']==.25
    invalid=client.get(common+'1').json()
    assert invalid['depth_m'] is None and not invalid['numeric_valid']
    assert invalid['calibrated'] is False and invalid['confidence_available'] is False
    assert client.get(common+'-1').status_code==422


def test_old_frame_is_hidden_and_both_image_and_depth_refused(prepared):
    service,clock,client=prepared
    assert client.get('/api/platform/phone-vision/status').json()['devices']['iphone']['frame_available']
    clock[0]+=1.01
    status=client.get('/api/platform/phone-vision/status').json()['devices']['iphone']
    assert status['phase']=='stale' and not status['frame_available'] and status['meta'] is None
    assert client.get('/api/platform/phone-vision/iphone/frame.jpg?session=first&seq=1').status_code==409
    assert client.get('/api/platform/phone-vision/iphone/depth?session=first&seq=1&x=0&y=0').status_code==409


def test_reconnect_never_serves_the_previous_session(prepared):
    service,clock,client=prepared
    service.states['iphone'].update(session='second')
    service.states['iphone']['frames'].clear()
    service._publish('iphone','first',message(2))
    assert not service.status()['devices']['iphone']['frame_available']
    service._publish('iphone','second',message(1))
    assert service.frame('iphone','second',1).session=='second'
    assert client.get('/api/platform/phone-vision/iphone/frame.jpg?session=first&seq=1').status_code==409


def test_repeated_sequence_and_queued_stale_packet_not_promoted(prepared):
    service,clock,client=prepared
    with pytest.raises(ValueError,match='Repeated'):service._publish('iphone','first',message())
    service._publish('iphone','first',message(2),received=98.)
    assert not service.status()['devices']['iphone']['frame_available']


def test_late_pipe_delivery_does_not_refresh_an_old_acquisition(prepared):
    service,clock,client=prepared
    meta,rgb,depth,raw=message(2)
    meta['host_acquired_monotonic']=98.
    service._publish('iphone','first',(meta,rgb,depth,raw),received=100.)
    assert not service.status()['devices']['iphone']['frame_available']


def test_stop_clears_data_without_actions_and_foreign_mutations_fail(prepared):
    service,clock,client=prepared
    assert client.post('/api/platform/phone-vision/iphone/stop',json={},headers={'Origin':'https://outside.example'}).status_code==403
    assert client.post('/api/platform/phone-vision/iphone/stop',json={'command':'anything'}).status_code==422
    assert service.status()['devices']['iphone']['enabled']
    assert client.post('/api/platform/phone-vision/iphone/stop',json={}).status_code==200
    assert not service.status()['devices']['iphone']['enabled']
    assert not service.states['iphone']['frames']
    assert client.post('/api/platform/phone-vision/drive',json={}).status_code==404


def test_stopping_state_does_not_invite_a_second_camera_owner(prepared):
    service,clock,client=prepared
    service.states['iphone']['enabled']=False
    service.workers['iphone']={}
    state=service.status()['devices']['iphone']
    assert state['stopping'] and state['phase']=='stopping'
    assert not state['frame_available']


def test_missing_native_dependencies_allow_page_and_observation_only(prepared):
    service,clock,client=prepared
    assert client.get('/phone-vision/view').status_code==200
    assert client.get('/api/platform/phone-vision/status').json()['motion_enabled'] is False
    assert client.post('/api/platform/phone-vision/android/start',json={}).status_code==422
    assert client.post('/api/platform/phone-vision/iphone/start',content=b'{}',headers={'Content-Type':'text/plain'}).status_code==415
    assert client.post('/api/platform/phone-vision/iphone/start',content=b'x'*1100,headers={'Content-Type':'application/json'}).status_code==413


def test_pipe_size_limit_and_depth_shape_are_checked_before_use():
    wire=io.BytesIO();write_message(wire,*message());wire.seek(0)
    assert read_message(wire)==message()
    with pytest.raises(ValueError):read_message(io.BytesIO(HEADER.pack(MAGIC,0,90000000,0,0)))
    wire=io.BytesIO();meta,rgb,depth,raw=message();write_message(wire,meta,rgb,depth,raw[:4]);wire.seek(0)
    with pytest.raises(ValueError,match='Depth dimensions'):read_message(wire)


def test_large_depth_packet_survives_partial_pipe_writes():
    class PartialWriter(io.BytesIO):
        def write(self,value):return super().write(value[:37])
    width,height=480,640
    meta={'kind':'frame','width':width,'height':height,'sequence':1}
    raw=struct.pack('<f',.5)*(width*height)
    wire=PartialWriter();write_message(wire,meta,b'\xff\xd8test',b'\xff\xd8depth',raw);wire.seek(0)
    assert read_message(wire)[3]==raw


def test_crashed_worker_cleanup_is_scoped_to_exact_session(tmp_path,monkeypatch):
    import subprocess
    calls=[]
    def run(command,**options):
        calls.append(command)
        return subprocess.CompletedProcess(command,0,stdout=b'phone-a tcp:1234 localabstract:scrcpy_01234567\nphone-b tcp:5678 localabstract:scrcpy_77777777\n')
    monkeypatch.setattr(subprocess,'run',run)
    service=PhoneVision(tmp_path,tmp_path/'data')
    service._cleanup_android({'adb':'adb.exe'},'0123456789abcdef0123456789abcdef')
    assert calls[1]==['adb.exe','-s','phone-a','forward','--remove','tcp:1234']
    assert calls[2][-1]=='/data/local/tmp/xlerobot-phone-01234567.jar'
    assert not any('phone-b' in command or 'tcp:5678' in command for command in calls)
