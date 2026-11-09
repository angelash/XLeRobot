"""Bounded motion executor for vision-guided skills (fast grasp).

The Studio-side skill (deployment/grasp) decides *what* to do from live camera
frames; this module, inside the only serial owner, decides whether a requested
motion is allowed and performs it with the self-test's safe move: current
limits only, an explicit torque ceiling of at most 75 %, stop on obstruction,
abort on gamepad input or a stop request. A session holds the same exclusion as
an assistant run, so goals - including a gripper squeeze - persist between
steps. Cleanup (hold every joint where it is, restore torque ceilings) runs once
when the session ends: on request, on lease expiry, on abort, on a hazard.
"""
from __future__ import annotations

import json
import math
import threading
import time
import uuid
from collections import deque
from pathlib import Path

from .control_runtime import now
from .limit_selftest import Aborted, Blocked
from .torque_control import word, write_register
from .whole_calibration import atomic_json
from .temperature_guard import POLICY_VERSION, confirm_temperature, normal_temperature

MAX_STEP_RAW = 900        # largest change of one joint in one step
MAX_STEP_S = 15.0         # a step must finish within this nominal time
EXPLICIT_TORQUE_MAX = 750  # 5 points under the servo's 80 % overload threshold
EXPLICIT_TORQUE_MIN = 150
SPEED_DEG = (3.0, 30.0)
LEASE_S = (1.0, 10.0)
MAX_SESSION_S = 300.0
TEMP_STOP_C = 50
VOLT_STOP = 10.5
ARM_IDS = range(1, 7)
# Chassis: reverse only, inside a budget the user granted in words (2026-11-03:
# "向后移动10-20厘米完全没问题"). Each leg stops by wheel counts, and on a stall
# (rotation_scan contact stop). Nothing behind the robot is seen by a camera.
BASE_LEG_M = 0.05
REVERSE_BUDGET_M = 0.20
MAX_GRANTED_REVERSE_M = 1.00  # explicit 2026-11-09 user allowance; default remains20cm
# 2026-11-03 later instruction: inside the authorised range the chassis may move both ways in small
# legs. Net position relative to where the user placed the robot stays inside
# [-REVERSE_BUDGET_M, +FORWARD_LIMIT_M] (the desk is just ahead of the start, so not past it); a
# forward leg also needs the caller to state that the head camera showed the way clear.
FORWARD_LIMIT_M = 0.0


