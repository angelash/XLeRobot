"""Offline, supervised tabletop trials through the existing bounded executor.

This is an experimental local skill for the two known unplugged adapters on
the cleared desk, not a general detector or a promoted autonomous policy.
No cloud/model provider, operator box, serial connection or chassis motion.
"""
from __future__ import annotations

import argparse
from collections import deque
from datetime import datetime
import hashlib
import json
import math
import os
from pathlib import Path
import time
import threading
import urllib.request
import zipfile

# The laptop cannot afford BLAS's automatic one-thread-per-core allocation.
for variable in ('OPENBLAS_NUM_THREADS', 'OMP_NUM_THREADS', 'MKL_NUM_THREADS'):
    os.environ[variable] = '1'
import cv2
import numpy as np
cv2.setNumThreads(1)

from . import arm
from .controller import GraspController, GraspFailure
from .service import HttpExecutor
from .tracker import TemplateTracker
from .flow import FeatureContinuity

REFS = {'left': (2211, 955, 2245), 'right': (2245, 901, 2049)}
CAMERAS = {'left': 2, 'right': 1}


def request(path, body=None, port=8765):
    raw = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(f'http://127.0.0.1:{port}'+path, data=raw,
                                 headers={'Content-Type': 'application/json'})
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.load(r)


def candidates(bgr, appearance, head=False):
    """Rectangular body + colour + surrounding clear table, restricted ROI.

    Shape/colour evidence is only for this named fixture. Reject ambiguities;
    local tracking can start only after three distinct agreeing source frames.
    """
    if appearance not in ('light', 'dark'):
        raise ValueError('Unknown fixture appearance')
    gray = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)
    height, width = gray.shape
    if (width, height) != (640, 480):
        raise ValueError('The local fixture uses 640x480 images only')
    low, high, size = (10, 30, 5) if appearance == 'light' else (20, 50, 3)
    edge = cv2.Canny(cv2.GaussianBlur(gray, (3, 3), 0), low, high)
    edge = cv2.morphologyEx(edge, cv2.MORPH_CLOSE, np.ones((size, size), np.uint8), iterations=2)
    contours, _ = cv2.findContours(edge, cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE)
    if appearance == 'light':
        # A white body's edges can merge into the desk seam during closing.
        # Local intensity contrast supplies a second contour proposal, still
        # subject to the same fixture, shape, desk-ring and ambiguity gates.
        adaptive=cv2.adaptiveThreshold(gray,255,cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
                                       cv2.THRESH_BINARY_INV,61,8)
        line_edge=cv2.Canny(cv2.GaussianBlur(gray,(3,3),0),10,30)
        lines=cv2.HoughLinesP(line_edge,1,np.pi/180,threshold=70,minLineLength=140,maxLineGap=20)
        seam_free=line_edge.copy()
        if lines is not None:
            for x1,y1,x2,y2 in np.asarray(lines).reshape(-1,4):
                cv2.line(adaptive,(x1,y1),(x2,y2),0,5)
                cv2.line(seam_free,(x1,y1),(x2,y2),0,5)
        adaptive=cv2.morphologyEx(adaptive,cv2.MORPH_CLOSE,np.ones((3,3),np.uint8))
        more,_=cv2.findContours(adaptive,cv2.RETR_LIST,cv2.CHAIN_APPROX_SIMPLE)
        # Removing a long seam before edge closing separates the full plastic
        # body from the desk. Adaptive contrast alone can retain just its dark
        # lower side. All proposals still pass the same fixture/ambiguity gates.
        seam_free=cv2.morphologyEx(seam_free,cv2.MORPH_CLOSE,np.ones((5,5),np.uint8),iterations=2)
        bodies,_=cv2.findContours(seam_free,cv2.RETR_LIST,cv2.CHAIN_APPROX_SIMPLE)
        contours=tuple(contours)+tuple(more)+tuple(bodies)
    found = []
    for contour in contours:
        x, y, w, h = cv2.boundingRect(contour)
        area = w*h
        if not (22 <= w <= 130 and 35 <= h <= 180 and 950 <= area <= 16000 and .25 <= w/h <= 1.8):
            continue
        if head:
            if not (145 < x and x+w < 465 and 105 < y and y+h < 320):
                continue
        elif not (65 < x and x+w < 560 and (110 if appearance=='light' else 150) < y and y+h < 405):
            continue
        fill = cv2.contourArea(contour)/area
        if fill < (.35 if appearance == 'light' else .55):
            continue
        crop = gray[y:y+h, x:x+w]
        mean = float(crop.mean())
        if appearance == 'dark' and not 20 < mean < 145:
            continue
        if appearance == 'light' and not 115 < mean < 238:
            continue
        # A bounded ring around the body must be predominantly a light desk.
        margin = 12
        a, b, c, d = max(0, x-margin), max(0, y-margin), min(width, x+w+margin), min(height, y+h+margin)
        ring = gray[b:d, a:c].copy()
        mask = np.ones(ring.shape, np.uint8)
        mask[y-b:y-b+h, x-a:x-a+w] = 0
        surroundings = ring[mask.astype(bool)]
        light_fraction = float((surroundings > 155).mean())
        if light_fraction < (.75 if appearance == 'dark' else (.65 if head else .50)):
            continue
        contrast = float(surroundings.mean()-mean)
        if appearance == 'dark' and contrast < 70:
            continue
        if appearance == 'light' and not 7 < contrast < 85:
            continue
        # Keep the full contour: threshold-based prong trimming also cut
        # the plastic body in half on weak-contrast white fixtures.
        found.append({'bbox': [x, y, w, h], 'fill': round(fill, 3), 'contrast': round(contrast, 2),
                      'surrounding_light_fraction': round(light_fraction, 3), 'mean': round(mean, 2),
                      'score': round(fill*light_fraction*math.sqrt(area), 2), 'appearance': appearance})
    found.sort(key=lambda p: p['score'], reverse=True)
    # Nested edge contours describe the same body, not distinct objects.
    unique = []
    for hit in found:
        x, y, w, h = hit['bbox']
        if any(q['bbox'][0]-3<=x and q['bbox'][1]-3<=y and
               x+w<=q['bbox'][0]+q['bbox'][2]+3 and y+h<=q['bbox'][1]+q['bbox'][3]+3
               for q in unique):
            continue
        if any(abs(x+w/2-(q['bbox'][0]+q['bbox'][2]/2)) < min(w, q['bbox'][2])*.6
               and abs(y+h/2-(q['bbox'][1]+q['bbox'][3]/2)) < min(h, q['bbox'][3])*.6 for q in unique):
            continue
        unique.append(hit)
    return unique


