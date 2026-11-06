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


def test_known_black_body_rejects_low_contrast_arm_patches():
    im=np.full((480,640,3),185,np.uint8)
    cv2.rectangle(im,(215,210),(265,260),(125,125,125),-1)
    assert not candidates(im,'dark')
    cv2.rectangle(im,(335,290),(370,355),(65,65,65),-1)
    hits=candidates(im,'dark')
    assert len(hits)==1 and hits[0]['bbox'][0]>320


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


def test_named_smooth_black_fixture_cannot_lift_on_marginal_preload(monkeypatch):
    from types import SimpleNamespace
    from deployment.grasp.controller import GraspController
    monkeypatch.setattr(GraspController,'_close',lambda *_:None)
    brain=LocalController(None,SimpleNamespace(appearance='dark'))
    brain.q={'grip':1450,'grip_goal':1433,'grip_load':80}
    monkeypatch.setattr(brain,'_refresh',lambda:None)
    monkeypatch.setattr(brain,'_settled_pressure',brain.pressure)
    monkeypatch.setattr(brain,'_move',lambda *a,**kw:None)
    with pytest.raises(GraspFailure,match='baseline insufficient'):
        brain._close({})
    brain.q.update(grip=1500,grip_goal=1455,grip_load=200)
    brain._close({})
    assert brain.baseline=={'load':200.,'lead':45}


def test_pressure_correction_accumulates_goal_without_unbounded_jaw_lead(monkeypatch):
    from types import SimpleNamespace
    from deployment.grasp.controller import GraspController
    monkeypatch.setattr(GraspController,'_close',lambda *_:None)
    brain=LocalController(None,SimpleNamespace(appearance='dark'))
    brain.q={'grip':1550,'grip_goal':1530,'grip_load':80}
    monkeypatch.setattr(brain,'_refresh',lambda:None)
    monkeypatch.setattr(brain,'_settled_pressure',brain.pressure)
    targets=[]
    def move(value,**kwargs):
        target=value['left:6'];targets.append(target)
        assert brain.q['grip']-target<=60 and target>=1450
        brain.q.update(grip_goal=target,grip_load=200 if len(targets)>1 else 100)
    monkeypatch.setattr(brain,'_move',move)
    brain._close({})
    assert targets==[1500,1490]


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
    monkeypatch.setattr(brain, '_settled_pressure', brain.pressure)
    commanded = []
    def move(targets, **kwargs):
        commanded.append((targets, kwargs))
        brain.q.update(grip=targets['left:6'], grip_goal=targets['left:6'])
    monkeypatch.setattr(brain, '_move', move)
    with pytest.raises(GraspFailure, match='baseline insufficient'):
        brain._close({})
    assert len(commanded) <= 10 and commanded[-1][0]['left:6'] == 1450
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


def test_light_body_keeps_its_full_height_when_only_lower_rows_are_dark():
    im = np.full((480, 640, 3), 220, np.uint8)
    cv2.rectangle(im, (350, 240), (408, 328), (200, 200, 200), -1)
    cv2.rectangle(im, (352, 286), (406, 327), (175, 175, 175), -1)
    cv2.rectangle(im, (368, 260), (375, 270), (45, 45, 45), -1)
    found = candidates(im, 'light')
    assert len(found) == 1
    assert found[0]['bbox'][1] <= 241
    assert found[0]['bbox'][3] >= 86


def test_observation_rejects_old_frames_and_propagates_camera_failure():
    import time
    from deployment.grasp.local_trials import Observer
    obs = Observer(None, 'left', 'light')
    obs.samples.append({'captured_at': time.time()-5, 'ok': True})
    with pytest.raises(GraspFailure, match='post-settle'):
        obs.observe(time.monotonic(), timeout=.04)
    obs.failure = 'camera disconnected'
    with pytest.raises(GraspFailure, match='camera disconnected'):
        obs.observe(time.monotonic(), timeout=.04)


def test_acquisition_uses_complete_consistent_body_across_fragment_flicker(monkeypatch):
    import time
    from types import SimpleNamespace
    from deployment.grasp import local_trials as local
    im=np.full((480,640,3),200,np.uint8)
    full=[300,140,40,100]; fragment=[307,200,25,40]
    sequence=iter([full,full,fragment,fragment,full,fragment,fragment,full,fragment,fragment,fragment,fragment])
    monkeypatch.setattr(local.time,'sleep',lambda _:None)
    monkeypatch.setattr(local,'candidates',lambda *_:[{'bbox':next(sequence)}])
    obs=local.Observer(SimpleNamespace(get=lambda _:(im,b'jpeg',time.time())),'left','light')
    captured=[]
    monkeypatch.setattr(obs.tracker,'init',lambda gray,box:captured.append(box))
    monkeypatch.setattr(obs.flow,'init',lambda *_:None)
    monkeypatch.setattr(local.threading,'Thread',lambda **_:SimpleNamespace(start=lambda:None))
    assert obs.locate()==full and captured==[full]


