"""Manual posing on the fake STS bus: release by hand, lock where it now sits."""
import json
import pytest
from fabrication.assembly.debug_console import original_runtime as orig
from fabrication.assembly.debug_console.test_whole_calibration import runtime,set_word  # noqa: F401 (fixture)
from fabrication.assembly.debug_console.torque_control import word


def reg(r,port,i):return r.session.ports[port].regs[i]


def test_release_frees_only_the_selected_group(runtime):
    runtime.scan()
    result=runtime.manual_pose('release',{'COM3':[1,2,3,4,5,6]})
    assert result['count']==6 and runtime.manual_pose_active
    assert all(reg(runtime,'COM3',i)[40]==0 for i in range(1,7))
    assert reg(runtime,'COM3',7)[40]==1 and reg(runtime,'COM4',1)[40]==1,'Head and the other arm keep holding'


def test_hold_locks_in_place_and_widens_limits_around_a_hand_pushed_joint(runtime):
    runtime.scan()
    orig.KINEMATIC_PROFILE.write_text(json.dumps({'serials':orig.EXPECTED_SERIALS,'kind':'observed_ranges_only','joints':{
        'COM3:3':{'min':1800,'max':2300,'homing_raw':0,'range_verified':True,'mid':2047}}}),encoding='utf-8')
    runtime.load_kinematic_profile();runtime.scan()
    assert 'COM3:3' in runtime.saved_ranges()
    runtime.manual_pose('release',{'COM3':[2,3]})
    set_word(reg(runtime,'COM3',3),56,2400)   # pushed by hand past the stored max
    set_word(reg(runtime,'COM3',2),56,2100)   # moved, still inside its limits
    result=runtime.manual_pose('hold',{'COM3':[2,3]})
    r3,r2=reg(runtime,'COM3',3),reg(runtime,'COM3',2)
    assert (word(r3,9),word(r3,11))==(1800,2400+orig.POSE_MARGIN_RAW)
    assert r3[40]==1 and word(r3,42)==2400 and word(r3,56)==2400,'Locked where it sits, no snap back'
    assert (word(r2,9),word(r2,11))==(1800,2300) and word(r2,42)==2100 and r2[40]==1
    assert [w['key'] for w in result['widened']]==['COM3:3'] and not runtime.manual_pose_active
    entry=json.loads(orig.KINEMATIC_PROFILE.read_text(encoding='utf-8'))['joints']['COM3:3']
    assert entry['max']==2408 and entry['manual_widen'][0]['from']==[1800,2300]
    assert 'COM3:3' in runtime.saved_ranges(),'The saved range follows the widened limits'


def test_hold_all_includes_wheels_and_skips_nothing_on_a_healthy_robot(runtime):
    runtime.scan();runtime.manual_pose('release')
    assert all(r[40]==0 for p in runtime.session.ports.values() for r in p.regs.values())
    result=runtime.manual_pose('hold')
    assert result['count']==16 and not result['skipped']
    assert all(r[40]==1 for p in runtime.session.ports.values() for r in p.regs.values())


def test_takeover_and_self_test_are_respected(runtime):
    runtime.scan()
    runtime.controller['controller']='online'
    with pytest.raises(ValueError,match='接管'):runtime.manual_pose('release',{'COM3':[6]})
    runtime.controller['controller']='offline';runtime.limit_test.running=True
    with pytest.raises(ValueError,match='自检'):runtime.manual_pose('hold')
    runtime.limit_test.running=False
    with pytest.raises(ValueError,match='未知'):runtime.manual_pose('release',{'COM9':[1]})

