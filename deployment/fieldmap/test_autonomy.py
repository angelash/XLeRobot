"""Outcomes at the local decision boundary; no robot, SSH or model calls."""
import asyncio
from copy import deepcopy
import json
from types import SimpleNamespace

import httpx
import pytest

from .autonomy import AutonomousSurvey,StudioInputs,anchor,decision
from .test_local_scan import Rig


class LocalRig(Rig):
    def __init__(self):
        super().__init__();self.ready=True;self.fresh=True;self.hard=None;self.probes=0
        self.now=[0.]

    async def inputs(self):
        self.probes+=1
        return dict(anchor=anchor(self.state()),hardware_ready=self.ready,hardware_issues=[] if self.ready else ['park_window'],
                    sources_fresh=self.fresh,source_issues=[] if self.fresh else ['stale_camera'],hard_stop=self.hard,
                    navigation_blockers=['metric_ground_clearance_unvalidated','continuous_localization_not_connected'])

    def autonomous(self,tmp_path):
        scan,mission=self.setup(tmp_path)
        job=AutonomousSurvey(tmp_path/'autonomy',mission,scan,self.inputs,clock=lambda:self.now[0],tick_s=.01)
        self.job=job;return job,scan,mission

    async def begin(self,job,mission,**extra):
        return await job.start(dict(confirmed=True,authorization='user authorized local exploration decisions',
                                    mission_id=mission.status()['session_id'],**extra))


async def until(predicate,timeout=8.):
    deadline=asyncio.get_running_loop().time()+timeout
    while not predicate():
        if asyncio.get_running_loop().time()>deadline:raise AssertionError('local condition did not occur')
        await asyncio.sleep(.002)


def test_robot_selects_and_completes_survey_then_waits_without_model_decisions(tmp_path):
    async def run():
        rig=LocalRig();job,scan,mission=rig.autonomous(tmp_path)
        await rig.begin(job,mission)
        await until(lambda:job.state['phase']=='waiting_navigation')
        s=job.status();assert s['progress']['views']==18 and s['progress']['station_scans']==1
        assert s['running'] and s['llm_calls']==0 and s['model_step_approvals']==0
        steps=rig.steps;await asyncio.sleep(.03)
        assert rig.steps==steps and not any(path=='/api/agent/run' for path,_ in rig.calls)
        kinds=[v['kind'] for v in job.state['events']]
        assert 'local_action_selected' in kinds and 'station_survey_completed' in kinds
        assert not s['automatic_translation'] and s['metric_map_completed'] is False
        await job.stop();await job.task
        assert not job.status()['running'] and not scan.reserved_by and not rig.active
    asyncio.run(run())


def test_source_and_hardware_waits_resume_locally_without_new_start_or_permission(tmp_path):
    async def run():
        rig=LocalRig();rig.fresh=False;job,scan,mission=rig.autonomous(tmp_path)
        started=await rig.begin(job,mission);await until(lambda:job.state['phase']=='waiting_source')
        assert not rig.steps
        rig.ready=False;rig.fresh=True
        await until(lambda:job.state['phase']=='waiting_hardware');assert not rig.steps
        rig.ready=True
        await until(lambda:job.state['phase']=='waiting_navigation')
        assert job.state['id']==started['id'] and job.state['progress']['views']==18
        await job.stop();await job.task
    asyncio.run(run())


def test_periodic_and_no_progress_reports_use_actual_progress_not_heartbeat(tmp_path):
    async def run():
        rig=LocalRig();rig.fresh=False;job,scan,mission=rig.autonomous(tmp_path)
        await rig.begin(job,mission)
        await until(lambda:job.state['phase']=='waiting_source')
        rig.now[0]=61
        await until(lambda:any(e['kind']=='periodic_summary' for e in job.state['events']))
        rig.now[0]=181
        await until(lambda:any(e['kind']=='no_progress' for e in job.state['events']))
        count=sum(e['kind']=='no_progress' for e in job.state['events'])
        await asyncio.sleep(.02)
        assert count==1 and sum(e['kind']=='no_progress' for e in job.state['events'])==1
        assert job.state['progress']['views']==0 and job.state['running']
        rig.fresh=True;await until(lambda:job.state['phase']=='waiting_navigation')
        assert not job.no_progress_reported
        await job.stop();await job.task
    asyncio.run(run())