def test_three_partial_sides_cannot_replace_only_two_complete_detections(monkeypatch):
    import time
    from types import SimpleNamespace
    from deployment.grasp import local_trials as local
    im=np.full((480,640,3),200,np.uint8)
    sequence=iter([[300,140,40,100]]*2+[[307,200,25,40]]*10)
    monkeypatch.setattr(local.time,'sleep',lambda _:None)
    monkeypatch.setattr(local,'candidates',lambda *_:[{'bbox':next(sequence)}])
    obs=local.Observer(SimpleNamespace(get=lambda _:(im,b'jpeg',time.time())),'left','light')
    with pytest.raises(GraspFailure,match='ambiguous'):
        obs.locate()


def test_continuous_worker_rejects_high_score_shrunken_body(tmp_path):
    import time
    from types import SimpleNamespace
    from deployment.grasp.local_trials import Observer
    im = np.full((480,640,3),200,np.uint8)
    frames=SimpleNamespace(root=tmp_path, get=lambda _: (im,b'jpeg',time.time()))
    obs=Observer(frames,'left','light')
    def update(_):
        obs.stop.set()
        return {'ok':True,'score':.95,'scale':.3,'bbox':[10,20,15,25]}
    obs.tracker.update=update
    obs._track()
    assert obs.samples[-1]['ok'] is False
    assert 'full-body' in obs.samples[-1]['why']


def test_near_fixture_cannot_start_blind_descent_or_move_chassis(monkeypatch):
    brain=LocalController(None,None)
    obs={'ok':True,'bbox':[360,325,60,70]}
    monkeypatch.setattr(brain,'_tip',lambda:{'tip':(12.5,8),'t3':130})
    calls=[]
    monkeypatch.setattr(brain,'_go',lambda *a,**k:calls.append(a))
    with pytest.raises(GraspFailure,match='clearance'):
        brain._approach(obs)
    assert not calls


def test_reacquisition_never_moves_at_unknown_low_clearance(monkeypatch):
    from types import SimpleNamespace
    def absent():
        raise GraspFailure('Local target ambiguous or absent')
    ex=SimpleNamespace(active=True)
    brain=LocalController(ex,SimpleNamespace(locate=absent))
    monkeypatch.setattr(brain,'_refresh',lambda:None)
    monkeypatch.setattr(brain,'_tip',lambda:{'tip':(18,-4),'t3':130})
    moves=[]
    monkeypatch.setattr(brain,'_go',lambda *a,**k:moves.append(a))
    with pytest.raises(GraspFailure,match='high clearance'):
        brain.acquire()
    assert not moves


def test_reacquisition_stops_when_rear_and_view_budgets_are_exhausted(monkeypatch):
    from types import SimpleNamespace
    def absent():
        raise GraspFailure('Local target ambiguous or absent')
    brain=LocalController(SimpleNamespace(active=True),SimpleNamespace(locate=absent))
    monkeypatch.setattr(brain,'_refresh',lambda:None)
    monkeypatch.setattr(brain,'_tip',lambda:{'tip':(12,8),'t3':150})
    moves=[]
    monkeypatch.setattr(brain,'_go',lambda *a,**k:moves.append(a))
    monkeypatch.setattr(brain,'_key',lambda *_:None)
    with pytest.raises(GraspFailure,match='exhausted'):
        brain.acquire()
    assert len(moves)==4


def test_camera_failure_never_triggers_a_search_move(monkeypatch):
    from types import SimpleNamespace
    def broken():raise GraspFailure('Stale source frame')
    brain=LocalController(SimpleNamespace(active=False),SimpleNamespace(locate=broken))
    with pytest.raises(GraspFailure,match='Stale'):
        brain.acquire()


def test_near_target_search_retreats_once_at_high_clearance_then_stops(monkeypatch):
    from types import SimpleNamespace
    from deployment.grasp import arm
    def absent():raise GraspFailure('Local target ambiguous or absent')
    brain=LocalController(SimpleNamespace(active=True),SimpleNamespace(locate=absent,appearance='dark'))
    monkeypatch.setattr(brain,'_refresh',lambda:None)
    monkeypatch.setattr(brain,'_tip',lambda:{'tip':(18,8),'t3':145})
    moves=[]
    monkeypatch.setattr(brain,'_go',lambda target,*a,**kw:moves.append(target))
    monkeypatch.setattr(brain,'_key',lambda *_:None)
    with pytest.raises(GraspFailure,match='exhausted'):
        brain.acquire()
    assert len(moves)==5
    assert arm.fk(*moves[-1])['tip']==pytest.approx((16,10),abs=.1)


def test_empty_arm_return_requires_open_low_load_gripper(monkeypatch):
    brain=LocalController(None,None)
    monkeypatch.setattr(brain,'_refresh',lambda:None)
    calls=[]
    monkeypatch.setattr(brain,'_go',lambda *a,**k:calls.append(k))
    monkeypatch.setattr(brain,'_key',lambda *_:None)
    for grip,load in ((1600,0),(2000,80)):
        brain.q={'grip':grip,'grip_load':load}
        with pytest.raises(GraspFailure,match='open unloaded'):
            brain._home()
    assert not calls
    brain.q={'grip':2000,'grip_load':0}
    brain._home()
    assert calls==[{'speed':8,'torque':600}]


