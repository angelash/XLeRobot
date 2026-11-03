"""Wrist-view geometry and checks used by the grasp skill.

Defaults come from the 2026-11-03 left-wrist photos (640x480): fixed-jaw tip
near (300, 312), open moving-jaw tip near (590, 350); an object whose near edge
reaches the bottom rows and whose top is below row ~215 sits between the jaws;
before the advance the object is held slightly toward the fixed jaw because it
drifts right by ~50 px while the gripper moves in.
"""
from __future__ import annotations

import cv2
import numpy as np

FRAME = (640, 480)
FIXED_JAW = [(105, 480), (296, 308), (318, 330), (262, 480)]
MOVING_JAW_OPEN = [(470, 480), (586, 346), (612, 372), (640, 470), (640, 480)]


def interp(value, x0, x1, y0, y1):
    if x1 == x0:
        return y0
    t = min(1.0, max(0.0, (value - x0)/(x1 - x0)))
    return y0 + t*(y1 - y0)


def ref_x(bottom, cfg):
    """Lateral target column for the object's centre, by how close it is (its bottom row)."""
    return interp(bottom, cfg['ref_bottom_far'], cfg['ref_bottom_near'], cfg['ref_x_far'], cfg['ref_x_near'])


def edges(bbox):
    x, y, w, h = bbox
    return {'left': x, 'right': x + w, 'top': y, 'bottom': y + h}


def in_jaws(bbox, cfg):
    e = edges(bbox)
    return e['bottom'] >= cfg['in_bottom'] and e['top'] >= cfg['in_top']


def background_shift(before, after, bbox, top_limit=300):
    """Phase-correlation shift of the scene band above the jaws, away from the target box."""
    h, w = before.shape[:2]
    y1 = int(min(top_limit, h))
    x, _, bw, _ = bbox if bbox else (w, 0, 0, 0)
    strips = [(0, int(max(0, x - 12))), (int(min(w, x + bw + 12)), w)]
    x0, x1 = max(strips, key=lambda s: s[1] - s[0])
    if x1 - x0 < 96:
        x0, x1 = 0, w
    a = cv2.resize(before[:y1, x0:x1], None, fx=0.5, fy=0.5, interpolation=cv2.INTER_AREA).astype(np.float32)
    b = cv2.resize(after[:y1, x0:x1], None, fx=0.5, fy=0.5, interpolation=cv2.INTER_AREA).astype(np.float32)
    window = cv2.createHanningWindow((a.shape[1], a.shape[0]), cv2.CV_32F)
    (dx, dy), response = cv2.phaseCorrelate(a, b, window)
    return {'dx': round(dx*2, 1), 'dy': round(dy*2, 1), 'shift': round(float(np.hypot(dx, dy))*2, 1), 'response': round(float(response), 3),
            'region': [x0, 0, x1 - x0, y1]}


def grip_window(bbox, frame=FRAME, shrink=0.12):
    """The part of the gripped object still in view, inset from its box edges."""
    if not bbox:
        return None
    x, y, w, h = bbox
    x0, y0 = max(0.0, x), max(0.0, y)
    x1, y1 = min(frame[0], x + w), min(frame[1], y + h)
    dx, dy = (x1 - x0)*shrink, (y1 - y0)*shrink
    box = [int(x0 + dx), int(y0 + dy), int(x1 - x0 - 2*dx), int(y1 - y0 - 2*dy)]
    return box if box[2] >= 24 and box[3] >= 24 else None


def window_similarity(before, after, window, search=12):
    """How unchanged a fixed wrist-view window stayed (best NCC within a few pixels).
    A gripped object moves with the camera, so its window keeps its content; an
    object left on the desk is replaced by other content once the gripper turns.
    Returns (similarity, texture) - similarity None if the window is too plain."""
    x, y, w, h = window
    a = before[y:y + h, x:x + w].astype(np.float32)
    texture = float(a.std())
    if texture < 4.0:
        return None, round(texture, 2)
    H, W = after.shape[:2]
    X0, Y0 = max(0, x - search), max(0, y - search)
    region = after[Y0:min(H, y + h + search), X0:min(W, x + w + search)].astype(np.float32)
    if region.shape[0] < h or region.shape[1] < w:
        return 0.0, round(texture, 2)
    a = cv2.resize(a, None, fx=0.5, fy=0.5, interpolation=cv2.INTER_AREA)
    region = cv2.resize(region, None, fx=0.5, fy=0.5, interpolation=cv2.INTER_AREA)
    if region.shape[0] < a.shape[0] or region.shape[1] < a.shape[1]:
        return 0.0, round(texture, 2)
    return round(float(cv2.matchTemplate(region, a, cv2.TM_CCOEFF_NORMED).max()), 3), round(texture, 2)


def propose(gray, jaw_row=330, min_area=600, max_area=70000):
    """Suggest a box around the most prominent textured blob above the jaws (operator still confirms)."""
    h, w = gray.shape[:2]
    blur = cv2.GaussianBlur(gray, (5, 5), 0)
    edge = cv2.Canny(blur, 30, 90)
    edge[min(jaw_row, h):, :] = 0
    edge[:, :6] = 0; edge[:, w - 6:] = 0; edge[:6, :] = 0
    closed = cv2.morphologyEx(edge, cv2.MORPH_CLOSE, np.ones((11, 11), np.uint8))
    contours, _ = cv2.findContours(closed, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    best = None
    for c in contours:
        x, y, bw, bh = cv2.boundingRect(c)
        area = bw*bh
        if not min_area <= area <= max_area or not 0.25 <= bw/max(bh, 1) <= 4 or x <= 8 or y <= 8 or x + bw >= w - 8:
            continue
        fill = cv2.contourArea(cv2.convexHull(c))/area
        score = area*(0.5 + fill) - 0.3*abs(x + bw/2 - w/2)*bh
        if best is None or score > best[0]:
            best = (score, [max(0, x - 6), max(0, y - 6), min(w - max(0, x - 6), bw + 12), min(h - max(0, y - 6), bh + 12)])
    return None if best is None else best[1]


def draw(bgr, track, cfg, phase='', note=''):
    out = bgr.copy()
    for poly in (FIXED_JAW,):
        cv2.polylines(out, [np.array(poly, np.int32)], True, (90, 90, 90), 1)
    cv2.line(out, (0, int(cfg['in_top'])), (640, int(cfg['in_top'])), (0, 160, 255), 1)
    cv2.line(out, (0, int(cfg['in_bottom'])), (640, int(cfg['in_bottom'])), (0, 160, 255), 1)
    if track and track.get('bbox'):
        x, y, w, h = track['bbox']
        colour = (60, 200, 60) if track.get('ok') else (40, 40, 230)
        cv2.rectangle(out, (int(x), int(y)), (int(x + w), int(y + h)), colour, 2)
        rx = int(ref_x(y + h, cfg))
        cv2.line(out, (rx, int(max(0, y - 20))), (rx, int(min(479, y + h + 20))), (255, 160, 0), 1)
        cv2.circle(out, (int(x + w/2), int(y + h/2)), 4, colour, -1)
        cv2.putText(out, f"{track.get('score', 0):.2f}", (int(x), int(max(14, y - 6))), cv2.FONT_HERSHEY_SIMPLEX, 0.5, colour, 1)
    label = (phase + (' ' + note if note else '')).strip()
    if label:
        cv2.rectangle(out, (0, 0), (640, 22), (0, 0, 0), -1)
        cv2.putText(out, label.encode('ascii', 'ignore').decode()[:80], (6, 16), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (255, 255, 255), 1)
    return out
