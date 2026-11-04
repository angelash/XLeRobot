"""Grasp knowledge that outlives a run (MANIPULATION_V2_ZH.md R6/R7).

What an object looks like from the survey pose, which settings picked it up,
how often the local finder agreed with the large model's box, and what the
site and chassis taught us. Plain files under platform-data/grasp/knowledge/:
knowledge.json plus grayscale templates. The local finder is multi-scale
normalized template matching at half resolution: no model download, about a
hundred milliseconds on the laptop CPU, and it abstains below a score so the
large model takes over. Matching runs on gradient magnitude: on the simulated
wrist view it kept the right place under 1-3 cm lateral shifts where plain
intensity matching jumped to the wrong spot.
"""
from __future__ import annotations

import json
import copy
import re
import threading
import time
from pathlib import Path

import cv2
import numpy as np

from .force_policy import assess_grip, normalize_profile

MAX_TEMPLATES = 8
SCALES = (0.8, 0.9, 1.0, 1.12, 1.25)
MIN_SCORE = 0.6
MARGIN_PX = 6
# Open jaws at the bottom corners of the 640x480 wrist view (2026-11-03 frames). From
# the survey pose a target is ahead of the tips, never centred inside a jaw.
JAW_ZONES = ((0, 330, 330, 480), (470, 330, 640, 480))


def iou(a, b):
    ax, ay, aw, ah = a; bx, by, bw, bh = b
    ix = max(0.0, min(ax + aw, bx + bw) - max(ax, bx)); iy = max(0.0, min(ay + ah, by + bh) - max(ay, by))
    inter = ix*iy
    union = aw*ah + bw*bh - inter
    return inter/union if union > 0 else 0.0


def _now():
    return time.strftime('%Y-%m-%dT%H:%M:%S%z')


def gradient(gray):
    g = cv2.GaussianBlur(gray, (3, 3), 0)
    return cv2.magnitude(cv2.Sobel(g, cv2.CV_32F, 1, 0), cv2.Sobel(g, cv2.CV_32F, 0, 1))


