"""Appearance-based free-space detection on a calibrated ground plane.

Classic mobile-robot method: learn what the floor looks like from an area the
supervisor confirmed clear, mark pixels that do not look like floor, find in
every image column the lowest non-floor run (the obstacle's contact with the
floor) and convert that pixel to metres through the ground homography. Only
things that touch the floor and differ in colour from it are found; overhangs,
glass, floor-coloured objects and anything outside the view are not.
"""
from __future__ import annotations

import time

import cv2
import numpy as np

WORK = (320, 240)
CALIB = (640, 480)
BINS = (19, 8, 8)  # 18 hue bins + one achromatic bin (low saturation ignores hue)
ACHROMATIC_S = 40

DEFAULT_CONFIG = {
    'camera': 0,
    'target_fps': 10,
    # Normalised polygons of the robot's own grippers/arms in the head view.
    'self_mask': [[[0.0, 0.70], [0.27, 0.70], [0.31, 1.0], [0.0, 1.0]], [[0.79, 0.68], [1.0, 0.68], [1.0, 1.0], [0.76, 1.0]]],
    'near_region_top': 0.58,
    'roi_top': 0.38,
    'column_px': 4,
    'min_run_px': 5,
    'floor_threshold': 0.5,
    'floor_coverage': 0.97,
    'corridor_half_width_m': 0.24,
    'max_range_m': 1.6,
    'max_occluded_fraction': 0.5,
    'history_frames': 3,
    'people_detector': True,
    'people_every_s': 0.5,
    'head_tolerance_counts': 25,
}


def polygon_mask(polygons, size=WORK):
    mask = np.zeros((size[1], size[0]), np.uint8)
    for polygon in polygons or []:
        points = np.array([[x*size[0], y*size[1]] for x, y in polygon], np.int32)
        if len(points) >= 3:
            cv2.fillPoly(mask, [points], 255)
    return mask


