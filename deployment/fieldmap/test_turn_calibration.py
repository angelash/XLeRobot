"""Finite experiment outcomes, cancellation and source ownership; no hardware."""
import asyncio
from copy import deepcopy
from types import SimpleNamespace

import cv2
import numpy as np
import pytest

from .test_autonomy import LocalRig, until
from .test_local_scan import image
from .turn_calibration import TurnCalibration, PLAN


class TurnRig(LocalRig):
    def __init__(self):
        super().__init__()
        self.session='phone-fixed';self.phone_missing=False;self.phone_seq=0
        self.submissions=[];self.hold_motion=False;self.fail_motion=False
        self.rgbd_bad=False
        self.dark_views=False

    async def camera(self,camera):
        raw,meta=await super().camera(camera)
        if self.dark_views:raw=cv2.imencode('.jpg',np.zeros((48,64),np.uint8))[1].tobytes()
        return raw,meta

    def frame(self,device):
        assert device=='iphone'
        if self.phone_missing:raise LookupError('source disconnected')
        self.phone_seq+=1
        return SimpleNamespace(session=self.session,sequence=self.phone_seq,
            rgb=image(),raw=np.ones((48,64),np.float32).tobytes(),received=self.now[0],
            meta=dict(rgbd=True,width=64,height=48,depth_source='record3d_hue_video',
                      intrinsics=dict(fx=90.,fy=90.,tx=32.,ty=24.)))

    def visual(self,first,last,intrinsics):
        # Synthetic visual measurement is deliberately independent of the
        # fitted model; the real image estimator is exercised separately.
        row=self.job.trial
        degrees=row['direction']*row['pulses']*3.5*.5 if row else 0.
        return dict(accepted=True,rotation_magnitude_deg=abs(degrees),
                    rotation_matrix=cv2.Rodrigues(np.array([0.,np.radians(degrees),0.]))[0].tolist())

    async def rgbd(self,row):
        return dict(ok=True,rotation_magnitude_deg=row['pulses']*3.5*.5+(15 if self.rgbd_bad else 0))

    async def turn(self,direction):
        scan=self.scan;self.submissions.append((scan.state['planned_pulses'],direction))
        while self.hold_motion and not scan.cancelled:await asyncio.sleep(.001)
        if scan.cancelled:raise RuntimeError('original finite executor stopped')
        rotations=[]
        for _ in range(1 if self.fail_motion else scan.state['planned_pulses']):
            self.pos['COM4:9']+=80*direction;self.pos['COM4:10']+=80*direction
            rotations.append(dict(op='rotate',status='done',yaw_delta_deg_estimate=direction*3.5))
        scan.state.update(native_run_id='native-'+scan.state['id'],
                          native_receipt=dict(results=rotations,partial=self.fail_motion))
        await asyncio.sleep(.003)
        if self.fail_motion:raise RuntimeError('native physical result uncertain')

    def calibration(self,tmp_path):
        scan,mission=self.setup(tmp_path);scan._turn=self.turn
        phone=SimpleNamespace(frame=self.frame,clock=lambda:self.now[0],repo=tmp_path)
        self.job=TurnCalibration(tmp_path/'calibration',mission,scan,self.inputs,lambda:phone,
            clock=lambda:self.now[0],tick_s=.001,settle_s=0,analyser=self.visual,rgbd_checker=self.rgbd)
        return self.job,scan,mission

    async def begin_calibration(self,job,scan,mission):
        review=await scan.review()
        return await job.start(dict(confirmed=True,authorization='user authorized bounded local self calibration',
            mission_id=mission.status()['session_id'],review_id=review['review_id']))


