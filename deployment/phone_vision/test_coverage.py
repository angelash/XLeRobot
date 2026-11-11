"""Coverage guides missing observations; it cannot manufacture free space."""
from dataclasses import replace
from io import BytesIO
import json
import struct

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from PIL import Image

from .coverage import Coverage, combine, frame_coverage
from .evidence import Evidence
from .integration import install as install_phone
from .reconstruction import Reconstruction
from .service import PhoneVision
from .test_evidence import frame
from deployment.fieldmap.integration import install as install_fieldmap


def image_frame(**meta):
    out=BytesIO();Image.new('RGB',(3,3),(12,34,56)).save(out,format='JPEG')
    f=frame()
    return replace(f,rgb=out.getvalue(),meta={**f.meta,**meta})


def test_holes_nonfinite_and_far_depth_are_distinct_and_never_free():
    f=frame();raw=struct.pack('<9f',0,float('nan'),float('inf'),.1,6.,1.,2.,3.,0.)
    report=frame_coverage(f.meta,raw)
    assert report['in_range']==3 and report['pixels']==9
    assert sum(c['missing'] for c in report['cells'])==4
    assert sum(c['too_far'] for c in report['cells'])==1
    assert sum(c['too_near'] for c in report['cells'])==1
    assert not report['motion_enabled'] and not report['registered_robot_map']
    assert any(c['fraction'] is None for c in report['cells'])


@pytest.mark.parametrize('rotation,azimuth,elevation',[(0,3,3),(90,0,4),(180,21,3),(270,0,1)])
def test_missing_rays_follow_sdk_pose_after_undoing_display_rotation(rotation,azimuth,elevation):
    f=frame(rotation);raw=struct.pack('<9f',0,0,0,0,0,1,0,0,0)
    value=frame_coverage(f.meta,raw)
    supplied=[c for c in value['angular'] if c['in_range']]
    assert len(supplied)==1
    assert (supplied[0]['azimuth'],supplied[0]['elevation'])==(azimuth,elevation)


def test_bad_pose_keeps_image_holes_but_cannot_invent_world_coverage():
    f=frame();report=frame_coverage({**f.meta,'tracking':'PAUSED'},f.raw)
    assert report['fraction']==1 and report['angular']==[] and report['capture_session'] is None
    assert combine([report],capture_session='ar-session')['source_frames']==0


def test_sessions_are_not_combined_and_motion_is_never_granted():
    f=frame();one=frame_coverage(f.meta,f.raw)
    other=frame_coverage({**f.meta,'arcore_capture_session':'other'},f.raw)
    report=combine([one,other],capture_session='ar-session')
    assert report['source_frames']==1 and report['target_source_frames']==1
    assert all(not v['motion_enabled'] for v in report['targets'])
    assert not report['motion_enabled'] and not report['registered_robot_map']


def test_new_location_recomputes_gaps_near_last_camera_instead_of_reusing_old_coverage():
    f=frame();old=frame_coverage(f.meta,f.raw)
    new=frame_coverage({**f.meta,'pose_translation':[10.,2.,3.]},struct.pack('<9f',*[40.]*9))
    report=combine([old,new],capture_session='ar-session')
    assert report['source_frames']==2 and report['target_source_frames']==1
    assert all(t['missing_fraction']==1 for t in report['targets'])
    assert report['target_reference_radius_m']==.5


def test_old_records_backfill_without_rewriting_sources_and_restart_keeps_reports(tmp_path):
    evidence=Evidence(tmp_path);f=image_frame();saved=evidence.capture('android',f)
    model=Reconstruction(evidence).add(saved,f)
    # Simulate an old immutable capture, before coverage sidecars existed.
    (evidence.folder('captures',saved['id'])/'coverage.json').unlink()
    original={str(p.relative_to(tmp_path)):p.read_bytes() for p in tmp_path.rglob('*') if p.is_file()}
    result=Coverage(evidence).read('models',model['id'])
    assert result['point_count']==9 and result['source_frames']==1
    assert result['sources'][0]['id']==saved['id']
    assert result['last_observation']['fraction']==1
    assert result==Coverage(Evidence(tmp_path)).read('models',model['id'])
    assert {str(p.relative_to(tmp_path)):p.read_bytes() for p in tmp_path.rglob('*') if p.is_file()}==original


def test_model_growth_invalidates_summary_cache_and_missing_sources_are_reported(tmp_path):
    evidence=Evidence(tmp_path);builder=Reconstruction(evidence);coverage=Coverage(evidence);f=image_frame()
    saved=evidence.capture('android',f);model=builder.add(saved,f)
    assert coverage.read('models',model['id'])['source_captures']==1
    f=replace(f,sequence=2);next_saved=evidence.capture('android',f);model=builder.add(next_saved,f)
    assert coverage.read('models',model['id'])['source_captures']==2
    # A new reader identifies the gap; do not remove or repair original data.
    (evidence.folder('captures',next_saved['id'])/'depth.f32').unlink()
    (evidence.folder('captures',next_saved['id'])/'coverage.json').unlink()
    report=Coverage(evidence).read('models',model['id'])
    assert report['unavailable_captures']==[next_saved['id']] and report['source_frames']==1


def test_live_routes_reject_stale_sessions_and_archived_zero_points_stay_readable(tmp_path):
    clock=[100.];service=PhoneVision(tmp_path,tmp_path/'phone',clock=lambda:clock[0]);f=image_frame()
    service.states['android'].update(enabled=True,phase='live',session='session')
    service.states['android']['frames'].append(f)
    app=FastAPI();install_fieldmap(app,data_dir=tmp_path/'map')
    install_phone(app,repo=tmp_path,data_dir=tmp_path/'phone',service=service)
    saved=app.state.phone_evidence.capture('android',replace(f,raw=struct.pack('<9f',*[40.]*9)))
    with TestClient(app) as client:
        api='/api/platform/fieldmap/phone-space'
        assert client.get(api+'/live?session=session&seq=1').json()['coverage']['fraction']==1
        assert client.get(api+'/live?session=old&seq=1').status_code==409
        lib=client.get(api).json();assert lib['captures'][0]['id']==saved['id'] and not lib['motion_enabled']
        query={'kind':'captures','identifier':saved['id']}
        archive=client.get(api+'/coverage',params=query).json()
        assert archive['point_count']==0 and archive['last_observation']['fraction']==0
        clock[0]+=1.01
        assert client.get(api+'/live?session=session&seq=1').status_code==409
        assert client.get(api+'/coverage',params=query).status_code==200
        assert client.get(api+'/coverage',params={'kind':'captures','identifier':'../private'}).status_code==422
        assert client.post(api+'/drive',json={}).status_code==404
        page=client.get('/fieldmap/view').text
        assert 'phoneSpaceCanvas' in page and 'phoneAngularGrid' in page
