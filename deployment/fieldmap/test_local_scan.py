"""Finite-job outcomes and stop/recovery behavior, with no physical hardware."""
import asyncio
from copy import deepcopy
from io import BytesIO
import json
import time

from PIL import Image
import pytest

from .exploration import Exploration
from .local_scan import LocalScan, head_path, measured_head


def image():
    out=BytesIO();Image.new('RGB',(64,48),(90,120,140)).save(out,'JPEG');return out.getvalue()


class Rig:
    def __init__(self):
        self.pos={f'{p}:{i}':2010 for p,ids in [('COM3',range(1,9)),('COM4',[1,2,3,4,5,6,9,10])] for i in ids}
        self.pos.update({'COM3:7':2010,'COM3:8':2355})
        for p in ('COM3','COM4'):self.pos.update({p+':1':2010,p+':2':1210,p+':3':2860,p+':6':1320})
        self.calls=[];self.seq=0;self.failed_views=0;self.active=False;self.contact=False;self.steps=0
        self.client=object();self.scan=None;self.after_step=None;self.phone_count=0

    def state(self):
        return {'server_pid':1,'ports':[{'port':p,'motors':[dict(id=int(k.split(':')[1]),position=v) for k,v in self.pos.items() if k.startswith(p+':')]} for p in ('COM3','COM4')],
            'agent_run':{'running':False},'web_control':{'active':False},'controller':{'controller':'offline'}}

    async def parked(self):return self.state(),[],{'ready':True,'stationary_base_verified':True}

    async def api(self,path,body=None,timeout=4):
        self.calls.append((path,deepcopy(body)))
        if path=='/api/servo/status':return {'session_id':'native','active':self.active,'busy':False}
        if path=='/api/servo/session':
            self.active=True;return {'session_id':'native','stationary_base':True,'wheel_park':{'verified':True}}
        if path=='/api/servo/step':
            assert self.active and set(body['targets'])=={'head:7','head:8'}
            assert body['torque']==450 and body['speed_deg']==18
            assert all(abs(self.pos['COM3:'+k.split(':')[1]]-v)<=360 for k,v in body['targets'].items())
            self.steps+=1
            for k,v in body['targets'].items():self.pos['COM3:'+k.split(':')[1]]=v
            if self.after_step:self.after_step()
            await asyncio.sleep(.002)
            return {'targets':body['targets'],'contact':{'joint':'head:7'} if self.contact else None,
                'joints':{k:{'position':v} for k,v in body['targets'].items()}}
        if path in ('/api/servo/stop','/api/servo/end'):self.active=False
        return {}

    async def camera(self,c):
        if self.failed_views:
            self.failed_views-=1;raise RuntimeError('temporary source gap')
        self.seq+=1;return image(),{'X-Captured-At':str(time.time()),'X-Frame-Sequence':str(self.seq)}

    def phone(self):
        self.phone_count+=1
        return {'session':'a' if self.phone_count<5 else 'b','sequence':self.phone_count,'meta':{'rgbd':False}}

    def setup(self,tmp_path):
        mission=Exploration(tmp_path/'exploration');mission.start('explicit finite local scanning',True)
        scan=LocalScan(tmp_path/'scan',mission,self.camera,self.phone,client=self.client)
        scan.api=self.api;scan.parked=self.parked;self.scan=scan
        return scan,mission

    async def start(self,scan,mission,**overrides):
        review=await scan.review()
        return await scan.start(dict(confirmed=True,authorization='explicit finite local scan',profile='head_layers',
            mission_id=mission.status()['session_id'],review_id=review['review_id'],**overrides))


def test_head_grid_runs_locally_in_one_session_and_returns_only_after_summary(tmp_path):
    async def run():
        rig=Rig();scan,mission=rig.setup(tmp_path);start=await rig.start(scan,mission)
        assert start['running'];await scan.task
        status=scan.status();assert status['phase']=='completed' and status['completed_views']==18
        assert status['llm_calls']==0 and status['parked_verified']
        assert len([c for c in rig.calls if c[0]=='/api/servo/session'])==1
        assert len(scan.state['phone_epochs'])==2 and not scan.state['phone_epochs'][1]['registered_with_previous']
        assert rig.pos['COM3:7']==2010 and rig.pos['COM3:8']==2355
        assert all(k.startswith('/api/servo/') for k,_ in rig.calls)
        assert len(list((scan.root/start['id']).glob('view-*.jpg')))==18
        assert scan.read(start['id'])['physical_steps']
    asyncio.run(run())


def test_contact_is_durable_and_never_retried_or_followed_by_return_pose(tmp_path):
    async def run():
        rig=Rig();rig.contact=True;scan,mission=rig.setup(tmp_path)
        await rig.start(scan,mission);await scan.task
        assert scan.status()['phase']=='needs_review' and rig.steps==1
        assert scan.state['physical_steps'][0]['contact'] and not rig.active
        assert not scan.state['observations']
    asyncio.run(run())


def test_source_gap_stops_inflight_move_but_recovers_while_parked(tmp_path):
    async def run():
        rig=Rig();scan,mission=rig.setup(tmp_path)
        def once():
            if rig.steps==1:rig.failed_views=1
        rig.after_step=once
        await rig.start(scan,mission);await scan.task
        assert scan.state['phase']=='needs_review'
        assert rig.steps==1 and scan.state.get('interrupted_receipts')
        rig.failed_views=3;rig.active=False;scan.servo_id=None;scan.cancelled=False
        scan.state['mission_id']=mission.status()['session_id']
        await scan._wait_views(seconds=1)
        assert scan.state['source_waits'][-1]['recovered']
    asyncio.run(run())