def test_local_eight_trial_plan_validates_without_model_or_geometry_writes(tmp_path):
    async def run():
        rig=TurnRig();job,scan,mission=rig.calibration(tmp_path)
        before=deepcopy(mission.status())
        await rig.begin_calibration(job,scan,mission);await job.task
        assert job.state['phase']=='completed' and rig.submissions==list(PLAN)
        assert job.state['model']['accepted'] and job.state['model']['gain']==pytest.approx(.5)
        assert job.state['closure']['heading_revisit_passed']
        assert job.status()['llm_calls']==0 and not job.status()['geometry_parameters_written']
        assert job.state['progress']['native_pulses']==60 and job.status()['completed_trials']==8
        assert mission.status()['turn_attempts']==before['turn_attempts']+60
        assert mission.status()['wheel_path_m']==before['wheel_path_m']
        assert job.state['parked_verified'] and scan.reserved_by is None
        assert len(list((job.root/job.state['id']).glob('trials/*/result.json')))==8
    asyncio.run(run())


def test_executor_reservation_and_stop_during_review_prevent_late_start(tmp_path):
    async def run():
        rig=TurnRig();job,scan,mission=rig.calibration(tmp_path)
        original=scan.review;entered=asyncio.Event();release=asyncio.Event()
        review=await original()
        async def blocked(**kw):
            entered.set();await release.wait();return await original(**kw)
        scan.review=blocked
        await job.start(dict(confirmed=True,authorization='user authorized bounded calibration',
            mission_id=mission.status()['session_id'],review_id=review['review_id']))
        await entered.wait()
        with pytest.raises(ValueError,match='自主任务'):
            await scan.start(dict(confirmed=True))
        await job.stop();release.set();await job.task
        assert rig.submissions==[] and job.state['phase']=='stopped' and scan.reserved_by is None
    asyncio.run(run())


@pytest.mark.parametrize('failure',['missing_phone','changed_session','unknown_feedback'])
def test_inflight_reference_or_feedback_loss_stops_without_replay(tmp_path,failure):
    async def run():
        rig=TurnRig();rig.hold_motion=True;job,scan,mission=rig.calibration(tmp_path)
        await rig.begin_calibration(job,scan,mission)
        await until(lambda:bool(rig.submissions))
        if failure=='missing_phone':rig.phone_missing=True
        elif failure=='changed_session':rig.session='new-phone-epoch'
        else:
            async def unavailable():raise ConnectionError('telemetry lost')
            job.inputs=unavailable
        await job.task
        assert len(rig.submissions)==1 and job.state['hard_hold']
        assert job.state['parked_verified'] and scan.state['review_required']
        with pytest.raises(ValueError,match='停止锁'):
            await rig.begin_calibration(job,scan,mission)
    asyncio.run(run())


def test_failed_child_is_recorded_even_when_phone_disappears_after_parking(tmp_path):
    async def run():
        rig=TurnRig();rig.fail_motion=True;job,scan,mission=rig.calibration(tmp_path)
        original=scan._run
        async def after(direction):
            await original(direction);rig.phone_missing=True
        scan._run=after
        await rig.begin_calibration(job,scan,mission);await job.task
        assert len(rig.submissions)==1 and job.state['hard_hold']=='physical_trial_failed_or_uncertain'
        assert job.state['current_trial']['child_result']['native_receipt']['partial']
        assert len(job.state['current_trial']['rotations'])==1
        assert job.state['progress']['native_pulses']==0
    asyncio.run(run())


def test_held_out_depth_disagreement_keeps_all_trials_and_rejects_model(tmp_path):
    async def run():
        rig=TurnRig();rig.rgbd_bad=True;job,scan,mission=rig.calibration(tmp_path)
        await rig.begin_calibration(job,scan,mission);await job.task
        assert rig.submissions==list(PLAN) and len(job.state['trials'])==8
        assert job.state['phase']=='not_adopted' and not job.state['model']['accepted']
        assert job.state['model']['reason']=='held_out_rgbd_cross_check_failed'
        assert job.state['model']['gain_validation_passed']
    asyncio.run(run())


