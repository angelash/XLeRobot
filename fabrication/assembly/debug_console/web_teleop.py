"""Leased browser control inside the existing serial owner; no synthetic gamepad.

Every input replaces the complete held-key set. Session/sequence checks reject
late requests; only this channel's input renews its 650 ms dead-man lease.

A session runs in one of two modes. ``keys`` is the keyboard/button page.
``vr`` takes analog sticks, clutch-style hand following and trigger grip from
the WebXR page; it shares the lease, exclusivity and joint guards above.
"""
from __future__ import annotations

import math
import threading
import time
import uuid
from datetime import datetime

from .control_runtime import now
from .temperature_guard import confirm_temperature, normal_temperature
from .torque_control import EXPECTED, read_registers, word
from .upstream_teleop import UpstreamTeleop, forward, inverse, neutral
from . import vr_kinematics
from . import vr_policy

LEASE_S = .65
MAX_SESSION_S = 1200
ARM_ACTIONS = ('extend', 'retract', 'up', 'down', 'pan_left', 'pan_right',
               'pitch_up', 'pitch_down', 'roll_left', 'roll_right', 'open', 'close')
ACTIONS = frozenset(['base.' + x for x in ('forward', 'back', 'left', 'right')] +
                    ['head.' + x for x in ('left', 'right', 'up', 'down')] +
                    ['arm.' + x for x in ARM_ACTIONS] +
                    [f'joint.{i}.{sign}' for i in range(1, 7) for sign in ('plus', 'minus')])
MODES = ('keys', 'vr')
# VR follows the per-axis settings; these only cap what a setting may request.
VR_MAX_RATE_DEG_S = 45.
VR_MAX_GRIP_RAW_S = 400
# Stick values are stepped so the wheel direction/ratio windows see stable commands.
VR_BASE_STEP = .25
VR_HEAD_DEADZONE = .05
VR_JOG_RATE_DEG_S = 6.


