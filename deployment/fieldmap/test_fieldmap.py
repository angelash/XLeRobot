"""Survey review must survive restart, reject stale writes and keep motion closed."""
import json
import threading
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from http.server import ThreadingHTTPServer
import pytest
from deployment.fieldmap.server import Survey,handler_for,atomic_json,watch_parent


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
    server=ThreadingHTTPServer(('127.0.0.1',0),handler_for(survey));thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    base=f'http://127.0.0.1:{server.server_port}'
    try:
        req=Request(base+'/api/review',data=json.dumps(review()).encode(),headers={'Origin':'http://other.example','Content-Type':'application/json'})
        with pytest.raises(HTTPError) as exc:urlopen(req)
        assert exc.value.code==403
        req=Request(base+'/api/drive',data=b'{}',headers={'Content-Type':'application/json'})
        with pytest.raises(HTTPError) as exc:urlopen(req)
        assert exc.value.code==404
        with urlopen(base+'/api/map') as response:assert json.load(response)['motion_enabled'] is False
    finally:
        server.shutdown();server.server_close();thread.join()


def test_parent_exit_closes_the_web_listener_without_a_motion_request():
    closed=threading.Event()
    class Server:
        def shutdown(self):closed.set()
    watch_parent(Server(),123,probe=lambda pid:False)
    assert closed.is_set()