def test_feature_continuity_accepts_rotation_but_rejects_vanished_fixture():
    from deployment.grasp.flow import FeatureContinuity
    rng=np.random.default_rng(42)
    im=np.full((480,640),220,np.uint8)
    im[220:360,330:450]=rng.integers(30,150,(140,120),dtype=np.uint8)
    flow=FeatureContinuity()
    flow.init(im,[330,220,120,140])
    transform=cv2.getRotationMatrix2D((390,290),3,1.02)
    transform[:,2]+=(2,-1)
    rotated=cv2.warpAffine(im,transform,(640,480),borderValue=220)
    result=flow.update(rotated)
    assert result['ok'] and result['inliers']>=8
    assert result['center']==pytest.approx([392,289],abs=1)
    assert not flow.update(np.full_like(im,220))['ok']


def test_closing_anchor_cannot_revive_failed_or_partial_target():
    from deployment.grasp.local_trials import Observer
    observer=Observer(None,'left','light')
    gray=np.random.default_rng(1).integers(0,255,(480,640),dtype=np.uint8)
    for box,ok,score in [([300,250,80,90],False,.95),([300,250,80,90],True,.6),
                          ([300,410,80,90],True,.95)]:
        with pytest.raises(GraspFailure):
            observer.reanchor({'gray':gray,'bbox':box,'ok':ok,'score':score})
    observer.reanchor({'gray':gray,'bbox':[300,250,80,90],'ok':True,'score':.8})
    assert observer.tracker.state()['bbox']==pytest.approx([300,250,80,90],abs=2)


def test_closing_drift_does_not_replace_original_verification_window(monkeypatch):
    from deployment.grasp.controller import GraspController
    brain=LocalController(None,None)
    brain.q={'grip':1500,'grip_goal':1480,'grip_load':80}
    def shrink(*_):brain.grip_box=[400,430,30,40]
    monkeypatch.setattr(GraspController,'_close',shrink)
    monkeypatch.setattr(brain,'_refresh',lambda:None)
    monkeypatch.setattr(brain,'_settled_pressure',brain.pressure)
    brain._close({'bbox':[320,250,120,150]})
    assert brain.grip_box==[320,250,120,150]


def test_pressure_baseline_waits_for_motion_to_settle_and_uses_median(monkeypatch):
    now=[0.]
    def sleep(seconds):now[0]+=seconds
    brain=LocalController(None,None,clock=lambda:now[0],sleep=sleep)
    values=iter([(1660,110),(1650,90),(1642,66),(1641,65),(1642,67),(1641,66),(1642,65)])
    def refresh():
        position,load=next(values)
        brain.q.update(grip=position,grip_goal=1625,grip_load=load)
    monkeypatch.setattr(brain,'_refresh',refresh)
    assert brain._settled_pressure()=={'load':66.,'lead':17.}
    assert now[0] >= .89
    assert brain.events[-1]['kind']=='settled_pressure'
    assert len(brain.events[-1]['samples'])==5


def test_unstable_pressure_fails_within_bound_without_commanding_more_force(monkeypatch):
    now=[0.]
    def sleep(seconds):now[0]+=seconds
    brain=LocalController(None,None,clock=lambda:now[0],sleep=sleep)
    def refresh():
        load=40 if round(now[0]/.15)%2 else 110
        brain.q.update(grip=1640,grip_goal=1620,grip_load=load)
    monkeypatch.setattr(brain,'_refresh',refresh)
    with pytest.raises(GraspFailure,match='stable pressure'):
        brain._settled_pressure()
    assert 2.5<=now[0]<2.7 and brain.events[-1]['kind']=='unstable_pressure'


def test_white_fixture_next_to_a_desk_seam_and_shadow_is_still_distinct():
    image=np.full((480,640,3),185,np.uint8)
    cv2.line(image,(300,0),(300,479),(110,110,110),3)
    cv2.rectangle(image,(317,300),(352,355),(145,145,145),-1)
    cv2.rectangle(image,(317,296),(352,312),(175,175,175),-1)
    found=candidates(image,'light')
    assert len(found)==1
    x,y,w,h=found[0]['bbox']
    assert 310<=x<=320 and 290<=y<=318 and w>=30 and h>=35


@pytest.mark.parametrize('shape',[(1,1,4),(1,4)])
def test_hough_segment_layout_works_on_both_opencv_versions(monkeypatch,shape):
    monkeypatch.setattr(cv2,'HoughLinesP',lambda *a,**k:np.array([300,0,300,479],np.int32).reshape(shape))
    image=np.full((480,640,3),185,np.uint8)
    assert candidates(image,'light')==[]
