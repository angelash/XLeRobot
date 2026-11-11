import json
import math
import struct
from dataclasses import replace
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from .evidence import Evidence,geometry
from .integration import install
from .service import Frame,PhoneVision


def frame(rotation=0):
    return Frame('session',1,100.,{'width':3,'height':3,'rgbd':True,'rotation_deg':rotation,
        'intrinsics':{'fx':1.,'fy':1.,'tx':1.,'ty':1.},'android_mode':'arcore','tracking':'TRACKING',
        'pose_valid':True,'pose_quaternion':[0.,0.,0.,1.],'pose_translation':[1.,2.,3.],
        'arcore_capture_session':'ar-session','depth_source':'arcore_depth_mm','camera_id':'0',
        'raw_depth_high_confidence_fraction':0.,'sensor_raw_depth':False},b'\xff\xd8test',b'\xff\xd8depth',struct.pack('<9f',*[1.]*9))


@pytest.mark.parametrize('rotation,expected',[(0,[2.,2.,2.]),(90,[1.,3.,2.]),(180,[0.,2.,2.]),(270,[1.,1.,2.])])
def test_display_rotation_is_undone_before_ar_pose(rotation,expected):
    value=geometry(frame(rotation),world=True)
    assert value['positions'][15:18]==expected
    assert value['coordinates']=='arcore_world_opengl' and value['capture_session']=='ar-session'
    assert not value['registered_robot_map'] and not value['calibrated']
    assert value['raw_high_confidence_fraction']==0.


def test_camera_view_and_ar_world_are_separate_and_invalid_tracking_is_rejected():
    f=frame();value=geometry(f)
    assert value['positions'][15:18]==[1.,0.,-1.] and value['capture_session'] is None
    m=dict(f.meta,tracking='PAUSED',pose_valid=False)
    with pytest.raises(ValueError,match='AR位姿'):geometry(replace(f,meta=m),world=True)


def test_quaternion_rotation_and_invalid_depth_do_not_create_robot_coordinates():
    f=frame();m=dict(f.meta,pose_translation=[0.,0.,0.],pose_quaternion=[0.,math.sqrt(.5),0.,math.sqrt(.5)])
    assert geometry(replace(f,meta=m),world=True)['positions'][15:18]==[-1.,0.,-1.]
    raw=struct.pack('<9f',0,float('nan'),float('inf'),.1,6.,1.,0.,0.,0.)
    result=geometry(replace(f,raw=raw));assert result['count']==1 and result['pixels']==[2,1]
    with pytest.raises(ValueError):geometry(replace(f,raw=b'bad'))


def test_observation_preserves_exact_bytes_and_distinct_session_metadata(tmp_path):
    store=Evidence(tmp_path);f=frame(90);result=store.capture('android',f,'主摄观察')
    assert store.media('captures',result['id'],'depth.f32').read_bytes()==f.raw
    assert store.media('captures',result['id'],'rgb.jpg').read_bytes()==f.rgb
    cloud=json.loads(store.media('captures',result['id'],'cloud.json').read_text('utf8'))
    assert cloud['capture_session']=='ar-session'
    assert result['historical'] and result['sequence']==1 and not result['motion_enabled']
    assert Evidence(tmp_path).list()['items'][0]['id']==result['id']
    for identifier in ('../other','x'*32):
        with pytest.raises(KeyError):store.media('captures',identifier,'rgb.jpg')
    with pytest.raises(KeyError):store.media('captures',result['id'],'../meta.json')


def test_far_estimates_remain_archived_but_do_not_offer_an_empty_spatial_model(tmp_path):
    f=replace(frame(),raw=struct.pack('<9f',*[65.]*9));store=Evidence(tmp_path)
    result=store.capture('android',f,'远距估计')
    assert not result['cloud_available'] and result['point_count']==0
    assert store.media('captures',result['id'],'depth.f32').read_bytes()==f.raw
    quality=result['depth_quality']
    assert quality['positive_fraction']==1 and quality['analysis_range_fraction']==0
    assert quality['positive_min_m']==65 and not quality['navigation_validated']
    path=store.folder('captures',result['id'])/'meta.json'
    old=json.loads(path.read_text('utf8'));old['cloud_available']=True;path.write_text(json.dumps(old),encoding='utf8')
    assert not store.list()['items'][0]['cloud_available'] and not store.read('captures',result['id'])['cloud_available']
    assert json.loads(path.read_text('utf8'))['cloud_available'] is True


def test_capture_and_preview_keep_freshness_and_old_session_rejection(tmp_path):
    clock=[100.];service=PhoneVision(tmp_path,tmp_path/'data',clock=lambda:clock[0]);service.states['android'].update(enabled=True,phase='live',session='session')
    service.states['android']['frames'].append(frame())
    app=FastAPI();install(app,repo=tmp_path,data_dir=tmp_path/'data',service=service,allowed_origins=['http://localhost:8770']);client=TestClient(app)
    base='/api/platform/phone-vision'
    assert client.get(base+'/android/preview.jpg').content==frame().rgb
    assert client.get(base+'/android/points?session=session&seq=1').json()['count']==9
    assert client.get(base+'/android/points?session=old&seq=1').status_code==409
    assert client.post(base+'/captures',json={'device':'android','session':None,'sequence':None}).status_code==422
    result=client.post(base+'/captures',json={'device':'android','session':'session','sequence':1}).json()
    assert client.get(base+'/evidence/captures/'+result['id']+'/rgb.jpg').content==frame().rgb
    clock[0]+=1.01
    assert client.get(base+'/android/preview.jpg').status_code==409
    assert client.post(base+'/captures',json={'device':'android','session':'session','sequence':1}).status_code==409
    assert client.get(base+'/evidence/captures/'+result['id']+'/rgb.jpg').status_code==200
    assert client.post(base+'/captures',json={},headers={'Origin':'http://foreign.test'}).status_code==403