class Frames:
    def __init__(self, root):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.previous = {}
        self.receipts = []
        self.lock = threading.Lock()

    def get(self, camera, after=0.):
        with self.lock:
            return self._get(camera, after)

    def _get(self, camera, after):
        deadline = time.monotonic()+2.5
        while True:
            with urllib.request.urlopen(f'http://127.0.0.1:8765/api/camera/{camera}/snapshot', timeout=3) as r:
                jpeg = r.read()
                captured = float(r.headers['X-Captured-At'])
                seq = int(r.headers['X-Frame-Sequence'])
            age = time.time()-captured
            if not -.1 <= age <= .8:
                raise GraspFailure('Stale source frame')
            if captured > after and (captured, seq) != self.previous.get(camera):
                break
            if time.monotonic() >= deadline:
                raise GraspFailure('No distinct fresh camera frame')
            time.sleep(.06)
        self.previous[camera] = (captured, seq)
        im = cv2.imdecode(np.frombuffer(jpeg, np.uint8), cv2.IMREAD_COLOR)
        if im is None:
            raise GraspFailure('Invalid camera JPEG')
        self.receipts.append({'camera': camera, 'captured_at': captured, 'age_s': age, 'sequence': seq})
        return im, jpeg, captured

    def save(self, tag):
        result = []
        for camera in (0, 1, 2):
            _, raw, captured = self.get(camera)
            name = f'{tag}-cam{camera}.jpg'
            (self.root/name).write_bytes(raw)
            result.append({'camera': camera, 'file': name, 'captured_at': captured})
        return result


class SideExecutor:
    """Reuse the verified planar kernel, with each arm's separate raw references."""
    def __init__(self, side):
        self.side, self.inner = side, HttpExecutor('http://127.0.0.1:8765')
        self.offset = {i: v-a for i, v, a in zip((2, 3, 4), REFS[side], REFS['left'])}
        self.contact = None

    @property
    def active(self):
        return self.inner.active

    def normalize(self, entry):
        out = dict(entry)
        joints = {}
        for key, value in (entry.get('joints') or {}).items():
            if key.startswith(self.side+':'):
                i = int(key.split(':')[1]); j = dict(value)
                for field in ('position', 'goal'):
                    if field in j:
                        j[field] -= self.offset.get(i, 0)
                joints[f'left:{i}'] = j
        out['joints'] = joints
        if entry.get('contact'):
            self.contact = entry['contact']
        return out

    def start(self, label=''):
        return self.inner.start('local-'+self.side+': '+label)

    def step(self, targets, torque=None, speed=12., label=''):
        if any(not k.startswith('left:') for k in targets):
            raise GraspFailure('Trial executor accepts only its assigned arm')
        real = {self.side+':'+k.split(':')[1]: v+self.offset.get(int(k.split(':')[1]), 0) for k, v in targets.items()}
        return self.normalize(self.inner.step(real, torque, speed, label))

    def hold(self):
        return self.normalize(self.inner.hold())

    def end(self, reason='完成'):
        self.inner.end(reason)

    def stop(self, reason='停止'):
        self.inner.stop(reason)

    def base(self, *_):
        raise GraspFailure('No chassis movement in grasp trials')