def test_stop_latch_during_motion_prevents_followup_targets(tmp_path):
    async def run():
        rig=Rig();scan,mission=rig.setup(tmp_path)
        def stop():scan.cancelled=True
        rig.after_step=stop
        await rig.start(scan,mission);await scan.task
        assert scan.status()['phase']=='cancelled' and rig.steps==1 and not rig.active
        assert scan.read(scan.state['id'])['cancelled']
    asyncio.run(run())


def test_stale_or_changed_reference_never_submits_a_motion(tmp_path):
    async def run():
        rig=Rig();scan,mission=rig.setup(tmp_path);review=await scan.review()
        rig.pos['COM4:9']+=10
        with pytest.raises(ValueError,match='姿态'):
            await scan.start(dict(confirmed=True,authorization='explicit finite local scan',profile='head_layers',
                mission_id=mission.status()['session_id'],review_id=review['review_id']))
        assert not any(path=='/api/servo/session' for path,_ in rig.calls)
    asyncio.run(run())


def test_restart_preserves_interrupt_and_never_replays_pending_actions(tmp_path):
    rig=Rig();scan,_=rig.setup(tmp_path);identifier='a'*32;(scan.root/identifier).mkdir()
    scan.state=dict(id=identifier,phase='moving',running=True,physical_steps=[{'original':'receipt'}]);scan._save()
    resumed=LocalScan(scan.root,scan.exploration,rig.camera,client=rig.client)
    assert resumed.status()['phase']=='interrupted' and resumed.status()['review_required']
    assert resumed.task is None and resumed.read(identifier)['physical_steps']==[{'original':'receipt'}]


def test_segment_path_respects_intermediate_constraints():
    origin={'head:7':2787,'head:8':2750};target={'head:7':1230,'head:8':2150}
    for part in head_path(origin,target):
        assert max(abs(part[k]-origin[k]) for k in part)<=360
        origin=part
    assert origin==target


def test_observation_uses_actual_encoders_instead_of_requested_target():
    assert measured_head({'targets':{'head:7':2010,'head:8':2355},
        'joints':{'head:7':{'position':2019},'head:8':{'position':2367}}})=={'head:7':2019,'head:8':2367}
    with pytest.raises(RuntimeError,match='实际位置'):measured_head({'targets':{'head:7':2010,'head:8':2355}})


def test_turn_batch_limit_and_stop_ownership_are_enforced(tmp_path):
    async def run():
        rig=Rig();scan,mission=rig.setup(tmp_path)
        review=await scan.review()
        with pytest.raises(ValueError,match='最多12'):
            await scan.start(dict(confirmed=True,authorization='explicit finite local scan',profile='turn_batch',pulses=13,
                mission_id=mission.status()['session_id'],review_id=review['review_id']))
        scan.servo_id='another-session';rig.active=True
        await scan._stop_native('do not steal another owner')
        assert rig.active
    asyncio.run(run())


def test_http_readonly_status_and_foreign_start_rejection(tmp_path):
    async def run():
        import httpx
        from fastapi import FastAPI
        from .integration import install,API
        app=FastAPI();install(app,data_dir=tmp_path,allowed_origins=['http://local'])
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://test') as client:
            status=await client.get(API+'/local-scan/status')
            assert status.json()['llm_calls']==0 and status.json()['translation_enabled'] is False
            rejected=await client.post(API+'/local-scan/start',json={},headers={'Origin':'https://foreign'})
            assert rejected.status_code==403
            bad=await client.get(API+'/local-scan/invalid/private.jpg');assert bad.status_code==404
        await app.state.local_scan.close()
    asyncio.run(run())


def test_real_grasp_attribute_contract_blocks_scan_before_capture(tmp_path):
    async def run():
        from types import SimpleNamespace
        import httpx
        from fastapi import FastAPI
        from .integration import install,API
        app=FastAPI();install(app,data_dir=tmp_path)
        app.state.grasp=SimpleNamespace(op='pick',executor=SimpleNamespace(active=False))
        rig=Rig();app.state.local_scan.parked=rig.parked;app.state.local_scan.api=rig.api
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://test') as client:
            response=await client.post(API+'/local-scan/review',json={})
            assert response.status_code==409 and '占用' in response.json()['detail']
        await app.state.local_scan.close()
    asyncio.run(run())


def test_stop_during_start_preflight_wins_over_queued_start(tmp_path):
    async def run():
        rig=Rig();scan,mission=rig.setup(tmp_path);review=await scan.review()
        entered=asyncio.Event();release=asyncio.Event()
        async def parked():
            entered.set();await release.wait();return await rig.parked()
        scan.parked=parked
        start=asyncio.create_task(scan.start(dict(confirmed=True,authorization='explicit finite local scan',
            profile='head_layers',mission_id=mission.status()['session_id'],review_id=review['review_id'])))
        await entered.wait();await scan.stop();release.set()
        with pytest.raises(ValueError,match='预检期间收到停止'):await start
        assert scan.task is None and not rig.active
    asyncio.run(run())