class Knowledge:
    def __init__(self, root):
        self.root = Path(root)
        (self.root/'templates').mkdir(parents=True, exist_ok=True)
        self.lock = threading.Lock()
        self._images = {}
        self.data = self._load()

    # ---- storage -----------------------------------------------------------
    def _load(self):
        try:
            data = json.loads((self.root/'knowledge.json').read_text(encoding='utf-8'))
            if isinstance(data, dict) and data.get('version') == 1:
                return data
        except (OSError, ValueError):
            pass
        return {'version': 1, 'objects': {}, 'site': {}, 'base': [], 'episodes': []}

    def _save(self):
        tmp = self.root/'knowledge.tmp'
        tmp.write_text(json.dumps(self.data, ensure_ascii=False, indent=1), encoding='utf-8')
        tmp.replace(self.root/'knowledge.json')

    @staticmethod
    def name(value):
        value = str(value or 'plug').strip().lower()
        if not re.fullmatch(r'[a-z0-9_-]{1,32}', value):
            raise ValueError('物体名只能用小写字母、数字、下划线和连字符')
        return value

    def _object(self, name):
        return self.data['objects'].setdefault(name, {'templates': [], 'params': {}, 'stats': {
            'attempts': 0, 'successes': 0, 'by_source': {}, 'agreement': []}})

    # ---- learning ----------------------------------------------------------
    def learn(self, name, gray, bbox, source, params=None, run=None):
        """Keep the selected view of a successfully handled object (and the settings that worked)."""
        name = self.name(name)
        x, y, w, h = (int(round(v)) for v in bbox)
        H, W = gray.shape[:2]
        x0, y0 = max(0, x - MARGIN_PX), max(0, y - MARGIN_PX)
        x1, y1 = min(W, x + w + MARGIN_PX), min(H, y + h + MARGIN_PX)
        if x1 - x0 < 16 or y1 - y0 < 16:
            raise ValueError('目标框太小，不能作为模板')
        crop = gray[y0:y1, x0:x1]
        with self.lock:
            obj = self._object(name)
            file = f'{name}-{time.strftime("%Y%m%d-%H%M%S")}-{len(obj["templates"])}.png'
            cv2.imwrite(str(self.root/'templates'/file), crop)
            obj['templates'].append({'file': file, 'bbox': [x, y, w, h], 'offset': [x - x0, y - y0], 'source': source,
                                     'run': run, 'at': _now()})
            while len(obj['templates']) > MAX_TEMPLATES:
                old = obj['templates'].pop(0)
                (self.root/'templates'/old['file']).unlink(missing_ok=True)
                self._images.pop(old['file'], None)
            if params:
                obj['params'] = {**params, 'from_run': run, 'at': _now()}
            self._save()
        return {'object': name, 'templates': len(obj['templates'])}

    def record(self, name, ok, source, run=None, agreement=None, seconds=None, steps=None, llm_calls=None, note=''):
        name = self.name(name)
        with self.lock:
            stats = self._object(name)['stats']
            stats['attempts'] += 1; stats['successes'] += int(bool(ok))
            kind = 'local' if str(source).startswith('local') else 'llm'
            row = stats['by_source'].setdefault(kind, {'attempts': 0, 'successes': 0})
            row['attempts'] += 1; row['successes'] += int(bool(ok))
            if agreement is not None:
                stats['agreement'] = (stats['agreement'] + [round(float(agreement), 3)])[-50:]
            self.data['episodes'] = (self.data['episodes'] + [{
                'object': name, 'ok': bool(ok), 'source': source, 'run': run, 'agreement': agreement, 'seconds': seconds,
                'steps': steps, 'llm_calls': llm_calls, 'note': str(note)[:160], 'at': _now()}])[-200:]
            self._save()

    def note_site(self, key, value, evidence):
        with self.lock:
            self.data['site'][str(key)[:40]] = {'value': value, 'evidence': str(evidence)[:200], 'at': _now()}
            self._save()

    def note_base(self, requested_m, estimate_m, stopped_by, image_shift_px=None, run=None):
        with self.lock:
            self.data['base'] = (self.data['base'] + [{'requested_m': requested_m, 'estimate_m': estimate_m,
                                                       'stopped_by': stopped_by, 'image_shift_px': image_shift_px,
                                                       'run': run, 'at': _now()}])[-100:]
            self._save()

    def params(self, name):
        obj = self.data['objects'].get(self.name(name))
        return dict(obj['params']) if obj and obj.get('params') else {}

    def profile(self, name):
        name = self.name(name)
        with self.lock:
            obj = self.data['objects'].get(name) or {}
            profile = copy.deepcopy(obj.get('profile') or normalize_profile({}))
            history = copy.deepcopy(obj.get('profile_history', []))
        return {'object': name, 'profile': profile, 'history': history, 'assessment': assess_grip(profile)}

    def set_profile(self, name, profile):
        name, profile = self.name(name), normalize_profile(profile)
        with self.lock:
            previous = copy.deepcopy(self.data)
            obj = self._object(name)
            obj['profile_history'] = (obj.get('profile_history', []) + [
                {'at': _now(), 'previous': copy.deepcopy(obj.get('profile')), 'profile': profile}])
            obj['profile'] = profile
            try:
                self._save()
            except OSError:
                self.data = previous
                raise
        return self.profile(name)

    # ---- local finder ------------------------------------------------------
    def _image(self, file):
        if file not in self._images:
            img = cv2.imread(str(self.root/'templates'/file), cv2.IMREAD_GRAYSCALE)
            self._images[file] = img
        return self._images[file]

    def locate(self, gray, name=None, min_score=MIN_SCORE):
        """Best template match over the stored views; None-score result when nothing is known."""
        small = gradient(cv2.resize(gray, (gray.shape[1]//2, gray.shape[0]//2), interpolation=cv2.INTER_AREA))
        best = {'ok': False, 'score': None, 'object': None, 'bbox': None, 'template': None}
        objects = self.data['objects'].items() if name is None else [(self.name(name), self.data['objects'].get(self.name(name)))]
        started = time.perf_counter()
        for obj_name, obj in objects:
            for t in (obj or {}).get('templates', []):
                tpl = self._image(t['file'])
                if tpl is None:
                    continue
                for s in SCALES:
                    tw, th = int(tpl.shape[1]*s/2), int(tpl.shape[0]*s/2)
                    if tw < 8 or th < 8 or tw >= small.shape[1] or th >= small.shape[0]:
                        continue
                    res = cv2.matchTemplate(small, gradient(cv2.resize(tpl, (tw, th), interpolation=cv2.INTER_AREA)), cv2.TM_CCOEFF_NORMED)
                    ox, oy = t.get('offset', [0, 0])
                    bw, bh = t['bbox'][2]*s, t['bbox'][3]*s
                    for zx0, zy0, zx1, zy1 in JAW_ZONES:
                        # result cell (u, v) puts the box centre at (2u + ox*s + bw/2, 2v + oy*s + bh/2)
                        u0 = max(0, int(np.floor((zx0 - ox*s - bw/2)/2))); u1 = max(0, int(np.ceil((zx1 - ox*s - bw/2)/2)))
                        v0 = max(0, int(np.floor((zy0 - oy*s - bh/2)/2))); v1 = max(0, int(np.ceil((zy1 - oy*s - bh/2)/2)))
                        res[v0:v1 + 1, u0:u1 + 1] = -1.0
                    _, score, _, loc = cv2.minMaxLoc(res)
                    if not np.isfinite(score):
                        continue
                    if best['score'] is None or score > best['score']:
                        best = {'ok': False, 'score': round(float(score), 3), 'object': obj_name, 'template': t['file'],
                                'scale': s, 'bbox': [round(loc[0]*2 + ox*s, 1), round(loc[1]*2 + oy*s, 1), round(bw, 1), round(bh, 1)]}
        best['ok'] = best['score'] is not None and best['score'] >= min_score
        best['ms'] = round((time.perf_counter() - started)*1000, 1)
        return best

    # ---- reporting ---------------------------------------------------------
    def summary(self):
        with self.lock:
            objects = {}
            for name, obj in self.data['objects'].items():
                s = obj['stats']
                agree = s.get('agreement') or []
                objects[name] = {'templates': len(obj['templates']), 'params': obj.get('params') or {},
                                 'profile': copy.deepcopy(obj.get('profile')), 'grip_assessment': assess_grip(obj.get('profile')),
                                 'attempts': s['attempts'], 'successes': s['successes'], 'by_source': s['by_source'],
                                 'agreement_n': len(agree), 'agreement_ok_rate': round(sum(a >= 0.5 for a in agree)/len(agree), 3) if agree else None}
            return {'objects': objects, 'site': dict(self.data['site']), 'base_legs': len(self.data['base']),
                    'base_recent': self.data['base'][-5:], 'episodes': self.data['episodes'][-10:]}

