"""Analytic synthetic office scans. No real household/robot data is embedded."""
from dataclasses import asdict
import math

from .metric import GridSpec


def configuration():
    return {'label': '合成办公室 · 已知位姿回放', 'source': 'analytic_synthetic_lidar',
            'epoch': 'synthetic-office-v1', 'frame_id': 'synthetic_map',
            'calibration_id': 'synthetic-identity-v1', 'grid': asdict(GridSpec()),
            'sensor_in_base': {'x_m': 0., 'y_m': 0., 'yaw_rad': 0.}}


def scans():
    # Outer walls, a dividing wall with a 1.4 m opening, and a rectangular desk.
    segments = [(-6, -4, 6, -4), (6, -4, 6, 4), (6, 4, -6, 4), (-6, 4, -6, -4),
                (1, -4, 1, -1.5), (1, -.1, 1, 4),
                (2.4, 1.2, 3.5, 1.2), (3.5, 1.2, 3.5, 2.8),
                (3.5, 2.8, 2.4, 2.8), (2.4, 2.8, 2.4, 1.2)]
    poses = [(-4., -1.5), (-3.6, -1.4), (-3.2, -1.3), (-2.8, -1.2),
             (-2.4, -1.1), (-2., -1.), (-1.6, -.8), (-1.2, -.7)]
    sequence = 0
    for x, y in poses:
        for repeat in range(3):
            rays = []
            for i in range(180):
                angle = -math.pi+i*2*math.pi/180
                ux, uy = math.cos(angle), math.sin(angle)
                nearest = 3.2
                hit = False
                for ax, ay, bx, by in segments:
                    vx, vy = bx-ax, by-ay
                    determinant = ux*vy-uy*vx
                    if abs(determinant) < 1e-10:
                        continue
                    distance = ((ax-x)*vy-(ay-y)*vx)/determinant
                    fraction = ((ax-x)*uy-(ay-y)*ux)/determinant
                    if .05 <= distance <= nearest and 0 <= fraction <= 1:
                        nearest, hit = distance, True
                rays.append({'angle_rad': angle, 'range_m': nearest, 'hit': hit})
            stamp = sequence*1.+1.
            yield {'source': 'analytic_synthetic_lidar', 'epoch': 'synthetic-office-v1',
                   'frame_id': 'synthetic_map', 'calibration_id': 'synthetic-identity-v1',
                   'sequence': sequence, 'stamp_s': stamp, 'range_min_m': .05, 'range_max_m': 3.2,
                   'pose': {'x_m': x, 'y_m': y, 'yaw_rad': 0., 'stamp_s': stamp, 'quality': 'tracked',
                            'position_sigma_m': 0., 'yaw_sigma_rad': 0.}, 'rays': rays}
            sequence += 1