class WebTeleop:
    def __init__(self, runtime, clock=time.monotonic, threaded=True):
        self.runtime = runtime
        self.clock = clock
        self.threaded = threaded
        self.active = False
        self.session_id = None
        self.seq = -1
        self.actions = frozenset()
        self.arm = 'left'
        self.scale = .5
        self.last_input = self.started = self.last_tick = 0.
        self.reason = '尚未开启网页控制'
        self.blocked = {}
        self.base_window = None
        self.voltage_bad = {}
        self._thread = None
        self.mode = 'keys'
        self.vr_disabled = {}
        self.tick_ms = None
        self.display_origins = {}
        self._reset_vr()

    def _reset_vr(self):
        self.vr = {'base': (0., 0.), 'head': (0., 0.), 'arms': {}, 'grip': {}, 'ratio': 1., 'jog': None}
        self.vr_arms = {side: {'state': 'idle', 'reason': None} for side in ('left', 'right')}
        self.vr_grip = {side: {'active': False, 'target': None, 'hold': None, 'reason': None}
                        for side in ('left', 'right')}

    def snapshot(self):
        # Session tokens are deliberately absent from shared/polled status.
        result = {'active': self.active, 'seq': self.seq, 'lease_ms': int(LEASE_S * 1000),
                  'actions': sorted(self.actions), 'arm': self.arm, 'reason': self.reason,
                  'blocked': dict(self.blocked), 'scale': self.scale, 'mode': self.mode}
        if self.mode == 'vr':
            result['vr'] = {
                'arms': {s: {k: v.get(k) for k in ('state', 'reason', 'engage')} for s, v in self.vr_arms.items()},
                'grip': {s: {'active': g['active'], 'holding': g['hold'] is not None, 'reason': g['reason']}
                         for s, g in self.vr_grip.items()},
                'ratio': self.vr['ratio'], 'tick_ms': self.tick_ms, 'disabled': dict(self.vr_disabled),
                'jog': self.vr['jog'], 'feedback': self.pose_feedback()}
        return result

    def pose_feedback(self):
        """Display only: cached encoder samples and targets already sent.

        No extra serial reads or preparation on the input/lease path. Pan,
        roll and head angles are relative to this runtime's first sample;
        the plane joints use verified calibration. Unknowns remain null.
        """
        r = self.runtime
        motors = r.motors()
        sampled_at = r.report.get('updated_at')
        try:
            age_ms = max(0., (datetime.now().astimezone() - datetime.fromisoformat(sampled_at)).total_seconds() * 1000)
        except (TypeError, ValueError):
            age_ms = None
        def raw(port, i, target):
            m = motors.get((port, i))
            if not m:
                return None
            return r.moving.get((port, i), m['position']) if target and self.active else m['position']
        def relative(port, i, target, sign=1):
            value = raw(port, i, target)
            if value is None:
                return None
            origin = self.display_origins.setdefault((port, i), motors[(port, i)]['position'])
            return sign * (value - origin) * r.settings.axis(port, i)['direction'] * 360 / 4096
        def arm(port, target):
            pose = {'pan': relative(port, 1, target, -1), 'roll': relative(port, 5, target)}
            for i, name in ((2, 'lift'), (3, 'elbow'), (4, 'wrist'), (6, 'grip')):
                ref, value = r.reference(port, i), raw(port, i, target)
                if not ref or value is None:
                    return None
                if i == 6:
                    span = ref.get('closed_raw', 0) - ref.get('open_raw', 0)
                    if not span:
                        return None
                    pose[name] = max(0., min(1., (value - ref['open_raw']) / span))
                else:
                    pose[name] = ref['reference_deg'] + (value - ref['reference_raw']) * ref['direction'] * 360 / 4096
            return pose if all(v is not None and math.isfinite(v) for v in pose.values()) else None
        arms = {side: {'measured': arm(port, False), 'target': arm(port, True)}
                for side, port in r.arm_ports().items()}
        return {'source': 'encoder_cache', 'sampled_at': sampled_at,
                'age_ms': round(age_ms, 1) if age_ms is not None else None, 'arms': arms,
                'head': [relative('COM3', i, False, -1) for i in (7, 8)],
                'relative_axes': ['pan', 'roll', 'head'], 'targets_are_sent_commands': True}

    def vr_readiness(self):
        """Read current cached motor inventory and references; never prepare torque."""
        r = self.runtime
        with r.io:
            disabled = vr_policy.read()
            motors = r.motors()
            complete = set(motors) == {(p, i) for p, ids in EXPECTED.items() for i in ids}
            busy = (self.active or r.controller['controller'] == 'online' or r.agent.active
                    or r.manual_pose_active or r.limit_test.running or r.manual_calibration.active
                    or r.native_menu.status()['busy'])
            channels = {}
            def unavailable(port, ids):
                # Preparation blocks belong to the previous session. This is a
                # fresh read-only eligibility check, including persistent holds.
                for i in ids:
                    if f'{port}:{i}' in r.service_blocked:
                        return r.service_blocked[f'{port}:{i}']
                    m = motors.get((port, i))
                    wheel = port == 'COM4' and i in (9, 10)
                    if (not m or m['mode'] != (1 if wheel else 0) or
                            (not wheel and (m['max'] - m['min'] < 24 or not m['min'] <= m['position'] <= m['max']))):
                        return '设备范围或当前位置未就绪'
                return None
            for side in ('left', 'right'):
                port = r.arm_ports()[side]
                reason = disabled.get(side)
                refs = {i: r.reference(port, i) for i in (2, 3, 4, 6)}
                follow_reason = reason or unavailable(port, range(1, 6))
                grip_reason = reason or unavailable(port, (6,))
                joints = {i: reason or unavailable(port, (i,)) for i in range(1, 6)}
                if not follow_reason:
                    if not all(refs[i] for i in (2, 3, 4)):
                        follow_reason = '此臂运动学参考未就绪'
                    else:
                        q = {i: refs[i]['reference_deg'] + (motors[(port, i)]['position'] - refs[i]['reference_raw'])
                             * refs[i]['direction'] * 360 / 4096 for i in (2, 3, 4)}
                        try:
                            vr_kinematics.anchor(q[2], q[3], q[4])
                        except ValueError as exc:
                            follow_reason = str(exc)
                if not grip_reason and (not refs[6] or refs[6].get('closed_raw') == refs[6].get('open_raw')):
                    grip_reason = '夹爪开合端点未确认'
                enabled = not follow_reason or not grip_reason or any(v is None for v in joints.values())
                channels[side] = {'enabled': enabled, 'reason': reason or (None if enabled else follow_reason),
                                  'follow_ready': not bool(follow_reason), 'follow_reason': follow_reason,
                                  'grip_ready': not bool(grip_reason), 'grip_reason': grip_reason,
                                  'joints': {str(i): {'enabled': v is None, 'reason': v,
                                             'position': motors.get((port, i), {}).get('position')}
                                             for i, v in joints.items()}}
            for name, port, ids in (('head', 'COM3', (7, 8)), ('base', 'COM4', (9, 10))):
                reason = disabled.get(name) or unavailable(port, ids)
                channels[name] = {'enabled': not bool(reason), 'reason': reason}
            return {'can_start': complete and not busy and any(c['enabled'] for c in channels.values()),
                    'reason': '已有控制或任务占用' if busy else '需两块控制板与16电机完整应答' if not complete else None,
                    'channels': channels, 'prepares_current_pose_on_start': True,
                    'active': self.active, 'read_only': True, 'feedback': self.pose_feedback()}

    def start(self, confirmed=False, mode='keys'):
        r = self.runtime
        with r.io:
            if mode not in MODES:
                raise ValueError('未知控制方式')
            if confirmed is not True:
                raise ValueError('请先确认已看清周围，手臂与底盘运动范围内无人')
            if self.active or r.controller['controller'] == 'online':
                raise ValueError('已有控制会话，请先结束后再开启网页控制')
            r._require_not_calibrating()
            if r.manual_pose_active or r.agent.active or r.native_menu.status()['busy']:
                raise ValueError('摆位、助手任务或手柄启动仍在进行')
            if r.gamepad.get('connected') and not neutral(r.gamepad):
                raise ValueError('请松开手柄所有输入；手柄输入会结束网页控制')
            disabled = vr_policy.read() if mode == 'vr' else {}
            if len(disabled) == len(vr_policy.CHANNELS):
                raise ValueError('VR所有通道均被限制，请先处理所示原因')
            r.capture_profile()
            motors = r.motors()
            if set(motors) != {(p, i) for p, ids in EXPECTED.items() for i in ids}:
                raise ValueError('需要两块原控制板和16颗电机完整应答')
            if any(motors[('COM4', i)]['goal_velocity'] for i in (9, 10)):
                raise ValueError('双轮目标非零，请先完成停车')
            prepared = (r.prepare_hardware(True, excluded=vr_policy.motors(disabled, r.arm_ports()))
                        if mode == 'vr' else r.prepare_hardware(True))
            self.blocked = {f"{m['port']}:{m['id']}": m['reason'] for m in prepared['blocked']}
            if not prepared['ready']:
                raise ValueError('没有设备完成当前位置保持')
            for e in r.envelopes.values():
                e['min'], e['max'] = e['device_min'], e['device_max']
            radius, track = r._wheel_geometry()
            r.engine = UpstreamTeleop(radius=radius, track=track)
            r.engine.arm_ports = r.arm_ports()
            r.control_profile = 'web'
            r.moving = {}; r.direct_targets = {}; r.feedback_window = {}
            r.wheels_commanded = False; r.wheel_goals = [0, 0]
            r.supply_bad = {}; r.notice = None
            self.session_id = uuid.uuid4().hex
            self.seq = -1; self.actions = frozenset()
            self.base_window = None
            self.voltage_bad = {}
            self.mode = mode; self._reset_vr(); self.tick_ms = None
            self.vr_disabled = disabled
            self.started = self.last_input = self.last_tick = self.clock()
            self.active = True
            self.reason = ('VR控制已开启：按住握把让同侧手臂跟随，扳机控制夹爪' if mode == 'vr'
                           else '网页控制已开启，等待按住按键或按钮')
            r.controller.update(controller='online', armed=True, paused=False, error=None,
                                pause_reason=None, last_action=self.reason, updated_at=now())
            r.log('web_start', self.reason)
            if self.threaded and (not self._thread or not self._thread.is_alive()):
                self._thread = threading.Thread(target=self._run, daemon=True, name='web-teleop')
                self._thread.start()
            return {**self.snapshot(), 'session_id': self.session_id}

    def _check(self, session_id):
        if not self.active or session_id != self.session_id:
            raise ValueError('网页会话已结束或编号不符，请重新开启')
        if self.clock() - self.last_input > LEASE_S:
            self.stop('网页输入超过650ms未更新，已停止；需重新开启')
            raise ValueError(self.reason)

    def input(self, session_id, seq, actions, arm='left', scale=.5):
        r = self.runtime
        with r.io:
            self._check(session_id)
            if self.mode != 'keys':
                raise ValueError('当前是VR会话，请使用VR输入')
            if isinstance(seq, bool) or not isinstance(seq, int) or seq <= self.seq:
                raise ValueError('输入序号过期，未续租或执行')
            if arm not in ('left', 'right', 'both') or not set(actions) <= ACTIONS:
                raise ValueError('未知控制对象或动作')
            if not math.isfinite(scale) or not .2 <= scale <= 1:
                raise ValueError('速度比例必须在0.2–1之间')
            self.seq = seq; self.actions = frozenset(actions); self.arm = arm; self.scale = scale
            self.last_input = self.clock()
            # Release packets stop immediately, without waiting for the worker.
            if not self.actions:
                r._hold()
                self.base_window = None
            return self.snapshot()

    def vr_input(self, session_id, seq, base=(0., 0.), head=(0., 0.), arms=None, grip=None,
                 scale=.5, ratio=1., jog=None):
        """Replace the complete VR state. Hand poses are displacements from the
        client's grip anchor; the robot-side anchor is taken on the next tick."""
        r = self.runtime
        with r.io:
            self._check(session_id)
            if self.mode != 'vr':
                raise ValueError('当前不是VR会话')
            if isinstance(seq, bool) or not isinstance(seq, int) or seq <= self.seq:
                raise ValueError('输入序号过期，未续租或执行')
            arms = {k: v for k, v in (arms or {}).items() if v is not None}
            grip = {k: v for k, v in (grip or {}).items() if v is not None}
            numbers = [*base, *head, scale, ratio, *grip.values()]
            for a in arms.values():
                numbers += [*a['pos'], a['pitch'], a['roll']]
            if jog is not None:
                if (not isinstance(jog, dict) or set(jog) != {'side', 'joint', 'value', 'engage'}
                        or jog['side'] not in ('left', 'right')
                        or type(jog['joint']) is not int or not 1 <= jog['joint'] <= 5
                        or type(jog['engage']) is not int or jog['engage'] < 1):
                    raise ValueError('逐关节输入格式错误')
                numbers.append(jog['value'])
                if arms or any(base):
                    raise ValueError('逐关节调整不能同时跟随手部或驾驶底盘')
            if (set(arms) | set(grip)) - {'left', 'right'} or not all(
                    isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x) for x in numbers):
                raise ValueError('VR输入格式错误')
            if (len(base) != 2 or len(head) != 2 or any(abs(x) > 1 for x in (*base, *head))
                    or not .2 <= scale <= 1 or not .25 <= ratio <= 2
                    or any(not 0 <= g <= 1 for g in grip.values())
                    or (jog is not None and abs(jog['value']) > 1)
                    or any(len(a['pos']) != 3 or abs(a['pitch']) > 180 or abs(a['roll']) > 180
                           or isinstance(a['engage'], bool) or not isinstance(a['engage'], int)
                           or a['engage'] < 0 for a in arms.values())):
                raise ValueError('VR输入超出范围')
            self.seq = seq; self.scale = scale; self.last_input = self.clock()
            self.vr = {'base': tuple(round(x / VR_BASE_STEP) * VR_BASE_STEP for x in base),
                       'head': tuple(0. if abs(x) < VR_HEAD_DEADZONE else float(x) for x in head),
                       'arms': {k: {'engage': a['engage'], 'pos': tuple(float(x) for x in a['pos']),
                                    'pitch': float(a['pitch']), 'roll': float(a['roll']),
                                    'orient': bool(a.get('orient', True))} for k, a in arms.items()},
                       'grip': {k: float(v) for k, v in grip.items()}, 'ratio': float(ratio),
                       'jog': dict(jog) if jog is not None else None}
            # A released drive stick stops the wheels now, not on the next tick.
            if not any(self.vr['base']) and r.wheels_commanded:
                r._drive_base(0, 0)
                self.base_window = None
            return self.snapshot()

    def goal_speed_cap(self, i):
        """Upper bound for the servo speed register written by a web or VR goal."""
        if self.mode == 'vr':
            if self.vr['jog'] is not None and i != 6:
                return VR_JOG_RATE_DEG_S * 4096 / 360
            return VR_MAX_GRIP_RAW_S if i == 6 else VR_MAX_RATE_DEG_S * 4096 / 360
        return 100 if i == 6 else 12 * 4096 / 360

    def squeeze_on_stall(self, key, pos, expected):
        """A VR gripper that meets an object keeps a light squeeze instead of
        ending the session; the trigger must open past the contact to resume."""
        port, i = key
        if self.mode != 'vr' or i != 6:
            return False
        for side, p in self.runtime.arm_ports().items():
            if p == port:
                g = self.vr_grip[side]
                g['hold'] = float(pos)
                g['target'] = float(pos + max(-40, min(40, expected - pos)))
                return True
        return False

    def stop(self, reason='已结束网页控制，保持当前位置', session_id=None):
        r = self.runtime
        with r.io:
            if session_id is not None and session_id != self.session_id:
                raise ValueError('会话编号不符，未停止其他会话')
            if not self.active:
                return self.snapshot()
            self.active = False; self.actions = frozenset(); self.session_id = None
            self.reason = reason
            self._reset_vr()
            r._stop(reason)
            if r.controller.get('error') or (r.notice or {}).get('level') == 'error':
                self.reason = r.controller['last_action']
            return self.snapshot()

    def _run(self):
        # One worker survives quick stop/start cycles; it always reads current
        # session state while holding the serial owner's lock.
        while not self.runtime.stop_event.wait(.04):
            with self.runtime.io:
                if self.active:
                    self.tick()

    def tick(self):
        r = self.runtime
        if not self.active:
            return
        if r.controller['controller'] != 'online' or r.controller['paused']:
            self.stop(r.controller.get('pause_reason') or '控制已停止')
            return
        if self.clock() - self.last_input > LEASE_S or self.clock() - self.started > MAX_SESSION_S:
            self.stop('网页输入过期或会话到期，已停止；需重新开启')
            return
        dt = max(0., min(.1, self.clock() - self.last_tick)); self.last_tick = self.clock()
        started = time.perf_counter()
        try:
            if self.mode == 'vr':
                if vr_policy.read() != self.vr_disabled:
                    raise ValueError('VR通道限制已变化，请重新核对后开启')
                self._apply_vr(dt)
            else:
                self._apply(dt)
            r._check_original_feedback()
        except Exception as exc:
            self.stop(('VR' if self.mode == 'vr' else '网页') + '动作已停止：' + str(exc))
        self.tick_ms = round((time.perf_counter() - started) * 1000, 1)

    def _read(self, port, i):
        r = self.runtime; key = f'{port}:{i}'
        if key in self.blocked or key in r.service_blocked:
            raise ValueError(self.blocked.get(key) or r.service_blocked[key])
        p = r.session.ports[port]
        expected_mode = 1 if port == 'COM4' and i in (9, 10) else 0
        data, check = confirm_temperature(read_registers(p, i), lambda: read_registers(p, i), 50,
            other_ok=lambda q: q[33] == expected_mode and q[40] == 1 and not q[65] and 10.5 <= q[62]/10 <= 13)
        if check:
            r.log('web_temperature', key + '同电机新包温度确认', key=key, confirmation=check)
        if (data[33] != (1 if port == 'COM4' and i in (9, 10) else 0)
                or data[40] != 1 or data[65] or not normal_temperature(data[63], 50)):
            raise ValueError(key + '模式、扭矩、状态或温度异常')
        r._supply_check(key, data)
        if not 10.5 <= data[62] / 10 <= 13:
            self.voltage_bad[key] = self.voltage_bad.get(key, 0) + 1
            if self.voltage_bad[key] >= 3:
                raise ValueError(key + '电压连续3份新读数异常')
        else:
            self.voltage_bad.pop(key, None)
        return data

    def _rate(self, port, i, value, dt, touched, gripper=False, rate_cap=None):
        r = self.runtime
        if not value:
            return
        touched.add((port, i)); self._read(port, i)
        cfg = r.settings.axis(port, i)
        direction = cfg['direction']
        if gripper:
            ref = r.reference(port, i)
            if not ref or ref.get('closed_raw') == ref.get('open_raw'):
                raise ValueError('夹爪开合端点未确认，请使用第6关节正负向按钮')
            direction = 1 if ref['closed_raw'] > ref['open_raw'] else -1
        # Per-axis settings remain an upper bound; browser speed is lower.
        cap = 12. if self.mode == 'keys' else VR_MAX_RATE_DEG_S
        if rate_cap is not None:
            cap = min(cap, rate_cap)
        speed = min(cap, cfg['joint_rate_deg_s']) if i != 6 else min(100., cfg['grip_raw_s']) * 360 / 4096
        r._direct(port, i, value * speed * self.scale * direction, dt)

    def _apply(self, dt):
        r = self.runtime; a = self.actions; touched = set()
        def axis(prefix, plus, minus):
            return int(prefix + plus in a) - int(prefix + minus in a)
        def direct(port, i, value, gripper=False):
            self._rate(port, i, value, dt, touched, gripper)
        sides = ('left', 'right') if self.arm == 'both' else (self.arm,)
        dx, dz = axis('arm.', 'extend', 'retract'), axis('arm.', 'up', 'down')
        plans = {side: self._plane(r.arm_ports()[side], dx, dz, dt) for side in sides} if dx or dz else {}
        for side in (('left', 'right') if self.arm == 'both' else (self.arm,)):
            port = r.arm_ports()[side]
            x, z = axis('arm.', 'extend', 'retract'), axis('arm.', 'up', 'down')
            joint_mode = any(s.startswith('joint.') for s in a)
            if (x or z) and joint_mode:
                raise ValueError('组合控制与逐关节控制不能同时发送')
            if x or z:
                for i, target in plans[side].items():
                    r._goal(port, i, target); touched.add((port, i))
            for i, positive, negative in ((1, 'pan_right', 'pan_left'), (4, 'pitch_down', 'pitch_up'),
                                           (5, 'roll_right', 'roll_left'), (6, 'close', 'open')):
                if i == 4 and (x or z) and axis('arm.', positive, negative):
                    raise ValueError('末端平移时先松开腕俯仰')
                direct(port, i, axis('arm.', positive, negative), gripper=i == 6)
            for i in range(1, 7):
                direct(port, i, axis(f'joint.{i}.', 'plus', 'minus'))
        direct('COM3', 7, axis('head.', 'right', 'left'))
        direct('COM3', 8, axis('head.', 'down', 'up'))
        for key in list(r.moving):
            if key not in touched:
                r._hold_one(key); r.direct_targets.pop(key, None)
        self._base(axis('base.', 'forward', 'back') * -.04 * self.scale,
                   axis('base.', 'left', 'right') * .25 * self.scale)

    def _base(self, v, omega):
        r = self.runtime
        if v or omega:
            readings = {}
            for i in (9, 10):
                readings[i] = word(self._read('COM4', i), 56)
            peak = max(abs(n) for n in r.wheel_command(v, omega).values())
            if peak > 160:
                v *= 160 / peak; omega *= 160 / peak
            command = r.wheel_command(v, omega)
            signature = tuple(command[i] for i in (9, 10))
            if self.base_window and self.base_window['signature'] == signature:
                if self.clock() - self.base_window['at'] >= .8:
                    deltas = {i: (readings[i] - self.base_window['positions'][i] + 2048) % 4096 - 2048 for i in (9, 10)}
                    for i in (9, 10):
                        sign = 0 if not command[i] else 1 if command[i] > 0 else -1
                        if sign and deltas[i] * sign < 2:
                            raise ValueError(f'轮{i}未按目标方向转动，已停止')
                    if all(abs(command[i]) >= 40 for i in (9, 10)):
                        travel = [abs(deltas[i] / command[i]) for i in (9, 10)]
                        if abs(travel[0] - travel[1]) > max(.08, .45 * max(travel)):
                            raise ValueError('双轮位移与目标比例明显不符，已停止')
                    self.base_window = None
            else:
                self.base_window = None
            if not self.base_window:
                self.base_window = {'at': self.clock(), 'positions': readings, 'signature': signature}
        else:
            self.base_window = None
        r._drive_base(v, omega)

    def _apply_vr(self, dt):
        r = self.runtime; v = self.vr; touched = set()
        if 'head' not in self.vr_disabled:
            cap = VR_JOG_RATE_DEG_S if v['jog'] is not None else None
            # The installed head pan direction is reversed for the VR view.
            # Correct only this input path; do not mutate shared axis settings.
            self._rate('COM3', 7, -v['head'][0], dt, touched, rate_cap=cap)
            self._rate('COM3', 8, -v['head'][1], dt, touched, rate_cap=cap)
        for side, port in r.arm_ports().items():
            if side in self.vr_disabled:
                self.vr_arms[side] = {'state': 'unavailable', 'reason': self.vr_disabled[side]}
                continue
            if v['jog'] is not None:
                self.vr_arms[side] = {'state': 'idle', 'reason': None}
                if v['jog']['side'] == side:
                    i = v['jog']['joint']
                    reason = self._unusable(port, (i,))
                    self.vr_arms[side] = {'state': 'unavailable' if reason else 'adjusting', 'reason': reason}
                    if not reason:
                        self._rate(port, i, v['jog']['value'], dt, touched, rate_cap=VR_JOG_RATE_DEG_S)
                self._vr_grip(side, port, None, dt, touched)
            else:
                self._vr_arm(side, port, v['arms'].get(side), dt, touched)
                self._vr_grip(side, port, v['grip'].get(side), dt, touched)
        for key in list(r.moving):
            if key not in touched:
                r._hold_one(key); r.direct_targets.pop(key, None)
        drive, turn = v['base']
        self._base(0. if 'base' in self.vr_disabled else drive * -.04 * self.scale,
                   0. if 'base' in self.vr_disabled else turn * .25 * self.scale)

    def _unusable(self, port, ids):
        r = self.runtime
        for i in ids:
            key = f'{port}:{i}'
            if key in self.blocked or key in r.service_blocked:
                return self.blocked.get(key) or r.service_blocked[key]
        return None

    def _vr_anchor(self, port, a):
        r = self.runtime
        state = {'engage': a['engage'], 'state': 'unavailable'}
        reason = self._unusable(port, range(1, 6))
        if reason:
            return {**state, 'reason': '此臂有关节不可用：' + reason}
        refs = {i: r.reference(port, i) for i in (2, 3, 4)}
        if not all(refs.values()):
            return {**state, 'reason': '此臂运动学参考未就绪，需要整机标定的A/B/C参考姿态'}
        raw = {i: word(self._read(port, i), 56) for i in range(1, 6)}
        q = {i: refs[i]['reference_deg'] + (raw[i] - refs[i]['reference_raw']) * refs[i]['direction'] * 360 / 4096
             for i in (2, 3, 4)}
        try:
            geometry = vr_kinematics.anchor(q[2], q[3], q[4])
        except ValueError as exc:
            return {**state, 'reason': str(exc)}
        return {**state, 'state': 'following', 'reason': None, 'refs': refs, 'raw': raw, 'geometry': geometry,
                'targets': {i: float(raw[i]) for i in range(1, 6)},
                'd0': a['pos'], 'p0': a['pitch'], 'r0': a['roll']}

    def _vr_arm(self, side, port, a, dt, touched):
        r = self.runtime; st = self.vr_arms[side]
        if not a or not a['engage']:
            if st['state'] != 'idle':
                self.vr_arms[side] = {'state': 'idle', 'reason': None}
            return
        if st.get('engage') != a['engage']:
            st = self.vr_arms[side] = self._vr_anchor(port, a)
        if st['state'] == 'unavailable':
            return
        touched.update((port, i) for i in range(1, 6))
        ratio = self.vr['ratio']
        displacement = [a['pos'][k] - st['d0'][k] for k in range(3)]
        if any(abs(d) > vr_kinematics.MAX_DELTA_M for d in displacement):
            st.update(state='boundary', reason='手柄位移超过0.6米，按追踪跳变丢弃')
            return
        delta = [ratio * d for d in displacement]
        pitch = vr_kinematics.wrap(a['pitch'] - st['p0']) if a['orient'] else 0.
        roll = vr_kinematics.wrap(a['roll'] - st['r0']) if a['orient'] else 0.
        desired = {}; reasons = []
        def to_raw(i, deg):
            ref = st['refs'][i]
            return ref['reference_raw'] + (deg - ref['reference_deg']) * ref['direction'] * 4096 / 360
        try:
            pan, lift, elbow, wrist, edge = vr_kinematics.follow(st['geometry'], delta, pitch)
        except ValueError as exc:
            pan = None; reasons.append(str(exc))
        if pan is not None:
            if edge:
                reasons.append('手已超出手臂可达范围，手臂停在最远处')
            desired.update({2: to_raw(2, lift), 3: to_raw(3, elbow), 4: to_raw(4, wrist)})
            # Positive pan/roll raw times the axis direction is "right", as on the keyboard page.
            for i, deg in ((1, -pan), (5, roll)):
                desired[i] = st['raw'][i] + deg * r.settings.axis(port, i)['direction'] * 4096 / 360
        limited = []
        for group in ((2, 3, 4), (1,), (5,)):
            group = [i for i in group if i in desired]
            gaps = {i: desired[i] - st['targets'][i] for i in group}
            # One shared fraction keeps the plane joints on one path; a joint
            # limit shortens the step for the whole group instead of bending it.
            k = 1.
            for i in group:
                e = r.envelopes[(port, i)]
                end = e['max'] if gaps[i] > 0 else e['min']
                if not e['min'] <= desired[i] <= e['max']:
                    limited.append(e['label'])
                    k = min(k, max(0., (end - st['targets'][i]) / gaps[i])) if gaps[i] else 0.
            gaps = {i: g * k for i, g in gaps.items()}
            if not group or all(abs(g) < 1 for g in gaps.values()):
                continue
            k = 1.
            for i in group:
                rate = min(VR_MAX_RATE_DEG_S, r.settings.axis(port, i)['joint_rate_deg_s']) * self.scale
                step = rate * dt * 4096 / 360
                if abs(gaps[i]) > step:
                    k = min(k, step / abs(gaps[i]))
            proposal = {i: st['targets'][i] + k * gaps[i] for i in group}
            positions = {i: word(self._read(port, i), 56) for i in group}
            if any(abs(proposal[i] - positions[i]) > r.settings.axis(port, i)['follow_delta_raw'] - 8 for i in group):
                continue  # The servo is behind; the stall check still watches it.
            for i in group:
                r._goal(port, i, proposal[i]); st['targets'][i] = proposal[i]
        if limited:
            reasons.append('、'.join(dict.fromkeys(limited)) + '到达限位')
        st['state'] = 'boundary' if reasons else 'following'
        st['reason'] = '；'.join(reasons) or None

    def _vr_grip(self, side, port, value, dt, touched):
        r = self.runtime; g = self.vr_grip[side]
        if value is None:
            # The caller will hold the encoder position. Reconnection requires
            # a fresh trigger press, just like the beginning of a session.
            g.update(active=False, target=None, hold=None)
            return
        if not g['active'] and value < .05:
            return
        ref = r.reference(port, 6)
        if not g['active']:
            reason = self._unusable(port, (6,))
            if not reason and (not ref or ref.get('closed_raw') == ref.get('open_raw')):
                reason = '夹爪开合端点未确认'
            g['reason'] = reason
            if reason:
                return
            g.update(active=True, target=float(word(self._read(port, 6), 56)), hold=None)
        touched.add((port, 6))
        desired = ref['open_raw'] + value * (ref['closed_raw'] - ref['open_raw'])
        closing = 1 if ref['closed_raw'] > ref['open_raw'] else -1
        if g['hold'] is not None:
            if (desired - g['hold']) * closing > -8:
                return  # Still pressing past the contact point: keep the light squeeze.
            g['hold'] = None
        rate = min(VR_MAX_GRIP_RAW_S, r.settings.axis(port, 6)['grip_raw_s']) * self.scale
        gap = desired - g['target']
        if abs(gap) < 1:
            return
        proposal = g['target'] + max(-rate * dt, min(rate * dt, gap))
        self._read(port, 6)
        if r._goal(port, 6, proposal):
            g['target'] = proposal

    def _plane(self, port, dx, dz, dt):
        r = self.runtime; keys = [(port, i) for i in (2, 3, 4)]
        refs = {i: r.reference(port, i) for _, i in keys}
        if not all(refs.values()):
            raise ValueError('此臂运动学参考未就绪，请使用逐关节控制')
        q = {}; positions = {}
        for _, i in keys:
            ref = refs[i]; positions[i] = word(self._read(port, i), 56)
            q[i] = ref['reference_deg'] + (positions[i] - ref['reference_raw']) * ref['direction'] * 360 / 4096
        x, z = forward(q[2], q[3])
        # Never change IK branches when a folded pose is outside the model.
        lift, elbow = inverse(x, z)
        if abs(lift - q[2]) > 1 or abs(elbow - q[3]) > 1:
            raise ValueError('当前折叠姿态不适合末端联动，请先用逐关节调整')
        lift, elbow = inverse(x + dx * .012 * self.scale * dt, z + dz * .012 * self.scale * dt)
        candidate = {2: lift, 3: elbow, 4: q[2] + q[3] + q[4] - lift - elbow}
        targets = {}
        for i, angle in candidate.items():
            ref = refs[i]; target = ref['reference_raw'] + (angle - ref['reference_deg']) * ref['direction'] * 4096 / 360
            e = r.envelopes[(port, i)]; delta = abs(target - positions[i])
            cfg = r.settings.axis(port, i)
            if (not e['min'] <= target <= e['max'] or delta > min(72, cfg['follow_delta_raw'])
                    or delta > cfg['joint_rate_deg_s'] * dt * 4096 / 360 + 1):
                raise ValueError('末端联动被当前关节限位或速度限制阻挡')
            targets[i] = target
        # Both selected arms are validated before the first output write.
        return targets