@pytest.mark.parametrize('reason',['operator_takeover','controller_restarted','actual_contact'])
def test_hard_stop_never_auto_recovers_and_survives_new_task_and_restart(tmp_path,reason):
    async def run():
        rig=LocalRig();rig.fresh=False;job,scan,mission=rig.autonomous(tmp_path)
        await rig.begin(job,mission);await until(lambda:job.state['phase']=='waiting_source')
        rig.hard=reason;await job.task;rig.hard=None;rig.fresh=True
        assert job.status()['hard_hold']==reason and not rig.steps
        with pytest.raises(ValueError,match='停止锁'):await rig.begin(job,mission)
        restarted=AutonomousSurvey(job.root,mission,scan,rig.inputs)
        assert restarted.state['hard_hold']==reason and restarted.task is None
    asyncio.run(run())


def test_manual_pose_change_invalidates_station_instead_of_reusing_prior_clearance(tmp_path):
    async def run():
        rig=LocalRig();rig.fresh=False;job,scan,mission=rig.autonomous(tmp_path)
        await rig.begin(job,mission);await until(lambda:job.state['phase']=='waiting_source')
        rig.pos['COM4:9']+=12;await job.task
        assert job.state['hard_hold']=='uncommanded_pose_change' and not rig.steps
    asyncio.run(run())


def test_child_contact_is_latched_with_original_receipt_and_no_repeated_motion(tmp_path):
    async def run():
        rig=LocalRig();rig.contact=True;job,scan,mission=rig.autonomous(tmp_path)
        await rig.begin(job,mission);await job.task
        assert rig.steps==1 and job.state['hard_hold']=='station_action_failed_or_uncertain'
        assert scan.read(job.state['child_id'])['physical_steps'][0]['contact']
    asyncio.run(run())


def test_human_stop_during_async_preflight_cannot_start_a_queued_survey(tmp_path):
    async def run():
        rig=LocalRig();job,scan,mission=rig.autonomous(tmp_path)
        entered=asyncio.Event();release=asyncio.Event()
        async def parked():entered.set();await release.wait();return await rig.parked()
        scan.parked=parked
        pending=asyncio.create_task(rig.begin(job,mission));await entered.wait()
        await job.stop('human stop');release.set()
        with pytest.raises(ValueError,match='预检期间收到停止'):await pending
        assert job.task is None and scan.reserved_by is None and not rig.steps
    asyncio.run(run())


def test_runner_reservation_rejects_another_supervisor_until_stopped(tmp_path):
    async def run():
        rig=LocalRig();rig.fresh=False;job,scan,mission=rig.autonomous(tmp_path)
        await rig.begin(job,mission);await until(lambda:job.state['phase']=='waiting_source')
        with pytest.raises(ValueError,match='自主任务'):await scan.review()
        with pytest.raises(ValueError,match='自主任务'):await scan.start({})
        await job.stop();await job.task
        assert (await scan.review())['review_id']
    asyncio.run(run())


def test_duration_expires_without_claiming_map_finished_or_clearing_intent(tmp_path):
    async def run():
        rig=LocalRig();rig.fresh=False;job,scan,mission=rig.autonomous(tmp_path)
        await rig.begin(job,mission,max_duration_s=60);await until(lambda:job.state['phase']=='waiting_source')
        rig.now[0]=61;await job.task
        assert job.status()['phase']=='budget_reached' and mission.status()['enabled']
        assert not job.status()['metric_map_completed'] and not rig.steps
    asyncio.run(run())