class Observer:
    def __init__(self, frames, side, appearance):
        self.frames, self.side, self.appearance = frames, side, appearance
        self.tracker = TemplateTracker()
        self.flow = FeatureContinuity()
        self.detections = []
        self.stop = threading.Event()
        self.lock = threading.Lock()
        self.samples = deque(maxlen=8)
        self.worker = None
        self.failure = None

    def close(self):
        self.stop.set()
        if self.worker:
            self.worker.join(timeout=4)
            if self.worker.is_alive():
                raise GraspFailure('Local camera worker did not stop')
        self.worker = None

    def reanchor(self, observation):
        """Refresh the same visible body at a stopped phase transition.

        Initial high-view texture differs greatly from the final approach.
        This does not acquire a different target or revive a failed track.
        """
        box = observation.get('bbox')
        gray = observation.get('gray')
        if not observation.get('ok') or observation.get('score', 0) < .70 or box is None or gray is None:
            raise GraspFailure('Cannot anchor an uncertain closing target')
        x,y,w,h = box
        if w < 24 or h < 24 or x < 0 or y < 0 or x+w > gray.shape[1] or y+h > gray.shape[0]:
            raise GraspFailure('Closing anchor must be fully visible')
        with self.lock:
            self.tracker.init(gray, box)
            self.flow.init(gray, box)
            self.samples.clear()
        self.detections.append({'kind':'closing_anchor','bbox':list(box),
                                'captured_at':observation.get('captured_at')})

    def _track(self):
        try:
            n = 0
            while not self.stop.is_set():
                began = time.monotonic()
                im, raw, at = self.frames.get(CAMERAS[self.side])
                gray = cv2.cvtColor(im, cv2.COLOR_BGR2GRAY)
                with self.lock:
                    state = self.tracker.update(gray)
                    continuity = self.flow.update(gray)
                    box = state.get('bbox')
                    # Sparse, multi-second updates previously shrank onto a
                    # prong and still reported ok. A fixture approaching the
                    # camera cannot become a quarter of its initial body.
                    if state.get('score', 0) < .65 or state.get('scale', 0) < .65:
                        state = {**state, 'ok': False, 'why': 'Unreliable full-body track'}
                    if continuity.get('ok') and box:
                        a,b=continuity['bbox'],box
                        distance=math.hypot(a[0]+a[2]/2-b[0]-b[2]/2,
                                            a[1]+a[3]/2-b[1]-b[3]/2)
                        agreement=distance<=30 and .65<=b[2]/a[2]<=1.5 and .65<=b[3]/a[3]<=1.5
                        if (not state['ok'] and state.get('score',0)>=.45
                                and state.get('scale',0)>=.65 and agreement):
                            state.update(ok=True,bbox=a,center=continuity['center'],
                                         why='Distributed reversible features confirm changing body')
                        if state['ok'] and state.get('score',0)>=.85 and agreement and n%5==0:
                            self.flow.init(gray,box)
                    elif state['ok'] and state.get('score',0)>=.85 and n%5==0:
                        self.flow.init(gray,box)
                    state['continuity']=continuity
                    state.update(gray=gray, captured_at=at, sampled_at=time.monotonic())
                    self.samples.append(state)
                    self.detections.append({k: v for k, v in state.items() if k != 'gray'})
                if n % 5 == 0:
                    (self.frames.root/f'track-{n:05d}-cam{CAMERAS[self.side]}.jpg').write_bytes(raw)
                n += 1
                self.stop.wait(max(0., .1-(time.monotonic()-began)))
        except Exception as exc:
            self.failure = f'{type(exc).__name__}: {exc}'

    def locate(self):
        self.close()
        hits = []
        time.sleep(.5)  # arm settling is different from having a fresh frame
        for _ in range(12):
            im, _, at = self.frames.get(CAMERAS[self.side])
            found = candidates(im, self.appearance)
            self.detections.append({'at': at, 'candidates': found})
            if len(found) == 1:
                hits.append((found[0]['bbox'], im, at))
            elif len(found) > 1:
                hits = []
            time.sleep(.12)
        # Complete bodies and small dark sides may alternate at the desk seam.
        # Sample the entire bounded window before choosing: three late small
        # boxes cannot replace a larger body seen in this same stationary view.
        if hits:
            largest = max(w*h for (x,y,w,h), _, _ in hits)
            hits = [h for h in hits if h[0][2]*h[0][3] >= largest*.65][-3:]
        if len(hits) < 3:
            raise GraspFailure('Local target ambiguous or absent; no model fallback')
        centers = np.array([[x+w/2, y+h/2] for (x, y, w, h), _, _ in hits])
        sizes = np.array([[w,h] for (x,y,w,h), _, _ in hits])
        if (np.ptp(centers, axis=0).max() > 8 or
                (sizes.max(axis=0)/sizes.min(axis=0)).max() > 1.35 or at-hits[-1][2] > .8):
            raise GraspFailure('Local initial detections disagree')
        gray = cv2.cvtColor(hits[-1][1], cv2.COLOR_BGR2GRAY)
        box = hits[-1][0]
        self.tracker.init(gray, box)
        self.flow.init(gray, box)
        self.samples.clear()
        self.failure = None
        self.stop.clear()
        self.worker = threading.Thread(target=self._track, name='local-wrist-tracker', daemon=True)
        self.worker.start()
        return box

    def observe(self, after, timeout=2.5):
        # Tracker runs during movement, so viewpoint changes arrive as small
        # frame steps. Decisions still wait for an actual post-settle frame.
        wall_after = time.time()+max(0., after-time.monotonic())
        deadline = time.monotonic()+timeout
        while time.monotonic() < deadline:
            if self.failure:
                raise GraspFailure('Local camera worker failed: '+self.failure)
            with self.lock:
                state = dict(self.samples[-1]) if self.samples else None
            if state and state['captured_at'] >= wall_after:
                if time.time()-state['captured_at'] > .8:
                    raise GraspFailure('Stale tracked source frame')
                return state
            time.sleep(.03)
        raise GraspFailure('No post-settle local tracking frame')


