"""Bounded survey pulses inside the existing serial owner.

Each pulse has an independent local timer, zero-speed readback and an abort
path. A timer cannot protect against killing the entire serial-owner process;
this primitive is supervised surveying, not unattended crash-safe navigation.
"""
from __future__ import annotations
import json
import math
import threading
import time
from pathlib import Path
from .torque_control import read_registers, write_register, word
from .upstream_teleop import encode_velocity
from .temperature_guard import confirm_temperature, normal_temperature

# Which wheel-command sign moves the chassis toward where the head and arms look.
# 2026-11-03: two legs commanded "backward" (-5cm each) brought the desk and the
# plug closer in both the head and the right-wrist cameras, so on this build the
# command sign is the opposite of the camera direction. Written per robot from
# camera evidence; absent means unverified (+1, the upstream convention).
CHASSIS_DIRECTION = Path(__file__).resolve().parents[3]/'.local-deploy'/'chassis-direction.json'


def chassis_direction():
    """(sign, source): multiply a camera-frame translation by sign to get the wheel command."""
    try:
        data = json.loads(CHASSIS_DIRECTION.read_text(encoding='utf-8'))
        sign = data.get('camera_forward_sign')
        if sign in (-1, 1) and data.get('evidence'):
            return sign, 'camera_verified'
    except (OSError, ValueError, AttributeError):
        pass
    return 1, 'uncalibrated'


def bounded_rotate(runtime, check_abort, direction=1, duration=.18, max_raw=160):
    return _bounded_pulse(runtime, check_abort, direction, duration, max_raw)


def bounded_advance(runtime, check_abort, duration=.18, max_raw=80):
    """A single forward-only pulse, after the caller checks fresh visual evidence.

    Raw speed and wheel counts are not calibrated ground speed or distance.
    """
    return _bounded_pulse(runtime, check_abort, 1, duration, max_raw, advance=True)


def bounded_travel(runtime, check_abort, distance_m=.05, max_raw=220):
    """Stop by wheel rotation for one nominal <=5cm leg, not elapsed time.

    The geometry may be a design value. This reports wheel-derived distance
    explicitly; visual confirmation is still required before another leg.
    """
    if (isinstance(distance_m,bool) or not isinstance(distance_m,(int,float))
            or not math.isfinite(distance_m) or not .005<=abs(distance_m)<=.05):
        raise ValueError('每段只接受正负0.5–5cm的估计距离')
    radius,_=runtime._wheel_geometry()
    if not math.isfinite(radius) or not .02<=radius<=.2:
        raise ValueError('轮径参数无效')
    target=round(abs(distance_m)/(2*math.pi*radius)*4096)
    result=_bounded_pulse(runtime,check_abort,1 if distance_m>0 else -1,6.,max_raw,
                          advance=True,target_counts=target)
    result.update(requested_distance_m=distance_m,
                  wheel_distance_m_estimate=(1 if distance_m>0 else -1)*sum(abs(d) for d in result['wheel_delta_raw'].values())/2*2*math.pi*radius/4096,
                  radius_m_used=radius,distance_source='wheel_rotation_and_configured_geometry_not_ground_measurement')
    return result


