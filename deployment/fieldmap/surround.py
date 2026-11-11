"""Persistent surround photographs and conservative AR floor-plan previews.

No actuator calls. A measured view is never a free-space certificate. Head
scans and AR sessions retain their independent reference frames and sources.
"""
from __future__ import annotations

from datetime import datetime
from io import BytesIO
import hashlib
import json
import math
from pathlib import Path
import re
import threading
import uuid

import numpy as np
from PIL import Image

from .store import atomic_json

ID = re.compile(r'^[a-f0-9]{32}$')


def stamp():
    return datetime.now().astimezone().isoformat()


def angle_delta(a, b):
    return (a-b+180) % 360-180


def circular_coverage(mask):
    """Each Boolean represents five degrees; wraparound gaps count once."""
    if len(mask) != 72:
        raise ValueError('需要72个方位格')
    gaps = []
    if not any(mask):
        gaps = [{'start_deg': -180, 'end_deg': 180, 'degrees': 360}]
    elif not all(mask):
        start = next(i for i, v in enumerate(mask) if v)
        opened = None
        for step in range(1, 73):
            i = (start+step) % 72
            if not mask[i] and opened is None:
                opened = i
            if mask[i] and opened is not None:
                count = (i-opened) % 72
                gaps.append({'start_deg': opened*5-180, 'end_deg': i*5-180, 'degrees': count*5})
                opened = None
    return {'seen_degrees': sum(mask)*5, 'missing_degrees': (72-sum(mask))*5,
            'largest_gap_degrees': max((v['degrees'] for v in gaps), default=0),
            'gaps': gaps, 'complete_360': all(mask), 'sectors': list(map(bool, mask)),
            'semantics': 'photographic_view_coverage_not_room_or_traversability_coverage'}


def estimate_focal(views):
    """Fit horizontal focal length to overlapping RGB and encoder yaw changes.

    The estimate is local and rotation-only; close parallax/dynamic objects can
    reject pairs. It is not metric optical or robot-extrinsic calibration.
    """
    import cv2
    estimates = []
    failures = []
    orb = cv2.ORB_create(nfeatures=1600)
    cache = {}
    for n, v in enumerate(views):
        im = cv2.imdecode(np.frombuffer(v['rgb'], np.uint8), cv2.IMREAD_GRAYSCALE)
        if im is None:
            raise ValueError('环视照片无法解码')
        cache[n] = (im, *orb.detectAndCompute(im, None))
    for i in range(len(views)-1):
        a, b = views[i], views[i+1]
        if abs(a['pitch_deg']-b['pitch_deg']) > 1:
            continue
        delta = math.radians(angle_delta(b['heading_deg'], a['heading_deg']))
        if not 8 <= abs(math.degrees(delta)) <= 42:
            continue
        ia, ka, da = cache[i]
        ib, kb, db = cache[i+1]
        if ia.shape != ib.shape or da is None or db is None:
            continue
        pairs = cv2.BFMatcher(cv2.NORM_HAMMING).knnMatch(da, db, k=2)
        good = [p[0] for p in pairs if len(p) == 2 and p[0].distance < .72*p[1].distance]
        if len(good) < 24:
            failures.append({'pair': [i, i+1], 'reason': 'insufficient_features', 'matches': len(good)})
            continue
        pa = np.float32([ka[m.queryIdx].pt for m in good])
        pb = np.float32([kb[m.trainIdx].pt for m in good])
        _, mask = cv2.findHomography(pa, pb, cv2.RANSAC, 3.)
        if mask is None or np.count_nonzero(mask) < 20:
            continue
        pa, pb = pa[mask.ravel() != 0], pb[mask.ravel() != 0]
        w = ia.shape[1]
        x1, x2 = pa[:, 0]-(w-1)/2, pb[:, 0]-(w-1)/2
        candidates = np.linspace(w*.32, w*1.65, 200)
        errors = np.median(np.abs(np.arctan(x1[None, :]/candidates[:, None])-
                                   np.arctan(x2[None, :]/candidates[:, None])-delta), axis=1)
        n = int(np.argmin(errors))
        residual = float(np.degrees(errors[n]))
        if residual < 1.8 and 2 < n < len(candidates)-3 and np.ptp(pa[:, 0]) > w*.2:
            estimates.append({'focal_px': float(candidates[n]), 'residual_deg': residual,
                              'matches': len(pa), 'pair': [i, i+1]})
    if len(estimates) >= 2:
        focal = float(np.median([v['focal_px'] for v in estimates]))
        spread = float(np.median([abs(v['focal_px']-focal) for v in estimates])/focal)
        if spread <= .18:
            return {'focal_px': focal, 'method': 'RGB_features_and_encoder_yaw_fit',
                    'pair_count': len(estimates), 'relative_spread': spread, 'pairs': estimates,
                    'metric_calibrated': False}
    # Use a conservative central crop until optical calibration is available.
    width = cache[0][0].shape[1]
    return {'focal_px': width*.9, 'method': 'provisional_pinhole_central_crop',
            'pair_count': len(estimates), 'pairs': estimates, 'failures': failures,
            'metric_calibrated': False}