class ServoSession:
    def __init__(self, runtime, root, clock=time.monotonic, watchdog=True, authorization_root=None):
        self.runtime = runtime
        self.root = Path(root)
        self.authorization_root = Path(authorization_root) if authorization_root is not None else self.root
        self.clock = clock
        self.watchdog = watchdog
        self.step_lock = threading.Lock()
        self.state_lock = threading.RLock()
        self.active = False
        self.busy = False
        self.session_id = None
        self.label = ''
        self.dir = None
        self.lease_s = 4.0
        self.started = None
        self.last_call = None
        self.started_at = None
        self.finished_at = None
        self.steps = 0
        self.ended_reason = None
        self.last = None
        self.contacts = deque(maxlen=20)
        self._thread = None
        self.stationary_base = False
        self.wheel_park = None

    @property
    def lt(self):
        return self.runtime.limit_test

    # ---- lifecycle ---------------------------------------------------------
    def start(self, label='', confirmed=False, lease_s=4.0, stationary_base=False):
        r, lt = self.runtime, self.lt
        with self.state_lock, r.io:
            if self.active:
                raise ValueError('已有抓取会话在进行')
            if lt.running:
                raise ValueError('自检或助手运行正在进行')
            if confirmed is not True:
                raise ValueError('请确认手臂运动范围内无人、工作区已清理')
            if r.controller['controller'] == 'online':
                raise ValueError('手柄接管中，先结束接管')
            if r.manual_calibration.active:
                raise ValueError('采集中，先结束采集')
            if getattr(r, 'manual_pose_active', False):
                raise ValueError('手动摆位中，先锁定姿态')
            if lt.require_pad and lt._pad_active():
                raise ValueError('手柄全部回中后再开始：会话中按手柄任意键会立即停止')
            r.scan()
            preparation = r.prepare_hardware(True)
            blocked = [b for b in preparation.get('blocked', []) if not (b['port'] == 'COM4' and b['id'] in (9, 10))]
            if blocked:
                raise ValueError('关节未就绪：' + '，'.join(f"{b['port']}:{b['id']} {b['reason']}" for b in blocked))
            self.stationary_base = stationary_base is True
            self.wheel_park = self._park_wheels() if self.stationary_base else None
            lt.running = True; lt.abort_reason = None; lt.error = None; lt.current = 'servo'; lt.live = {}
            lt.plan = []; lt.results = {}; lt.torque_backup = {}; lt.limit_backup = {}; lt.finalized = set()
            lt.arm_origin = {}; lt.events.clear(); lt.started_at = now(); lt.finished_at = None
            self.session_id = uuid.uuid4().hex[:12]
            self.label = str(label)[:80]
            self.lease_s = max(LEASE_S[0], min(LEASE_S[1], float(lease_s)))
            self.dir = self.root/(time.strftime('%Y%m%d-%H%M%S') + '-' + self.session_id[:4])
            self.dir.mkdir(parents=True, exist_ok=True)
            self.active = True; self.busy = False; self.steps = 0; self.ended_reason = None; self.last = None
            self.contacts.clear()
            self.started = self.last_call = self.clock(); self.started_at = now(); self.finished_at = None
            atomic_json(self.dir/'session.json', {'label': self.label, 'session_id': self.session_id, 'started_at': self.started_at,
                                                  'lease_s': self.lease_s})
            if self.wheel_park:atomic_json(self.dir/'wheel-park.json',self.wheel_park)
            lt._log(f'抓取会话开始：{self.label or self.session_id}')
        if self.watchdog:
            self._thread = threading.Thread(target=self._watch, daemon=True, name='servo-session-watchdog')
            self._thread.start()
        return self.snapshot()

    def _check(self, session_id):
        if not self.active:
            raise ValueError('抓取会话未开始或已结束' + (f'：{self.ended_reason}' if self.ended_reason else ''))
        if session_id != self.session_id:
            raise ValueError('会话编号不符')

    def end(self, session_id, reason='完成'):
        self._check(session_id)
        with self.step_lock:
            self._end(str(reason or '完成')[:120])
        return self.snapshot()

    def stop(self, reason='页面点击立即停止'):
        """Callable at any time by any local caller: aborts a running step."""
        if self.active:
            self.lt.abort_reason = reason
            if self.step_lock.acquire(timeout=3):
                try:
                    self._end(reason)
                finally:
                    self.step_lock.release()
        return self.snapshot()

    def _end(self, reason):
        with self.state_lock:
            if not self.active:
                return
            lt = self.lt
            try:
                lt._cleanup()
            finally:
                if self.stationary_base:self._zero_wheels()
                lt.running = False; lt.current = None; lt.finished_at = now()
                self.active = False; self.ended_reason = reason; self.finished_at = now()
            try:
                atomic_json(self.dir/'result.json', {'label': self.label, 'session_id': self.session_id, 'started_at': self.started_at,
                                                     'finished_at': self.finished_at, 'ended': reason, 'steps': self.steps,
                                                     'contacts': list(self.contacts)})
            except OSError:
                pass
            lt._log(f'抓取会话结束：{reason}')

    def _watch(self):
        while True:
            time.sleep(0.2)
            if not self.active:
                return
            self.watchdog_tick()

    def watchdog_tick(self):
        """One supervision pass; returns why the session was ended, if it was."""
        with self.state_lock:
            if not self.active:
                return None
            idle = self.clock() - self.last_call
            age = self.clock() - self.started
            busy = self.busy
        lt = self.lt
        if lt.require_pad and lt._pad_active():
            reason = '检测到手柄输入，抓取会话已立即停止'
            self.stop(reason)
            return reason
        if age > MAX_SESSION_S:
            reason = '会话超过最长时间'
            self.stop(reason)
            return reason
        if self.stationary_base:
            hazard=self._parked_hazard()
            if hazard:
                self.stop(hazard)
                return hazard
        if not busy and idle > self.lease_s and self.step_lock.acquire(blocking=False):
            try:
                reason = f'租约超时：抓取端{idle:.1f}秒未发送'
                self._end(reason)
                return reason
            finally:
                self.step_lock.release()
        return None

    # ---- motion ------------------------------------------------------------
    def _zero_wheels(self):
        failures=[]
        with self.runtime.io:
            for address,value,size in ((46,0,2),(40,0,1)):
                for i in (9,10):
                    try:write_register(self.runtime.session.ports['COM4'],i,address,value,size)
                    except Exception as exc:failures.append(f'{i}:{exc}')
        if failures:self.lt._log('静止底盘停车写入异常：'+'; '.join(failures))
        return failures

    def _wheel_row(self):
        row={'at':time.monotonic(),'wheels':{}}
        for i in (9,10):
            reg=self.lt._read('COM4',i)
            reg,check=confirm_temperature(reg,lambda:self.lt._read('COM4',i),45,
                other_ok=lambda r:r[40]==0 and r[33]==1 and not r[65] and 11.5<=r[62]/10<=12.6)
            row['wheels'][i]={'position':word(reg,56),'speed':word(reg,58),
                             'goal_velocity':word(reg,46),'torque':reg[40],
                             'mode':reg[33],'status':reg[65],
                             'voltage':reg[62]/10,'temperature':reg[63]}
            if check:
                row['wheels'][i]['temperature_check']=check
                if self.active and self.dir:
                    with (self.dir/'wheel-temperature-checks.jsonl').open('a',encoding='utf8') as f:
                        f.write(json.dumps({'at':now(),'motor':i,'check':check},ensure_ascii=False)+'\n')
        return row

    def _park_wheels(self):
        from deployment.grasp.recovery import parked_wheels
        if self._zero_wheels():raise ValueError('静止底盘停车写入失败，禁止手臂动作')
        rows=[];deadline=time.monotonic()+2.5
        while time.monotonic()<deadline:
            rows.append(self._wheel_row())
            if parked_wheels(rows):
                return {'verified':True,'policy':'torque-off-encoder-window-v1','rows':rows}
            time.sleep(.08)
        raise ValueError('静止底盘未取得600ms编码器稳定证据，禁止手臂动作')

    def _parked_hazard(self):
        reason=None;row=None
        try:
            row=self._wheel_row()
            for i,m in row['wheels'].items():
                if (m['torque']!=0 or m['mode']!=1 or m['goal_velocity']!=0 or m['status']
                        or (m['speed']&0x7fff)>50 or not normal_temperature(m['temperature'],45)
                        or not 11.5<=m['voltage']<=12.6):
                    reason=f'静止底盘轮{i}状态变化，停止手臂';break
                origin=self.wheel_park['rows'][0]['wheels'][i]['position']
                if abs((m['position']-origin+2048)%4096-2048)>2:
                    reason=f'静止底盘轮{i}编码器移动，停止手臂';break
        except Exception as exc:reason='静止底盘回读失败：'+str(exc)
        if reason and self.dir:
            try:
                with (self.dir/'wheel-guard.jsonl').open('a',encoding='utf8') as f:
                    f.write(json.dumps({'at':now(),'reason':reason,'row':row},ensure_ascii=False)+'\n')
            except OSError:pass
        return reason

    def _reverse_used(self):
        path = self.authorization_root/'base-authorizations.json'
        paths = [path]
        if not path.exists() and self.authorization_root != self.root:
            # Historical logs used startup-date folders. Bootstrap the durable
            # ledger without discarding older consumed travel on a day change.
            paths = sorted(self.authorization_root.glob('????-??-??/servo-sessions/base-authorizations.json'))
        merged = {}
        try:
            for source in paths:
                if not source.exists():continue
                data = json.loads(source.read_text(encoding='utf-8'))
                if not isinstance(data, dict) or any(not isinstance(r,dict) for r in data.values()):
                    raise ValueError('授权台账格式无效，不能按空额度继续')
                def remaining(row):
                    values=[row.get(k,default) for k,default in (
                        ('granted_m',REVERSE_BUDGET_M),('used_m',0.),('forward_m',0.),('forward_correction_m',0.))]
                    if any(type(v) not in (int,float) or not math.isfinite(v) or v<0 for v in values):
                        raise ValueError('授权台账数值无效')
                    grant,used,forward,correction=values
                    return grant+forward-max(0.,used-correction)
                for auth,row in data.items():
                    balance=remaining(row)
                    # Conflicting legacy dates retain the smallest allowance;
                    # the original records stay intact for later review.
                    if auth not in merged or balance<=remaining(merged[auth]):merged[auth]=row
            return merged
        except (OSError, ValueError) as exc:
            raise ValueError('后退授权台账读取失败，保持停止：'+str(exc)) from exc

    def reverse_budget(self, authorization):
        row = self._reverse_used().get(str(authorization or '').strip(), {})
        row = row if isinstance(row, dict) else {}
        # forward_correction_m: legs recorded as reverse that the cameras showed moved toward the front
        # (sign error before the chassis direction was verified); the budget is net backward travel.
        back = max(0.0, float(row.get('used_m', 0.0)) - float(row.get('forward_correction_m', 0.0)))
        net = float(row.get('forward_m', 0.0)) - back          # chassis position relative to the start, + ahead
        granted = row.get('granted_m', REVERSE_BUDGET_M)
        if type(granted) not in (int, float) or not math.isfinite(granted) or not 0 < granted <= MAX_GRANTED_REVERSE_M:
            granted = REVERSE_BUDGET_M
        return {'budget_m': granted, 'used_m': round(max(0.0, -net), 4),
                'left_m': round(max(0.0, granted + net), 4), 'net_m': round(net, 4),
                'forward_left_m': round(max(0.0, FORWARD_LIMIT_M - net), 4)}

    def grant_reverse(self, authorization, distance_m, confirmed=False,
                      previous_distance_m=None, extension_authorization=''):
        """Record an explicit grant; extending it retains all consumed travel.

        The supervisor binds the user's words to the current physical start.
        Translation remains in <=5 cm legs. Turning does not make the budget
        a map: after a turn, re-establish the original heading before travel.
        An increase requires the exact prior grant and new authorization text.
        """
        auth = str(authorization or '').strip()
        if confirmed is not True or len(auth) < 8:
            raise ValueError('需要用户授权原文及当前起点确认')
        if type(distance_m) not in (int, float) or not math.isfinite(distance_m) or not .05 <= distance_m <= MAX_GRANTED_REVERSE_M:
            raise ValueError('后退授权需为5–100cm')
        extending = previous_distance_m is not None
        if extending and (type(previous_distance_m) not in (int, float)
                          or not math.isfinite(previous_distance_m)
                          or not isinstance(extension_authorization, str)
                          or len(extension_authorization.strip()) < 8):
            raise ValueError('扩展额度需提供原额度和用户追加授权原文')
        with self.state_lock, self.runtime.io:
            if self.active or self.lt.running or self.runtime.controller['controller'] == 'online':
                raise ValueError('动作或接管中不能登记后退授权')
            used = self._reverse_used()
            row = used.get(auth, {})
            if not isinstance(row, dict):
                raise ValueError('已有授权记录无效，需要先核验')
            prior = row.get('granted_m', REVERSE_BUDGET_M)
            if extending:
                # Compare the old value and retain every consumed leg: never
                # create another anchor to renew a spent physical allowance.
                if auth not in used or prior != previous_distance_m or distance_m <= prior:
                    raise ValueError('原额度已变化、授权不存在或新额度未增加')
                row.setdefault('extensions', []).append({'at': now(), 'previous_m': prior,
                    'granted_m': float(distance_m), 'authorization': extension_authorization.strip()[:1000]})
            elif auth in used and prior != distance_m:
                raise ValueError('已有授权额度不能修改或重置')
            row.update(granted_m=float(distance_m), grant_at=row.get('grant_at') or now(),
                       boundary='current_start_and_original_heading', authorization=auth[:1000])
            used[auth] = row
            self.authorization_root.mkdir(parents=True,exist_ok=True)
            atomic_json(self.authorization_root/'base-authorizations.json', used)
            return self.reverse_budget(auth)

    def base(self, session_id, distance_m, authorization='', max_raw=160, label='', forward_clear=False):
        """One chassis leg inside the session (wheels only, arms held): reverse inside the budget, or a
        forward leg back toward the start, at most FORWARD_LIMIT_M past it."""
        self._check(session_id)
        if self.stationary_base:raise ValueError('静止底盘会话禁止行驶；移动需另开明确的行驶会话')
        if (isinstance(distance_m, bool) or not isinstance(distance_m, (int, float)) or not math.isfinite(distance_m)
                or not 0.005 <= abs(distance_m) <= BASE_LEG_M + 1e-9):
            raise ValueError(f'底盘每段只接受0.5–{BASE_LEG_M*100:g}cm（后退取负值）')
        if distance_m > 0 and forward_clear is not True:
            raise ValueError('前进需先用头部相机确认前方通道无人无障碍（forward_clear）')
        auth = str(authorization or '').strip()
        if len(auth) < 8:
            raise ValueError('底盘移动需要附上用户授权原文')
        if type(max_raw) is not int or not 60 <= max_raw <= 220:
            raise ValueError('轮速需在60–220')
        from .rotation_scan import chassis_direction
        if chassis_direction()[1] != 'camera_verified':
            raise ValueError('底盘前后方向未经相机核实，不执行后退（先写入chassis-direction.json）')
        budget = self.reverse_budget(auth)
        if distance_m < 0 and abs(distance_m) > budget['left_m'] + 1e-9:
            raise ValueError(f'本授权后退额度{budget["budget_m"]*100:g}cm，已用{budget["used_m"]*100:.1f}cm')
        if distance_m > 0 and budget['net_m'] + distance_m > FORWARD_LIMIT_M + 1e-9:
            raise ValueError(f'前进会超出授权起点前方{FORWARD_LIMIT_M*100:g}cm（当前净位置{budget["net_m"]*100:+.1f}cm）')
        if not self.step_lock.acquire(blocking=False):
            raise ValueError('上一步尚未完成')
        try:
            self._check(session_id)
            self.busy = True; self.last_call = self.clock()
            begun = time.perf_counter()
            from .rotation_scan import bounded_travel
            try:
                result = bounded_travel(self.runtime, self.lt._check_abort, float(distance_m), max_raw)
            except Aborted as exc:
                self._end(str(exc))
                raise ValueError(str(exc)) from None
            except Exception as exc:  # noqa: BLE001 - every wheel fault ends the session holding the arms
                try:
                    with open(self.dir/'steps.jsonl', 'a', encoding='utf-8') as f:
                        f.write(json.dumps({'n': self.steps, 'at': now(), 'label': str(label or '底盘后退')[:80], 'base_error': str(exc),
                                            'evidence': getattr(exc, 'rotation', None)}, ensure_ascii=False, default=str) + '\n')
                except OSError:
                    pass
                self._end('底盘后退失败：' + str(exc)[:80])
                raise ValueError('底盘后退失败：' + str(exc)) from None
            finally:
                used = self._reverse_used()
                row = used.get(auth) if isinstance(used.get(auth), dict) else {'used_m': 0.0, 'legs': 0}
                if distance_m < 0:
                    row['used_m'] = round(float(row.get('used_m', 0.0)) + abs(distance_m), 4)
                else:
                    row['forward_m'] = round(float(row.get('forward_m', 0.0)) + distance_m, 4)
                row['legs'] = int(row.get('legs', 0)) + 1
                row['last_at'] = now()
                used[auth] = row
                try:
                    self.authorization_root.mkdir(parents=True,exist_ok=True)
                    atomic_json(self.authorization_root/'base-authorizations.json', used)
                except OSError:
                    pass
            contact = None
            if result.get('stopped_by') == 'stall':
                contact = {'joint': 'base', 'reason': (result.get('stall') or {}).get('message', '底盘疑似碰撞'), 'at': now()}
                self.contacts.append(contact)
                self.lt._log('抓取会话底盘后退受阻：' + contact['reason'])
            joints = self.telemetry()
            summary = {k: result.get(k) for k in ('requested_distance_m', 'wheel_distance_m_estimate', 'wheel_delta_raw',
                                                  'stopped_by', 'duration_s', 'target_counts', 'radius_m_used')}
            summary['read_glitches'] = len(result.get('read_glitches') or [])
            entry = {'n': self.steps, 'at': now(), 'label': str(label or '底盘后退')[:80], 'base': summary,
                     'authorization': auth[:120], 'elapsed_ms': round((time.perf_counter() - begun)*1000),
                     'contact': contact, 'joints': joints, 'budget': self.reverse_budget(auth)}
            self.steps += 1; self.last = entry
            try:
                with open(self.dir/'steps.jsonl', 'a', encoding='utf-8') as f:
                    f.write(json.dumps({**entry, 'base_full': result}, ensure_ascii=False, default=str) + '\n')
            except OSError:
                pass
            hazard = self._hazard(joints)
            if hazard:
                entry['ended'] = hazard
                self._end(hazard)
            self.last_call = self.clock()
            return entry
        finally:
            self.busy = False
            self.step_lock.release()

    def step(self, session_id, targets, torque=None, speed_deg=12, label=''):
        self._check(session_id)
        if self.stationary_base:
            hazard=self._parked_hazard()
            if hazard:
                self.stop(hazard)
                raise ValueError(hazard)
        if not isinstance(targets, dict) or not targets or len(targets) > 8:
            raise ValueError('需要1–8个关节目标')
        if not self.step_lock.acquire(blocking=False):
            raise ValueError('上一步尚未完成')
        try:
            self._check(session_id)
            self.busy = True; self.last_call = self.clock()
            lt = self.lt
            speed = float(speed_deg)
            if not SPEED_DEG[0] <= speed <= SPEED_DEG[1]:
                raise ValueError(f'速度需在{SPEED_DEG[0]:g}–{SPEED_DEG[1]:g}°/s')
            ceiling = None
            if torque is not None:
                if isinstance(torque, bool) or not isinstance(torque, (int, float)):
                    raise ValueError('扭矩上限需为数字')
                ceiling = max(EXPLICIT_TORQUE_MIN, min(EXPLICIT_TORQUE_MAX, int(torque)))
            resolved, start = {}, {}
            for key, value in targets.items():
                port, i = self.runtime.agent.resolve(key)
                if port == 'COM4' and i in (9, 10):
                    raise ValueError('抓取会话不驱动底盘轮')
                if isinstance(value, bool) or not isinstance(value, (int, float)):
                    raise ValueError(f'{key}目标需为数字')
                reg = lt._read(port, i)
                low, high, pos = word(reg, 9), word(reg, 11), word(reg, 56)
                target = int(round(value))
                if not low <= target <= high:
                    raise ValueError(f'{key}目标{target}超出当前限位{low}–{high}')
                if abs(target - pos) > MAX_STEP_RAW:
                    raise ValueError(f'{key}单步变化{abs(target - pos)}超过{MAX_STEP_RAW}计数')
                if abs(target - pos)*360/4096/speed > MAX_STEP_S:
                    raise ValueError(f'{key}按{speed:g}°/s预计超过{MAX_STEP_S:g}秒，请分段或提高速度')
                resolved[(port, i)] = target; start[(port, i)] = pos
            begun = time.perf_counter(); contact = None
            try:
                lt._check_abort()
                lt._move_to(resolved, speed, label or '抓取会话移动', False, ceiling)
            except Blocked as exc:
                contact = {'joint': self._name(exc.port, exc.id), 'port': exc.port, 'id': exc.id, **exc.detail,
                           'from': start.get((exc.port, exc.id)), 'settle_ms': lt._settle(exc.port, exc.id), 'at': now()}
                self.contacts.append(contact)
                lt._log(f'抓取会话受阻：{contact["joint"]}在{contact.get("position")}停住（目标{contact.get("target")}），保持原地')
            except Aborted as exc:
                self._end(str(exc))
                raise ValueError(str(exc)) from None
            except Exception as exc:
                self._end('动作失败：' + str(exc))
                raise
            joints = self.telemetry()
            entry = {'n': self.steps, 'at': now(), 'label': str(label)[:80],
                     'targets': {self._name(p, i): v for (p, i), v in resolved.items()}, 'torque': ceiling, 'speed_deg': speed,
                     'elapsed_ms': round((time.perf_counter() - begun)*1000), 'contact': contact, 'joints': joints}
            self.steps += 1; self.last = entry
            try:
                with open(self.dir/'steps.jsonl', 'a', encoding='utf-8') as f:
                    f.write(json.dumps(entry, ensure_ascii=False) + '\n')
            except OSError:
                pass
            hazard = self._hazard(joints)
            if hazard:
                entry['ended'] = hazard
                self._end(hazard)
            self.last_call = self.clock()
            return entry
        finally:
            self.busy = False
            self.step_lock.release()

    def hold(self, session_id):
        self._check(session_id)
        if self.stationary_base:
            hazard=self._parked_hazard()
            if hazard:
                self.stop(hazard)
                raise ValueError(hazard)
        with self.state_lock:
            self.last_call = self.clock()
        return {'session_id': self.session_id, 'active': self.active, 'joints': self.telemetry()}

    # ---- readback ----------------------------------------------------------
    def _name(self, port, i):
        for side, p in self.runtime.arm_ports().items():
            if p == port and i in ARM_IDS:
                return f'{side}:{i}'
        if port == 'COM3' and i in (7, 8):
            return f'head:{i}'
        return f'{port}:{i}'

    def telemetry(self):
        out = {}
        joints = [(p, j) for p in self.runtime.arm_ports().values() for j in ARM_IDS] + [('COM3', 7), ('COM3', 8)]
        for port, i in joints:
            try:
                reg = self.lt._read(port, i)
                reg, temperature_check = confirm_temperature(
                    reg, lambda: self.lt._read(port, i), TEMP_STOP_C,
                    other_ok=lambda r:r[40]==1 and not r[65] and r[62]/10>=VOLT_STOP,
                    check_abort=self.lt._check_abort)
            except Exception as exc:  # noqa: BLE001 - reported per joint
                out[self._name(port, i)] = {'error': str(exc)[:80]}
                continue
            load = word(reg, 60)
            out[self._name(port, i)] = {'position': word(reg, 56), 'goal': word(reg, 42), 'load': load & 0x3ff,
                                        'load_sign': -1 if load & 0x400 else 1, 'torque_limit': word(reg, 48),
                                        'torque': reg[40], 'temperature': reg[63], 'voltage': reg[62]/10, 'status': reg[65]}
            if temperature_check:
                out[self._name(port, i)]['temperature_check'] = temperature_check
        return out

    def _hazard(self, joints):
        for name, j in joints.items():
            if 'error' in j:
                return f'{name}读数失败：{j["error"]}'
            if not normal_temperature(j['temperature'], TEMP_STOP_C):
                return f'{name}温度复读仍异常：{j["temperature"]}°C（停止线{TEMP_STOP_C}°C）'
            if 0 < j['voltage'] < VOLT_STOP:
                return f'{name}电压{j["voltage"]}V过低'
            if j['status'] & 32:
                return f'{name}过载保护触发'
            if not j['torque']:
                return f'{name}扭矩被关闭'
        return None

    def snapshot(self):
        with self.state_lock:
            last = dict(self.last) if self.last else None
            if last:
                last.pop('joints', None)
            return {'active': self.active, 'busy': self.busy, 'session_id': self.session_id if self.active else None,
                    'stationary_base':self.stationary_base,
                    'wheel_park':{'verified':bool(self.wheel_park),'policy':'torque-off-encoder-window-v1'},
                    'temperature_policy': POLICY_VERSION,
                    'label': self.label, 'steps': self.steps, 'lease_s': self.lease_s,
                    'idle_s': round(self.clock() - self.last_call, 2) if self.active and self.last_call else None,
                    'started_at': self.started_at, 'finished_at': self.finished_at, 'ended': self.ended_reason,
                    'dir': str(self.dir) if self.dir else None, 'last': last, 'contacts': list(self.contacts)[-5:]}
