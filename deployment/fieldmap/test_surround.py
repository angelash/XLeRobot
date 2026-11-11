"""A surround atlas must keep its holes, references and original evidence."""
from io import BytesIO
import hashlib
import json
from pathlib import Path

import numpy as np
from PIL import Image
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from .surround import Surround, circular_coverage, floor_plan, panorama
from .integration import install, API
from deployment.phone_vision.evidence import Evidence


def photo(color=(180, 80, 40)):
    out=BytesIO();Image.new('RGB',(160,120),color).save(out,'JPEG');return out.getvalue()


def test_circular_gap_crossing_back_seam_counts_once():
    mask=[True]*72
    for i in list(range(68,72))+list(range(4)):mask[i]=False
    coverage=circular_coverage(mask)
    assert coverage['missing_degrees']==40 and coverage['largest_gap_degrees']==40
    assert len(coverage['gaps'])==1 and not coverage['complete_360']
    assert circular_coverage([False]*72)['missing_degrees']==360
    assert circular_coverage([True]*72)['complete_360']


def test_single_photo_and_head_fan_cannot_become_full_360_by_projection():
    v={'rgb':photo(),'heading_deg':0.,'pitch_deg':0.}
    raw,result=panorama([v],{'focal_px':144.})
    assert raw.startswith(b'\xff\xd8') and result['seen_degrees']<90 and not result['complete_360']
    assert not result['missing_pixels_filled'] and not result['simultaneous_exposure']
    views=[{**v,'heading_deg':a} for a in range(-90,91,30)]
    _,fan=panorama(views,{'focal_px':144.})
    assert 180<=fan['seen_degrees']<280 and fan['missing_degrees']>80


def test_full_generated_ring_has_coverage_only_when_all_directions_are_supplied():
    views=[{'rgb':photo((30+a,80,130)),'heading_deg':float(a),'pitch_deg':0.} for a in range(-180,180,30)]
    _,result=panorama(views,{'focal_px':144.})
    assert result['complete_360'] and result['missing_degrees']==0
    assert all(s['source_indices'] for s in result['sector_sources'])


def test_floor_candidates_do_not_certify_free_space_and_walls_do_not_become_floor():
    points=[]
    for x in np.linspace(-1.,1.,81):
        for z in np.linspace(-1.,1.,81):points.extend((float(x),-1.,float(z)))
    for y in np.linspace(-.8,.5,20):
        for z in np.linspace(-1.,1.,35):points.extend((.8,float(y),float(z)))
    result=floor_plan({'positions':points,'trajectory':[[0.,0.,0.]]})
    assert result['surface_candidate'] and result['surface_candidate']['height_ar_m']==-1.
    assert any(c['kind']=='floor_candidate' for c in result['cells'])
    assert any(c['kind']=='obstacle_candidate' for c in result['cells'])
    assert result['verified_free_cells']==0 and result['unknown_default'] and not result['motion_enabled']
    wall=floor_plan({'positions':[v for y in np.linspace(-1.,1.,30) for z in np.linspace(-1.,1.,30) for v in (0.,float(y),float(z))]})
    assert wall['surface_candidate'] is None and all(c['kind']=='surface' for c in wall['cells'])
    with pytest.raises(ValueError):floor_plan({'positions':[float('nan'),0.,0.]})


def head_fixture(tmp_path,*,wheel_delta=0,ok=True):
    root=tmp_path/'tests/20261111/phone-roam/light-surround-test-01';root.mkdir(parents=True)
    positions={'COM4:9':1200,'COM4:10':2400,'COM3:7':2015,'COM3:8':2220}
    (root/'head.jpg').write_bytes(photo())
    value={'ok':ok,'base_drive_commands':0,'health':{'stationary_base_verified':True},'head_reference_raw':2015,
           'initial':{'ports':[{'port':'COM4','motors':[{'id':9,'position':1200},{'id':10,'position':2400}]}]},
           'shots':[{'positions':{**positions,'COM4:9':1200+wheel_delta},'index':0,'frames':[{'camera':0,'file':'head.jpg','captured_at':100.}]}]}
    (root/'result.json').write_text(json.dumps(value),encoding='utf8');return root


