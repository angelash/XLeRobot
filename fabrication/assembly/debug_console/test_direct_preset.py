"""Direct preset control over saved ranges, isolated faults and chassis settings."""
from copy import deepcopy
import json
import re
import time
from pathlib import Path
import pytest
from fabrication.assembly.debug_console import whole_calibration as cal
from fabrication.assembly.debug_console.control_settings import ControllerSettings
from fabrication.assembly.debug_console.test_original_teleop import pad,step
from fabrication.assembly.debug_console.test_whole_calibration import runtime,set_word  # noqa: F401 (fixture)
from fabrication.assembly.debug_console.torque_control import word


def settle(c):
    stamp=time.monotonic()
    for s in c.samples.values():
        s.at=stamp;s.recent.clear()
        for delta in (-.15,-.1,-.05):s.recent.append((stamp+delta,s.current))


def widen(c,keys,below=600,above=500):
    for key in keys:
        s=c.samples[key];s.low=s.current-below;s.high=s.current+above


def limits(r,key):
    port,i=key.split(':');reg=r.session.ports[port].regs[int(i)]
    return word(reg,9),word(reg,11),word(reg,31)


def test_saved_ranges_open_preset_without_pose_cards_and_carry_into_next_session(runtime):
    c=runtime.manual_calibration;c.start(True)
    keys=['COM3:2','COM3:6','COM4:3'];widen(c,keys)
    card={k:s.current for k,s in c.samples.items()}
    c.captures['left_open']={'port':'COM3','positions':card,'captured_at':'t'}
    settle(c);c.apply_ranges(True)
    assert not c.active and c.applied and c.applied_kind=='ranges'
    data=json.loads(c.profile_path.read_text(encoding='utf-8'))
    assert data['kind']=='observed_ranges_only' and set(data['joints'])==set(keys)
    assert not any(j['verified'] for j in data['joints'].values()),'Ranges are not pose references'
    low,high,_=limits(runtime,'COM3:2');assert high-low>=1000
    assert all(reg[40]==0 for p in runtime.session.ports.values() for reg in p.regs.values())
    runtime.pad_time=time.monotonic();runtime.begin_preset(True)
    e=runtime.envelopes[('COM3',2)];assert (e['min'],e['max'])==(low,high)
    runtime.stop()
    c.start(True)
    assert set(c.seeded)==set(keys) and c.samples['COM3:2'].high-c.samples['COM3:2'].low>=1000
    shift=2047-data['joints']['COM3:6']['mid']
    assert c.captures['left_open']['positions']['COM3:6']==card['COM3:6']+shift


def test_range_save_failure_rolls_back_and_keeps_collection(runtime,monkeypatch):
    c=runtime.manual_calibration;c.start(True);widen(c,['COM3:2','COM3:3'])
    c.captures['left_a']={'port':'COM3','positions':{k:s.current for k,s in c.samples.items()},'captured_at':'t'}
    settle(c);before={k:limits(runtime,k) for k in ('COM3:2','COM3:3')}
    original=cal.configure_measured_range;calls=[]
    def fail_second(port,i,*args):
        calls.append(i)
        if len(calls)==2:raise RuntimeError('lost ack')
        return original(port,i,*args)
    monkeypatch.setattr(cal,'configure_measured_range',fail_second)
    with pytest.raises(RuntimeError,match='已逐颗回滚'):c.apply_ranges(True)
    assert c.active and not c.error and 'left_a' in c.captures and not c.applied
    assert {k:limits(runtime,k) for k in before}==before