def bounded_drive(runtime, check_abort, gate, distance_m=.3, max_raw=220, stop_margin_m=.12):
    """Forward drive watched continuously by the visual obstacle gate.

    Each monitoring cycle asks for clearance covering the remaining distance
    plus a stop margin. A blocked gate is a normal safety stop, not a fault.
    The head camera cannot see behind, so reversing is not offered here.
    """
    if (isinstance(distance_m, bool) or not isinstance(distance_m, (int, float)) or not math.isfinite(distance_m)
            or not .05 <= distance_m <= .6):
        raise ValueError('避障行驶只接受0.05–0.6m前进估计距离')
    if (isinstance(stop_margin_m, bool) or not isinstance(stop_margin_m, (int, float)) or not math.isfinite(stop_margin_m)
            or not .08 <= stop_margin_m <= .5):
        raise ValueError('停车余量需在0.08–0.5m')
    radius, _ = runtime._wheel_geometry()
    target = round(distance_m/(2*math.pi*radius)*4096)
    clearances = []

    def check(progress_m):
        ok, reason, message = gate.check(max(0., distance_m-progress_m)+stop_margin_m)
        if message and message.get('clear_m') is not None:
            clearances.append(message['clear_m'])
        return ok, reason
    duration = min(90., 3.+distance_m/.006)
    result = _bounded_pulse(runtime, check_abort, 1, duration, max_raw, advance=True, target_counts=target,
                            gate=check, max_duration=90.)
    result.update(requested_distance_m=distance_m, stop_margin_m=stop_margin_m, radius_m_used=radius,
                  wheel_distance_m_estimate=sum(abs(d) for d in result['wheel_delta_raw'].values())/2*2*math.pi*radius/4096,
                  min_clear_m=min(clearances) if clearances else None, gate_checks=len(clearances),
                  distance_source='wheel_rotation_and_configured_geometry_not_ground_measurement')
    return result