def test_head_import_retains_originals_restart_and_distinct_immutable_scans(tmp_path):
    source=head_fixture(tmp_path);before={p.name:p.read_bytes() for p in source.iterdir()}
    store=Surround(tmp_path/'atlas',lambda:None,tmp_path/'tests')
    a=store.import_head('light-surround-test-01');b=store.import_head('light-surround-test-01')
    assert a['id']!=b['id'] and not a['coverage']['complete_360']
    assert store.media(a['id'],'view-000.jpg').read_bytes()==before['head.jpg']
    assert len(Surround(tmp_path/'atlas',lambda:None).list()['items'])==2
    assert {p.name:p.read_bytes() for p in source.iterdir()}==before
    assert store.read(a['id'])['panorama_sha256']==hashlib.sha256(store.media(a['id'],'panorama.jpg').read_bytes()).hexdigest()
    assert a['preview_sha256']==hashlib.sha256(store.media(a['id'],'preview.jpg').read_bytes()).hexdigest()
    with pytest.raises(KeyError):store.media(a['id'],'../../other')
    with pytest.raises(ValueError):store.read('../private')


def test_topdown_projection_keeps_original_cloud_and_session_on_repeated_reads(tmp_path):
    evidence=Evidence(tmp_path/'phone')
    identifier='a'*32
    folder=evidence.folder('models',identifier);folder.mkdir(parents=True)
    raw=json.dumps({'positions':[0.,-1.,-1.,.4,-.6,-1.],
                    'trajectory':[[0.,0.,0.],[.2,0.,0.]]}).encode()
    (folder/'cloud.json').write_bytes(raw)
    (folder/'meta.json').write_text(json.dumps({'id':identifier,'capture_session':'old-session',
        'label':'saved depth','source_frames':2,'point_count':2}),encoding='utf8')
    before={p.name:p.read_bytes() for p in folder.iterdir()}
    store=Surround(tmp_path/'atlas',lambda:evidence)
    first=store.model_plan(identifier);second=store.model_plan(identifier)
    assert first==second and first['capture_session']=='old-session'
    assert first['point_count']==2 and first['verified_free_cells']==0
    assert first['model_sha256']==hashlib.sha256(raw).hexdigest()
    assert not first['registered_robot_map'] and first['historical']
    assert {p.name:p.read_bytes() for p in folder.iterdir()}==before


@pytest.mark.parametrize('wheel_delta,ok',[(5,True),(0,False)])
def test_changed_base_or_failed_scan_cannot_be_stitched_as_one_station(tmp_path,wheel_delta,ok):
    head_fixture(tmp_path,wheel_delta=wheel_delta,ok=ok)
    store=Surround(tmp_path/'atlas',lambda:None,tmp_path/'tests')
    with pytest.raises(ValueError):store.import_head('light-surround-test-01')
    assert store.list()['items']==[]


def test_surround_routes_are_read_or_build_only_and_foreign_origin_is_rejected(tmp_path):
    head_fixture(tmp_path);app=FastAPI();install(app,data_dir=tmp_path/'map',history_fieldtests_root=tmp_path/'tests',allowed_origins=['http://localhost:8770'])
    with TestClient(app) as client:
        assert client.get(API+'/surround').json()['items']==[]
        assert client.post(API+'/surround/import-head',json={'source_trial':'light-surround-test-01'},headers={'Origin':'https://foreign.test'}).status_code==403
        r=client.post(API+'/surround/import-head',json={'source_trial':'light-surround-test-01'});assert r.status_code==200
        saved=r.json();assert not saved['motion_enabled']
        assert client.get(saved['panorama_url']).status_code==200
        assert client.get(API+'/surround/'+saved['id']).json()['view_count']==1
        assert client.post(API+'/surround/drive',json={}).status_code in (404,405)
        assert client.post(API+'/surround/import-head',json={'source_trial':'../private'}).status_code==422
        assert 'surroundRing' in client.get('/fieldmap/view').text