def _bin_index(hsv):
    """Flat HSV histogram bin per pixel (numpy; avoids 3-D Mat conversion quirks)."""
    h = np.minimum(hsv[..., 0].astype(np.int32)*18//180, 17)
    h = np.where(hsv[..., 1] < ACHROMATIC_S, 18, h)
    s = hsv[..., 1].astype(np.int32)*BINS[1]//256
    v = hsv[..., 2].astype(np.int32)*BINS[2]//256
    return (h*BINS[1]+s)*BINS[2]+v


def _smooth(hist):
    """[1,2,1] smoothing: hue is circular for chromatic bins; edges replicate."""
    out = hist.copy()
    chroma = out[:18]
    out[:18] = (np.roll(chroma, 1, 0)+2*chroma+np.roll(chroma, -1, 0))/4
    for axis in (1, 2):
        padded = np.concatenate([out.take([0], axis), out, out.take([-1], axis)], axis)
        n = out.shape[axis]
        out = (padded.take(range(0, n), axis)+2*padded.take(range(1, n+1), axis)+padded.take(range(2, n+2), axis))/4
    return out


class FloorModel:
    """Normalised HSV histogram of confirmed floor pixels."""

    def __init__(self, hist=None, samples=0):
        self.hist = np.zeros(BINS, np.float32) if hist is None else np.asarray(hist, np.float32).reshape(BINS)
        self.samples = int(samples)

    @property
    def ready(self):
        return self.samples >= 500 and float(self.hist.max()) > 0

    def learn(self, hsv, region):
        if int(np.count_nonzero(region)) < 200:
            raise ValueError('可用于学习的地面像素太少')
        counts = np.bincount(_bin_index(hsv)[region > 0], minlength=int(np.prod(BINS)))
        self.hist += counts.reshape(BINS).astype(np.float32)
        self.samples += int(np.count_nonzero(region))

    def table(self, coverage=0.97):
        """Bins that together hold the given share of the smoothed floor mass."""
        mass = _smooth(self.hist).ravel()
        order = np.argsort(mass)[::-1]
        cumulative = np.cumsum(mass[order])/max(float(mass.sum()), 1e-9)
        keep = order[:int(np.searchsorted(cumulative, coverage))+1]
        table = np.zeros(mass.size, np.float32)
        table[keep[mass[keep] > 0]] = 1
        return table

    def probability(self, hsv, coverage=0.97):
        return cv2.GaussianBlur(self.table(coverage)[_bin_index(hsv)], (5, 5), 0)

    def to_json(self):
        return {'bins': list(BINS), 'samples': self.samples, 'hist': self.hist.ravel().round(3).tolist()}

    @classmethod
    def from_json(cls, data):
        if not data or list(data.get('bins', [])) != list(BINS):
            return cls()
        return cls(np.array(data['hist'], np.float32), data.get('samples', 0))


def near_region(config, self_mask):
    region = np.zeros((WORK[1], WORK[0]), np.uint8)
    region[int(config['near_region_top']*WORK[1]):, :] = 255
    region[self_mask > 0] = 0
    return region


class Calibration:
    """Ground homography (640×480 pixels → floor metres, x forward, y left)."""

    def __init__(self, H, intrinsics=None, head_pose=None, source=None):
        self.H = np.asarray(H, np.float64).reshape(3, 3)
        self.Hinv = np.linalg.inv(self.H)
        self.intrinsics = intrinsics
        self.head_pose = head_pose
        self.source = source or {}
        if intrinsics:
            self.K = np.asarray(intrinsics['K'], np.float64).reshape(3, 3)
            self.dist = np.asarray(intrinsics.get('dist', [0]*5), np.float64).ravel()

    def to_floor(self, pixels):
        pts = np.asarray(pixels, np.float64).reshape(-1, 1, 2)
        if self.intrinsics:
            pts = cv2.undistortPoints(pts, self.K, self.dist, P=self.K)
        return cv2.perspectiveTransform(pts, self.H).reshape(-1, 2)

    def to_image(self, floor):
        """Floor → undistorted pixel (good enough for drawing overlays)."""
        return cv2.perspectiveTransform(np.asarray(floor, np.float64).reshape(-1, 1, 2), self.Hinv).reshape(-1, 2)


class PeopleDetector:
    def __init__(self):
        self.hog = cv2.HOGDescriptor()
        self.hog.setSVMDetector(cv2.HOGDescriptor_getDefaultPeopleDetector())

    def detect(self, bgr):
        boxes, weights = self.hog.detectMultiScale(bgr, winStride=(8, 8), padding=(8, 8), scale=1.08)
        return [(int(x), int(y), int(w), int(h), float(s)) for (x, y, w, h), s in zip(boxes, np.ravel(weights)) if s > 0.5]


def analyse(bgr, model, config, calibration=None, self_mask=None, people=None):
    """Process one WORK-size BGR frame. Returns result dict and debug layers."""
    started = time.perf_counter()
    if bgr.shape[1] != WORK[0] or bgr.shape[0] != WORK[1]:
        bgr = cv2.resize(bgr, WORK, interpolation=cv2.INTER_AREA)
    if self_mask is None:
        self_mask = polygon_mask(config['self_mask'])
    reasons = []
    result = {'valid': False, 'metric': calibration is not None, 'clear_m': None, 'blind_m': None, 'obstacles': [],
              'people': people or [], 'reasons': reasons}
    if not model or not model.ready:
        reasons.append('尚未学习地面外观')
        result['processing_ms'] = round((time.perf_counter()-started)*1000, 2)
        return result, None
    hsv = cv2.cvtColor(bgr, cv2.COLOR_BGR2HSV)
    floor = (model.probability(hsv, config.get('floor_coverage', 0.97)) >= config['floor_threshold']).astype(np.uint8)
    floor = cv2.morphologyEx(floor, cv2.MORPH_OPEN, np.ones((3, 3), np.uint8))
    floor = cv2.morphologyEx(floor, cv2.MORPH_CLOSE, np.ones((3, 3), np.uint8))
    selfish = self_mask > 0
    top = int(config['roi_top']*WORK[1])
    if calibration is not None:
        # With a metric ground plane the look-ahead limit comes from max_range.
        horizon = calibration.to_image([[config['max_range_m'], 0.0]])[0][1]/(CALIB[1]/WORK[1])
        if np.isfinite(horizon):
            top = int(min(WORK[1]-20, max(0, horizon)))
    width, run = int(config['column_px']), int(config['min_run_px'])
    cols = WORK[0]//width
    obstacle = ((floor == 0) & ~selfish)[:, :cols*width].reshape(WORK[1], cols, width).mean(axis=2) > 0.5
    masked = selfish[:, :cols*width].reshape(WORK[1], cols, width).mean(axis=2) > 0.5
    boundary = np.full(cols, -1)
    lowest_visible = np.full(cols, -1)
    streak = np.zeros(cols, int)
    for row in range(WORK[1]-1, top-1, -1):
        visible = ~masked[row]
        lowest_visible[(lowest_visible < 0) & visible] = row
        streak = np.where(obstacle[row] & (boundary < 0), streak+1, 0)
        hit = (streak >= run) & (boundary < 0)
        boundary[hit] = row+run-1
    result['floor_fraction'] = float(floor[top:][~selfish[top:]].mean()) if np.any(~selfish[top:]) else 0.
    result['columns'] = {'count': cols, 'blocked': int((boundary >= 0).sum()), 'roi_top_px': top}
    layers = {'floor': floor, 'boundary': boundary, 'top': top, 'lowest_visible': lowest_visible, 'column_px': width}
    if calibration is None:
        reasons.append('未完成地面标定：只能显示图像中的可行区域，不能给出距离')
        result['processing_ms'] = round((time.perf_counter()-started)*1000, 2)
        return result, layers
    scale = CALIB[0]/WORK[0]
    centres = (np.arange(cols)+0.5)*width*scale
    hw, max_range = config['corridor_half_width_m'], config['max_range_m']
    def points(rows):
        return calibration.to_floor(np.stack([centres, (rows+0.5)*scale], axis=1))
    contact = points(np.where(boundary >= 0, boundary, top))
    near = points(np.where(lowest_visible >= 0, lowest_visible, WORK[1]-1))
    in_corridor = (np.abs(contact[:, 1]) <= hw) & (contact[:, 0] > 0)
    blocked = in_corridor & (boundary >= 0)
    obstacles = [{'x_m': round(float(x), 3), 'y_m': round(float(y), 3), 'kind': 'floor_contact'} for x, y in contact[blocked]]
    for (px, py, pw, ph, score) in people or []:
        foot = calibration.to_floor([[(px+pw/2)*scale, (py+ph)*scale]])[0]
        if foot[0] > 0 and abs(foot[1]) <= hw+0.2:
            obstacles.append({'x_m': round(float(foot[0]), 3), 'y_m': round(float(foot[1]), 3), 'kind': 'person', 'score': round(score, 2)})
    corridor_cols = (np.abs(near[:, 1]) <= hw) & (near[:, 0] > 0)
    visible = corridor_cols & (lowest_visible >= 0)
    occluded = 1-(visible.sum()/max(1, corridor_cols.sum()))
    far_limit = contact[in_corridor & (boundary < 0), 0]
    candidates = [o['x_m'] for o in obstacles if abs(o['y_m']) <= hw]
    clear = min(candidates+[float(far_limit.min()) if far_limit.size else max_range, max_range])
    result.update(clear_m=round(max(0., clear), 3), blind_m=round(float(near[visible, 0].max()), 3) if visible.any() else None,
                  occluded_fraction=round(float(occluded), 3), obstacles=sorted(obstacles, key=lambda o: o['x_m'])[:12])
    if not corridor_cols.any():
        reasons.append('走廊不在画面内，检查头部姿态与标定')
    elif occluded > config['max_occluded_fraction']:
        reasons.append(f'走廊{occluded:.0%}被车体遮挡')
    result['valid'] = not reasons
    result['processing_ms'] = round((time.perf_counter()-started)*1000, 2)
    return result, layers


def render(bgr, result, layers, config, calibration=None):
    """Annotated view for people: floor tint, contact points, corridor, distance."""
    view = cv2.resize(bgr, WORK) if bgr.shape[1] != WORK[0] else bgr.copy()
    if layers is not None:
        tint = view.copy()
        tint[layers['floor'] > 0] = (80, 200, 120)
        view = cv2.addWeighted(tint, 0.25, view, 0.75, 0)
        width = layers['column_px']
        for c, row in enumerate(layers['boundary']):
            if row >= 0:
                cv2.line(view, (c*width, int(row)), (c*width+width-1, int(row)), (40, 40, 240), 2)
        cv2.line(view, (0, layers['top']), (WORK[0], layers['top']), (200, 200, 200), 1)
    for polygon in config['self_mask']:
        pts = np.array([[x*WORK[0], y*WORK[1]] for x, y in polygon], np.int32)
        cv2.polylines(view, [pts], True, (160, 160, 160), 1)
    if calibration is not None:
        scale = WORK[0]/CALIB[0]
        hw = config['corridor_half_width_m']
        for side in (-hw, hw):
            line = calibration.to_image([[d, side] for d in np.linspace(0.05, config['max_range_m'], 12)])*scale
            cv2.polylines(view, [line.astype(np.int32)], False, (0, 220, 255), 1)
        if result.get('clear_m') is not None:
            mark = calibration.to_image([[result['clear_m'], -hw], [result['clear_m'], hw]])*scale
            cv2.line(view, tuple(mark[0].astype(int)), tuple(mark[1].astype(int)), (0, 165, 255), 2)
    for (x, y, w, h, _) in result.get('people') or []:
        cv2.rectangle(view, (x, y), (x+w, y+h), (255, 120, 0), 1)
    label = 'clear %.2fm' % result['clear_m'] if result.get('clear_m') is not None else ('no metric' if result.get('reasons') else '--')
    color = (60, 200, 60) if result.get('valid') else (40, 40, 230)
    cv2.rectangle(view, (0, 0), (WORK[0], 18), (20, 20, 20), -1)
    cv2.putText(view, label+('  OK' if result.get('valid') else '  BLOCKED'), (4, 13), cv2.FONT_HERSHEY_SIMPLEX, 0.42, color, 1, cv2.LINE_AA)
    return view