def test_source_wait_before_submission_resumes_the_same_task(tmp_path):
    async def run():
        rig=TurnRig();rig.phone_missing=True;job,scan,mission=rig.calibration(tmp_path)
        started=await rig.begin_calibration(job,scan,mission)
        await until(lambda:job.state['phase']=='waiting_source')
        assert not rig.submissions
        rig.phone_missing=False;await job.task
        assert job.state['id']==started['id'] and rig.submissions==list(PLAN)
        assert job.state['phase']=='completed'
    asyncio.run(run())


def test_dark_station_waits_without_motion_and_resumes_locally_after_lighting_returns(tmp_path):
    async def run():
        rig=TurnRig();rig.dark_views=True;job,scan,mission=rig.calibration(tmp_path)
        started=await rig.begin_calibration(job,scan,mission)
        await until(lambda:job.state['phase']=='waiting_visibility')
        assert not rig.submissions
        rig.dark_views=False
        # The normal owner supplies the new views; no model clearance flag.
        await scan._views();await job.task
        assert job.state['id']==started['id'] and job.state['phase']=='completed'
        assert rig.submissions==list(PLAN)
    asyncio.run(run())


def test_owned_depth_worker_honours_overall_budget_and_is_terminated(tmp_path,monkeypatch):
    async def run():
        rig=TurnRig();job,scan,mission=rig.calibration(tmp_path)
        # Prepare the parked helper boundary without starting the motion loop.
        job.state.update(id='a'*32,mission_id=mission.status()['session_id'],policy=dict(max_duration_s=60))
        job.begun=0.;rig.now[0]=61.
        folder=tmp_path/'calibration'/job.state['id']/'trials'/'000';folder.mkdir(parents=True)
        worker=tmp_path/'.venv-dense/Scripts/python.exe';worker.parent.mkdir(parents=True);worker.touch()
        class Process:
            returncode=None;terminated=False
            def poll(self):return self.returncode
            def terminate(self):self.terminated=True;self.returncode=1
            def wait(self,timeout):return self.returncode
        process=Process()
        monkeypatch.setattr('deployment.fieldmap.turn_calibration.subprocess.Popen',lambda *a,**kw:process)
        result=await job._rgbd_check(dict(index=0))
        assert result['reason']=='rgbd_budget_reached' and process.terminated
        assert not rig.submissions
    asyncio.run(run())


def test_studio_routes_reject_external_targets_and_generic_stop_owns_parent(tmp_path):
    async def run():
        import httpx
        from fastapi import FastAPI
        from .integration import install,API
        app=FastAPI();install(app,data_dir=tmp_path,allowed_origins=['http://local'])
        rig=TurnRig();rig.phone_missing=True
        job=app.state.fieldmap_turn_calibration;scan=app.state.local_scan;mission=app.state.exploration
        scan.parked=rig.parked;scan.api=rig.api;scan.camera=rig.camera
        rig.scan=scan;rig.job=job;job.inputs=rig.inputs;job.tick_s=.001
        job.phone=lambda:SimpleNamespace(frame=rig.frame,clock=lambda:rig.now[0])
        mission.start('user authorized local station calibration',True)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://test') as client:
            review=(await client.post(API+'/local-scan/review',json={})).json()
            value=dict(confirmed=True,authorization='user authorized station calibration',
                       mission_id=mission.status()['session_id'],review_id=review['review_id'])
            assert (await client.post(API+'/turn-calibration/start',json=value,headers={'Origin':'https://foreign'})).status_code==403
            assert (await client.post(API+'/turn-calibration/start',json={**value,'direction':1})).status_code==409
            assert (await client.post(API+'/turn-calibration/start',json=value)).status_code==200
            await until(lambda:job.state['phase']=='waiting_source')
            assert (await client.post(API+'/local-scan/stop',json={})).status_code==200
            await job.task
            status=(await client.get(API+'/turn-calibration/status')).json()
            assert status['cancelled'] and not status['running'] and not rig.submissions
            assert scan.reserved_by is None
            assert (await client.get(API+'/turn-calibration/invalid')).status_code==404
    asyncio.run(run())
