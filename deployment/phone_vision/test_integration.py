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


def test_raw_depth_preserves_numeric_values_and_fresh_session_contract(prepared):
    service,clock,client=prepared
    path='/api/platform/phone-vision/iphone/depth.raw?session=first&seq=1'
    response=client.get(path)
    assert response.content==message()[3]
    assert response.headers['X-Depth-Format']=='float32-le-meters'
    assert response.headers['X-Depth-Width']=='2'
    assert client.get(path.replace('first','old')).status_code==409
    clock[0]+=1.01
    assert client.get(path).status_code==409


def test_wifi_decoded_depth_is_explicitly_distinct_from_original_usb_sensor_data(prepared):
    service,clock,client=prepared
    meta,rgb,preview,raw=message(2)
    meta.update(camera_type=None,transport='wifi',depth_source='record3d_hue_video',sensor_raw_depth=False)
    service._publish('iphone','first',(meta,rgb,preview,raw))
    value=client.get('/api/platform/phone-vision/iphone/depth?session=first&seq=2&x=0&y=0').json()
    assert not value['sensor_raw_depth'] and value['depth_source']=='record3d_hue_video' and '近似' in value['note']
    response=client.get('/api/platform/phone-vision/iphone/depth.raw?session=first&seq=2')
    assert response.headers['X-Depth-Semantics']=='record3d_hue_video'


def test_arcore_rgb_fallback_never_serves_old_depth(prepared):
    service,clock,client=prepared
    service.states['android'].update(enabled=True,session='android',phase='starting')
    meta,rgb,preview,raw=message()
    meta.update(android_mode='arcore',depth_source='arcore_depth_mm',sensor_raw_depth=False)
    service._publish('android','android',(meta,rgb,preview,raw))
    assert client.get('/api/platform/phone-vision/android/depth.raw?session=android&seq=1').status_code==200
    meta=dict(meta,sequence=2,rgbd=False,depth_unavailable_reason='彩色已连接；深度已过期')
    service._publish('android','android',(meta,rgb,b'',b''))
    assert client.get('/api/platform/phone-vision/android/frame.jpg?session=android&seq=2').status_code==200
    assert client.get('/api/platform/phone-vision/android/depth.raw?session=android&seq=2').status_code==422
    assert '过期' in service.status()['devices']['android']['reason']


def test_transient_arcore_receiver_failure_preserves_native_session(tmp_path,monkeypatch):
    calls=[]
    class Result:
        stdout=b''
    def run(args,**kwargs):calls.append(args);return Result()
    monkeypatch.setattr('deployment.phone_vision.service.subprocess.run',run)
    service=PhoneVision(tmp_path,tmp_path/'data')
    cfg={'adb':'adb.exe','android_mode':'arcore','android_transport':'usb'}
    service._cleanup_android(cfg,'0123456789abcdef0123456789abcdef',release_camera=False)
    assert calls==[['adb.exe','forward','--list']]
    calls.clear()
    service._cleanup_android(cfg,'0123456789abcdef0123456789abcdef',release_camera=True)
    assert calls==[['adb.exe','-d','shell','am','force-stop','org.xlerobot.arvision'],['adb.exe','forward','--list']]


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


def test_android_view_configuration_requires_local_origin_and_leaves_iphone_frames_intact(prepared):
    service,clock,client=prepared
    path='/api/platform/phone-vision/android/view'
    value={'camera_id':'0','rotation_deg':90}
    assert client.post(path,json=value,headers={'Origin':'https://outside.example'}).status_code==403
    assert client.post(path,json={**value,'command':'ignored'}).status_code==422
    assert client.post(path,json={'camera_id':'9','rotation_deg':0}).status_code==422
    result=client.post(path,json=value)
    assert result.status_code==200 and result.json()['devices']['android']['rotation_deg']==90
    assert service.frame('iphone','first',1).raw==message()[3]


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
