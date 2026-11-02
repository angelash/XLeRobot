"""Survey review must survive restart, reject stale writes and keep motion closed."""
import json
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from deployment.fieldmap.store import Survey,atomic_json
from deployment.fieldmap.integration import install,data_directory,API


@pytest.fixture
def survey(tmp_path):
    atomic_json(tmp_path/'map.json',{'schema':1,'id':'scan-1','features':[{'id':'chair','name':'Chair','kind':'blocked'}],
                                   'observations':[{'id':'photo','image':'front.jpg'}],'motion_enabled':True})
    (tmp_path/'images').mkdir();(tmp_path/'images'/'front.jpg').write_bytes(b'image')
    (tmp_path/'images'/'private.jpg').write_bytes(b'private')
    return Survey(tmp_path)


def review(**changes):
    return {'survey_id':'scan-1','revision':0,'feature_id':'chair','kind':'blocked','note':'椅脚较宽','confirmed':True,**changes}


def test_review_persists_without_changing_original_or_enabling_motion(survey):
    survey.review(review())
    restarted=Survey(survey.root).load()
    assert restarted['review']['items']['chair']['confirmed']
    assert restarted['features'][0]['kind']=='blocked' and restarted['motion_enabled'] is False
    assert restarted['review']['revision']==1


def test_stale_review_or_wrong_scan_never_overwrites_another_review(survey):
    survey.review(review())
    for update in ({'revision':0},{'survey_id':'old-scan','revision':1},{'kind':'walk_enabled','revision':1}):
        with pytest.raises(ValueError):survey.review(review(**update))
    assert len(survey.load()['review']['history'])==1


@pytest.mark.parametrize('name',['../map.json','private.jpg','missing.jpg','front.jpg/../../reviews.json'])
def test_only_named_images_are_exposed(survey,name):
    with pytest.raises(FileNotFoundError):survey.image(name)


def test_controller_timeout_is_unknown_not_stopped(survey,monkeypatch):
    def fail(*_):raise TimeoutError('no response')
    monkeypatch.setattr(survey,'controller',fail)
    status=survey.status()
    assert not status['online'] and status['wheels_stopped'] is None


def test_foreign_origin_and_drive_routes_are_rejected(survey):
    app=FastAPI();install(app,data_dir=survey.root,allowed_origins=('http://127.0.0.1:8770',))
    with TestClient(app) as client:
        assert client.post(API+'/review',json=review(),headers={'Origin':'http://other.example'}).status_code==403
        assert client.post(API+'/drive',json={}).status_code==404
        assert client.get(API+'/map').json()['motion_enabled'] is False
        assert client.get('/api/map').status_code==404
        assert client.get('/fieldmap/',follow_redirects=False).headers['location']=='/#fieldmap'


def test_local_data_config_reuses_existing_photos_and_reviews(tmp_path,monkeypatch):
    monkeypatch.delenv('XLEROBOT_FIELDMAP_DATA',raising=False)
    assert data_directory(tmp_path,tmp_path/'platform-data')==tmp_path/'platform-data/fieldmap'
    (tmp_path/'.local-deploy').mkdir()
    config=tmp_path/'.local-deploy/studio-network.json'
    existing=tmp_path/'existing-data'
    config.write_text(json.dumps({'fieldmap_data_dir':str(existing)}),encoding='utf-8')
    assert data_directory(tmp_path,tmp_path/'platform-data')==existing
    monkeypatch.setenv('XLEROBOT_FIELDMAP_DATA','relative/path')
    with pytest.raises(ValueError):data_directory(tmp_path,tmp_path/'platform-data')


def test_platform_module_keeps_review_storage_and_images(survey):
    app=FastAPI();install(app,data_dir=survey.root,allowed_origins=('http://127.0.0.1:8770',))
    with TestClient(app) as client:
        assert client.get('/fieldmap/images/front.jpg').content==b'image'
        assert client.get('/fieldmap/images/private.jpg').status_code==404
        assert client.post(API+'/review',json=review(),headers={'Origin':'http://127.0.0.1:8770'}).status_code==200
        assert client.post(API+'/review',json=review()).status_code==409
        assert client.post(API+'/review',content='x',headers={'Content-Type':'text/plain'}).status_code==415
        assert client.post(API+'/review',content=b'x'*32001,headers={'Content-Type':'application/json'}).status_code==413
    assert Survey(survey.root).load()['review']['revision']==1


def test_stop_attempts_both_existing_controller_actions_and_reports_failures(survey,monkeypatch):
    app=FastAPI();instance=install(app,data_dir=survey.root);requests=[]
    def controller(path,body=None):
        requests.append(path)
        if path=='/api/agent/stop':raise TimeoutError('agent stop timeout')
        return {'ports':[]}
    monkeypatch.setattr(instance,'controller',controller)
    with TestClient(app) as client:
        r=client.post(API+'/stop',json={})
        assert r.status_code==503 and r.json()['requested'] and r.json()['errors']
    assert requests==['/api/agent/stop','/api/controller/stop','/api/status']
