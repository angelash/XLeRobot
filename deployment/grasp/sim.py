"""Offline stand-in for the robot: a wrist camera rendered from the planar arm model.

Used by the tests and for rehearsing the skill without hardware. The desk is a
world-fixed texture (so the scene shifts when the camera moves), the object a
textured box. Simple contact physics: the moving jaw pushes the box toward the
fixed jaw and stalls when it is wedged; a box gripped by its body follows the
gripper; opening releases it onto the desk; the tip cannot pass the desk. It
is a rehearsal model, not evidence about the real robot.
"""
from __future__ import annotations

import math

import cv2
import numpy as np

from . import arm
from .tracker import TemplateTracker

F, CX, CY, WIDTH, HEIGHT = 420.0, 320.0, 240.0, 640, 480
LIMITS = {1: (729, 2852), 2: (1132, 3471), 3: (727, 2926), 4: (980, 3258), 5: (1284, 4083), 6: (1301, 2811)}
NAMES = {1: 'pan', 2: 'lift', 3: 'elbow', 4: 'pitch', 5: 'roll', 6: 'grip'}


def unit(v):
    return v/np.linalg.norm(v)


class SimWorld:
    def __init__(self, obj=(29.5, 0.0), size=(3.0, 3.0, 3.0), desk_z=-8.0, start=(2037, 2211, 955, 2245, 2124, 1340),
                 sag=0, jaw_reach=7.0, cam_back=8.0, cam_up=4.5, cam_left=1.0, cam_pitch=18.0, fixed_side=2.2, texture=1.0):
        self.q = dict(zip(('pan', 'lift', 'elbow', 'pitch', 'roll', 'grip'), (float(v) for v in start)))
        self.goal = dict(self.q)
        self.size = np.array(size, float)       # depth (along x), width (lateral), height
        self.obj = np.array([obj[0], obj[1], desk_z], float)  # bottom centre, world
        self.yaw = 0.0
        self.desk_z = desk_z
        self.sag = sag
        self.jaw_reach = jaw_reach
        self.cam = (cam_back, cam_up, cam_left, math.radians(cam_pitch))
        self.held = None
        self.held_grip = None
        self.fixed_side = fixed_side
        self.texture = texture
        self.load = 0
        self.trajectory = []
        self.rays = self._rays()

    # ---- geometry ----------------------------------------------------------
    def frame(self, q=None):
        q = q or self.q
        f = arm.fk(q['lift'], q['elbow'], q['pitch'])
        phi = math.radians(arm.pan_angle(q['pan']))
        t3 = math.radians(f['t3'])
        radial = np.array([math.cos(phi), math.sin(phi), 0.0])
        left = np.array([-math.sin(phi), math.cos(phi), 0.0])
        z = np.array([0.0, 0.0, 1.0])
        tip = f['tip'][0]*radial + f['tip'][1]*z
        d = math.sin(t3)*radial + math.cos(t3)*z           # gripper axis, toward the tips
        n = -math.cos(t3)*radial + math.sin(t3)*z          # gripper 'up'
        return {'tip': tip, 'd': d, 'n': n, 'left': left, 'fk': f}

    def camera(self, q=None):
        fr = self.frame(q)
        back, up, lft, pitch = self.cam
        pos = fr['tip'] - back*fr['d'] + up*fr['n'] + lft*fr['left']
        fwd = unit(math.cos(pitch)*fr['d'] - math.sin(pitch)*fr['n'])
        right = unit(np.cross(fwd, fr['n']))
        down = np.cross(fwd, right)
        return pos, np.stack([right, down, fwd])

    def _rays(self):
        u, v = np.meshgrid(np.arange(WIDTH, dtype=np.float32), np.arange(HEIGHT, dtype=np.float32))
        return np.stack([(u - CX)/F, (v - CY)/F, np.ones_like(u)], -1)

    def project(self, pts, q=None):
        pos, R = self.camera(q)
        c = (np.asarray(pts, float) - pos) @ R.T
        depth = c[:, 2]
        uv = np.stack([CX + F*c[:, 0]/np.maximum(depth, 1e-6), CY + F*c[:, 1]/np.maximum(depth, 1e-6)], -1)
        return uv, depth

    def box_corners(self, q=None):
        if self.held is not None:
            fr = self.frame(q)
            local = self.held[1]
            return fr['tip'] + local[:, :1]*fr['d'] + local[:, 1:2]*fr['n'] + local[:, 2:3]*fr['left']
        d, w, h = self.size
        c, s = math.cos(self.yaw), math.sin(self.yaw)
        out = []
        for dz in (0, h):
            for dx, dy in ((-d/2, -w/2), (d/2, -w/2), (d/2, w/2), (-d/2, w/2)):
                out.append(self.obj + np.array([c*dx - s*dy, s*dx + c*dy, dz]))
        return np.array(out)

    def bbox(self, q=None):
        uv, depth = self.project(self.box_corners(q), q)
        if (depth < 0.5).any():
            return None
        x0, y0 = uv.min(0); x1, y1 = uv.max(0)
        return [float(x0), float(y0), float(x1 - x0), float(y1 - y0)]

    # ---- rendering ---------------------------------------------------------
    def render(self, q=None):
        pos, R = self.camera(q)
        dirs = self.rays @ R
        dz = dirs[..., 2]
        t = np.where(dz < -1e-6, (self.desk_z - pos[2])/np.where(dz < -1e-6, dz, -1), -1)
        X = pos[0] + t*dirs[..., 0]; Y = pos[1] + t*dirs[..., 1]
        tex = 196 + self.texture*(18*np.sin(X*1.9)*np.cos(Y*1.4) + 14*np.sin(X*0.7 + Y*2.3) + 10*np.sign(np.sin(X*4.1))*np.sign(np.sin(Y*3.7)))
        img = np.where(t > 0, tex, 150.0)
        bgr = np.repeat(np.clip(img, 0, 255).astype(np.uint8)[..., None], 3, -1)
        self._draw_box(bgr, q)
        self._draw_jaws(bgr, q)
        return bgr

    def _draw_box(self, bgr, q):
        corners = self.box_corners(q)
        uv, depth = self.project(corners, q)
        if (depth < 0.5).any():
            return
        cam, _ = self.camera(q)
        faces = [((0, 1, 2, 3), 0, 150), ((4, 5, 6, 7), 1, 238), ((0, 1, 5, 4), 2, 205), ((1, 2, 6, 5), 3, 185),
                 ((2, 3, 7, 6), 4, 215), ((3, 0, 4, 7), 5, 170)]
        centre = corners.mean(0)
        order = []
        for idx, kind, shade in faces:
            pts = corners[list(idx)]
            fc = pts.mean(0)
            normal = unit(fc - centre)
            if normal @ (cam - fc) > 0:
                order.append((-np.linalg.norm(cam - fc), idx, kind, shade))
        for _, idx, kind, shade in sorted(order):
            poly = uv[list(idx)].astype(np.int32)
            cv2.fillConvexPoly(bgr, poly, (shade, shade, shade))
            cv2.polylines(bgr, [poly], True, (110, 110, 110), 1)
            if kind == 1:  # top face: two dark prongs
                a, b, c, d = (corners[i] for i in idx)
                for frac in (0.35, 0.65):
                    p0 = a + (b - a)*0.3 + (d - a)*frac; p1 = a + (b - a)*0.7 + (d - a)*frac
                    pr, dp = self.project(np.array([p0, p1]), q)
                    if (dp > 0.5).all():
                        cv2.line(bgr, tuple(pr[0].astype(int)), tuple(pr[1].astype(int)), (70, 70, 70), 3)
            elif kind in (2, 3, 4, 5):  # side faces: a printed label stripe
                a, b, c, d = (corners[i] for i in idx)
                p = np.array([a + (b - a)*0.2 + (d - a)*0.4, a + (b - a)*0.8 + (d - a)*0.4,
                              a + (b - a)*0.8 + (d - a)*0.55, a + (b - a)*0.2 + (d - a)*0.55])
                pr, dp = self.project(p, q)
                if (dp > 0.5).all():
                    cv2.fillConvexPoly(bgr, pr.astype(np.int32), (shade - 45,)*3)

    def _draw_jaws(self, bgr, q):
        q = q or self.q
        opening = (q['grip'] - 1340)/(2045 - 1340)
        tip_x = int(470 + 120*opening)
        cv2.fillConvexPoly(bgr, np.array([(105, 480), (296, 308), (318, 330), (262, 480)], np.int32), (235, 235, 235))
        cv2.fillConvexPoly(bgr, np.array([(400, 480), (tip_x, 346), (tip_x + 26, 372), (tip_x + 40, 480)], np.int32), (235, 235, 235))
        cv2.fillConvexPoly(bgr, np.array([(100, 480), (250, 440), (250, 480)], np.int32), (20, 20, 20))

    # ---- physics -----------------------------------------------------------
    def gap(self, grip):
        return 2*self.jaw_reach*math.sin(math.radians(max(0.0, (grip - 1340)*arm.K)/2))

    def _in_jaws(self, q):
        fr = self.frame(q)
        centre = self.obj + np.array([0, 0, self.size[2]/2])
        rel = centre - fr['tip']
        along = -(rel @ fr['d'])                       # distance back from the tips
        lateral = rel @ fr['left']
        jaw_z = fr['tip'][2] - along*fr['d'][2]
        ok_depth = 0.3 <= along <= self.jaw_reach - 1.0
        ok_height = self.desk_z + 0.3 <= jaw_z <= self.desk_z + self.size[2] - 0.3
        return ok_depth and ok_height, lateral, fr

    def _attach(self, fr):
        """Remember the bottom centre and all corners in the gripper frame: a held box turns with the gripper."""
        rel = self.obj - fr['tip']
        corners = self.box_corners() - fr['tip']
        local = np.stack([corners @ fr['d'], corners @ fr['n'], corners @ fr['left']], -1)
        self.held = (np.array([rel @ fr['d'], rel @ fr['n'], rel @ fr['left']]), local)

    def _carry(self, q):
        fr = self.frame(q)
        (a, b, c), _ = self.held
        self.obj = fr['tip'] + a*fr['d'] + b*fr['n'] + c*fr['left']

    def _release(self):
        corners = self.box_corners()
        bottom = corners[np.argsort(corners[:, 2])[:4]]
        edge = corners[1] - corners[0]
        self.yaw = math.atan2(edge[1], edge[0])
        self.held = None
        self.held_grip = None
        self.obj = np.array([bottom[:, 0].mean(), bottom[:, 1].mean(), self.desk_z])

    def _violates(self, q):
        fr = self.frame(q)
        if fr['tip'][2] < self.desk_z + 0.15:
            return True
        return self.held is not None and (self.frame(q)['tip'] + self.held[0][0]*fr['d'] + self.held[0][1]*fr['n'])[2] < self.desk_z - 0.2

    def apply(self, targets):
        """Move to targets (logical names) the way the executor would; returns (contact, trajectory)."""
        start = dict(self.q)
        goal = {**self.q, **{NAMES[int(k.split(':')[1])]: float(v) for k, v in targets.items()}}
        if 'lift' in goal and goal['lift'] < start['lift'] and self.sag:
            goal['lift'] = min(start['lift'], goal['lift'] + self.sag)
        self.goal = dict(goal)
        contact = None
        self.load = 0
        steps = 12
        traj = []
        grip_stall = None
        for k in range(1, steps + 1):
            t = k/steps
            q = {n: start[n] + (goal[n] - start[n])*t for n in start}
            if self.held is not None and q['grip'] < self.held_grip:
                q['grip'] = self.held_grip                       # squeezing a held box: the jaw stays on it
                grip_stall = self.held_grip
            elif q['grip'] < start['grip']:
                inside, lateral, fr = self._in_jaws(q)
                if inside and self.held is None:
                    fixed_side = self.fixed_side
                    moving_side = fixed_side - self.gap(q['grip'])
                    w = self.size[1]
                    if moving_side > lateral - w/2:            # moving jaw touches: push toward the fixed jaw
                        self.load = max(self.load, 35)
                        lateral = moving_side + w/2
                        if lateral + w/2 >= fixed_side - 1e-6:  # wedged
                            g_c = 1340 + 2*math.degrees(math.asin(min(1.0, w/(2*self.jaw_reach))))/arm.K
                            q['grip'] = max(q['grip'], g_c)
                            grip_stall = q['grip']
                            self._attach(fr)
                            self.held_grip = grip_stall
                        else:
                            shift = lateral - ((self.obj + [0, 0, self.size[2]/2]) - fr['tip']) @ fr['left']
                            self.obj = self.obj + shift*fr['left']
            elif self.held is not None and q['grip'] > self.held_grip + 15:
                self._release()
            if self._violates(q):
                contact = {'joint': 'left:2', 'position': round(traj[-1]['lift'] if traj else start['lift']), 'target': goal['lift'],
                           'peak_load': 420}
                q = traj[-1] if traj else dict(start)
                traj.append(dict(q))
                break
            traj.append(dict(q))
            if self.held is not None:
                self._carry(q)
        final = traj[-1]
        if grip_stall is not None:
            final['grip'] = grip_stall
            self.load = int(min(600, 30 + 6*max(0.0, grip_stall - goal['grip'])))
        self.q = {n: float(v) for n, v in final.items()}
        self.trajectory = traj
        return contact

    def joints(self):
        out = {}
        for i, n in NAMES.items():
            pos = int(round(self.q[n]))
            out[f'left:{i}'] = {'position': pos, 'goal': int(round(self.goal.get(n, pos))), 'load': self.load if n == 'grip' else 0,
                                'load_sign': 1, 'torque_limit': 1000, 'torque': 1, 'temperature': 35, 'voltage': 12.1, 'status': 0}
        return out


