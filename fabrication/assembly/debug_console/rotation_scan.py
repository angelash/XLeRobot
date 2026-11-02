"""Bounded survey pulses inside the existing serial owner.

Each pulse has an independent local timer, zero-speed readback and an abort
path. A timer cannot protect against killing the entire serial-owner process;
this primitive is supervised surveying, not unattended crash-safe navigation.
"""
from __future__ import annotations
import math
import threading
import time
from .torque_control import read_registers, write_register, word
from .upstream_teleop import encode_velocity


def bounded_rotate(runtime, check_abort, direction=1, duration=.18, max_raw=160):
    return _bounded_pulse(runtime, check_abort, direction, duration, max_raw)


def bounded_advance(runtime, check_abort, duration=.18, max_raw=80):
    """A single forward-only pulse, after the caller checks fresh visual evidence.

    Raw speed and wheel counts are not calibrated ground speed or distance.
    """
    return _bounded_pulse(runtime, check_abort, 1, duration, max_raw, advance=True)


def _bounded_pulse(runtime, check_abort, direction, duration, max_raw, advance=False):
    if type(direction) is not int or direction not in (-1, 1):
        raise ValueError('转向只能是-1或1')
    duration_limit = .65
    speed_limit = 120 if advance else 160
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
        with stop_lock:
            if stopped.is_set():
                return
            try:
                with runtime.io:
                    # Both wheels are attempted even after a lost acknowledgement.
                    port = runtime.session.ports['COM4']
                    for motor in (9, 10):
                        try:
                            write_register(port, motor, 46, 0, 2)
                        except Exception as exc:
                            failures.append(f'{motor}: {exc}')
                    # Wheel-mode zero speed still hunts by a few encoder counts
                    # on these servos. Like the existing chassis short-test,
                    # remove wheel torque between pulses; arms remain held.
                    for motor in (9, 10):
                        try:
                            write_register(port, motor, 40, 0, 1)
                        except Exception as exc:
                            failures.append(f'{motor} release: {exc}')
                    runtime.wheels_commanded = False
                    runtime.wheel_goals = [0, 0]
                    runtime.engine.wheels = [0, 0]
                    zero_at = time.monotonic()
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
                        readbacks.append({'after_zero_ms': round((time.monotonic()-zero_at)*1000),
                                          'wheels': dict(final)})
                        if failures:
                            break
                        recent=[row for row in readbacks if readbacks[-1]['after_zero_ms']-row['after_zero_ms']<=240]
                        if stable(recent):
                            break
                        if time.monotonic()-zero_at >= .8:
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
                stopped.set()

    try:
        with runtime.io:
            check_abort()
            if runtime.controller['controller'] == 'online' or runtime.manual_calibration.active:
                raise ValueError('接管或采集中不接受扫描')
            port = runtime.session.ports['COM4']
            for motor in (9, 10):
                reg = read_registers(port, motor)
                if reg[33] != 1 or reg[40] not in (0, 1) or not 11.5 <= reg[62]/10 <= 12.6 or reg[63] >= 45 or reg[65]:
                    raise ValueError(f'轮{motor}模式、扭矩、供电、温度或状态异常')
                if word(reg,46):
                    raise ValueError(f'轮{motor}存在其他速度指令')
            # Agent preparation enables wheel torque at zero. Explicitly release
            # it again and prove rest before each pulse, including the first.
            for motor in (9,10):
                write_register(port,motor,46,0,2)
                write_register(port,motor,40,0,1)
            check_start=time.monotonic();rows=[]
            while time.monotonic()-check_start<.3:
                check_abort();entry={}
                for motor in (9,10):
                    reg=read_registers(port,motor)
                    entry[motor]={'position':word(reg,56),'goal_velocity':word(reg,46),'speed':word(reg,58),'torque':reg[40]}
                rows.append({'after_zero_ms':round((time.monotonic()-check_start)*1000),'wheels':entry})
                time.sleep(.025)
            if not stable(rows):
                raise ValueError('轮组编码器尚未稳定，未发送旋转')
            before={motor:rows[-1]['wheels'][motor]['position'] for motor in (9,10)}
            # The mirrored-left chassis uses opposite raw signs for forward.
            command = runtime.wheel_command(.02, 0, max_raw=max_raw) if advance else runtime.wheel_command(0, direction*.25, max_raw=max_raw)
            if (set(command) != {9, 10} or not command[9]
                    or command[9] != (-command[10] if advance else command[10])
                    or max(abs(v) for v in command.values()) > max_raw):
                raise ValueError('拒绝非直行的轮速组合' if advance else '拒绝非原地旋转的轮速组合')
            timer = threading.Timer(duration, stop)
            timer.daemon = True
            started = time.monotonic()
            timer.start()  # Expiry exists before the first nonzero write.
            for motor in (9, 10):
                check_abort()
                if time.monotonic()-started >= duration:
                    raise RuntimeError('写入轮速前脉冲已过期')
                write_register(port, motor, 40, 1, 1)
                write_register(port, motor, 46, encode_velocity(command[motor]), 2)
            runtime.wheels_commanded = True
            runtime.wheel_goals = [command[9], command[10]]
    except BaseException:
        # Release the serial lock before joining the independent expiry path.
        if timer is not None:
            stop()
            timer.cancel()
        raise
    try:
        while not stopped.wait(.015):
            check_abort()
            if time.monotonic()-started > 1.5:
                raise RuntimeError('观察脉冲停车超时')
        check_abort()
    finally:
        stop()
        timer.cancel()
    if failures:
        error = RuntimeError('扫描停车失败：'+'; '.join(failures))
        error.rotation = {'readbacks': readbacks, 'failures': failures}
        raise error
    deltas = {motor: (final[motor]['position']-before[motor]+2048) % 4096-2048 for motor in (9, 10)}
    # A few counts differ under caster drag; record that as possible drift.
    # Larger disagreement or overshoot stops the scan instead of accumulating it.
    disagreement=abs(deltas[9]+deltas[10]) if advance else abs(deltas[9]-deltas[10])
    if (disagreement > max(8 if advance else 10,.30*max(abs(v) for v in deltas.values()))
            or max(abs(v) for v in deltas.values()) > (100 if advance else 130)
            or (advance and any(deltas[motor]*command[motor] <= 0 for motor in (9,10)))):
        error=RuntimeError('观察轮子位移不对称、反向或超限，已停')
        error.rotation={'wheel_delta_raw':deltas,'stop_readback':final,'readbacks':readbacks}
        raise error
    if min(abs(v) for v in deltas.values()) < 2:
        raise RuntimeError('轮子未记录到有效转动，已停；需观察原因')
    if advance:
        return {'wheel_delta_raw': deltas, 'stop_readback': final, 'stop_trace': readbacks,
                'duration_s': duration, 'command_raw': command, 'rotation_command_rad_s': 0,
                'direction': 'forward', 'distance_source': 'not_measured_no_metric_claim'}
    radius, track = runtime._wheel_geometry()
    angle = sum(deltas.values())/2 * 2*math.pi/4096 * 2*radius/track
    return {'wheel_delta_raw': deltas, 'stop_readback': final,
            'stop_trace': readbacks,
            'yaw_delta_deg_estimate': math.degrees(angle), 'duration_s': duration,
            'geometry_source': 'existing_design_values_not_measured',
            'translation_command_m_s': 0}
