"""Describe missing depth and rank observation targets, without granting travel.

Image cells count every supplied depth pixel. Angular cells sample image rays in
one AR session; they describe views, not floor occupancy or room completeness.
All reports can be rebuilt from retained RGB-D sources; no source is rewritten.
"""
from __future__ import annotations

from collections import OrderedDict
import json
import math
import struct
import threading

from .evidence import IDENTIFIER

COLS, ROWS = 6, 8
AZIMUTHS, ELEVATIONS = 24, 6


def _pose(meta):
    q = meta.get('pose_quaternion') or []
    if not (meta.get('pose_valid') and meta.get('tracking') == 'TRACKING'
            and meta.get('arcore_capture_session') and len(q) == 4
            and all(isinstance(v, (int, float)) and math.isfinite(v) for v in q)
            and abs(sum(v*v for v in q)-1) < .01):
        return None
    x, y, z, a = q
    return ((1-2*(y*y+z*z), 2*(x*y-z*a), 2*(x*z+y*a)),
            (2*(x*y+z*a), 1-2*(x*x+z*z), 2*(y*z-x*a)),
            (2*(x*z-y*a), 2*(y*z+x*a), 1-2*(x*x+y*y)))


def _world_ray(px, py, meta, matrix):
    k = meta['intrinsics']
    x, y = (px-k['tx'])/k['fx'], (py-k['ty'])/k['fy']
    x, y = {0: (x, y), 90: (y, -x), 180: (-x, -y), 270: (-y, x)}[meta.get('rotation_deg', 0)]
    ray = (x, -y, -1.)
    return [sum(a*b for a, b in zip(row, ray)) for row in matrix]


def _angular(ray):
    x, y, z = ray
    az = math.degrees(math.atan2(x, -z)) % 360
    elevation = math.degrees(math.atan2(y, math.hypot(x, z)))
    return int(az/15) % AZIMUTHS, min(ELEVATIONS-1, max(0, int((elevation+90)/30)))


