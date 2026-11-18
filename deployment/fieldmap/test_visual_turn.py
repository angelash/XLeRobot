from copy import deepcopy
import cv2
import numpy as np
import pytest
from .visual_turn import fit_turn_gain,rgb_turn


def test_image_estimator_recovers_known_perspective_rotation():
    rng=np.random.default_rng(19)
    first=cv2.GaussianBlur(rng.integers(0,256,(480,640),dtype=np.uint8),(3,3),.6)
    k=dict(fx=500.,fy=500.,tx=320.,ty=240.)
    K=np.array([[500.,0,320.],[0,500.,240.],[0,0,1.]])
    actual=cv2.Rodrigues(np.radians(np.array([1.,8.,2.])))[0]
    last=cv2.warpPerspective(first,K@actual@np.linalg.inv(K),(640,480))
    result=rgb_turn(first,last,k)
    assert result['accepted'] and result['inliers']>=30
    delta=np.asarray(result['rotation_matrix'])@actual.T
    error=np.degrees(np.arccos(np.clip((np.trace(delta)-1)/2,-1,1)))
    assert error<.2


def test_textureless_images_cannot_supply_a_rotation():
    blank=np.zeros((240,320),np.uint8)
    assert not rgb_turn(blank,blank,dict(fx=217.,fy=217.,tx=160.,ty=120.))['accepted']


def trials():
    rows=[]
    for i,d in enumerate((12,-12,26,-26,42,-42,20,-20)):
        angle=d*.47+(.1 if i%2 else -.1)
        R=cv2.Rodrigues(np.array([.1,.95,.25])/np.linalg.norm([.1,.95,.25])*np.radians(angle))[0]
        rows.append(dict(id=str(i),reference={'controller_pid':10,'phone_session':'same','intrinsics':'fixed'},
                         native_completed=True,parked_verified=True,design_angle_deg=d,
                         visual={'accepted':True,'rotation_matrix':R.tolist()}))
    return rows[:6],rows[6:]


def test_two_direction_model_must_predict_unseen_trials_without_actuator_permission():
    training,validation=trials();r=fit_turn_gain(training,validation)
    assert r['accepted'] and r['gain']==pytest.approx(.47,abs=.01)
    assert r['maximum_validation_error_deg']<.3 and not r['actuator_permission']
    assert not r['geometry_parameters_written'] and r['systematic_error_unknown']


@pytest.mark.parametrize('change,reason',[
    ('validation_error','held_out'),('axis','axis_inconsistent'),('reference','reference_changed'),
    ('unknown','uncertain_physical'),('duplicate','duplicate'),('weak','visual_evidence'),
])
def test_all_trials_are_preserved_and_bad_validation_cannot_be_ignored(change,reason):
    training,validation=trials();before=deepcopy(training)
    if change=='validation_error':validation[-1]['visual']['rotation_matrix']=cv2.Rodrigues(np.array([.1,.95,.25])/np.linalg.norm([.1,.95,.25])*np.radians(-22))[0].tolist()
    if change=='axis':validation[-1]['visual']['rotation_matrix']=cv2.Rodrigues(np.array([np.radians(-9),0.,0.]))[0].tolist()
    if change=='reference':validation[-1]['reference']['phone_session']='new'
    if change=='unknown':validation[-1]['native_completed']=False
    if change=='duplicate':validation[-1]['id']=training[0]['id']
    if change=='weak':validation[-1]['visual']['accepted']=False
    r=fit_turn_gain(training,validation)
    assert not r['accepted'] and reason in r['reason'] and training==before


def test_one_direction_and_reused_trials_cannot_validate_a_calibration():
    training,validation=trials()
    assert not fit_turn_gain(training,training[:2])['accepted']
    assert not fit_turn_gain(training[:3],validation)['accepted']
    for row in training:
        if row['design_angle_deg']<0:row['design_angle_deg']*=-1
    assert 'both_directions' in fit_turn_gain(training,validation)['reason']
