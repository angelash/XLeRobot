"""Grasp state machine: every decision from the tracked wrist view and servo readback.

No large model in the loop. The operator (or one model call) boxes the object
once; afterwards each motion is chosen from the tracker, the planar arm model
and the executor's telemetry. Phases and thresholds are documented in
docs/autonomy/FAST_GRASP_ZH.md; defaults come from the 2026-11-03 trials.
"""
from __future__ import annotations

import math
import time

from . import arm
from .vision import background_shift, edges, grip_window, in_jaws, ref_x, window_similarity

DEFAULTS = {
    # geometry (planar model, cm / degrees)
    'desk_z': -8.0, 'approach_clearance': 1.4, 'survey_x': 23.0, 'survey_height': 6.0, 'survey_pitch': 108.0,
    'approach_pitch': 105.0, 'min_bend': 22.0, 'path_clearance': 2.5,
    # lateral servo on the shoulder pan (px per raw count; negative: pan up moves the object left)
    'pan_gain': -1.9, 'pan_step_max': 30, 'align_tol_px': 14, 'align_max_iter': 8,
    'ref_x_far': 395.0, 'ref_x_near': 440.0, 'ref_bottom_far': 300.0, 'ref_bottom_near': 470.0,
    # when the object counts as ahead of the tips / at the tips / between the jaws. Near the jaws the
    # tracker's scale lags, so once the near edge reaches the tips the insertion is a measured distance.
    'clear_bottom': 335.0, 'tips_bottom': 360.0, 'insert_cm': 3.0, 'in_bottom': 465.0, 'in_top': 215.0,
    'advance_far_cm': 2.0, 'advance_near_cm': 1.0, 'advance_near_bottom': 380.0, 'advance_max_steps': 12,
    'approach_torque': 450, 'approach_speed': 6.0,
    # gripper
    'grip_open': 2045, 'grip_free': 1960, 'grip_step': 30, 'grip_empty': 1450, 'grip_contact_load': 30,
    'grip_settle_px': 4.0, 'grip_after_contact_max': 220, 'squeeze_lead': 55, 'squeeze_torque': 750,
    # lift / verify / place
    'lift_raw': 150, 'lift_extra_raw': 100, 'lift_speed': 5.0, 'hold_s': 1.5,
    'focal_px': 450.0, 'verify_min_expect_px': 35.0, 'verify_held_frac': 0.45, 'verify_left_frac': 0.7,
    # 2026-11-03 wrist frames: held lift 0.968, left on the desk 0.349, released and retreated 0.448
    'verify_held_sim': 0.75, 'verify_left_sim': 0.5,
    'place_torque': 450, 'place_speed': 3.0, 'release_target': 2000,
    'retreat_raw': 150, 'end_pose': 'upright', 'max_attempts': 2,
    'place_lateral_cm': 0.0, 'place_forward_cm': 0.0, 'stop_after': None,
    'observe_timeout_s': 2.5, 'settle_s': 0.12,
}
EDITABLE = {
    'desk_z': (-14.0, -2.0), 'approach_clearance': (0.8, 3.0), 'survey_x': (18.0, 30.0), 'survey_height': (3.0, 12.0),
    'approach_pitch': (95.0, 125.0), 'pan_gain': (-6.0, -0.4), 'align_tol_px': (6, 40), 'ref_x_far': (200.0, 520.0),
    'ref_x_near': (200.0, 560.0), 'clear_bottom': (250.0, 420.0), 'tips_bottom': (300.0, 440.0), 'insert_cm': (1.5, 5.0),
    'in_bottom': (380.0, 520.0), 'in_top': (120.0, 320.0),
    'grip_contact_load': (10, 120), 'squeeze_lead': (20, 65), 'squeeze_torque': (300, 750), 'lift_raw': (60, 300),
    'hold_s': (0.0, 10.0), 'max_attempts': (1, 3), 'place_lateral_cm': (-8.0, 8.0), 'place_forward_cm': (-5.0, 5.0),
}
UPRIGHT_GRIP = 1340


