"""Time-based wheel dynamics, rather than instantaneous write=motion mocks."""
import math
import time
import pytest
from fabrication.assembly.debug_console.test_whole_calibration import runtime, set_word  # noqa: F401
from fabrication.assembly.debug_console.rotation_scan import bounded_rotate
from fabrication.assembly.debug_console.torque_control import word
from fabrication.assembly.debug_console.upstream_teleop import encode_velocity


def dynamics(runtime, *, stalled=False, reversed_wheel=False):
    port=runtime.session.ports['COM4'];original=port.write
    positions={9:4090.,10:4090.};last=time.perf_counter()
    for motor in positions:set_word(port.regs[motor],56,4090)
    def write(packet):
        nonlocal last
        now=time.perf_counter();dt=now-last;last=now
        for motor in positions:
            reg=port.regs[motor];encoded=word(reg,46)
            velocity=(encoded&0x7fff)*(-1 if encoded&0x8000 else 1) if reg[40] else 0
            velocity*=1.6 if motor==9 else 0 if stalled else -1 if reversed_wheel else 1
            positions[motor]+=velocity*dt
            set_word(reg,56,round(positions[motor])%4096)
            set_word(reg,58,encode_velocity(velocity))
        return original(packet)
    port.write=write
    return port


@pytest.mark.parametrize('direction',[-1,1])
def test_balance_corrects_unequal_response_without_boosting_or_extending(runtime,direction):
    port=dynamics(runtime)
    result=bounded_rotate(runtime,lambda:None,direction=direction,duration=.65)
    delta=result['wheel_delta_raw']
    assert all(50<delta[motor]*direction<=130 for motor in (9,10))
    assert abs(delta[9]-delta[10])<25
    adjustments=[row for row in result['control_trace'] if row['phase']=='balance_ack']
    assert adjustments and any(row['motor']==9 and 0<(row['value']&0x7fff)<160 for row in adjustments)
    assert all(0<=(row['value']&0x7fff)<=160 for row in adjustments)
    assert len(result['powered_trace'])>=3
    assert runtime.bounded_wheel_deadline is None
    assert all(word(port.regs[motor],46)==0 and port.regs[motor][40]==0 for motor in (9,10))


@pytest.mark.parametrize('kwargs',[{'stalled':True},{'reversed_wheel':True}])
def test_balance_cannot_hide_a_stall_or_wrong_direction(runtime,kwargs):
    port=dynamics(runtime,**kwargs)
    with pytest.raises(RuntimeError,match='旋转中轮位') as caught:
        bounded_rotate(runtime,lambda:None,duration=.65)
    assert caught.value.rotation['travel_trace']
    assert all(word(port.regs[motor],46)==0 and port.regs[motor][40]==0 for motor in (9,10))
    assert runtime.bounded_wheel_deadline is None


@pytest.mark.parametrize('direction',[-1,1])
def test_late_breakaway_is_braked_before_it_builds_a_large_position_lead(runtime,direction):
    port=runtime.session.ports['COM4'];original=port.write
    positions={9:4090.,10:4090.};velocities={9:0.,10:0.}
    last=time.perf_counter();drive_start=None
    for motor in positions:set_word(port.regs[motor],56,4090)
    def write(packet):
        nonlocal last,drive_start
        now=time.perf_counter();dt=now-last;last=now
        for motor in positions:
            reg=port.regs[motor];encoded=word(reg,46)
            desired=(encoded&0x7fff)*(-1 if encoded&0x8000 else 1) if reg[40] else 0
            if desired and drive_start is None:drive_start=now
            elapsed=0 if drive_start is None else now-drive_start
            if motor==10:
                # A finite initial delay and a transient breakaway surge. The
                # physical plant responds over time, including after zero.
                desired*=0 if elapsed<.35 else 1+2*math.exp(-(elapsed-.35)/.13)
            velocities[motor]+=(desired-velocities[motor])*(1-math.exp(-dt/.025))
            positions[motor]+=velocities[motor]*dt
            set_word(reg,56,round(positions[motor])%4096)
            set_word(reg,58,encode_velocity(velocities[motor]))
        return original(packet)
    port.write=write
    result=bounded_rotate(runtime,lambda:None,direction=direction,duration=.65)
    assert all(20<result['wheel_delta_raw'][motor]*direction<130 for motor in (9,10))
    trace=result['powered_trace']
    anticipatory=[row for row in trace if row['counts'][10]<=row['counts'][9]+5
                  and row['encoder_rates_counts_s'][10]>row['encoder_rates_counts_s'][9]+50]
    assert anticipatory
    assert any(event['phase']=='balance_ack' and event['motor']==10 and (event['value']&0x7fff)<160
               and 0<=event['t_ms']-sample['t']*1000<35
               for sample in anticipatory for event in result['control_trace'])
    assert all(word(port.regs[motor],46)==0 and port.regs[motor][40]==0 for motor in (9,10))


