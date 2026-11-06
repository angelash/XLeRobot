import cv2
import numpy as np
import pytest

from deployment.grasp.local_trials import candidates, SideExecutor, LocalController
from deployment.grasp.controller import GraspFailure


def test_offline_fixture_detection_rejects_empty_desk_and_jaw_corner():
    image = np.full((480, 640, 3), 235, np.uint8)
    assert not candidates(image, 'dark')
    cv2.rectangle(image, (5, 340), (90, 460), (35, 35, 35), -1)
    assert not candidates(image, 'dark')
    cv2.rectangle(image, (320, 230), (365, 295), (45, 45, 45), -1)
    result = candidates(image, 'dark')
    assert len(result) == 1
    assert result[0]['bbox'][0] == pytest.approx(319, abs=2)
    assert not candidates(image, 'light')
    pin = np.full((480,640,3),235,np.uint8)
    cv2.rectangle(pin,(330,210),(360,232),(35,35,35),-1)
    assert not candidates(pin,'dark')


def test_head_roi_rejects_background_equipment():
    image = np.full((480, 640, 3), 235, np.uint8)
    cv2.rectangle(image, (310, 20), (365, 85), (40, 40, 40), -1)
    assert not candidates(image, 'dark', head=True)
    cv2.rectangle(image, (310, 180), (345, 235), (40, 40, 40), -1)
    assert len(candidates(image, 'dark', head=True)) == 1


def test_right_reference_mapping_preserves_pressure_and_refuses_other_arm(monkeypatch):
    ex = SideExecutor('right')
    entries = []
    def step(targets, torque, speed, label):
        entries.append(targets)
        return {'joints': {'right:2': {'position': 2245, 'goal': 2245},
                           'right:6': {'position': 1500, 'goal': 1450, 'load': 100}}}
    monkeypatch.setattr(ex.inner, 'step', step)
    result = ex.step({'left:2': 2211, 'left:3': 955, 'left:4': 2245}, 450, 5)
    assert entries == [{'right:2': 2245, 'right:3': 901, 'right:4': 2049}]
    assert result['joints']['left:2']['position'] == 2211
    assert result['joints']['left:6']['position']-result['joints']['left:6']['goal'] == 50
    with pytest.raises(Exception, match='assigned arm'):
        ex.step({'right:2': 2245})
    with pytest.raises(Exception, match='chassis'):
        ex.base(-.05)


def test_visual_similarity_cannot_override_lost_grip_pressure(monkeypatch):
    from deployment.grasp.controller import GraspController
    monkeypatch.setattr(GraspController, '_verify_held', lambda *_: {'held': True, 'why': 'visual'})
    brain = LocalController(None, None)
    brain.phase = 'lift'
    brain.baseline = {'load': 100, 'lead': 30}
    brain.q = {'grip': 1501, 'grip_goal': 1500, 'grip_load': 4}
    result = brain._verify_held({}, {}, {})
    assert result['held'] is False and result['pressure_retained'] is False
    brain.q = {'grip': 1525, 'grip_goal': 1500, 'grip_load': 90}
    assert brain._verify_held({}, {}, {})['held'] is True


def test_release_must_be_confirmed_after_empty_retreat(monkeypatch):
    from deployment.grasp.controller import GraspController
    monkeypatch.setattr(GraspController, '_place', lambda *_: None)
    brain = LocalController(None, None)
    brain.q = {'grip': 1800}
    brain.events = [{'kind': 'release_check', 'left_behind': False}]
    with pytest.raises(GraspFailure, match='Release'):
        brain._place()
    brain.events = [{'kind': 'release_check', 'left_behind': True}]
    brain._place()


def test_pressure_correction_has_a_finite_budget_and_never_lifts_empty(monkeypatch):
    from deployment.grasp.controller import GraspController
    monkeypatch.setattr(GraspController, '_close', lambda *_: None)
    brain = LocalController(None, None)
    brain.q = {'grip': 1700, 'grip_goal': 1700, 'grip_load': 0}
    monkeypatch.setattr(brain, '_refresh', lambda: None)
    commanded = []
    def move(targets, **kwargs):
        commanded.append((targets, kwargs))
        brain.q.update(grip=targets['left:6'], grip_goal=targets['left:6'])
    monkeypatch.setattr(brain, '_move', move)
    with pytest.raises(GraspFailure, match='baseline insufficient'):
        brain._close({})
    assert len(commanded) == 7
    assert all(v[1]['torque'] == 600 and set(v[0]) == {'left:6'} for v in commanded)
    assert min(v[0]['left:6'] for v in commanded) >= 1450


def test_release_ambiguity_requires_whole_body_local_redetection(monkeypatch):
    from deployment.grasp.controller import GraspController
    from types import SimpleNamespace
    monkeypatch.setattr(GraspController, '_place', lambda *_: None)
    brain = LocalController(None, SimpleNamespace(locate=lambda: [280,230,50,70]))
    brain.q = {'grip': 2000, 'grip_load': 10}
    brain.events = [{'kind': 'release_check', 'left_behind': False}]
    monkeypatch.setattr(brain, '_home', lambda: None)
    monkeypatch.setattr(brain, '_refresh', lambda: None)
    brain._place()
    assert brain.events[-1]['kind'] == 'release_redetection'
    brain.obs.locate = lambda: [280,400,50,70]
    with pytest.raises(GraspFailure, match='away from jaws'):
        brain._place()


def test_lateral_goal_accumulates_inside_bounded_motor_deadband(monkeypatch):
    brain = LocalController(None, None)
    brain.q = {'pan': 2000}
    obs = {'ok': True, 'bbox': [340,240,40,60], 'center': [360,270]}
    targets=[]
    monkeypatch.setattr(brain,'_look',lambda *_: obs)
    monkeypatch.setattr(brain,'_move',lambda v,**_:targets.append(v['left:1']))
    brain._lateral_step(obs)
    brain._lateral_step(obs)
    assert abs(targets[1]-2000)>abs(targets[0]-2000)
    for _ in range(10):brain._lateral_step(obs)
    assert all(abs(t-2000)<=100 for t in targets)


def test_closed_jaws_only_open_from_a_clear_high_pose(monkeypatch):
    brain=LocalController(None,None)
    brain.q={'grip':1350}
    monkeypatch.setattr(brain,'_refresh',lambda:None)
    monkeypatch.setattr(brain,'_tip',lambda:{'tip':(18,-5)})
    calls=[]
    monkeypatch.setattr(brain,'_move',lambda *a,**k:calls.append(a))
    with pytest.raises(GraspFailure,match='clear high pose'):
        brain.prepare_open()
    assert not calls
    monkeypatch.setattr(brain,'_tip',lambda:{'tip':(18,8)})
    brain.prepare_open()
    assert calls==[({'left:6':2045},)]