def panorama(views, focal):
    """Paint only captured rays into a 360-degree canvas; leave holes visible."""
    import cv2
    width, height = 2048, 1024
    yy, xx = np.indices((height, width))
    yaw = (xx+.5)/width*2*np.pi-np.pi
    elevation = np.pi/2-(yy+.5)/height*np.pi
    world = (np.sin(yaw)*np.cos(elevation), np.sin(elevation), np.cos(yaw)*np.cos(elevation))
    out = np.empty((height, width, 3), np.uint8)
    out[:] = (17, 25, 21)
    out[((xx+yy)//12) % 7 == 0] = (42, 52, 43)
    score = np.zeros((height, width), np.float32)
    owners = np.full((height, width), -1, np.int16)
    for i, view in enumerate(views):
        rgb = np.asarray(Image.open(BytesIO(view['rgb'])).convert('RGB'))
        h, w = rgb.shape[:2]
        f = focal['focal_px']
        y, p = math.radians(view['heading_deg']), math.radians(view['pitch_deg'])
        rx = math.cos(y)*world[0]-math.sin(y)*world[2]
        rz = math.sin(y)*world[0]+math.cos(y)*world[2]
        ry = math.cos(p)*world[1]-math.sin(p)*rz
        depth = math.sin(p)*world[1]+math.cos(p)*rz
        u = (w-1)/2+f*rx/np.maximum(depth, 1e-8)
        v = (h-1)/2-f*ry/np.maximum(depth, 1e-8)
        # Peripheral lens distortion is uncalibrated: retain central 76%.
        valid = (depth > 0) & (u >= w*.12) & (u < w*.88) & (v >= h*.08) & (v < h*.92)
        merit = (1-np.abs(u-(w-1)/2)/(w/2))*(1-np.abs(v-(h-1)/2)/(h/2))
        choose = valid & (merit >= score)
        sampled = cv2.remap(rgb, u.astype(np.float32), v.astype(np.float32), cv2.INTER_LINEAR)
        out[choose] = sampled[choose]
        score[choose] = merit[choose]
        owners[choose] = i
    sectors = []
    details = []
    for i in range(72):
        patch = owners[int(height*.4):int(height*.75), round(i*width/72):round((i+1)*width/72)]
        valid = patch[patch >= 0]
        covered = len(valid) >= patch.size*.08
        sectors.append(covered)
        details.append({'index': i, 'start_deg': i*5-180, 'observed': bool(covered),
                        'source_indices': sorted(map(int, np.unique(valid)))})
    result = BytesIO()
    Image.fromarray(out).save(result, 'JPEG', quality=88)
    observed_rows = np.flatnonzero(np.any(owners >= 0, axis=1))
    crop = [max(0, int(observed_rows[0])-16), min(height, int(observed_rows[-1])+17)] if len(observed_rows) else [0, height]
    return result.getvalue(), {**circular_coverage(sectors), 'sector_sources': details,
                              'preview_rows': crop,
                              'projection': 'approximate_pinhole_yaw_pitch_to_equirectangular',
                              'missing_pixels_filled': False, 'simultaneous_exposure': False}


def floor_plan(cloud):
    """Top-down, bounded 5cm AR estimate cells. No inferred free-space rays."""
    positions = cloud.get('positions', [])
    if not isinstance(positions, list) or len(positions) % 3 or len(positions) > 180000:
        raise ValueError('点云格式或读取预算无效')
    points = np.asarray(positions, dtype=np.float64).reshape((-1, 3))
    if len(points) and not np.all(np.isfinite(points)):
        raise ValueError('点云包含非有限坐标')
    trajectory = cloud.get('trajectory') or []
    origin = trajectory[0] if trajectory else [0., 0., 0.]
    if len(origin) != 3 or not all(math.isfinite(v) for v in origin):
        raise ValueError('相机会话参考位置无效')
    cells = {}
    # Candidate horizontal surface from supported low-y bins; never certify it.
    candidates = points[(points[:, 1] < origin[1]-.25) & (points[:, 1] > origin[1]-2.)] if len(points) else points
    plane = None
    if len(candidates) >= 100:
        values, counts = np.unique(np.round(candidates[:, 1]/.04).astype(int), return_counts=True)
        center = values[int(np.argmax(counts))]*.04
        inliers = candidates[np.abs(candidates[:, 1]-center) <= .05]
        if len(inliers) >= max(100, len(points)*.12) and np.ptp(inliers[:, 0]) >= .7 and np.ptp(inliers[:, 2]) >= .7:
            plane = {'height_ar_m': float(np.median(inliers[:, 1])), 'support_points': len(inliers),
                     'status': 'unvalidated_horizontal_surface_candidate', 'ground_calibrated': False}
    for x, y, z in points:
        dx, dz = x-origin[0], z-origin[2]
        if abs(dx) > 8 or abs(dz) > 8:
            continue
        key = (math.floor(dx/.05), math.floor(dz/.05))
        row = cells.setdefault(key, {'samples': 0, 'floor': 0, 'obstacle': 0, 'low': float(y), 'high': float(y)})
        row['samples'] += 1
        row['low'], row['high'] = min(row['low'], y), max(row['high'], y)
        if plane:
            dy = y-plane['height_ar_m']
            row['floor'] += int(abs(dy) <= .06)
            row['obstacle'] += int(.12 <= dy <= 1.8)
    output = [{'x': x, 'z': z, 'points': v['samples'],
               'kind': 'obstacle_candidate' if v['obstacle'] >= 2 else 'floor_candidate' if v['floor'] >= 2 else 'surface',
               'height_span_ar_m': round(v['high']-v['low'], 3)} for (x, z), v in cells.items()]
    return {'resolution_ar_m': .05, 'cells': output, 'point_count': len(points), 'surface_candidate': plane,
            'origin_ar': list(origin), 'camera_trajectory_ar': trajectory[-1000:],
            'bounds_cells': [min((r['x'] for r in output), default=-30), max((r['x'] for r in output), default=30),
                             min((r['z'] for r in output), default=-30), max((r['z'] for r in output), default=30)],
            'unknown_default': True, 'verified_free_cells': 0, 'metric_accuracy_verified': False,
            'registered_robot_map': False, 'motion_enabled': False,
            'coordinates': 'same_AR_session_xz_projection_not_robot_or_room_frame'}


class Surround:
    def __init__(self, root, evidence_provider, fieldtests_root=None):
        self.root = Path(root)
        self.evidence_provider = evidence_provider
        self.fieldtests_root = Path(fieldtests_root) if fieldtests_root else None
        self.lock = threading.RLock()
        self.cache = {}

    def folder(self, identifier):
        if not isinstance(identifier, str) or not ID.fullmatch(identifier):
            raise ValueError('环视记录编号无效')
        return self.root/identifier

    def list(self):
        items = []
        for path in sorted(self.root.glob('*/scan.json'), key=lambda p: p.stat().st_mtime, reverse=True)[:100]:
            if not ID.fullmatch(path.parent.name):
                continue
            value = json.loads(path.read_text('utf8'))
            items.append({k: value[k] for k in ('id', 'label', 'created_at', 'source_type', 'view_count', 'coverage')})
        return {'items': items, 'motion_enabled': False, 'retention': 'immutable_scans_no_automatic_deletion'}

    def read(self, identifier):
        path = self.folder(identifier)/'scan.json'
        if not path.is_file():
            raise KeyError('环视记录不存在')
        return json.loads(path.read_text('utf8'))

    def media(self, identifier, name):
        if name not in ('panorama.jpg', 'preview.jpg') and not re.fullmatch(r'view-\d{3}\.jpg', name):
            raise KeyError('照片不存在')
        path = self.folder(identifier)/name
        if not path.is_file():
            raise KeyError('照片不存在')
        return path

    def save_views(self, views, *, label, source_type, provenance):
        if not 1 <= len(views) <= 100:
            raise ValueError('每轮环视需1—100张新照片')
        for v in views:
            if not all(math.isfinite(v[k]) for k in ('heading_deg', 'pitch_deg')):
                raise ValueError('相机方位无效')
        with self.lock:
            focal = estimate_focal(views)
            jpeg, coverage = panorama(views, focal)
            identifier = uuid.uuid4().hex
            folder = self.folder(identifier)
            folder.mkdir(parents=True)
            saved = []
            for n, view in enumerate(views):
                name = f'view-{n:03d}.jpg'
                (folder/name).write_bytes(view['rgb'])
                saved.append({k: v for k, v in view.items() if k != 'rgb'} | {
                    'index': n, 'file': name, 'sha256': hashlib.sha256(view['rgb']).hexdigest(),
                    'url': f'/api/platform/fieldmap/surround/{identifier}/{name}'})
            (folder/'panorama.jpg').write_bytes(jpeg)
            preview = BytesIO()
            full = Image.open(BytesIO(jpeg))
            top, bottom = coverage['preview_rows']
            full.crop((0, top, full.width, bottom)).save(preview, 'JPEG', quality=90)
            (folder/'preview.jpg').write_bytes(preview.getvalue())
            value = {'id': identifier, 'label': label, 'created_at': stamp(), 'source_type': source_type,
                     'view_count': len(views), 'views': saved, 'coverage': coverage, 'optics': focal,
                     'provenance': provenance, 'panorama_url': f'/api/platform/fieldmap/surround/{identifier}/panorama.jpg',
                     'preview_url': f'/api/platform/fieldmap/surround/{identifier}/preview.jpg',
                     'preview_sha256': hashlib.sha256(preview.getvalue()).hexdigest(),
                     'panorama_sha256': hashlib.sha256(jpeg).hexdigest(), 'motion_enabled': False,
                     'metric_calibrated': False, 'registered_robot_map': False}
            atomic_json(folder/'scan.json', value)
            return value

    def import_head(self, trial):
        if not isinstance(trial, str) or not re.fullmatch(r'light-surround-[a-z0-9-]{1,48}', trial) or not self.fieldtests_root:
            raise ValueError('请选择已保存的有限环视采集运行')
        matches = [p for p in self.fieldtests_root.glob('20??????/phone-roam/'+trial+'/result.json')
                   if p.resolve().is_relative_to(self.fieldtests_root.resolve())]
        if len(matches) != 1:
            raise ValueError('环视源运行不存在或不唯一')
        path = matches[0]
        if path.stat().st_size > 2_000_000:
            raise ValueError('源运行超过读取预算')
        source = json.loads(path.read_text('utf8'))
        if not source.get('ok') or source.get('base_drive_commands') != 0 or not source.get('health', {}).get('stationary_base_verified'):
            raise ValueError('需要完成且底盘停车核验通过的头部环视')
        initial = {f"{p['port']}:{m['id']}": m['position'] for p in source['initial']['ports'] for m in p['motors']}
        views = []
        seen = set()
        for shot in source.get('shots', []):
            positions = shot['positions']
            if any(abs((positions[k]-initial[k]+2048) % 4096-2048) > 2 for k in ('COM4:9', 'COM4:10')):
                raise ValueError('头部环视期间底盘位姿变化，不能强拼一个驻点')
            frame = next((f for f in shot['frames'] if f['camera'] == 0 and 'file' in f), None)
            if not frame:
                continue
            image = (path.parent/frame['file']).resolve()
            if image.parent != path.parent.resolve() or image.suffix != '.jpg':
                raise ValueError('源图不在运行目录内')
            token = (positions['COM3:7']//8, positions['COM3:8']//8)
            if token in seen:
                continue
            seen.add(token)
            views.append({'rgb': image.read_bytes(), 'heading_deg': (positions['COM3:7']-source['head_reference_raw'])*360/4096,
                          'pitch_deg': -(positions['COM3:8']-2220)*360/4096,
                          'captured_at': frame.get('captured_at'), 'source_trial': trial, 'source_index': shot['index'],
                          'head_raw': [positions['COM3:7'], positions['COM3:8']], 'wheel_raw': [positions['COM4:9'], positions['COM4:10']]})
        return self.save_views(views, label='驻点环视 · '+trial, source_type='stationary_head_rgb',
                               provenance={'trial': trial, 'run_sha256': hashlib.sha256(path.read_bytes()).hexdigest(),
                                           'heading': 'head_encoder_relative_to_2015_not_optical_extrinsics',
                                           'pitch_reference_raw': 2220, 'base_drive_commands': 0,
                                           'unobserved_back_sector_preserved': True})

    def model_plan(self, identifier):
        evidence = self.evidence_provider()
        meta = evidence.read('models', identifier)
        path = evidence.media('models', identifier, 'cloud.json')
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        key = (identifier, digest)
        with self.lock:
            if key in self.cache:
                return self.cache[key]
            value = floor_plan(json.loads(path.read_text('utf8')))
            value.update(id=identifier, capture_session=meta.get('capture_session'), label=meta.get('label'),
                         source_frames=meta['source_frames'], model_sha256=digest, historical=True)
            self.cache = {key: value}
            return value