def test_poll_yields_near_expiry_without_publishing_a_partial_report(runtime,monkeypatch):
    from fabrication.assembly.debug_console import control_runtime
    old=runtime.report
    reads=[];original=control_runtime.read_registers
    def read(port,motor):
        reads.append(motor)
        result=original(port,motor)
        runtime.bounded_wheel_deadline=time.perf_counter()
        return result
    monkeypatch.setattr(control_runtime,'read_registers',read)
    runtime.bounded_wheel_deadline=time.perf_counter()+1
    assert runtime.scan() is False
    assert len(reads)==1 and runtime.report is old
    runtime.bounded_wheel_deadline=None
    monkeypatch.setattr(control_runtime,'read_registers',original)
    assert runtime.scan() is not False
    assert runtime.report is not old


def test_yielded_monitor_does_not_follow_up_with_another_full_bus_read(runtime,monkeypatch):
    runtime.profile=None;runtime.pending={'test':True};calls=[]
    def scan():
        runtime.stop_event.set()
        return False
    monkeypatch.setattr(runtime,'scan',scan)
    monkeypatch.setattr(runtime,'capture_profile',lambda:calls.append('capture'))
    monkeypatch.setattr(runtime,'_check_feedback',lambda:calls.append('feedback'))
    runtime._monitor()
    assert calls==[]


def test_independent_probe_preserves_first_failure_and_reads_remaining_motors(runtime,monkeypatch):
    from fabrication.assembly.debug_console import control_runtime
    original=control_runtime.read_registers
    for port in runtime.session.ports.values():port.writes.clear()
    def read(port,motor):
        if port is runtime.session.ports['COM3'] and motor==1:raise RuntimeError('first motor unavailable')
        return original(port,motor)
    monkeypatch.setattr(control_runtime,'read_registers',read)
    result=runtime.probe_motors(['COM3:1','COM4:9','COM4:10'])
    assert [row['ok'] for row in result['motors']]==[False,True,True]
    assert result['motion_commands']==result['configuration_writes']==0
    assert not any(packet[4]==3 for port in runtime.session.ports.values() for packet in port.writes)


@pytest.mark.parametrize('keys',[[],['COM4:9','COM4:9'],['COM4:7'],[None]])
def test_invalid_motor_probe_never_touches_bus(runtime,keys):
    for port in runtime.session.ports.values():port.writes.clear()
    with pytest.raises(ValueError):runtime.probe_motors(keys)
    assert not any(port.writes for port in runtime.session.ports.values())


def test_probe_refuses_while_servo_session_is_active(runtime):
    from types import SimpleNamespace
    runtime.servo=SimpleNamespace(active=True)
    for port in runtime.session.ports.values():port.writes.clear()
    with pytest.raises(ValueError,match='动作或接管中'):
        runtime.probe_motors(['COM4:9'])
    assert not any(port.writes for port in runtime.session.ports.values())


def test_probe_http_contract_and_preview_exclusion(runtime,monkeypatch):
    from fastapi.testclient import TestClient
    from fabrication.assembly.debug_console import app as api
    from fabrication.assembly.debug_console.original_runtime import PreviewRuntime
    monkeypatch.setattr(api,'runtime',runtime)
    client=TestClient(api.app)
    response=client.post('/api/diagnostics/motors/read',json={'joints':['COM4:9','COM4:10']})
    assert response.status_code==200
    assert [m['joint'] for m in response.json()['motors']]==['COM4:9','COM4:10']
    assert client.post('/api/diagnostics/motors/read',json={'joints':['COM4:9','COM4:9']}).status_code==400
    assert client.post('/api/diagnostics/motors/read',json={'joints':[]}).status_code==422
    with pytest.raises(ValueError,match='隔离预览不访问串口'):
        PreviewRuntime.probe_motors(None,['COM4:9'])