def frame_coverage(meta, raw):
    w, h = meta.get('width'), meta.get('height')
    if type(w) is not int or type(h) is not int or not 1 <= w <= 4096 or not 1 <= h <= 4096 or len(raw) != w*h*4:
        raise ValueError('深度尺寸或原始数组不一致')
    cells = [{'row': r, 'col': c, 'pixels': 0, 'in_range': 0, 'missing': 0, 'too_near': 0, 'too_far': 0}
             for r in range(ROWS) for c in range(COLS)]
    matrix = _pose(meta)
    k = meta.get('intrinsics') or {}
    if matrix and (meta.get('rotation_deg', 0) not in (0, 90, 180, 270)
                   or not all(isinstance(k.get(n), (int, float)) and math.isfinite(k[n]) for n in ('fx', 'fy', 'tx', 'ty'))
                   or min(k.get('fx', 0), k.get('fy', 0)) <= 0):
        matrix = None
    angular = {}
    # Bound angular work; image-space statistics below still count every pixel.
    stride = max(1, math.ceil(math.sqrt(w*h/512)))
    for i, (z,) in enumerate(struct.iter_unpack('<f', raw)):
        py, px = divmod(i, w)
        cell = cells[min(ROWS-1, py*ROWS//h)*COLS+min(COLS-1, px*COLS//w)]
        cell['pixels'] += 1
        valid = math.isfinite(z) and .2 <= z <= 5.
        field = 'in_range' if valid else 'missing' if not math.isfinite(z) or z <= 0 else 'too_near' if z < .2 else 'too_far'
        cell[field] += 1
        if matrix and px % stride == 0 and py % stride == 0:
            a, e = _angular(_world_ray(px, py, meta, matrix))
            entry = angular.setdefault((a, e), {'azimuth': a, 'elevation': e, 'rays': 0, 'in_range': 0})
            entry['rays'] += 1
            entry['in_range'] += int(valid)
    for cell in cells:
        cell['fraction'] = cell['in_range']/cell['pixels'] if cell['pixels'] else None
    regions = []
    for c, label in enumerate(('画面左侧', '画面中间', '画面右侧')):
        chosen = [v for v in cells if v['row'] >= ROWS//2 and v['col']//2 == c]
        total = sum(v['pixels'] for v in chosen)
        supplied = sum(v['in_range'] for v in chosen)
        regions.append({'id': ('left', 'center', 'right')[c], 'label': label+'下半部',
                        'pixels': total, 'in_range': supplied, 'missing_fraction': 1-supplied/total if total else None})
    regions.sort(key=lambda v: (-(v['missing_fraction'] or 0), v['id'] != 'center'))
    total_valid = sum(v['in_range'] for v in cells)
    forward = _world_ray(k['tx'], k['ty'], meta, matrix) if matrix else None
    return {'schema': 1, 'columns': COLS, 'rows': ROWS, 'width': w, 'height': h,
            'cells': cells, 'regions': regions, 'pixels': w*h, 'in_range': total_valid,
            'fraction': total_valid/(w*h), 'analysis_window_m': [.2, 5.],
            'angular': list(angular.values()), 'capture_session': meta.get('arcore_capture_session') if matrix else None,
            'forward_azimuth_deg': math.degrees(math.atan2(forward[0], -forward[2])) % 360 if forward else None,
            'pose_translation': meta.get('pose_translation') if matrix else None,
            'raw_high_confidence_fraction': meta.get('raw_depth_high_confidence_fraction'),
            'confidence_scope': 'raw_depth_only_not_per_pixel_confidence_of_smoothed_depth',
            'calibrated': False, 'registered_robot_map': False, 'motion_enabled': False,
            'semantics': 'supplied_depth_estimates_in_analysis_window_not_verified_surfaces_or_free_space'}


def combine(reports, *, capture_session):
    bins = [{'azimuth': a, 'elevation': e, 'rays': 0, 'in_range': 0, 'view_frames': 0}
            for e in range(ELEVATIONS) for a in range(AZIMUTHS)]
    accepted = 0
    last = None
    for report in reports:
        if not capture_session or report.get('capture_session') != capture_session:
            continue
        accepted += 1
        last = report
        for cell in report['angular']:
            target = bins[cell['elevation']*AZIMUTHS+cell['azimuth']]
            for key in ('rays', 'in_range'):
                target[key] += cell[key]
            target['view_frames'] += 1
    for cell in bins:
        cell['fraction'] = cell['in_range']/cell['rays'] if cell['rays'] else None
        cell['state'] = 'unseen' if not cell['rays'] else 'depth_gap' if cell['fraction'] < .5 else 'estimated'
    forward = last.get('forward_azimuth_deg') if last else None
    # Select views near the camera's horizontal plane. Unknown directions are
    # observation targets only; no missing point can authorize driving there.
    headings = []
    reference = last.get('pose_translation') if last else None
    local = [r for r in reports if r.get('capture_session') == capture_session
             and reference and len(reference) == 3 and len(r.get('pose_translation') or []) == 3
             and sum((a-b)**2 for a,b in zip(reference,r['pose_translation'])) <= .5**2]
    local_cells = [c for r in local for c in r['angular']]
    for a in range(AZIMUTHS):
        band = [c for c in local_cells if c['azimuth'] == a and c['elevation'] in (2, 3)]
        rays, supplied = sum(c['rays'] for c in band), sum(c['in_range'] for c in band)
        missing = 1-supplied/rays if rays else 1.
        delta = ((a*15+7.5-(forward or 0)+180) % 360)-180
        headings.append({'azimuth_deg': a*15+7.5, 'relative_camera_deg': round(delta, 1),
                         'state': 'unseen' if not rays else 'depth_gap' if missing > .5 else 'estimated',
                         'missing_fraction': missing, 'view_rays': rays,
                         'score': round(missing + (.15 if not rays else 0) - abs(delta)/180*.12, 4),
                         'motion_enabled': False})
    headings.sort(key=lambda v: -v['score'])
    return {'capture_session': capture_session, 'azimuth_columns': AZIMUTHS, 'elevation_rows': ELEVATIONS,
            'cells': bins, 'source_frames': accepted, 'last': last,
            'targets': headings[:5] if local else [], 'current_forward_azimuth_deg': forward,
            'target_reference_radius_m': .5, 'target_source_frames': len(local),
            'coordinates': 'arcore_session_view_directions_not_robot_headings',
            'coverage_scope': 'selected_model_part_sampled_views_not_room_or_floor_coverage',
            'motion_enabled': False, 'registered_robot_map': False, 'metric_accuracy_verified': False}


class Coverage:
    def __init__(self, evidence):
        self.evidence = evidence
        self.lock = threading.RLock()
        self.cache = OrderedDict()
        self.capture_cache = OrderedDict()

    def _capture(self, identifier):
        if identifier in self.capture_cache:
            self.capture_cache.move_to_end(identifier)
            return self.capture_cache[identifier]
        value = self.evidence.read('captures', identifier)
        folder = self.evidence.folder('captures', identifier)
        sidecar = folder/'coverage.json'
        report = json.loads(self.evidence.media('captures',identifier,'coverage.json').read_text('utf8')) if sidecar.is_file() else frame_coverage(value['meta'], self.evidence.media('captures', identifier, 'depth.f32').read_bytes())
        result = {**report, 'capture_id': identifier, 'saved_at': value['saved_at'],
                'rgb_url': self.url('captures', identifier, 'rgb.jpg'),
                'depth_url': self.url('captures', identifier, 'depth.jpg') if (folder/'depth.jpg').is_file() else None}
        self.capture_cache[identifier] = result
        while len(self.capture_cache) > 512:
            self.capture_cache.popitem(last=False)
        return result

    @staticmethod
    def url(kind, identifier, name):
        return f'/api/platform/phone-vision/evidence/{kind}/{identifier}/{name}'

    def read(self, kind, identifier):
        if kind not in ('captures', 'models') or not isinstance(identifier, str) or not IDENTIFIER.fullmatch(identifier):
            raise ValueError('请选择已保存的点云或RGB-D观察')
        with self.lock:
            meta = self.evidence.read(kind, identifier)
            key = (kind, identifier, meta.get('source_frames'), meta.get('updated_at'))
            if key in self.cache:
                self.cache.move_to_end(key)
                return self.cache[key]
            missing, reports = [], []
            ids = meta.get('source_captures', []) if kind == 'models' else [identifier]
            if len(ids) > 500:
                raise ValueError('点云分卷源帧超过读取预算')
            for source in ids:
                try:
                    reports.append(self._capture(source))
                except (KeyError, ValueError, OSError):
                    missing.append(source)
            session = meta.get('capture_session') if kind == 'models' else meta.get('meta', {}).get('arcore_capture_session')
            result = combine(reports, capture_session=session)
            result.update(kind=kind, id=identifier, label=meta.get('label'),
                          source_captures=len(ids), unavailable_captures=missing,
                          preview_url=self.url(kind, identifier, 'preview.jpg' if kind == 'models' else 'rgb.jpg'),
                          cloud_url=self.url(kind, identifier, 'cloud.json') if meta.get('point_count', 0) else None,
                          point_count=meta.get('point_count', 0), historical=True,
                          sources=[{'id': r['capture_id'], 'saved_at': r['saved_at'], 'fraction': r['fraction'], 'rgb_url': r['rgb_url']} for r in reports],
                          last_observation=reports[-1] if reports else None,
                          plan={'action': 'inspect_missing_views', 'motion_enabled': False,
                                'reason': '按点阵缺口安排观察；缺点、遮挡和范围外估计均不表示可走',
                                'requires': ['fresh_rgb_clearance', 'full_body_and_near_wheel_clearance', 'original_motion_guards', 'same_session_pose_or_relocalization']})
            self.cache[key] = result
            while len(self.cache) > 8:
                self.cache.popitem(last=False)
            return result