def test_restart_records_interruption_without_replaying_child(tmp_path):
    rig=LocalRig();job,scan,mission=rig.autonomous(tmp_path);identifier='a'*32
    (job.root/identifier).mkdir()
    job.state.update(id=identifier,running=True,phase='survey_station',child_id='original-child')
    job._save()
    new=AutonomousSurvey(job.root,mission,scan,rig.inputs)
    assert new.state['phase']=='interrupted' and new.state['hard_hold']=='restart_requires_review'
    assert new.state['child_id']=='original-child' and new.task is None and not rig.steps


def test_failed_initial_persistence_cannot_leave_a_live_or_restartable_task(tmp_path,monkeypatch):
    async def run():
        rig=LocalRig();job,scan,mission=rig.autonomous(tmp_path)
        def fail():raise PermissionError('initial persistence failed')
        monkeypatch.setattr(job,'_save',fail)
        with pytest.raises(PermissionError):await rig.begin(job,mission)
        assert not job.state['running'] and job.state['hard_hold']=='start_failed'
        assert job.task is None and scan.reserved_by is None and not rig.steps
        with pytest.raises(ValueError,match='停止锁'):await rig.begin(job,mission)
    asyncio.run(run())


def test_storage_failure_after_start_still_stops_owned_executor(tmp_path,monkeypatch):
    async def run():
        rig=LocalRig();rig.fresh=False;job,scan,mission=rig.autonomous(tmp_path)
        await rig.begin(job,mission);await until(lambda:job.state['phase']=='waiting_source')
        def fail():raise PermissionError('persistent file lock')
        monkeypatch.setattr(job,'_save',fail)
        scan.servo_id='native';rig.active=True
        scan.task=asyncio.create_task(asyncio.sleep(.1))
        await job.stop();await job.task
        assert job.state['persistence_error'] and not rig.active and not scan.reserved_by
    asyncio.run(run())


def test_preflight_source_loss_has_bounded_recovery_without_mechanical_attempts(tmp_path):
    async def run():
        rig=LocalRig();job,scan,mission=rig.autonomous(tmp_path)
        async def rejected(**_):raise ValueError('frame expired before native call')
        scan.review=rejected
        await rig.begin(job,mission)
        for count in range(1,11):
            await until(lambda:job.preflight_failures>=count)
            rig.now[0]+=20
        await job.task
        assert not rig.steps and job.state['hard_hold']=='local_preflight_recovery_exhausted'
        assert len([e for e in job.state['events'] if e['kind']=='preflight_deferred'])==10
    asyncio.run(run())


def test_live_probe_rejects_rgbd_tracking_and_mock_map_as_navigation_permission(tmp_path):
    async def run():
        rig=LocalRig();scan,mission=rig.setup(tmp_path)
        phone={'devices':{'android':{'frame_available':True,'mode':'arcore','meta':{'rgbd':True,'tracking':'TRACKING','depth_02_to_5m_fraction':1.}}}}
        app=SimpleNamespace(state=SimpleNamespace(phone_vision=SimpleNamespace(status=lambda:phone),metric_mapping={'motion_enabled':True},perception=SimpleNamespace(status=lambda:{'calibration':{'status':'adopted'}})))
        async def api(path,body=None,timeout=4):
            if path=='/api/status':return rig.state()
            return {'active':False,'busy':False}
        scan.api=api
        inputs=StudioInputs(app,scan);observed=await inputs()
        assert not observed['live_navigation_connected'] and 'live_navigation_backend_not_connected' in observed['navigation_blockers']
        observed['hardware_ready']=True
        assert decision(observed,True)[0]=='waiting_navigation'
    asyncio.run(run())