class Recorder:
    def __init__(self, frames):
        self.frames = frames
        self.captures = []
        self.events = []

    def event(self, item):
        self.events.append(item)
        with (self.frames.root/'events.jsonl').open('a', encoding='utf-8') as f:
            f.write(json.dumps(item, ensure_ascii=False, default=str)+'\n')

    def keyframe(self, tag, phase=''):
        files = self.frames.save(f'{len(self.captures):02d}-{tag}')
        self.captures.append({'tag': tag, 'phase': phase, 'frames': files})


class LocalController(GraspController):
    def acquire(self):
        """Finite local search at high clearance, never a blind approach."""
        scan_x = scan_z = None
        for n in range(6):
            try:
                return self.obs.locate()
            except GraspFailure as exc:
                if not str(exc).startswith(('Local target ambiguous','Local initial detections')):
                    raise
                if n == 5:
                    raise GraspFailure('Bounded local view search exhausted: '+str(exc)) from None
                if not self.ex.active:
                    self.ex.start('bounded local reacquisition at high clearance')
                    self.started = time.monotonic()
                self._refresh()
                f = self._tip()
                if f['tip'][1] < self.cfg['desk_z']+6:
                    raise GraspFailure('Local reacquisition requires high clearance')
                if scan_x is None:
                    scan_x,scan_z=f['tip'][0],max(8.,f['tip'][1])
                if n == 4:
                    # A near body may sit below the acquisition ROI at every
                    # view angle. Make a single bounded high retreat rather
                    # than accepting a cropped body next to the open fingers.
                    if scan_x-2 < 12:
                        raise GraspFailure('Local high retreat budget exhausted')
                    target = arm.ik(scan_x-2, scan_z+2, 145, min_bend=22)
                elif getattr(self.obs, 'appearance', None) == 'light':
                    # Search both farther and nearer before retreating. The
                    # previous 115->130->150 sequence missed far white bodies.
                    target = arm.ik(scan_x, scan_z, (115,105,130,145)[n], min_bend=22)
                elif scan_x >= 12:
                    # Repeated placement may leave the dark body farther
                    # ahead. Looking only down/back hides it above the frame.
                    target = arm.ik(scan_x, scan_z, (130,115,145,105)[n], min_bend=22)
                else:
                    raise GraspFailure('Local high search exhausted without target')
                self._go(target, 'local view search', speed=6, torque=450)
                self._key('acquisition-search')

    def _approach(self, obs):
        # High/downward search can see an object which the horizontal
        # descent would put behind the wrist camera. Make room with the arm
        # while keeping the camera down; no chassis fallback or blind lower.
        for _ in range(4):
            if obs['bbox'][1]+obs['bbox'][3] < 330:
                break
            f = self._tip()
            if f['tip'][1] < self.cfg['desk_z']+6 or f['tip'][0]-2 < 12:
                raise GraspFailure('No observed clearance for local descent')
            self._go(arm.ik(f['tip'][0]-2, f['tip'][1], max(130, f['t3']), min_bend=22),
                     'make visual clearance before descent', torque=450, speed=6)
            obs = self._need_track(self._look('high retreat before descent'), 'high retreat')
            self._key('descent-clearance')
        else:
            raise GraspFailure('Local descent clearance budget exhausted')
        if getattr(self.obs, 'appearance', None) == 'light':
            measured = max(105., min(150., self._tip()['t3']))
            self.cfg['survey_pitch'] = measured
            self.cfg['approach_pitch'] = min(125., measured if measured<=115 else measured-5)
            self._event('visible_approach_pitch', measured=measured,
                        target=self.cfg['approach_pitch'])
        return super()._approach(obs)

    def prepare_open(self):
        self._refresh()
        if self.q['grip'] >= 1950:
            return
        if self._tip()['tip'][1] < self.cfg['desk_z']+6:
            raise GraspFailure('Open-jaw preparation requires a clear high pose')
        self._move({'left:6':2045},torque=450,speed=12,label='open jaws before target acquisition')
        self._key('open-before-acquisition')

    def _lateral_step(self, obs):
        error = self._lateral_error(obs)
        if abs(error) <= self.cfg['align_tol_px']:
            return obs
        delta = max(-30, min(30, round(-error/self.gain)))
        actual = self.q['pan']
        target = getattr(self, 'local_pan_goal', actual)+delta
        target = max(actual-100, min(actual+100, target))
        self._move({'left:1': target}, torque=450, speed=6, label='local pan with bounded deadband compensation')
        self.local_pan_goal = target
        newer = self._need_track(self._look('local lateral correction'), 'local lateral correction')
        moved = self.q['pan']-actual
        if abs(moved) >= 3:
            measured = (newer['center'][0]-obs['center'][0])/moved
            if measured < -.2:
                self.gain = max(-6.,min(-.4,.6*self.gain+.4*measured))
        self._event('local_lateral', error=error, target=target, actual_motion=moved, gain=self.gain)
        return newer

    def _home(self):
        self._phase('home', '回到高位本地观察，避免目标被固定指挡住')
        self._refresh()
        if self.q['grip'] < 1950 or abs(self.q.get('grip_load', 0)) > 60:
            raise GraspFailure('High return requires an open unloaded gripper')
        # Known empty-arm return after supported release: 45 percent was
        # insufficient against gravity on both shoulders in inspected free
        # space. Keep approach at 45 percent and all contact guards intact.
        self._go(arm.ik(18,8,150,min_bend=22), 'local high observation', speed=8, torque=600)
        self._key('home')

    def _close(self, obs):
        anchor = list(obs['bbox']) if obs.get('bbox') else None
        if hasattr(self.obs, 'reanchor'):
            x,y,w,h=anchor
            visible=min(h,obs['gray'].shape[0]-y)
            if visible < h*.70:
                raise GraspFailure('Too little closing surface remains visible')
            anchor=[x,y,w,visible]
            self.obs.reanchor({**obs,'bbox':anchor})
            self._event('closing_anchor', bbox=anchor)
        super()._close(obs)
        # Closing fingers can partly hide/rotate the body. A smaller NCC box
        # must not silently move the lift-verification window onto the desk.
        if anchor is not None:
            self.grip_box = anchor
        self._refresh()
        self.baseline = self._settled_pressure()
        # The old controller could stop after 220 counts of visual movement,
        # although no pressure existed. Keep a finite measured closing budget.
        for _ in range(10):
            if self.baseline['load'] >= 60 and self.baseline['lead'] >= 15:
                break
            target = max(1450, int(self.q['grip'])-30)
            if target >= self.q['grip']-5:
                break
            self._move({'left:6': target}, torque=600, speed=4, label='bounded local pressure correction')
            self._refresh()
            self.baseline = self._settled_pressure()
            self._event('pressure_correction', pressure=self.baseline, target=target)
        if self.baseline['load'] < 60 or self.baseline['lead'] < 15:
            raise GraspFailure('Local pressure baseline insufficient; no lift')
        self.grasp_pose = dict(self.q)

    def pressure(self):
        return {'load': float(self.q.get('grip_load') or 0),
                'lead': self.q['grip']-self.q.get('grip_goal', self.q['grip'])}

    def _settled_pressure(self):
        """Measure steady preload without changing the goal or torque."""
        recent = deque(maxlen=5)
        deadline = self.clock()+2.5
        while self.clock() < deadline:
            self._stop_check()
            self._refresh()
            p = self.pressure()
            recent.append({'t':self.clock(), 'position':self.q['grip'], **p})
            if len(recent)==5 and recent[-1]['t']-recent[0]['t'] >= .55:
                values={key:[row[key] for row in recent] for key in ('position','load','lead')}
                if (all(math.isfinite(v) for seq in values.values() for v in seq)
                        and max(values['position'])-min(values['position']) <= 5
                        and max(values['load'])-min(values['load']) <= 25
                        and max(values['lead'])-min(values['lead']) <= 6):
                    pressure={k:float(np.median(values[k])) for k in ('load','lead')}
                    self._event('settled_pressure', pressure=pressure, samples=list(recent))
                    return pressure
            self.sleep(.15)
        self._event('unstable_pressure', samples=list(recent))
        raise GraspFailure('No stable pressure baseline; no lift')

    def _verify_held(self, before, after, pose_before, window=None):
        verdict = super()._verify_held(before, after, pose_before, window)
        if self.phase == 'lift' and verdict.get('held') is True:
            p = self.pressure()
            retained = p['load'] >= self.baseline['load']*.75 and p['lead'] >= self.baseline['lead']*.65
            verdict.update(pressure=p, pressure_retained=retained)
            if not retained:
                verdict.update(held=False, why='Pressure lost after test lift')
        return verdict

    def _hold(self):
        super()._hold()
        self._refresh()
        p = self.pressure()
        if p['load'] < self.baseline['load']*.75 or p['lead'] < self.baseline['lead']*.65:
            raise GraspFailure('Pressure lost during hold')

    def _place(self):
        super()._place()
        check = next((e for e in reversed(self.events) if e['kind'] == 'release_check'), None)
        if not check or not check.get('left_behind'):
            # A weakly changed window is uncertainty. Obtain a larger measured
            # retreat, then independently detect the whole body on the desk.
            if self.q['grip'] < 1950 or abs(self.q.get('grip_load', 0)) > 60:
                raise GraspFailure('Release after retreat not confirmed locally')
            self._home()
            self._refresh()
            box = self.acquire()
            if not box or box[1]+box[3] >= 405:
                raise GraspFailure('Released body not redetected away from jaws')
            self._event('release_redetection', bbox=box, full_body_detected=True,
                        grip_position=self.q['grip'], grip_load=self.q.get('grip_load'))
            self._key('release-redetected')

    def _make_room(self, obs):
        raise GraspFailure('Insufficient approach space; chassis is fixed for this trial')


