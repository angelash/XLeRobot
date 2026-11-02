"""No real hardware: verify rotation expiry, abort and partial-write failures."""
import threading
import time
import pytest
from fabrication.assembly.debug_console.test_whole_calibration import runtime, set_word  # noqa: F401
from fabrication.assembly.debug_console.rotation_scan import bounded_rotate
from fabrication.assembly.debug_console.torque_control import word


def wheels(runtime):
    port=runtime.session.ports['COM4'];original=port.write
    def write(packet):
        result=original(packet)
        if packet[4]==3 and packet[5]==46 and word(port.regs[packet[2]],46):
            set_word(port.regs[packet[2]],56,word(port.regs[packet[2]],56)+12)
        return result
    port.write=write
    return port


def test_rotation_has_no_translation_and_verified_local_stop(runtime):
    p=wheels(runtime)
    # A disconnected gamepad is explicitly allowed by this user's survey request.
    runtime.gamepad={'connected':False}
    result=bounded_rotate(runtime,lambda:None,duration=.04)
    assert result['translation_command_m_s']==0
    assert result['wheel_delta_raw']=={9:12,10:12}
    assert all(word(p.regs[i],46)==0 for i in (9,10))
    assert not runtime.wheels_commanded


@pytest.mark.parametrize('kwargs',[{'duration':float('nan')},{'duration':float('inf')},{'duration':.66},{'max_raw':161},{'direction':True}])
def test_invalid_pulse_cannot_touch_hardware(runtime,kwargs):
    p=wheels(runtime);p.writes.clear()
    with pytest.raises(ValueError):bounded_rotate(runtime,lambda:None,**kwargs)
    assert p.writes==[]


def test_independent_timer_stops_when_action_loop_is_delayed(runtime):
    p=wheels(runtime);checks=0;observed=threading.Event()
    def check():
        nonlocal checks
        checks+=1
        if not runtime.io._is_owned() and any(word(p.regs[i],46) for i in (9,10)) and not observed.is_set():
            time.sleep(.2)
            assert all(word(p.regs[i],46)==0 for i in (9,10))
            observed.set()
    bounded_rotate(runtime,check,duration=.06)
    assert observed.is_set()


def test_abort_stops_both_wheels_before_propagating(runtime):
    p=wheels(runtime);checks=0
    def check():
        nonlocal checks
        checks+=1
        if checks>=4:raise RuntimeError('operator input')
    with pytest.raises(RuntimeError,match='operator input'):bounded_rotate(runtime,check)
    assert all(word(p.regs[i],46)==0 for i in (9,10))


def test_zero_goal_is_not_enough_wait_for_measured_deceleration(runtime):
    p=wheels(runtime);original=p.write;counts={9:0,10:0}
    def write(packet):
        i=packet[2]
        if i in counts and packet[4]==2 and word(p.regs[i],46)==0 and p.regs[i][40]==0:
            counts[i]+=1
            set_word(p.regs[i],58,30 if 2<=counts[i]<4 else 0)
        return original(packet)
    p.write=write
    result=bounded_rotate(runtime,lambda:None,duration=.04)
    assert len(result['stop_trace'])>=3
    assert all(q['speed']==0 for q in result['stop_readback'].values())


def test_quantized_idle_speed_requires_torque_off_and_stable_encoder(runtime):
    p=wheels(runtime);original=p.write
    def write(packet):
        i=packet[2]
        if i in (9,10) and packet[4]==2 and p.regs[i][40]==0:
            set_word(p.regs[i],58,50)
        return original(packet)
    p.write=write
    result=bounded_rotate(runtime,lambda:None,duration=.04)
    assert all(q['torque']==0 for q in result['stop_readback'].values())
    assert result['stop_trace'][-1]['after_zero_ms']>=200


def test_slow_wheel_drift_is_not_mistaken_for_idle_speed_quantization(runtime):
    p=wheels(runtime);original=p.write
    def write(packet):
        i=packet[2]
        if i in (9,10) and packet[4]==2 and p.regs[i][40]==0:
            set_word(p.regs[i],58,50)
            set_word(p.regs[i],56,word(p.regs[i],56)+2)
        return original(packet)
    p.write=write
    with pytest.raises(ValueError,match='编码器尚未稳定'):
        bounded_rotate(runtime,lambda:None,duration=.04)
    assert all(word(p.regs[i],46)==0 and p.regs[i][40]==0 for i in (9,10))


def test_one_nonzero_write_fails_but_both_stop_commands_are_attempted(runtime):
    p=wheels(runtime);original=p.write
    def write(packet):
        if packet[2]==10 and packet[4]==3 and packet[5]==46 and any(packet[6:8]):
            raise OSError('lost ACK')
        return original(packet)
    p.write=write
    with pytest.raises(OSError,match='lost ACK'):bounded_rotate(runtime,lambda:None,duration=.04)
    assert all(word(p.regs[i],46)==0 for i in (9,10))


def test_scan_failure_cannot_continue_to_next_step(runtime):
    agent=runtime.agent;runtime.limit_test.require_pad=False
    agent.start([{'op':'rotate'},{'op':'wait','seconds':0}],True,background=False)
    assert agent.error and len(agent.results)==1 and agent.results[0]['status']=='failed'
    with pytest.raises(ValueError):agent.start([{'op':'rotate','v':1}],True,background=False)