class SimExecutor:
    """The 8765 servo-session contract over SimWorld."""
    def __init__(self, world):
        self.world = world
        self.active = False
        self.steps = []
        self.ended = None
        self.stopped = None

    def start(self, label=''):
        self.active, self.ended = True, None
        return 'sim'

    def _need(self):
        if not self.active:
            raise ValueError('会话未开始或已结束')

    def step(self, targets, torque=None, speed=12, label=''):
        self._need()
        for key, value in targets.items():
            i = int(key.split(':')[1])
            lo, hi = LIMITS[i]
            if not lo <= value <= hi:
                raise ValueError(f'{key}目标{value}超出当前限位{lo}–{hi}')
        contact = self.world.apply(targets)
        entry = {'n': len(self.steps), 'label': label, 'targets': dict(targets), 'torque': torque, 'contact': contact,
                 'joints': self.world.joints()}
        self.steps.append(entry)
        return entry

    def hold(self):
        self._need()
        return {'active': True, 'joints': self.world.joints()}

    def end(self, reason='完成'):
        if self.active:
            self.active, self.ended = False, reason

    def stop(self, reason='停止'):
        self.stopped = reason
        self.end(reason)


class SimObserver:
    """Renders the motion path frame by frame into the tracker, like the live 15 fps stream."""
    def __init__(self, world, tracker=None, clock=None):
        self.world = world
        self.tracker = tracker or TemplateTracker()
        self.t = 0.0
        self.last = None
        self.frames = 0

    def gray(self, q=None):
        return cv2.cvtColor(self.world.render(q), cv2.COLOR_BGR2GRAY)

    def select_exact(self):
        g = self.gray()
        box = self.world.bbox()
        x, y, w, h = box
        x0, y0 = max(0, x), max(0, y)
        return self.tracker.init(g, [x0, y0, min(WIDTH, x + w) - x0, min(HEIGHT, y + h) - y0])

    def observe(self, after=None, timeout=2.0):
        path = self.world.trajectory or [self.world.q]
        self.world.trajectory = []
        state, g = None, None
        for q in path[::2] + [path[-1]]:
            g = self.gray(q)
            state = self.tracker.update(g)
            self.frames += 1
        self.t += 1.0
        self.last = {**state, 't': self.t, 'gray': g}
        return self.last


