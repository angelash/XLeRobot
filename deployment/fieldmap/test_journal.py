import hashlib,json,time
from pathlib import Path
from io import BytesIO
from dataclasses import replace
from PIL import Image
import pytest
from .journal import SpatialJournal
from .store import Survey,atomic_json
from .history import History
from deployment.phone_vision.evidence import Evidence
from deployment.phone_vision.reconstruction import Reconstruction
from deployment.phone_vision.test_evidence import frame


def state(a,b,pid=1):
    return {'server_pid':pid,'updated_at':'fresh-test','ports':[{'port':'COM4','motors':[{'id':9,'position':a},{'id':10,'position':b}]}],
            'controller':{'controller':'online'},'web_control':{'active':True,'actions':['base.forward'],'seq':3}}


def test_exact_inputs_and_encoder_outcomes_survive_restart_and_import(tmp_path):
    clock=[time.time()];survey=Survey(tmp_path/'map');root=tmp_path/'journal'
    journal=SpatialJournal(root,survey,lambda:None,forward_sign=-1,clock=lambda:clock[0]);journal.configure(True)
    journal.accepted_input('/api/web/input',{'session_id':'SECRET_TOKEN','seq':1,'actions':['base.forward'],'arm':'left','scale':.5},{'active':True,'session_id':'SECRET_TOKEN','seq':1})
    journal.sample(state(4000,96));clock[0]+=.5;journal.sample(state(4050,46));journal._flush(True)
    file=journal.folder/'inputs.jsonl';assert 'SECRET_TOKEN' not in file.read_text('utf8')
    assert journal.run['events'][1]['wheel_delta_raw']=={'9':50,'10':-50} if journal.run['events'][0]['op']=='manual_input' else journal.run['events'][0]['wheel_delta_raw']=={'9':50,'10':-50}
    index=History(tmp_path/'index',journal_root=root,forward_sign=-1);index.scan();result=index.query(0,clock[0]*1000+1000)
    assert result['summary']['translations']==1 and len(result['tracks'])==1
    assert result['tracks'][0]['points'][-1]['x_m']>0
    restarted=SpatialJournal(root,survey,lambda:None);assert restarted.enabled() and file.exists()


def test_sampling_gap_or_new_controller_breaks_track_without_discarding_prior_run(tmp_path):
    clock=[time.time()];j=SpatialJournal(tmp_path/'j',Survey(tmp_path/'map'),lambda:None,clock=lambda:clock[0]);j.configure(True)
    j.sample(state(0,0));first=j.folder;clock[0]+=4;j.sample(state(1000,1000))
    assert first!=j.folder and json.loads((first/'run.json').read_text('utf8'))['gaps']==1
    assert not any(e['op']=='rotate' for e in j.run['events'])


def test_passive_assistant_motion_does_not_double_count_native_agent_history(tmp_path):
    clock=[time.time()];j=SpatialJournal(tmp_path/'j',Survey(tmp_path/'map'),lambda:None,clock=lambda:clock[0]);j.configure(True)
    first=state(0,0);first['web_control']['active']=False;first['controller']['controller']='offline';first['agent_run']={'running':True}
    j.sample(first);clock[0]+=.5;second=state(50,4046);second.update(web_control={'active':False},controller={'controller':'offline'},agent_run={'running':True});j.sample(second)
    assert j.run['events'][0]['op']=='observed_motion' and j.run['events'][0]['wheel_delta_raw']=={'9':50,'10':-50}


def test_checkpoint_keeps_each_version_and_rejects_stale_save(tmp_path):
    s=Survey(tmp_path);original={'id':'first','features':[],'observations':[]};atomic_json(tmp_path/'map.json',original)
    raw=(tmp_path/'map.json').read_bytes();s.preserve();updated={**original,'id':'second'};s.save(updated,hashlib.sha256(raw).hexdigest())
    assert len(s.snapshots()['items'])==3
    with pytest.raises(ValueError):s.save(original,hashlib.sha256(raw).hexdigest())
    assert s.load()['id']=='second' and any(s.read_map(p)['id']=='first' for p in (tmp_path/'history').glob('*.json'))


def test_reconstruction_never_merges_ar_sessions_and_keeps_original_captures(tmp_path):
    data=BytesIO();Image.new('RGB',(3,3),(50,100,150)).save(data,format='JPEG');f=replace(frame(),rgb=data.getvalue());store=Evidence(tmp_path);builder=Reconstruction(store)
    saved=store.capture('android',f);m=builder.add(saved,f);assert m['point_count']>0
    assert builder.add(saved,f)['source_frames']==1
    other=replace(f,meta={**f.meta,'arcore_capture_session':'second'});s2=store.capture('android',other);m2=builder.add(s2,other)
    assert m['id']!=m2['id'] and len(store.list('models')['items'])==2
    assert store.media('captures',saved['id'],'depth.f32').read_bytes()==f.raw
    empty=replace(f,raw=b'\0'*36);s3=store.capture('android',empty);m3=builder.add(s3,empty)
    assert m3['point_count']==m['point_count'] and m3['source_frames']==2


def test_mobility_is_separate_from_blocked_status(tmp_path):
    s=Survey(tmp_path);atomic_json(tmp_path/'map.json',{'id':'m','features':[{'id':'chair','kind':'blocked'}],'observations':[]})
    s.review({'survey_id':'m','revision':0,'feature_id':'chair','kind':'blocked','mobility':'movable','confirmed':False})
    v=s.load();assert v['features'][0]['kind']=='blocked' and v['review']['items']['chair']['mobility']=='movable'