def _bounded_pulse(runtime, check_abort, direction, duration, max_raw, advance=False,target_counts=None,
                   gate=None, max_duration=None, stall_s=.35):
    if type(direction) is not int or direction not in (-1, 1):
        raise ValueError('转向只能是-1或1')
    duration_limit = max_duration if gate is not None and max_duration else 6. if target_counts is not None else .65
    speed_limit = 220 if target_counts is not None else 120 if advance else 160
    if isinstance(duration, bool) or not isinstance(duration, (float, int)) or not math.isfinite(duration) or not .04 <= duration <= duration_limit:
        raise ValueError(f'观察脉冲只能持续40–{round(duration_limit*1000)}ms')
    if type(max_raw) is not int or not 60 <= max_raw <= speed_limit:
        raise ValueError(f'观察轮速仅允许60–{speed_limit}')
    before = {}
    stopped = threading.Event()
    stop_lock = threading.Lock()
    failures = []
    final = {}
    readbacks = []
    started = None
    timer = None
    travel_trace=[]
    reached=False
    drive_elapsed=None
    fault=None
    blocked=None
    best=0;best_t=0.
    glitches=[]
    other_glitches=0
    control_trace=[]
    stopping=threading.Event()
    last_rotation_sample=0.
    applied={}
    # Python 3.11's monotonic clock can quantize Windows writes to 0ms.
    clock=time.perf_counter

    def mark(phase, **values):
        control_trace.append({'phase':phase,
                              't_ms':round((clock()-started)*1000,3),
                              **values})

    def drive_write(port, motor, address, value, length, phase):
        mark(phase+'_begin', motor=motor, address=address, value=value)
        try:
            write_register(port, motor, address, value, length)
        except BaseException as exc:
            mark(phase+'_error', motor=motor, error=str(exc))
            raise
        mark(phase+'_ack', motor=motor, address=address, value=value)

    def stable(rows):
        # STS speed is quantized in 50-count steps: idle encoder jitter by one
        # count can report +/-50. Accept that only with torque off, zero goal,
        # and <=2 encoder counts of spread over a complete 200ms window.
        if not rows or rows[-1]['after_zero_ms']-rows[0]['after_zero_ms'] < 200:
            return False
        return all(
            all(row['wheels'][motor]['torque']==0 and row['wheels'][motor]['goal_velocity']==0
                and (row['wheels'][motor]['speed'] & 0x7fff)<=50 for row in rows)
            and max((row['wheels'][motor]['position']-rows[0]['wheels'][motor]['position']+2048)%4096-2048 for row in rows)
              - min((row['wheels'][motor]['position']-rows[0]['wheels'][motor]['position']+2048)%4096-2048 for row in rows)<=2
            for motor in (9,10))

    def stop():
        nonlocal drive_elapsed
        stopping.set()
        with stop_lock:
            if stopped.is_set():
                return
            if started is not None:
                drive_elapsed=clock()-started
                mark('stop_requested')
            try:
                with runtime.io:
                    mark('stop_io_acquired')
                    # Both wheels are attempted even after a lost acknowledgement.
                    port = runtime.session.ports['COM4']
                    for motor in (9, 10):
                        try:
                            drive_write(port, motor, 46, 0, 2, 'zero')
                        except Exception as exc:
                            failures.append(f'{motor}: {exc}')
                    # Wheel-mode zero speed still hunts by a few encoder counts
                    # on these servos. Like the existing chassis short-test,
                    # remove wheel torque between pulses; arms remain held.
                    for motor in (9, 10):
                        try:
                            drive_write(port, motor, 40, 0, 1, 'release')
                        except Exception as exc:
                            failures.append(f'{motor} release: {exc}')
                    runtime.wheels_commanded = False
                    runtime.wheel_goals = [0, 0]
                    runtime.engine.wheels = [0, 0]
                    zero_at = clock()
                    # A zero command is immediate; measured speed may still be
                    # decelerating. Require a stable encoder window within 800ms,
                    # never infer rest from the command register alone.
                    while True:
                        final.clear()
                        for motor in (9, 10):
                            try:
                                reg = read_registers(port, motor)
                                final[motor] = {'position': word(reg, 56), 'goal_velocity': word(reg, 46),
                                                'speed': word(reg, 58), 'torque': reg[40]}
                                if word(reg, 46):
                                    failures.append(f'{motor}: 停车目标回读非零')
                            except Exception as exc:
                                failures.append(f'{motor}: {exc}')
                        readbacks.append({'after_zero_ms': round((clock()-zero_at)*1000),
                                          'wheels': dict(final)})
                        if failures:
                            break
                        recent=[row for row in readbacks if readbacks[-1]['after_zero_ms']-row['after_zero_ms']<=240]
                        if stable(recent):
                            break
                        if clock()-zero_at >= .8:
                            failures.append('800ms内未取得200ms的轮组编码器静止证据')
                            break
                        time.sleep(.025)
                    if failures:
                        # Stop verification failed: remove wheel drive, retaining arms.
                        for motor in (9, 10):
                            try:
                                write_register(port, motor, 40, 0, 1)
                            except Exception as exc:
                                failures.append(f'{motor} release: {exc}')
            except Exception as exc:
                failures.append(str(exc))
            finally:
                runtime.bounded_wheel_deadline=None
                stopped.set()

    try:
        with runtime.io:
            check_abort()
            if runtime.controller['controller'] == 'online' or runtime.manual_calibration.active:
                raise ValueError('接管或采集中不接受扫描')
            port = runtime.session.ports['COM4']
            for motor in (9, 10):
                reg = read_registers(port, motor)
                other_ok = lambda r: r[33] == 1 and r[40] in (0, 1) and 11.5 <= r[62]/10 <= 12.6 and not r[65]
                reg, temperature_check = confirm_temperature(
                    reg, lambda: read_registers(port, motor), 45,
                    other_ok=other_ok, check_abort=check_abort)
                if temperature_check:
                    glitches.append({'phase':'preflight','motor':motor,**temperature_check})
                if not other_ok(reg) or not normal_temperature(reg[63], 45):
                    raise ValueError(f'轮{motor}模式、扭矩、供电、温度或状态异常')
                if word(reg,46):
                    raise ValueError(f'轮{motor}存在其他速度指令')
            # Agent preparation enables wheel torque at zero. Explicitly release
            # it again and prove rest before each pulse, including the first.
            for motor in (9,10):
                write_register(port,motor,46,0,2)
                write_register(port,motor,40,0,1)
            check_start=clock();rows=[]
            while True:
                check_abort();entry={}
                for motor in (9,10):
                    reg=read_registers(port,motor)
                    entry[motor]={'position':word(reg,56),'goal_velocity':word(reg,46),'speed':word(reg,58),'torque':reg[40]}
                rows.append({'after_zero_ms':round((clock()-check_start)*1000),'wheels':entry})
                if clock()-check_start>=.3 and stable(rows):break
                if clock()-check_start>=.8:break
                time.sleep(.025)
            if not stable(rows):
                raise ValueError('轮组编码器尚未稳定，未发送旋转')
            before={motor:rows[-1]['wheels'][motor]['position'] for motor in (9,10)}
            if gate is not None:
                ok,reason=gate(0.)
                if not ok:
                    raise ValueError('视觉避障未放行：'+reason)
            # The mirrored-left chassis uses opposite raw signs for forward.
            # Translations are in the camera frame: the calibrated sign maps them to wheel commands.
            sign, direction_source = chassis_direction() if advance else (1, 'rotation')
            command = runtime.wheel_command(sign*direction*(.025 if target_counts is not None else .02), 0, max_raw=max_raw) if advance else runtime.wheel_command(0, direction*.25, max_raw=max_raw)
            if (set(command) != {9, 10} or not command[9]
                    or command[9] != (-command[10] if advance else command[10])
                    or max(abs(v) for v in command.values()) > max_raw):
                raise ValueError('拒绝非直行的轮速组合' if advance else '拒绝非原地旋转的轮速组合')
            timer = threading.Timer(duration, stop)
            timer.daemon = True
            started = clock()
            runtime.bounded_wheel_deadline=started+duration
            mark('deadline_armed', requested_duration_s=duration)
            timer.start()  # Expiry exists before the first nonzero write.
            for motor in (9, 10):
                check_abort()
                if clock()-started >= duration:
                    raise RuntimeError('写入轮速前脉冲已过期')
                drive_write(port, motor, 40, 1, 1, 'enable')
                if stopping.is_set() or clock()-started>=duration:
                    raise RuntimeError('轮组使能后脉冲已过期，未写入新轮速')
                drive_write(port, motor, 46, encode_velocity(command[motor]), 2, 'command')
            runtime.wheels_commanded = True
            runtime.wheel_goals = [command[9], command[10]]
            applied=dict(command)
    except BaseException as exc:
        # Release the serial lock before joining the independent expiry path.
        if timer is not None:
            stop()
            timer.cancel()
        if glitches or control_trace:
            exc.rotation={'wheel_start_raw':before,'read_glitches':glitches,'readbacks':readbacks,
                          'control_trace':control_trace}
        raise
    try:
        while not stopped.wait(.015):
            check_abort()
            if clock()-started>duration+1.2:
                raise RuntimeError('观察脉冲停车超时')
            if target_counts is not None or not advance:
                if stopping.is_set():continue
                if not advance and clock()-last_rotation_sample<.05:continue
                with runtime.io:
                    if stopping.is_set() or clock()-started>=duration:continue
                    progress={};powered={}
                    for motor in (9,10):
                        reg=read_registers(runtime.session.ports['COM4'],motor)
                        other_ok=lambda r:r[33]==1 and r[40]==1 and not r[65] and 11.5<=r[62]/10<=12.6
                        reg, temperature_check = confirm_temperature(
                            reg, lambda: read_registers(runtime.session.ports['COM4'],motor), 45,
                            other_ok=other_ok, check_abort=check_abort)
                        if temperature_check:
                            glitches.append({'t':round(clock()-started,3),'motor':motor,**temperature_check})
                        bad=lambda r:not other_ok(r) or not normal_temperature(r[63],45)
                        if not other_ok(reg):
                            # Preserve the existing bounded recovery for non-temperature faults.
                            first=list(reg)
                            reg=read_registers(runtime.session.ports['COM4'],motor)
                            if not bad(reg):
                                other_glitches+=1
                                glitches.append({'kind':'other','t':round(clock()-started,3),'motor':motor,'first':first,'reread':list(reg)})
                                if other_glitches>3:
                                    raise RuntimeError('行进中轮组读数反复异常，已停')
                        if bad(reg):
                            fault={'motor':motor,'registers':list(reg),'position':word(reg,56),
                                   'mode':reg[33],'torque':reg[40],'status':reg[65],
                                   'temperature':reg[63],'voltage':reg[62]/10,'glitches':glitches}
                            raise RuntimeError(f'行进中轮{motor}状态异常：mode={reg[33]}, torque={reg[40]}, '
                                               f'status={reg[65]}, temperature={reg[63]}, voltage={reg[62]/10}')
                        progress[motor]=((word(reg,56)-before[motor]+2048)%4096-2048)*(1 if command[motor]>0 else -1)
                        powered[motor]={'position':word(reg,56),'speed':word(reg,58),
                                        'goal_velocity':word(reg,46),'load_raw':word(reg,60),
                                        'torque':reg[40],'status':reg[65],
                                        'temperature':reg[63],'voltage':reg[62]/10,
                                        't':round(clock()-started,6)}
                    travel_trace.append({'t':round(clock()-started,3),'counts':progress,'wheels':powered})
                    if not advance:
                        last_rotation_sample=clock()
                        if (min(progress.values()) < -4 or max(progress.values())>130
                                or abs(progress[9]-progress[10])>max(35,.30*max(progress.values()))):
                            raise RuntimeError('旋转中轮位反向、不对称或超限，已停')
                        # Breakaway can send a formerly slow wheel past its peer
                        # before the next sample. Predict 100ms of relative
                        # encoder travel using each wheel's own sample times;
                        # do not assume the servo speed register's unit factor.
                        rates={9:0.,10:0.}
                        if len(travel_trace)>1:
                            previous=travel_trace[-2]
                            for motor in (9,10):
                                dt=powered[motor]['t']-previous['wheels'][motor]['t']
                                if dt>.01:
                                    rates[motor]=max(0.,(progress[motor]-previous['counts'][motor])/dt)
                        travel_trace[-1]['encoder_rates_counts_s']=rates
                        travel_trace[-1]['prediction_horizon_s']=.1
                        # Only slow the wheel leading in predicted travel. Never
                        # boost the other wheel, extend expiry or relax guards.
                        for motor,other in ((9,10),(10,9)):
                            lead=max(0,progress[motor]-progress[other]+.1*(rates[motor]-rates[other])-2)
                            desired=(1 if command[motor]>0 else -1)*round(max(0,abs(command[motor])-4*lead))
                            if abs(desired-applied[motor])>=8:
                                if stopping.is_set() or clock()-started>=duration:break
                                drive_write(runtime.session.ports['COM4'],motor,46,
                                            encode_velocity(desired),2,'balance')
                                applied[motor]=desired
                        runtime.wheel_goals=[applied[9],applied[10]]
                        runtime.engine.wheels=list(runtime.wheel_goals)
                if not advance:continue
                if min(progress.values()) < -4 or abs(progress[9]-progress[10]) > max(35,.25*max(progress.values())):
                    raise RuntimeError('直行中轮位反向或不对称，已停')
                # Contact stop: once moving, both wheels must keep gaining. A stall
                # (pushing against something) ends the leg within stall_s, as a
                # normal safety stop rather than a fault.
                now_t=travel_trace[-1]['t']
                if min(progress.values())>=best+6:
                    best,best_t=min(progress.values()),now_t
                if stall_s and best>=20 and now_t>.5 and now_t-best_t>stall_s:
                    blocked={'reason':'stall','t':now_t,'counts':progress,
                             'message':f'双轮{stall_s:g}秒内合计进展不足6计数，疑似碰撞，已停'}
                    stop()
                    break
                if gate is not None:
                    radius,_=runtime._wheel_geometry()
                    ok,reason=gate(max(0,min(progress.values()))*2*math.pi*radius/4096)
                    if not ok:
                        blocked={'reason':reason,'t':travel_trace[-1]['t'],'counts':progress}
                        stop()
                        break
                if clock()-started>1.2 and min(progress.values())<2:
                    raise RuntimeError('直行没有取得双轮有效进展，已停')
                if max(progress.values())>=target_counts-10:
                    reached=True;stop()
            if clock()-started > duration+1.2:
                raise RuntimeError('观察脉冲停车超时')
        check_abort()
    except BaseException as exc:
        stop()
        exc.rotation={'travel_trace':travel_trace,'readbacks':readbacks,
                      'target_counts':target_counts,'failures':failures,
                      'wheel_start_raw':before,'fault':fault,'read_glitches':glitches,
                      'control_trace':control_trace}
        raise
    finally:
        stop()
        timer.cancel()
    deltas = {motor: (final[motor]['position']-before[motor]+2048) % 4096-2048
              for motor in (9, 10) if motor in final}
    stop_evidence = {'wheel_delta_raw': deltas, 'stop_readback': final, 'readbacks': readbacks,
                     'travel_trace': travel_trace, 'target_counts': target_counts,
                     'command_raw': command, 'duration_s': drive_elapsed, 'read_glitches': glitches,
                     'failures': failures, 'control_trace':control_trace}
    if failures:
        error = RuntimeError('扫描停车失败：'+'; '.join(failures))
        error.rotation = stop_evidence
        raise error
    # A few counts differ under caster drag; record that as possible drift.
    # Larger disagreement or overshoot stops the scan instead of accumulating it.
    disagreement=abs(deltas[9]+deltas[10]) if advance else abs(deltas[9]-deltas[10])
    if (disagreement > max(8 if advance else 10,.30*max(abs(v) for v in deltas.values()))
            or max(abs(v) for v in deltas.values()) > (target_counts+max(20,round(target_counts*.1)) if target_counts is not None else 100 if advance else 130)
            or (advance and any(deltas[motor]*command[motor] <= 0 for motor in (9,10)))):
        error=RuntimeError('观察轮子位移不对称、反向或超限，已停')
        error.rotation=stop_evidence
        raise error
    if min(abs(v) for v in deltas.values()) < 2 and not blocked:
        error = RuntimeError('轮子未记录到有效转动，已停；需观察原因')
        error.rotation = stop_evidence
        raise error
    if target_counts is not None and not blocked and (not reached or min(abs(v) for v in deltas.values())<target_counts*.75):
        error = RuntimeError('直行未达到计数目标，已停；不自动续走')
        error.rotation = stop_evidence
        raise error
    if advance:
        stopped_by=('stall' if blocked and blocked.get('reason')=='stall' else 'obstacle_gate' if blocked else 'target')
        return {'wheel_delta_raw': deltas, 'stop_readback': final, 'stop_trace': readbacks,
                'control_trace':control_trace,
                'duration_s': duration if target_counts is None else round(drive_elapsed,3),
                'command_raw': command, 'rotation_command_rad_s': 0,
                'direction': 'forward' if direction>0 else 'backward', 'direction_frame': 'camera',
                'command_sign': sign, 'direction_source': direction_source, 'distance_source': 'not_measured_no_metric_claim',
                **({'target_counts':target_counts,'travel_trace':travel_trace,'max_duration_s':duration,
                    'wheel_start_raw':before,'stopped_by':stopped_by,'read_glitches':glitches,
                    **({'stall':blocked} if stopped_by=='stall' else {})} if target_counts is not None else {}),
                **({'stopped_by':stopped_by,'gate_block':blocked if stopped_by=='obstacle_gate' else None} if gate is not None else {})}
    radius, track = runtime._wheel_geometry()
    angle = sum(deltas.values())/2 * 2*math.pi/4096 * 2*radius/track
    return {'wheel_delta_raw': deltas, 'stop_readback': final,
            'stop_trace': readbacks,
            'control_trace':control_trace,
            'powered_trace':travel_trace,'wheel_start_raw':before,
            'balance_policy':'reduce_predicted_leading_wheel_within_original_caps',
            'read_glitches': glitches,
            'yaw_delta_deg_estimate': math.degrees(angle), 'duration_s': duration,
            'geometry_source': 'existing_design_values_not_measured',
            'translation_command_m_s': 0}