def preflight():
    state = request('/api/status')
    servo = request('/api/servo/status')
    grasp = request('/api/platform/grasp/status', port=8770)
    studio = request('/api/platform/status', port=8770)
    motors = [m for p in state['ports'] for m in p['motors']]
    if (servo['active'] or grasp['operation'] or studio['recording'] or state['agent_run']['running']
            or state['limit_selftest']['running'] or state['controller']['controller'] != 'offline'):
        raise GraspFailure('Other operator/activity owns the robot')
    if len(motors) != 16 or any(m['voltage'] < 11.5 or not 0 < m['temperature'] < 45 or m['status'] for m in motors):
        raise GraspFailure('Hardware health not ready')
    if any(m['goal_velocity'] for m in motors if m['id'] in (9, 10)):
        raise GraspFailure('Wheel goals not zero')
    return state


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=('inspect', 'survey', 'high', 'search', 'recover', 'park', 'pick'))
    parser.add_argument('--side', choices=('left', 'right'), required=True)
    parser.add_argument('--appearance', choices=('light', 'dark'),
                        help='Named unplugged fixture; defaults to light left / dark right')
    parser.add_argument('--root', required=True)
    parser.add_argument('--confirmed', action='store_true')
    parser.add_argument('--previous')
    args = parser.parse_args()
    root = Path(args.root)
    root.mkdir(parents=True, exist_ok=False)
    frames = Frames(root)
    recorder = Recorder(frames)
    appearance = args.appearance or ('light' if args.side == 'left' else 'dark')
    result = {'ok': False, 'side': args.side, 'appearance': appearance, 'action': args.action, 'llm_calls': 0,
              'policy_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
              'source': 'local_opencv_and_bounded_planar_controller', 'started_at': datetime.now().astimezone().isoformat()}
    ex = obs = None
    try:
        state = preflight()
        recorder.keyframe('preflight')
        obs = Observer(frames, args.side, appearance)
        if args.action == 'inspect':
            result['bbox'] = obs.locate()
            result['ok'] = True
        else:
            if not args.confirmed:
                raise GraspFailure('Supervised cleared-workspace confirmation required')
            ex = SideExecutor(args.side)
            config = {'max_attempts': 1, 'hold_s': 3., 'end_pose': 'survey',
                      'survey_pitch': 115. if appearance=='light' else 130.,
                      'approach_pitch': 105. if appearance=='light' else 125.,
                      'squeeze_torque': 600, 'reverse_authorization': '', 'approach_torque': 450}
            brain = LocalController(ex, obs, recorder, config)
            if args.action == 'survey':
                result.update(brain.run_survey())
            elif args.action == 'park':
                ex.start('return empty arm upright then folded')
                brain.started = time.monotonic()
                brain._refresh()
                brain._go(arm.UPRIGHT, 'park upright', speed=8, torque=600)
                real = (1168, 2914, 2246) if args.side == 'left' else (1230, 2868, 2312)
                normalized = tuple(v-o for v,o in zip(real, [ex.offset[i] for i in (2,3,4)]))
                initial = tuple(brain.q[k] for k in ('lift','elbow','pitch'))
                for k in range(1,8):
                    q = tuple(round(a+(b-a)*k/7) for a,b in zip(initial, normalized))
                    brain._arm(q, torque=450, speed=12, label='known folded transition')
                brain._move({'left:6':1340}, torque=450, speed=12, label='empty gripper stowed')
                recorder.keyframe('folded')
                result['ok'] = True
            elif args.action == 'high':
                ex.start('bounded raised observation; no approach')
                brain.started = time.monotonic()
                brain._refresh()
                target = arm.ik(18, 8, 130, min_bend=22)
                start = tuple(brain.q[k] for k in ('lift', 'elbow', 'pitch'))
                brain._go(target, 'raise observation behind target', speed=8, torque=450)
                recorder.keyframe('high-observe')
                result['bbox'] = obs.locate()
                result['ok'] = True
            elif args.action == 'recover':
                previous = Path(args.previous or '').resolve()
                if not previous.is_relative_to(root.parent.resolve()):
                    raise GraspFailure('Recovery receipt outside trial root')
                old = json.loads(previous.read_text(encoding='utf-8'))
                if old.get('side') != args.side or old.get('contact'):
                    raise GraspFailure('Recovery requires the same arm with no contact')
                events = old.get('controller', {}).get('events', [])
                squeezed = next((e['q'] for e in reversed(events) if e.get('label') == '加压'), None)
                if squeezed is None:
                    # A visual refusal may occur during closing, before the
                    # squeeze event. Use the measured support pose before
                    # the first closure, never the later failure raise.
                    squeezed = next((e['q'] for e in events
                                     if e.get('label') == '夹爪快速收拢'), None)
                if not squeezed:
                    raise GraspFailure('No recorded supported grasp pose')
                ex.start('controlled support release after insufficient pressure')
                brain.started = time.monotonic()
                brain._refresh()
                brain._arm(tuple(squeezed[k] for k in ('lift','elbow','pitch')), torque=450, speed=5, label='return to recorded support')
                brain._move({'left:6': 2045}, torque=450, speed=12, label='release with support')
                recorder.keyframe('supported-release')
                brain._go(arm.ik(18, 8, 130, min_bend=22), 'clear high observation', speed=8, torque=450)
                recorder.keyframe('recovered-high')
                result['ok'] = True
            elif args.action == 'search':
                ex.start('high observation search, no descent')
                brain.started = time.monotonic()
                brain._refresh()
                for n in range(4):
                    ex.hold()
                    try:
                        result['bbox'] = obs.locate()
                        result['ok'] = True
                        break
                    except GraspFailure:
                        if n == 3:
                            raise
                        f = brain._tip()
                        if f['tip'][1] < brain.cfg['desk_z']+3:
                            raise GraspFailure('Search must stay high and within the bounded rear workspace')
                        # Retain the downward camera angle while stepping
                        # back. Turning it forward to 108 hid near fixtures.
                        # At the rear boundary, look further down with the
                        # same high tip instead of moving into the chassis.
                        if f['tip'][0]-2 >= 16:
                            q = arm.ik(f['tip'][0]-2, f['tip'][1]+2,
                                       max(130, f['t3']), min_bend=22)
                        elif f['t3'] < 148:
                            q = arm.ik(f['tip'][0], f['tip'][1],
                                       min(150, f['t3']+15), min_bend=22)
                        else:
                            raise GraspFailure('Bounded high search exhausted; target remains unconfirmed')
                        start = tuple(brain.q[k] for k in ('lift', 'elbow', 'pitch'))
                        if arm.segment_min_z(start, q)[0] < f['tip'][1]-.3:
                            raise GraspFailure('Search interpolation would descend')
                        brain._arm(q, torque=450, speed=6, label='local search: high retreat 2cm')
                        recorder.keyframe('search-high-retreat')
            else:
                # Every new trial begins with an open empty gripper at high
                # clearance; the original survey path opened it, high did not.
                state_by_id = {m['id']:m for p in state['ports'] if p['port']==('COM4' if args.side=='left' else 'COM3') for m in p['motors']}
                if state_by_id[6]['position'] < 1950:
                    ex.start('prepare open jaws at high clearance')
                    brain.started = time.monotonic()
                    brain.prepare_open()
                    ex.end('open-jaw preparation complete')
                result['bbox'] = brain.acquire()
                if ex.active:
                    ex.end('local acquisition complete')
                result.update(brain.run_pick_place())
            result['controller'] = brain.status()
            result['contact'] = ex.contact
            result['servo_receipt'] = request('/api/servo/status')
        result['detections'] = obs.detections
    except Exception as exc:
        result.update(ok=False, error=type(exc).__name__+': '+str(exc))
    finally:
        if ex:
            ex.end('local trial complete')
            if 'servo_receipt' not in result and ex.inner.session_id:
                try:
                    receipt = request('/api/servo/status')
                    source=Path(receipt.get('dir') or '')
                    session=source/'session.json'
                    matches=(receipt.get('session_id') == ex.inner.session_id or
                             ('servo-sessions' in source.parts and session.exists() and
                              json.loads(session.read_text(encoding='utf8')).get('session_id')==ex.inner.session_id))
                    if matches:
                        result['servo_receipt'] = receipt
                except Exception as exc:
                    result['receipt_error'] = type(exc).__name__
            if 'brain' in locals():
                result.setdefault('controller', brain.status())
            result.setdefault('contact', ex.contact)
        if obs:
            try:
                obs.close()
            except Exception as exc:
                result.update(ok=False, observer_error=str(exc))
        result.update(finished_at=datetime.now().astimezone().isoformat(), captures=recorder.captures,
                      frame_receipts=frames.receipts, detections=obs.detections if obs else [])
        result['entered_closure'] = any(e.get('phase')=='close' and e.get('kind')=='step'
            and any(k.endswith(':6') for k in e.get('targets',{})) for e in recorder.events)
        (root/'result.json').write_text(json.dumps(result, ensure_ascii=False, default=str, indent=2), encoding='utf-8')
        # Existing history importer understands paired control/photo archives.
        # Keep private images and receipt copies on the laptop, outside Git.
        if result.get('servo_receipt', {}).get('dir'):
            source = Path(result['servo_receipt']['dir']).resolve()
            if 'servo-sessions' in source.parts:
                with zipfile.ZipFile(root.parent/(root.name+'-control.zip'), 'w', zipfile.ZIP_DEFLATED) as z:
                    for p in source.iterdir():
                        if p.is_file():
                            z.write(p, p.name)
                with zipfile.ZipFile(root.parent/(root.name+'.zip'), 'w', zipfile.ZIP_DEFLATED) as z:
                    for p in root.iterdir():
                        if p.is_file():
                            z.write(p, p.name)
                    captures = []
                    for cap in recorder.captures:
                        if not cap['frames']:
                            continue
                        name = cap['frames'][0]['file'].rsplit('-cam', 1)[0]
                        captures.append({'name': name, 'frames': cap['frames']})
                    z.writestr('log.json', json.dumps({'captures': captures}, ensure_ascii=False))
    print(json.dumps({k: v for k, v in result.items() if k not in ('captures', 'frame_receipts', 'controller', 'servo_receipt','detections')}, ensure_ascii=False))


if __name__ == '__main__':
    main()
