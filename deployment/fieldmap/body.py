"""Rough whole-body geometry for supervised planning; never grants motion.

All heights are projected to the floor so table overhangs matter as well as
chair feet. The current build has no measured axle/footprint/cable calibration.
"""
from __future__ import annotations

import math


def profile():
    return {
        'id': 'folded-rough-20261105-v1',
        'status': 'engineering_assumption_not_measured',
        'frame': 'drive_axle_midpoint; x_forward_y_left_z_up; metres',
        'posture': 'both_arms_folded_empty_grippers',
        'box_m': {'x_min': -.40, 'x_max': .40, 'y_min': -.30, 'y_max': .30,
                  'z_min': 0., 'z_max': 1.50},
        'margin_m': .05,
        'full_turn_radius_m': .55,
        'straight_corridor_width_m': .70,
        'sources': [
            {'path': 'PROJECT_STATE.md', 'entry': '2026-10-20 推车与大件拾取范围评估',
             'kind': 'recorded_published_specification', 'cart_m': [.45, .35, .77]},
            {'path': 'simulation/xlerobot/xlerobot.urdf', 'kind': 'legacy_model_not_this_build_calibration',
             'chassis_box_m': [.3913, .459, .1], 'axle_x_m': -.15, 'wheel_track_m': .45},
            {'path': 'fabrication/assembly/debug_console/whole_calibration.py',
             'kind': 'controller_design_not_measured', 'radius_m': .0635, 'track_m': .25},
            {'kind': 'user_photo_and_contact_confirmation', 'date': '2026-11-05',
             'note': 'cart close to desk, folded arms, cameras and hanging cables; no metric image measurement'},
        ],
        'conditions': [
            'Fold both arms and keep grippers empty before chassis turning.',
            'Check low cart corners, upper arms/cameras and rear swing, not only the front camera centre.',
            'Hanging cables and actual axle offset are unmeasured; this box is a planning assumption, not a guaranteed bound.',
            'Measured current obstacles and robot pose are required for a geometric clearance claim.',
            'The full circle is sufficient under the assumed geometry; a short turn can use its smaller swept bounds.',
        ],
        'motion_enabled': False,
        'automatic_clearance': False,
    }


def number(value, name, low, high):
    if type(value) not in (int, float) or not math.isfinite(value) or not low <= value <= high:
        raise ValueError(f'{name}需为{low}–{high}内有限数值')
    return float(value)


def rotation_bounds(box, turn_deg, margin):
    """Exact AABB of an axle-fixed rotating rectangle, enlarged by a disk.

    Include derivative roots inside the arc: endpoint-only checks miss a
    corner hitting a wall midway through a 90/180 degree turn.
    """
    end = math.radians(turn_deg)
    lo, hi = sorted((0., end))
    xs, ys = [], []
    for x in (box['x_min'], box['x_max']):
        for y in (box['y_min'], box['y_max']):
            angles = [lo, hi]
            for root in (math.atan2(-y, x), math.atan2(x, y)):
                for k in range(-3, 4):
                    angle = root + k*math.pi
                    if lo <= angle <= hi:
                        angles.append(angle)
            for angle in angles:
                xs.append(x*math.cos(angle)-y*math.sin(angle))
                ys.append(x*math.sin(angle)+y*math.cos(angle))
    return {'x_min': min(xs)-margin, 'x_max': max(xs)+margin,
            'y_min': min(ys)-margin, 'y_max': max(ys)+margin}


def plan(payload):
    """Preview translate-then-turn sweep. No live pose inference or actuator I/O."""
    if payload.get('posture') != 'folded':
        raise ValueError('当前粗略包络只适用于已核对的双臂收起空爪姿态')
    forward = number(payload.get('forward_m', 0.), 'forward_m', 0., .5)
    turn = number(payload.get('turn_deg', 0.), 'turn_deg', -180., 180.)
    p = profile()
    b, margin = p['box_m'], p['margin_m']
    rotation = rotation_bounds(b, turn, margin)
    rotation = {key: value + (forward if key.startswith('x_') else 0.) for key, value in rotation.items()}
    translation = {'x_min': b['x_min']-margin, 'x_max': b['x_max']+margin+forward,
                   'y_min': b['y_min']-margin, 'y_max': b['y_max']+margin}
    bounds = {key: (min if key.endswith('_min') else max)(translation[key], rotation[key])
              for key in translation}
    obstacles = payload.get('obstacles', [])
    if not isinstance(obstacles, list) or len(obstacles) > 50:
        raise ValueError('obstacles需为最多50个同坐标系障碍矩形')
    intersections = []
    for i, obstacle in enumerate(obstacles):
        if not isinstance(obstacle, dict):
            raise ValueError('障碍需为矩形对象')
        o = {key: number(obstacle.get(key), key, -20., 20.) for key in bounds}
        if o['x_min'] >= o['x_max'] or o['y_min'] >= o['y_max']:
            raise ValueError('障碍矩形边界无效')
        hits = []
        for name, envelope in [('translation', translation), ('rotation', rotation)]:
            if not (o['x_min'] > envelope['x_max'] or o['x_max'] < envelope['x_min']
                    or o['y_min'] > envelope['y_max'] or o['y_max'] < envelope['y_min']):
                hits.append(name)
        if hits:
            intersections.append({'index': i, 'stages': hits})
    # Even a caller-supplied measured scene cannot validate this rough body or
    # locate unseen cables/people. Absence of a known hit must never enable drive.
    return {'profile_id': p['id'], 'status': 'rough_preview_only',
            'forward_m': forward, 'turn_deg': turn, 'margin_m': margin,
            'translation_aabb_m': translation, 'rotation_aabb_m': rotation,
            'combined_aabb_m': bounds, 'full_turn_radius_m': p['full_turn_radius_m'],
            'known_intersections': intersections,
            'assessment': 'possible_collision' if intersections else 'clearance_unconfirmed',
            'unknowns': ['actual_body_and_axle', 'hanging_cables', 'current_pose_and_scene_coverage'],
            'motion_enabled': False, 'automatic_clearance': False}
