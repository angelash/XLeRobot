import copy
from datetime import datetime, timezone

import pytest

from deployment.grasp.recovery import EXPECTED, assess, hardware_readiness, hold_kind


NOW = 1793977687.
CAMERAS = {camera:{'fresh':True} for camera in (0,1,2)}


def healthy():
    return {'updated_at':datetime.fromtimestamp(NOW,timezone.utc).isoformat(),
            'controller':{'controller':'offline'},
            'ports':[{'port':port,'connected':True,'motors':[
                {'id':i,'model':777,'mode':1 if port=='COM4' and i in (9,10) else 0,
                 'position':1500,'speed':0,'goal_velocity':0,'voltage':12.,'temperature':35,
                 'status':0,'torque':1} for i in ids]} for port,ids in EXPECTED.items()]}


def test_zero_motor_scan_never_claims_torque_or_readiness():
    state=healthy();state.update(ports=[],error='ID 1 状态读取失败')
    result=assess(state,side='left',cameras=CAMERAS,now=NOW)
    assert result['next_step']=='restore_motor_communication'
    assert result['hardware']['online_confirmed']==0
    assert not result['hardware']['all_torque'] and not result['hardware']['ready']
    assert len(result['hardware']['missing'])==16
    assert not result['motion_enabled'] and not result['hold_cleared']


def test_count_of_sixteen_cannot_hide_missing_motor_or_duplicate_identity():
    state=healthy();state['ports'][1]['motors'][-1]=copy.deepcopy(state['ports'][1]['motors'][-2])
    result=hardware_readiness(state,NOW)
    assert not result['ready'] and 'COM4:10' in result['missing']
    assert 'duplicate_motor:COM4:9' in result['issues']


@pytest.mark.parametrize('change,issue',[
    ({'mode':1},'unexpected_mode'),({'temperature':0},'temperature_not_ready'),
    ({'temperature':float('nan')},'invalid_temperature'),({'voltage':10.4},'voltage_not_ready'),
    ({'status':32},'motor_status_error'),({'model':999},'unexpected_model')])
def test_hardware_faults_never_become_scene_recovery(change,issue):
    state=healthy();state['ports'][0]['motors'][0].update(change)
    result=assess(state,side='left',cameras=CAMERAS,now=NOW)
    assert result['next_step']=='inspect_hardware_fault'
    assert any(s.startswith(issue) for s in result['hardware']['issues'])


def test_stale_healthy_packet_cannot_authorize_recovery():
    assert not hardware_readiness(healthy(),NOW+3)['ready']
    assert not hardware_readiness({'ports':healthy()['ports']},NOW)['ready']


def test_prepare_current_pose_is_only_proposed_when_all_telemetry_is_known():
    state=healthy();state['ports'][0]['motors'][0]['torque']=0
    result=assess(state,side='left',cameras=CAMERAS,now=NOW)
    assert result['next_step']=='prepare_hold_at_current_position'
    assert result['hardware']['torque_off']==['COM3:1']
    state['ports'][1]['motors'].pop()
    assert assess(state,side='left',cameras=CAMERAS,now=NOW)['next_step']=='restore_motor_communication'


def test_moving_wheel_and_current_operator_block_recovery():
    state=healthy();state['ports'][1]['motors'][-1]['speed']=10
    assert not hardware_readiness(state,NOW)['ready']
    result=assess(healthy(),side='left',servo={'active':True},cameras=CAMERAS,now=NOW)
    assert result['next_step']=='wait_for_current_operator'


def test_scene_reset_is_autonomous_but_physical_hold_is_not_cleared():
    holds={'left':'objects adjacent and white knocked down. Await physical separation',
           'right':'shoulder timeout then contact; inspect'}
    before=copy.deepcopy(holds)
    left=assess(healthy(),side='left',holds=holds,cameras=CAMERAS,now=NOW)
    right=assess(healthy(),side='right',holds=holds,cameras=CAMERAS,now=NOW)
    assert left['next_step']=='recover_scene_autonomously'
    assert not left['manual_scene_reset_required']
    assert right['next_step']=='diagnose_arm_before_reuse'
    assert holds==before and not right['hold_cleared'] and not right['motion_enabled']
    assert not left['physical_recovery_verified']


def test_unknown_holds_and_missing_views_require_inspection():
    assert hold_kind('unclassified reason')=='physical'
    assert hold_kind({'kind':'scene_replan'})=='scene'
    assert hold_kind({'kind':'contact'})=='physical'
    assert assess(healthy(),side='left',cameras={0:{'fresh':True}},now=NOW)['next_step']=='restore_shared_camera_observation'


def test_live_assessment_contract_only_reads_controller_and_preserves_holds(tmp_path,monkeypatch):
    import json
    import sys
    from deployment.grasp import recovery
    holds={'left':{'kind':'scene_replan'},'right':{'kind':'contact'}}
    p=tmp_path/'ARM_HOLDS.json';p.write_text(json.dumps(holds),encoding='utf8')
    original=p.read_bytes();requests=[]
    class Response:
        headers={'X-Captured-At':str(NOW),'X-Frame-Sequence':'1'}
        def __enter__(self):return self
        def __exit__(self,*_):pass
        def read(self):return self.payload
    def urlopen(url,timeout):
        assert isinstance(url,str) and url.startswith('http://127.0.0.1:8765/api/')
        assert '/status' in url or '/snapshot' in url
        requests.append(url);r=Response()
        r.payload=(json.dumps(healthy()).encode() if url.endswith('/api/status') else
                   json.dumps({'active':False}).encode() if url.endswith('/servo/status') else b'jpeg')
        return r
    monkeypatch.setattr(recovery.urllib.request,'urlopen',urlopen)
    monkeypatch.setattr(recovery.time,'time',lambda:NOW)
    monkeypatch.setattr(sys,'argv',['recovery','--root',str(tmp_path)])
    recovery.main()
    report=json.loads(next((tmp_path/'recovery-assessments').glob('*/assessment.json')).read_text('utf8'))
    assert len(requests)==5 and report['motor_commands']==0
    assert report['arms']['left']['next_step']=='recover_scene_autonomously'
    assert report['arms']['right']['next_step']=='diagnose_arm_before_reuse'
    assert p.read_bytes()==original