class GraspStopped(Exception):
    pass


class GraspFailure(Exception):
    pass


class GraspController:
    def __init__(self, executor, observer, recorder=None, config=None, stop_event=None, clock=time.monotonic, sleep=time.sleep):
        self.ex = executor
        self.obs = observer
        self.rec = recorder
        self.cfg = {**DEFAULTS, **(config or {})}
        self.stop_event = stop_event
        self.clock, self.sleep = clock, sleep
        self.phase = 'idle'
        self.note = ''
        self.q = {}
        self.events = []
        self.phases = []
        self.attempt = 0
        self.started = None
        self.result = None
        self.gain = float(self.cfg['pan_gain'])
        self.grasp_pose = None
        self.grip_box = None
        self.last_obs = None

    # ---- bookkeeping -------------------------------------------------------
    def _phase(self, name, note=''):
        now = self.clock()
        if self.phases and self.phases[-1]['end'] is None:
            self.phases[-1]['end'] = now
            self.phases[-1]['seconds'] = round(now - self.phases[-1]['start'], 2)
        self.phase, self.note = name, note
        self.phases.append({'phase': name, 'start': now, 'end': None, 'seconds': None})
        self._event('phase', phase=name, note=note)

    def _event(self, kind, **fields):
        item = {'t': round(self.clock() - (self.started or self.clock()), 2), 'kind': kind, 'phase': self.phase, **fields}
        self.events.append(item)
        if self.rec:
            self.rec.event(item)

    def _key(self, tag):
        if self.rec:
            self.rec.keyframe(tag, self.phase)

    def _stop_check(self):
        if self.stop_event is not None and self.stop_event.is_set():
            raise GraspStopped('收到停止请求')

    def _update(self, joints):
        for i, name in ((1, 'pan'), (2, 'lift'), (3, 'elbow'), (4, 'pitch'), (5, 'roll'), (6, 'grip')):
            j = joints.get(f'left:{i}') or {}
            if 'position' in j:
                self.q[name] = j['position']
                if name == 'grip':
                    self.q['grip_goal'] = j.get('goal'); self.q['grip_load'] = j.get('load', 0)

    def _move(self, targets, torque=None, speed=12.0, label=''):
        self._stop_check()
        entry = self.ex.step(targets, torque=torque, speed=speed, label=label)
        self._update(entry.get('joints') or {})
        self._event('step', label=label, targets=targets, torque=torque, contact=entry.get('contact'),
                    q={k: self.q.get(k) for k in ('pan', 'lift', 'elbow', 'pitch', 'grip')})
        if entry.get('ended'):
            raise GraspFailure('控制服务结束了会话：' + entry['ended'])
        self._stop_check()
        return entry

    def _arm(self, q3, torque=None, speed=12.0, label=''):
        return self._move({'left:2': q3[0], 'left:3': q3[1], 'left:4': q3[2]}, torque=torque, speed=speed, label=label)

    def _look(self, label=''):
        self._stop_check()
        obs = self.obs.observe(self.clock() + self.cfg['settle_s'], self.cfg['observe_timeout_s'])
        if obs is None:
            raise GraspFailure('相机画面中断或过期')
        self.last_obs = obs
        self._event('observe', label=label, ok=obs.get('ok'), bbox=obs.get('bbox'), score=obs.get('score'))
        return obs

    def _need_track(self, obs, what):
        if not obs.get('ok') or not obs.get('bbox'):
            raise GraspFailure(f'{what}时目标跟踪丢失（得分{obs.get("score")}）')
        return obs

    def _tip(self):
        return arm.fk(self.q['lift'], self.q['elbow'], self.q['pitch'])

    def _floor(self):
        return self.cfg['desk_z'] + self.cfg['path_clearance']

    def _refresh(self):
        held = self.ex.hold()
        self._update(held.get('joints') or {})

    # ---- paths -------------------------------------------------------------
    def _plan(self, target, final_floor=None):
        """Waypoints from the current pose to target that stay above the desk. A
        path may start below the safety height (after a grasp) but never dips
        below where each segment starts."""
        here = (self.q['lift'], self.q['elbow'], self.q['pitch'])
        floor = self._floor()
        final_floor = floor if final_floor is None else final_floor
        def ok(path):
            points = [here] + list(path)
            for k, (a, b) in enumerate(zip(points, points[1:])):
                tip, wrist = arm.segment_min_z(a, b)
                start = arm.fk(*a)
                limit = min(floor, start['tip'][1] - 0.3)
                if k == len(points) - 2:
                    limit = min(limit, final_floor)
                if tip < limit - 0.05 or wrist < min(floor, start['wrist'][1] - 0.3):
                    return False
            return True
        candidates = [[target], [arm.HIGH_OVER_DESK, target], [arm.UPRIGHT, arm.HIGH_OVER_DESK, target]]
        if here[1] > 2600 and here[0] < 1500:        # folded: unfold the elbow first (verified 2026-11-03)
            candidates.insert(0, [(here[0], 2400, here[2]), arm.UPRIGHT, arm.HIGH_OVER_DESK, target])
        for path in candidates:
            if ok(path):
                return path
        raise GraspFailure('找不到不低于桌面安全高度的路径，请先把手臂收回竖直')

    def _go(self, target, label, speed=12.0, torque=None, final_floor=None):
        for k, q3 in enumerate(self._plan(target, final_floor)):
            self._arm(q3, torque=torque, speed=speed, label=f'{label}{k + 1}')

    def survey_joints(self):
        c = self.cfg
        return arm.ik(c['survey_x'], c['desk_z'] + c['survey_height'], c['survey_pitch'])

    # ---- operations --------------------------------------------------------
    def run_survey(self):
        self.started = self.clock()
        self.ex.start('快速抓取：到观察位')
        try:
            self._phase('survey', '移动到观察位')
            self._refresh()
            self._go(self.survey_joints(), '观察位')
            self._move({'left:6': self.cfg['grip_open']}, torque=450, speed=20, label='张开夹爪')
            self._key('survey')
            self._phase('ready', '在观察位，等待框选目标')
            self.result = {'ok': True, 'phase': 'survey'}
        except GraspStopped as exc:
            self._phase('stopped', str(exc)); self.result = {'ok': False, 'error': str(exc)}
        except Exception as exc:  # noqa: BLE001 - every failure ends the session holding still
            self._phase('failed', str(exc)); self.result = {'ok': False, 'error': str(exc)}
        finally:
            self.ex.end(self.note or '结束')
        return self.result

    def run_pick_place(self):
        self.started = self.clock()
        self.ex.start('快速抓取：抓起并放下')
        reason = '完成'
        try:
            self._refresh()
            obs = self._need_track(self._look('开始'), '开始')
            self._key('start')
            if self.cfg.get('stop_after') == 'inserted':
                self._approach(obs)
                self._phase('rehearsal', '演练：已送入两指之间，不闭合，抬起收回')
                self._move({'left:2': self.q['lift'] - self.cfg['retreat_raw']}, torque=self.cfg['approach_torque'],
                           speed=self.cfg['lift_speed'], label='演练后抬离')
                self._home()
                self._phase('done', '演练完成（未夹取）')
                self.result = {'ok': True, 'rehearsal': True, 'seconds': round(self.clock() - self.started, 1),
                               'steps': sum(1 for e in self.events if e['kind'] == 'step')}
                return self.result
            for self.attempt in range(1, int(self.cfg['max_attempts']) + 1):
                try:
                    obs = self._approach(obs)
                    self._close(obs)
                    if self._lift():
                        break
                    self._abandon_grasp()
                    if self.attempt >= int(self.cfg['max_attempts']):
                        raise GraspFailure('两次提起都未带起目标')
                    obs = self._need_track(self._look('重试前'), '重试')
                except GraspFailure:
                    raise
            self._hold()
            self._place()
            self._home()
            self._phase('done', '抓起并放下完成')
            self.result = {'ok': True, 'attempts': self.attempt, 'seconds': round(self.clock() - self.started, 1),
                           'steps': sum(1 for e in self.events if e['kind'] == 'step')}
        except GraspStopped as exc:
            reason = str(exc)
            self.ex.stop(reason)
            self._phase('stopped', reason)
            self.result = {'ok': False, 'error': reason}
        except Exception as exc:  # noqa: BLE001
            reason = str(exc)
            self._phase('failed', reason)
            self._safe_raise()
            self.result = {'ok': False, 'error': reason, 'attempts': self.attempt}
        finally:
            self.ex.end(reason[:100])
            if self.phases and self.phases[-1]['end'] is None:
                self.phases[-1]['end'] = self.clock()
        return self.result

    # ---- phases ------------------------------------------------------------
    def _approach(self, obs):
        c = self.cfg
        self._phase('align', '对准目标（肩水平）')
        obs = self._align(obs)
        self._key('aligned')
        self._phase('lower', '降到贴近桌面的推进高度')
        z_app = c['desk_z'] + c['approach_clearance']
        tip = self._tip()['tip']
        for _ in range(3):
            if edges(obs['bbox'])['bottom'] < c['clear_bottom']:
                break
            back = arm.ik(tip[0] - 2.0, tip[1], c['survey_pitch'], min_bend=c['min_bend'])
            self._arm(back, torque=c['approach_torque'], speed=c['approach_speed'], label='后撤留出下降空间')
            tip = self._tip()['tip']
            obs = self._need_track(self._look('后撤后'), '后撤')
        else:
            raise GraspFailure('目标离指尖太近，无法安全下降')
        mid = arm.ik(tip[0], (tip[1] + z_app)/2, (c['survey_pitch'] + c['approach_pitch'])/2, min_bend=c['min_bend'])
        low = arm.ik(tip[0], z_app, c['approach_pitch'], min_bend=c['min_bend'])
        self._go(mid, '下降', speed=8, torque=c['approach_torque'], final_floor=z_app - 0.3)
        self._arm(low, torque=c['approach_torque'], speed=c['approach_speed'], label='下降到推进高度')
        obs = self._need_track(self._look('下降后'), '下降')
        self._key('lowered')
        self._phase('advance', '贴近桌面水平推进')
        at_tips = None
        for k in range(int(c['advance_max_steps'])):
            tip = self._tip()['tip']
            bottom = edges(obs['bbox'])['bottom']
            if at_tips is None and bottom >= c['tips_bottom']:
                at_tips = tip[0]
                self._event('at_tips', tip_x=round(tip[0], 2), bottom=round(bottom, 1))
                self._key('at_tips')
            inserted = None if at_tips is None else tip[0] - at_tips
            if in_jaws(obs['bbox'], c) or (inserted is not None and inserted >= c['insert_cm'] - 0.05):
                break
            near = bottom >= c['advance_near_bottom']
            step = c['advance_near_cm'] if near else c['advance_far_cm']
            if inserted is not None:
                step = max(0.5, min(c['advance_far_cm'], c['insert_cm'] - inserted))
            try:
                q3 = arm.ik(tip[0] + step, z_app, c['approach_pitch'], min_bend=c['min_bend'])
            except arm.Unreachable as exc:
                raise GraspFailure(f'目标超出够取范围：{exc}') from None
            entry = self._arm(q3, torque=c['approach_torque'], speed=c['approach_speed'], label=f'推进{step:g}cm')
            if entry.get('contact'):
                back = arm.ik(self._tip()['tip'][0] - 1.0, z_app + 0.5, c['approach_pitch'], min_bend=10)
                self._arm(back, torque=c['approach_torque'], speed=c['approach_speed'], label='受阻后退')
                raise GraspFailure('推进时受阻：' + str(entry['contact'].get('joint')))
            obs = self._need_track(self._look(f'推进{k + 1}'), '推进')
            if inserted is None:
                obs = self._lateral_step(obs)     # lateral trim only while the object is ahead of the tips
        else:
            raise GraspFailure('推进次数用完，目标仍未进入两指之间')
        self._event('inserted', by_vision=in_jaws(obs['bbox'], c), tip_x=round(self._tip()['tip'][0], 2),
                    inserted_cm=None if at_tips is None else round(self._tip()['tip'][0] - at_tips, 2))
        self._key('in_jaws')
        return obs

    def _lateral_error(self, obs):
        x, y, w, h = obs['bbox']
        return (x + w/2) - ref_x(y + h, self.cfg)

    def _lateral_step(self, obs):
        c = self.cfg
        err = self._lateral_error(obs)
        if abs(err) <= c['align_tol_px']:
            return obs
        dpan = int(max(-c['pan_step_max'], min(c['pan_step_max'], round(-err/self.gain))))
        if dpan == 0:
            return obs
        before = obs['center'][0]
        self._move({'left:1': self.q['pan'] + dpan}, torque=c['approach_torque'], speed=6, label=f'肩水平{dpan:+d}')
        obs = self._need_track(self._look('横向修正后'), '横向修正')
        measured = (obs['center'][0] - before)/dpan
        if measured < -0.2 and abs(dpan) >= 5:
            self.gain = max(-6.0, min(-0.4, 0.6*self.gain + 0.4*measured))
        self._event('lateral', err=round(err, 1), dpan=dpan, gain=round(self.gain, 3), measured=round(measured, 3))
        return obs

    def _align(self, obs):
        for _ in range(int(self.cfg['align_max_iter'])):
            if abs(self._lateral_error(obs)) <= self.cfg['align_tol_px']:
                return obs
            obs = self._lateral_step(obs)
        raise GraspFailure('横向对准没有收敛')

    def _close(self, obs):
        c = self.cfg
        self._phase('close', '闭合夹爪')
        self.grip_box = obs['bbox'] if obs.get('ok') else self.grip_box
        self._move({'left:6': c['grip_free']}, torque=450, speed=10, label='夹爪快速收拢')
        obs = self._need_track(self._look('收拢后'), '收拢')
        left = obs['bbox'][0]
        contact_at, still = None, 0
        while True:
            target = self.q['grip'] - c['grip_step']
            if target <= c['grip_empty']:
                raise GraspFailure('夹爪合到底仍未接触目标（空夹）')
            self._move({'left:6': target}, torque=450, speed=4, label=f'闭合到{target}')
            obs = self._need_track(self._look('闭合后'), '闭合')
            if obs.get('ok') and obs.get('score', 0) >= 0.6:
                self.grip_box = obs['bbox']
            dx = obs['bbox'][0] - left
            left = obs['bbox'][0]
            load = self.q.get('grip_load') or 0
            if contact_at is None:
                if load >= c['grip_contact_load'] or abs(dx) >= 5:
                    contact_at = self.q['grip']
                    self._event('contact', grip=contact_at, load=load, dx=round(dx, 1))
                    self._key('contact')
            else:
                still = still + 1 if abs(dx) <= c['grip_settle_px'] else 0
                if still >= 2 or contact_at - self.q['grip'] >= c['grip_after_contact_max'] or (self.q.get('grip_goal') or 0) < self.q['grip'] - 15:
                    break
        self._phase('squeeze', '加压夹紧')
        self._move({'left:6': self.q['grip'] - c['squeeze_lead']}, torque=c['squeeze_torque'], speed=3, label='加压')
        self.grasp_pose = dict(self.q)
        self._key('squeezed')

    def _lift(self):
        c = self.cfg
        self._phase('lift', '只抬肩提起并验证')
        before = self._look('提起前')
        window = grip_window(self.grip_box)
        pose_before = dict(self.q)
        self._move({'left:2': self.q['lift'] - c['lift_raw']}, speed=c['lift_speed'], label='抬肩提起')
        for extra in (0, 1):
            after = self._look('提起后')
            verdict = self._verify_held(before, after, pose_before, window)
            self._event('verify', **verdict)
            if verdict['held'] is True:
                self._key('lifted')
                return True
            if verdict['held'] is False or extra:
                return verdict['held'] is True
            self._move({'left:2': self.q['lift'] - c['lift_extra_raw']}, speed=c['lift_speed'], label='补抬')
        return False

    def _verify_held(self, before, after, pose_before, window=None):
        """Held = the object stays put in the wrist view while the gripper turned
        enough that anything left on the desk would have moved a lot. The
        expected shift of a static object comes from the gripper's actual pitch
        change (servo readback, so sag is included). The primary cue compares
        the window the gripped object filled before and after (independent of
        the tracker, which degrades between the jaws); the tracked box is the
        fallback for a textureless window. Background motion is only logged:
        the desk is too plain for it."""
        c = self.cfg
        t0 = arm.fk(pose_before['lift'], pose_before['elbow'], pose_before['pitch'])['t3']
        t1 = self._tip()['t3']
        expected = c['focal_px']*math.tan(math.radians(min(60.0, abs(t1 - t0))))
        similarity, texture = window_similarity(before['gray'], after['gray'], window) if window else (None, None)
        base = {'expected_px': round(expected, 1), 'pitch_change_deg': round(t1 - t0, 2), 'window': window,
                'window_similarity': similarity, 'window_texture': texture}
        if expected < c['verify_min_expect_px']:
            return {'held': None, 'why': '夹爪转角太小，留在桌上的物体也不会明显移动：需补抬', **base}
        if similarity is not None:
            if similarity >= c['verify_held_sim']:
                return {'held': True, 'why': '夹爪转动后，物体所在的画面区域保持原样：已随夹爪提起', **base}
            if similarity <= c['verify_left_sim']:
                return {'held': False, 'why': '物体所在的画面区域换成了别的内容：留在了桌面', **base}
            return {'held': None, 'why': '画面区域部分变化：补抬后再判断', **base}
        if not after.get('bbox') or not before.get('bbox'):
            return {'held': False, 'why': '提起后目标丢失且区域缺少纹理', **base}
        bg = background_shift(before['gray'], after['gray'], after['bbox'])
        (bx, by), (ax, ay) = before['center'], after['center']
        moved = math.hypot(ax - bx, ay - by)
        ratio = after['bbox'][2]/max(1.0, before['bbox'][2])
        base.update(object_px=round(moved, 1), size_ratio=round(ratio, 3), background_px=bg['shift'], bg_response=bg['response'])
        if not after.get('ok') or moved >= c['verify_left_frac']*expected or not 0.6 <= ratio <= 1.6:
            return {'held': False, 'why': '目标在画面中按静止物体的方式移动或丢失：留在了桌面', **base}
        if moved <= c['verify_held_frac']*expected and 0.75 <= ratio <= 1.3:
            return {'held': True, 'why': '夹爪转动后目标在腕部画面中基本不动：已随夹爪提起', **base}
        return {'held': None, 'why': '位移介于两者之间：补抬后再判断', **base}

    def _abandon_grasp(self):
        c = self.cfg
        self._phase('retry', '未带起：放回、张开、后撤后重试')
        self._move({'left:2': self.grasp_pose['lift']}, torque=c['place_torque'], speed=c['place_speed'], label='放回')
        self._move({'left:6': c['grip_open']}, torque=450, speed=10, label='张开')
        self._move({'left:2': self.q['lift'] - c['retreat_raw']}, torque=c['approach_torque'], speed=c['lift_speed'], label='抬离')
        tip = self._tip()['tip']
        z = c['desk_z'] + c['survey_height']
        self._go(arm.ik(tip[0] - 3.0, z, c['survey_pitch'], min_bend=10), '后撤抬高', speed=8, torque=c['approach_torque'])
        self.gain = float(c['pan_gain'])

    def _hold(self):
        c = self.cfg
        self._phase('hold', '保持')
        end = self.clock() + c['hold_s']
        while self.clock() < end:
            self._stop_check()
            self._refresh()
            self.sleep(min(0.5, max(0.0, end - self.clock())))
        self._key('held')

    def _place(self):
        c = self.cfg
        if c['place_lateral_cm'] or c['place_forward_cm']:
            self._phase('transport', '搬到放置位置')
            if c['place_lateral_cm']:
                radius = max(10.0, self._tip()['tip'][0])
                dpan = arm.lateral_to_pan(c['place_lateral_cm'], radius)
                self._move({'left:1': self.q['pan'] + dpan}, speed=6, label=f'横移{c["place_lateral_cm"]:g}cm')
            if c['place_forward_cm']:
                f = self._tip()
                q3 = arm.ik(f['tip'][0] + c['place_forward_cm'], f['tip'][1], f['t3'], min_bend=c['min_bend'])
                self._arm(q3, speed=6, label=f'前后移{c["place_forward_cm"]:g}cm')
        self._phase('place', '放下')
        if c['place_forward_cm']:
            g = self.grasp_pose
            z = arm.fk(g['lift'], g['elbow'], g['pitch'])['tip'][1]
            f = self._tip()
            target = arm.ik(f['tip'][0], z + 0.3, f['t3'], min_bend=10)
            self._arm(target, torque=c['place_torque'], speed=c['place_speed'], label='放下')
        else:
            self._move({'left:2': self.grasp_pose['lift']}, torque=c['place_torque'], speed=c['place_speed'], label='放下')
        self._key('placed')
        self._phase('release', '张开松手')
        self._move({'left:6': c['release_target']}, torque=450, speed=8, label='张开')
        self._key('released')
        self._phase('retreat', '抬起撤离')
        before = self._look('撤离前')
        window = grip_window(self.grip_box)
        pose_before = dict(self.q)
        self._move({'left:2': self.q['lift'] - c['retreat_raw']}, speed=c['lift_speed'], label='抬肩撤离')
        after = self._look('撤离后')
        verdict = self._verify_held(before, after, pose_before, window)
        self._event('release_check', left_behind=verdict['held'] is False, **{k: v for k, v in verdict.items() if k != 'held'})
        self._key('retreated')

    def _home(self):
        c = self.cfg
        self._phase('home', '收回' + ('竖直' if c['end_pose'] == 'upright' else '观察位'))
        if c['end_pose'] == 'survey':
            self._go(self.survey_joints(), '回观察位')
        else:
            self._go(arm.UPRIGHT, '收回竖直')
            self._move({'left:6': UPRIGHT_GRIP}, speed=20, label='合上夹爪')
        self._key('home')

    def _safe_raise(self):
        """After a failure: never open a possibly holding gripper; only lift clear if the tip is low."""
        try:
            if not getattr(self.ex, 'active', True) or 'lift' not in self.q:
                return
            if self._tip()['tip'][1] < self.cfg['desk_z'] + 4.0:
                self._move({'left:2': max(1200, self.q['lift'] - 100)}, torque=self.cfg['approach_torque'], speed=5, label='失败后抬离桌面')
        except Exception as exc:  # noqa: BLE001 - best effort; the session still ends holding
            self._event('safe_raise_failed', error=str(exc)[:120])

    def status(self):
        return {'phase': self.phase, 'note': self.note, 'attempt': self.attempt, 'gain': round(self.gain, 3), 'q': dict(self.q),
                'phases': [{k: (round(v - (self.started or 0), 2) if k in ('start', 'end') and v is not None else v) for k, v in p.items()}
                           for p in self.phases], 'events': self.events[-30:], 'result': self.result}