def test_shared_selftest_mutex_accepts_only_the_exact_owned_servo_session(tmp_path):
    async def run():
        rig=LocalRig();scan,mission=rig.setup(tmp_path)
        scan.servo_id='our-native';scan.task=asyncio.create_task(asyncio.sleep(10))
        servo={'active':True,'busy':True,'session_id':'our-native'}
        state=rig.state();state['limit_selftest']={'running':True,'current':'servo'}
        async def api(path,body=None,timeout=4):return deepcopy(state if path=='/api/status' else servo)
        scan.api=api;app=SimpleNamespace(state=SimpleNamespace())
        inputs=StudioInputs(app,scan)
        assert not (await inputs())['hard_stop']
        servo['session_id']='foreign-native'
        assert (await inputs())['hard_stop']=='foreign_servo_operation'
        servo['session_id']='our-native';state['limit_selftest']['current']='head:pan'
        assert (await inputs())['hard_stop']=='other_robot_operation'
        scan.task.cancel();await asyncio.gather(scan.task,return_exceptions=True)
    asyncio.run(run())


def test_pending_session_response_uses_exact_job_label_and_retains_owned_stop(tmp_path):
    async def run():
        rig=LocalRig();scan,mission=rig.setup(tmp_path)
        label='local station head '+'a'*32
        scan.state.update(pending_servo_start=True,native_servo_label=label)
        scan.task=asyncio.create_task(asyncio.sleep(10))
        servo={'active':True,'busy':False,'session_id':'response-not-yet-received','label':label}
        state=rig.state();state['limit_selftest']={'running':True,'current':'servo'}
        calls=[]
        async def api(path,body=None,timeout=4):
            calls.append(path)
            if path=='/api/servo/stop':servo['active']=False
            return deepcopy(state if path=='/api/status' else servo)
        scan.api=api;inputs=StudioInputs(SimpleNamespace(state=SimpleNamespace()),scan)
        assert not (await inputs())['hard_stop']
        servo['label']='foreign operation'
        assert (await inputs())['hard_stop']=='foreign_servo_operation'
        await scan._stop_native('do not stop foreign session');assert '/api/servo/stop' not in calls
        servo['label']=label
        await scan._stop_native('stop owned pending session');assert not servo['active']
        scan.task.cancel();await asyncio.gather(scan.task,return_exceptions=True)
    asyncio.run(run())


def test_http_rejects_external_decisions_and_stops_scheduler_before_controller(tmp_path):
    async def run():
        from fastapi import FastAPI
        from .integration import install,API
        app=FastAPI();survey=install(app,data_dir=tmp_path,allowed_origins=['http://local'])
        rig=LocalRig();job=app.state.fieldmap_autonomy;scan=app.state.local_scan;mission=app.state.exploration
        scan.parked=rig.parked;scan.api=rig.api;scan.camera=rig.camera
        rig.fresh=False;job.inputs=rig.inputs;job.tick_s=.001
        mission.start('explicit autonomous survey authorization',True)
        calls=[]
        def stop(path,body):
            assert job.cancelled and not mission.status()['enabled'];calls.append(path)
        survey.controller=stop
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://test') as client:
            value=dict(confirmed=True,authorization='explicit autonomous survey authorization',mission_id=mission.status()['session_id'])
            assert (await client.post(API+'/autonomy/start',json=value,headers={'Origin':'https://foreign'})).status_code==403
            for key in ('clearance_confirmed','waypoints','navigation_ready','direction'):
                assert (await client.post(API+'/autonomy/start',json={**value,key:True})).status_code==409
            response=await client.post(API+'/autonomy/start',json=value);assert response.status_code==200
            await until(lambda:job.state['phase']=='waiting_source')
            assert (await client.post(API+'/local-scan/review',json={})).status_code==409
            response=await client.post(API+'/exploration/stop',json={});assert response.status_code==200
            await job.task
            s=(await client.get(API+'/autonomy/status')).json();assert not s['running'] and s['cancelled']
            assert (await client.get(API+'/autonomy/invalid')).status_code==404
        assert calls==['/api/agent/stop','/api/servo/stop']
        await scan.close()
    asyncio.run(run())
