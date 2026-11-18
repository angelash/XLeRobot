"""Long-lived task intent retains the exact motion, source and stop boundaries."""
import asyncio
import json

import pytest

from .test_autonomy import LocalRig,until
from .autonomy import AutonomousSurvey


def test_round_rollover_keeps_same_reference_and_does_not_replay_station_survey(tmp_path):
    async def run():
        rig=LocalRig();job,scan,mission=rig.autonomous(tmp_path)
        started=await rig.begin(job,mission,continuous=True,max_duration_s=60)
        await until(lambda:job.state['phase']=='waiting_navigation')
        steps=rig.steps
        for n in range(2,5):
            rig.now[0]+=61
            await until(lambda:job.state['round']==n)
            assert job.state['id']==started['id'] and rig.steps==steps
            assert job.state['progress']['station_scans']==1 and scan.reserved_by==started['id']
        assert job.status()['continuous'] and job.status()['task_lifetime']=='until_stop_or_guard'
        assert job.status()['automatic_translation'] is False
        assert job.state['latest_observation']['motor_commands']==0
        await job.stop();await job.task
        assert not scan.reserved_by
    asyncio.run(run())


def test_source_recovery_after_round_expiry_uses_same_task_and_only_one_survey(tmp_path):
    async def run():
        rig=LocalRig();rig.fresh=False;job,scan,mission=rig.autonomous(tmp_path)
        started=await rig.begin(job,mission,continuous=True,max_duration_s=60)
        await until(lambda:job.state['phase']=='waiting_source')
        rig.now[0]=61;await until(lambda:job.state['round']==2)
        assert not rig.steps
        rig.fresh=True;await until(lambda:job.state['phase']=='waiting_navigation')
        assert job.state['id']==started['id'] and job.state['progress']['station_scans']==1
        await job.stop();await job.task
    asyncio.run(run())


def test_stop_during_round_parking_cannot_roll_over_or_start_another_action(tmp_path):
    async def run():
        rig=LocalRig();rig.fresh=False;job,scan,mission=rig.autonomous(tmp_path)
        await rig.begin(job,mission,continuous=True,max_duration_s=60)
        await until(lambda:job.state['phase']=='waiting_source')
        entered=asyncio.Event();release=asyncio.Event()
        async def parked():
            entered.set();await release.wait();return await rig.parked()
        scan.parked=parked;rig.now[0]=61;await entered.wait()
        await job.stop();release.set();await job.task
        assert job.state['round']==1 and job.state['cancelled'] and not rig.steps
    asyncio.run(run())


@pytest.mark.parametrize('cause',['operator_takeover','controller_restarted','actual_contact'])
def test_continuous_authorization_cannot_clear_fault_or_restart_hold(tmp_path,cause):
    async def run():
        rig=LocalRig();rig.fresh=False;job,scan,mission=rig.autonomous(tmp_path)
        await rig.begin(job,mission,continuous=True,max_duration_s=60)
        await until(lambda:job.state['phase']=='waiting_source');rig.hard=cause
        await job.task;rig.hard=None
        with pytest.raises(ValueError,match='停止锁'):await rig.begin(job,mission,continuous=True)
        resumed=AutonomousSurvey(job.root,mission,scan,rig.inputs)
        assert resumed.state['hard_hold']==cause and not resumed.task and not rig.steps
    asyncio.run(run())


def test_resource_wait_recovers_without_motion_or_counter_reset(tmp_path):
    async def run():
        rig=LocalRig();job,scan,mission=rig.autonomous(tmp_path)
        issue=['vehicle_battery_low']
        class Inputs:
            async def __call__(self):return await rig.inputs()
            def resources(self):return dict(issues=list(issue),free_bytes=1000000000)
        job.inputs=Inputs();before=mission.status()
        await rig.begin(job,mission,continuous=True,max_duration_s=60)
        await until(lambda:job.state['phase']=='waiting_resources')
        assert not rig.steps
        issue.clear();rig.now[0]=31
        await until(lambda:job.state['phase']=='waiting_navigation')
        assert mission.status()['translation_legs']==before['translation_legs'] and mission.status()['wheel_path_m']==before['wheel_path_m']
        await job.stop();await job.task
    asyncio.run(run())


def test_bounded_task_still_expires_and_restart_never_resumes_continuous_motion(tmp_path):
    rig=LocalRig();job,scan,mission=rig.autonomous(tmp_path);identifier='b'*32
    (job.root/identifier).mkdir()
    job.state.update(id=identifier,running=True,continuous=True,round=4,phase='waiting_navigation',child_id='original')
    job._save();resumed=AutonomousSurvey(job.root,mission,scan,rig.inputs)
    assert resumed.state['phase']=='interrupted' and resumed.state['round']==4
    assert resumed.state['hard_hold']=='restart_requires_review' and not resumed.task


def test_continuous_flag_must_be_explicit_boolean_and_cannot_enter_turn_experiment(tmp_path):
    async def run():
        rig=LocalRig();job,scan,mission=rig.autonomous(tmp_path)
        with pytest.raises(ValueError,match='true或false'):await rig.begin(job,mission,continuous='true')
        from .test_turn_calibration import TurnRig
        turn=TurnRig();cal,scanner,exploration=turn.calibration(tmp_path/'turn')
        review=await scanner.review()
        with pytest.raises(ValueError,match='有限试验'):
            await cal.start(dict(confirmed=True,authorization='user asks continuous exploration',
                mission_id=exploration.status()['session_id'],review_id=review['review_id'],continuous=True))
        assert cal.task is None and scanner.reserved_by is None
    asyncio.run(run())