def calibrate(world_kwargs=None, lateral=0.4, pitch=105.0, desk_clear=1.4):
    """Wrist-view thresholds for SimWorld, the way the defaults were read off real photos."""
    kw = dict(world_kwargs or {})
    w = SimWorld(**kw)
    z = w.desk_z + desk_clear
    def bbox_at(along):
        # Put the gripper so the object centre sits 'along' cm behind the tips, 'lateral' cm toward the fixed jaw.
        tip_x = w.obj[0] + along + w.size[0]*0  # object at x; tip ahead of it by 'along'
        joints = arm.ik(tip_x, z, pitch)
        q = {**w.q, 'lift': joints[0], 'elbow': joints[1], 'pitch': joints[2], 'grip': 2045}
        fr = w.frame(q)
        shift = lateral - (w.obj - fr['tip']) @ fr['left']
        probe = SimWorld(**{**kw, 'obj': (w.obj[0] + shift*fr['left'][0], w.obj[1] + shift*fr['left'][1])})
        return probe.bbox(q)
    far, near, tips, inside = bbox_at(-8.0), bbox_at(-1.0), bbox_at(-w.size[0]/2), bbox_at(2.6)
    return {'ref_bottom_far': far[1] + far[3], 'ref_x_far': far[0] + far[2]/2,
            'ref_bottom_near': inside[1] + inside[3], 'ref_x_near': inside[0] + inside[2]/2,
            'clear_bottom': near[1] + near[3] + 5, 'tips_bottom': tips[1] + tips[3], 'insert_cm': 3.0,
            'in_bottom': inside[1] + inside[3] - 12, 'in_top': inside[1] - 12,
            'desk_z': w.desk_z, 'approach_clearance': desk_clear, 'approach_pitch': pitch}
