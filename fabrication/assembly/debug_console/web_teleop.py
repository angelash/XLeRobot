"""Leased browser control inside the existing serial owner; no synthetic gamepad.

Every input replaces the complete held-key set. Session/sequence checks reject
late requests; only this channel's input renews its 650 ms dead-man lease.
"""
from __future__ import annotations

import math
import threading
import time
import uuid

from .control_runtime import now
from .temperature_guard import confirm_temperature, normal_temperature
from .torque_control import EXPECTED, read_registers, word
from .upstream_teleop import UpstreamTeleop, forward, inverse, neutral

LEASE_S = .65
MAX_SESSION_S = 1200
ARM_ACTIONS = ('extend', 'retract', 'up', 'down', 'pan_left', 'pan_right',
               'pitch_up', 'pitch_down', 'roll_left', 'roll_right', 'open', 'close')
ACTIONS = frozenset(['base.' + x for x in ('forward', 'back', 'left', 'right')] +
                    ['head.' + x for x in ('left', 'right', 'up', 'down')] +
                    ['arm.' + x for x in ARM_ACTIONS] +
                    [f'joint.{i}.{sign}' for i in range(1, 7) for sign in ('plus', 'minus')])


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

    def snapshot(self):
        # Session tokens are deliberately absent from shared/polled status.
        return {'active': self.active, 'seq': self.seq, 'lease_ms': int(LEASE_S * 1000),
                'actions': sorted(self.actions), 'arm': self.arm, 'reason': self.reason,
                'blocked': dict(self.blocked), 'scale': self.scale}

    def start(self, confirmed=False):
        r = self.runtime
        with r.io:
            if confirmed is not True:
                raise ValueError('请先确认已看清周围，手臂与底盘运动范围内无人')
            if self.active or r.controller['controller'] == 'online':
                raise ValueError('已有控制会话，请先结束后再开启网页控制')
            r._require_not_calibrating()
            if r.manual_pose_active or r.agent.active or r.native_menu.status()['busy']:
                raise ValueError('摆位、助手任务或手柄启动仍在进行')
            if r.gamepad.get('connected') and not neutral(r.gamepad):
                raise ValueError('请松开手柄所有输入；手柄输入会结束网页控制')
            r.capture_profile()
            motors = r.motors()
            if set(motors) != {(p, i) for p, ids in EXPECTED.items() for i in ids}:
                raise ValueError('需要两块原控制板和16颗电机完整应答')
            if any(motors[('COM4', i)]['goal_velocity'] for i in (9, 10)):
                raise ValueError('双轮目标非零，请先完成停车')
            prepared = r.prepare_hardware(True)
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
            self.started = self.last_input = self.last_tick = self.clock()
            self.active = True; self.reason = '网页控制已开启，等待按住按键或按钮'
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

    def stop(self, reason='已结束网页控制，保持当前位置', session_id=None):
        r = self.runtime
        with r.io:
            if session_id is not None and session_id != self.session_id:
                raise ValueError('会话编号不符，未停止其他会话')
            if not self.active:
                return self.snapshot()
            self.active = False; self.actions = frozenset(); self.session_id = None
            self.reason = reason
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
        try:
            self._apply(dt)
            r._check_original_feedback()
        except Exception as exc:
            self.stop('网页动作已停止：' + str(exc))

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

    def _apply(self, dt):
        r = self.runtime; a = self.actions; touched = set()
        def axis(prefix, plus, minus):
            return int(prefix + plus in a) - int(prefix + minus in a)
        def direct(port, i, value, gripper=False):
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
            speed = min(12., cfg['joint_rate_deg_s']) if i != 6 else min(100., cfg['grip_raw_s']) * 360 / 4096
            r._direct(port, i, value * speed * self.scale * direction, dt)
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
        v = axis('base.', 'forward', 'back') * -.04 * self.scale
        omega = axis('base.', 'left', 'right') * .25 * self.scale
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