def test_preset_skips_unusable_joint_and_isolates_runtime_fault(runtime):
    reg=runtime.session.ports['COM3'].regs[5];set_word(reg,56,word(reg,11)+200)
    runtime.pad_time=time.monotonic();runtime.begin_preset(True)
    assert 'COM3:5' in runtime.preset_blocked and runtime.controller['controller']=='online'
    goal=word(reg,42)
    step(runtime,pad([1,0,0,0,0,0],buttons=(4,)))
    assert word(reg,42)==goal and '腕旋转' in runtime.notice['message']
    runtime.session.ports['COM3'].regs[2][40]=0
    step(runtime,pad([1,0,0,0,0,0]))
    assert 'COM3:2' in runtime.joint_faults and not runtime.controller['paused']
    step(runtime,pad());step(runtime,pad([0,-1,0,0,0,0]))
    assert ('COM3',3) in runtime.moving,'Other joints stay controllable'


def test_preset_base_uses_forward_and_turn_settings_without_verification(runtime):
    runtime.pad_time=time.monotonic();runtime.begin_preset(True)
    p=runtime.session.ports['COM4'];v=lambda i:word(p.regs[i],46)
    step(runtime,pad(hat=(0,1)))
    assert v(9)&0x8000 and 0<v(10)<0x8000,'D-pad up drives the defined front'
    step(runtime,pad());assert v(9)==v(10)==0
    runtime.stop();runtime.update_controller_settings({'base_turn':'reverse'})
    runtime.pad_time=time.monotonic();runtime.begin_preset(True)
    step(runtime,pad(hat=(-1,0)))
    assert v(9)&0x8000 and v(10)&0x8000
    step(runtime,pad(hat=(0,1)))
    assert v(9)&0x8000 and 0<v(10)<0x8000,'Turn reversal leaves forward unchanged'


def test_preset_gripper_moves_at_configured_raw_rate(runtime):
    runtime.pad_time=time.monotonic();runtime.begin_preset(True)
    start=word(runtime.session.ports['COM3'].regs[6],56)
    step(runtime,pad([0,0,0,0,1,0]))
    target=runtime.direct_targets[('COM3',6)]
    assert 0<abs(target-start)<=120*.1+1


def test_legacy_wheel_swap_migrates_to_equivalent_direction_settings(tmp_path):
    path=tmp_path/'settings.json';path.write_text(json.dumps({'wheel_mapping':'swapped'}),encoding='utf-8')
    s=ControllerSettings(path)
    assert (s.data['wheel_mapping'],s.data['base_forward'],s.data['base_turn'])==('normal','reverse','normal')
    assert s.base_signs()==(-1,1)


def test_previous_day_collection_is_recovered_after_midnight_restart(runtime,tmp_path):
    day1=tmp_path/'2026-10-29'/'whole-calibration'/'session.json'
    day2=tmp_path/'2026-10-30'/'whole-calibration'/'session.json'
    c=cal.WholeCalibration(runtime,day1,runtime.manual_calibration.profile_path);runtime.manual_calibration=c
    c.start(True);widen(c,['COM3:2']);c.persist()
    fresh=cal.WholeCalibration(runtime,day2,c.profile_path);runtime.manual_calibration=fresh
    assert fresh.find_recoverable()['id']==c.id,'Dated evidence folders must not hide yesterday\'s collection'
    resumed=fresh.resume_latest()
    assert resumed['active'] and fresh.path.parent==day1.parent
    assert fresh.samples['COM3:2'].high-fresh.samples['COM3:2'].low>=1000


def test_gateway_allows_every_route_the_console_calls():
    from fabrication.assembly.debug_console import platform_app
    static=Path(platform_app.__file__).resolve().parent/'static'
    text=''.join((static/name).read_text(encoding='utf-8') for name in ('original.js','whole-calibration.js','original.html'))
    # Literal routes end at a quote or query string; a dynamic prefix ending
    # in "/" is covered by the calibrationCommand action names below.
    routes={m for m in re.findall(r"['\"`]/api/([a-z][a-z/-]*[a-z])(?=['\"`?])",text)}
    routes|={'whole-calibration/'+m for m in re.findall(r"calibrationCommand\('([a-z-]+)'",text)}
    allowed=platform_app.PROXY_GET|platform_app.PROXY_POST
    assert routes and not routes-allowed,sorted(routes-allowed)

