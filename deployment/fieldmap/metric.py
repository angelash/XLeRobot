"""Deterministic, known-pose occupancy mapping and geometric planning.

This is a replay engine, not a SLAM backend or a motion authorization system.
No controller, camera, network, model, or hardware imports belong here.
"""
from __future__ import annotations

from dataclasses import dataclass
import heapq
import math
import re

import numpy as np


def number(value, name, lo, hi):
    if type(value) not in (int, float) or not math.isfinite(value) or not lo <= value <= hi:
        raise ValueError(f'{name}: expected finite number in [{lo}, {hi}]')
    return float(value)


def identifier(value, name):
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9_.:-]{1,96}', value):
        raise ValueError(f'{name}: invalid identifier')
    return value


def exact(value, fields, name):
    if not isinstance(value, dict) or set(value) != set(fields):
        raise ValueError(f'{name}: expected fields {sorted(fields)}')


@dataclass(frozen=True)
class GridSpec:
    width: int = 160
    height: int = 120
    resolution_m: float = .1
    origin_x_m: float = -8.
    origin_y_m: float = -6.

    @classmethod
    def parse(cls, value):
        exact(value, cls.__dataclass_fields__, 'grid')
        for key in ('width', 'height'):
            if type(value[key]) is not int or not 16 <= value[key] <= 400:
                raise ValueError(f'{key}: expected integer in [16, 400]')
        return cls(value['width'], value['height'],
                   number(value['resolution_m'], 'resolution_m', .025, .2),
                   number(value['origin_x_m'], 'origin_x_m', -100, 100),
                   number(value['origin_y_m'], 'origin_y_m', -100, 100))

    def cell(self, x, y):
        return math.floor((x-self.origin_x_m)/self.resolution_m), math.floor((y-self.origin_y_m)/self.resolution_m)

    def world(self, x, y):
        return [self.origin_x_m+(x+.5)*self.resolution_m, self.origin_y_m+(y+.5)*self.resolution_m]

    def inside(self, x, y):
        return 0 <= x < self.width and 0 <= y < self.height


def ray_cells(start, end):
    """Integer Bresenham ray, including origin and endpoint."""
    x, y = start
    tx, ty = end
    dx, dy = abs(tx-x), -abs(ty-y)
    sx, sy = (1 if x < tx else -1), (1 if y < ty else -1)
    error = dx+dy
    while True:
        yield x, y
        if (x, y) == (tx, ty):
            return
        twice = 2*error
        if twice >= dy:
            error += dy; x += sx
        if twice <= dx:
            error += dx; y += sy


class Occupancy:
    def __init__(self, config):
        self.config = config
        self.spec = GridSpec.parse(config['grid'])
        shape = self.spec.height, self.spec.width
        self.odds = np.zeros(shape, dtype=np.int16)  # log-odds * 100
        self.seen = np.zeros(shape, dtype=bool)
        self.last_seen = np.full(shape, -1., dtype=np.float64)
        self.scan_count = 0
        self.last_pose = None
        self.last_stamp = None

    def integrate(self, packet):
        exact(packet, ('source', 'epoch', 'frame_id', 'calibration_id', 'sequence', 'stamp_s',
                       'range_min_m', 'range_max_m', 'pose', 'rays'), 'scan')
        for key in ('source', 'epoch', 'frame_id', 'calibration_id'):
            if packet[key] != self.config[key]:
                raise ValueError(f'{key}: changed; create a separate mapping session')
        if type(packet['sequence']) is not int or packet['sequence'] != self.scan_count:
            raise ValueError('sequence: must be the next contiguous scan index')
        stamp = number(packet['stamp_s'], 'stamp_s', 0, 1e12)
        if self.last_stamp is not None and stamp <= self.last_stamp:
            raise ValueError('stamp_s: duplicate or reversed source time')
        low = number(packet['range_min_m'], 'range_min_m', .02, 2.)
        high = number(packet['range_max_m'], 'range_max_m', low, 20.)
        pose = packet['pose']
        exact(pose, ('x_m', 'y_m', 'yaw_rad', 'stamp_s', 'quality', 'position_sigma_m', 'yaw_sigma_rad'), 'pose')
        x = number(pose['x_m'], 'x_m', -1000, 1000)
        y = number(pose['y_m'], 'y_m', -1000, 1000)
        yaw = number(pose['yaw_rad'], 'yaw_rad', -math.pi, math.pi)
        pose_stamp = number(pose['stamp_s'], 'pose.stamp_s', 0, 1e12)
        number(pose['position_sigma_m'], 'position_sigma_m', 0, .1)
        number(pose['yaw_sigma_rad'], 'yaw_sigma_rad', 0, math.radians(5))
        if pose['quality'] != 'tracked' or abs(pose_stamp-stamp) > .1000001:
            raise ValueError('pose: tracking or timestamp pairing rejected')
        extrinsic = self.config['sensor_in_base']
        sx = x+math.cos(yaw)*extrinsic['x_m']-math.sin(yaw)*extrinsic['y_m']
        sy = y+math.sin(yaw)*extrinsic['x_m']+math.cos(yaw)*extrinsic['y_m']
        sensor_yaw = yaw+extrinsic['yaw_rad']
        origin = self.spec.cell(sx, sy)
        if not self.spec.inside(*origin):
            raise ValueError('pose: sensor origin outside bounded map')
        if self.last_pose:
            dt = stamp-self.last_stamp
            distance = math.hypot(x-self.last_pose['x_m'], y-self.last_pose['y_m'])
            angle = abs(math.atan2(math.sin(yaw-self.last_pose['yaw_rad']), math.cos(yaw-self.last_pose['yaw_rad'])))
            if distance > dt*1.0+.15 or angle > dt*2.0+.15:
                raise ValueError('pose: discontinuity; relocalize into a new epoch')
        rays = packet['rays']
        if not isinstance(rays, list) or not 1 <= len(rays) <= 720:
            raise ValueError('rays: expected 1..720 explicit measurements')
        # Validate the entire scan before mutating a single cell.
        checked = []
        for ray in rays:
            exact(ray, ('angle_rad', 'range_m', 'hit'), 'ray')
            angle = number(ray['angle_rad'], 'angle_rad', -math.pi, math.pi)
            if type(ray['hit']) is not bool:
                raise ValueError('hit: explicit boolean required')
            if ray['range_m'] is None:
                if ray['hit']:
                    raise ValueError('ray: a missing measurement cannot declare a hit')
                continue  # Missing is not a certified max-range clearing ray.
            distance = number(ray['range_m'], 'range_m', low, high)
            checked.append((sensor_yaw+angle, distance, ray['hit']))
        if not checked:
            raise ValueError('scan: contains no valid measurements')
        free, occupied = set(), set()
        clipped = 0
        for angle, distance, hit in checked:
            end = self.spec.cell(sx+distance*math.cos(angle), sy+distance*math.sin(angle))
            clipped += not self.spec.inside(*end)
            for cx, cy in ray_cells(origin, end):
                if not self.spec.inside(cx, cy):
                    break
                if (cx, cy) == end and hit:
                    occupied.add((cx, cy))
                else:
                    free.add((cx, cy))
        free -= occupied  # One update per cell per scan; occupied endpoint wins.
        for cells, increment in ((free, -40), (occupied, 85)):
            if not cells:
                continue
            cols, rows = np.array(sorted(cells), dtype=int).T
            self.odds[rows, cols] = np.clip(self.odds[rows, cols]+increment, -400, 400)
            self.seen[rows, cols] = True
            self.last_seen[rows, cols] = stamp
        self.scan_count += 1
        self.last_pose, self.last_stamp = dict(pose), stamp
        return {'valid_rays': len(checked), 'missing_rays': len(rays)-len(checked),
                'clipped_rays': int(clipped), 'free_updates': len(free), 'occupied_updates': len(occupied)}

    def cells(self):
        result = np.full(self.odds.shape, -1, dtype=np.int16)
        result[self.seen] = 50
        result[self.seen & (self.odds <= -50)] = 0
        result[self.seen & (self.odds >= 60)] = 100
        return result

    def export(self):
        cells = self.cells()
        counts = {key: int(np.count_nonzero(cells == value)) for key, value in
                  [('free', 0), ('occupied', 100), ('uncertain', 50), ('unknown', -1)]}
        return {'grid': self.config['grid'], 'cells': cells.ravel().tolist(), 'counts': counts,
                'observed_area_m2': int(self.seen.sum())*self.spec.resolution_m**2,
                'scan_count': self.scan_count, 'last_pose': self.last_pose, 'source_stamp_s': self.last_stamp,
                'localization_backend': 'provided_pose_only', 'live_freshness_verified': False,
                'llm_calls': 0, 'motion_enabled': False, 'automatic_clearance': False}


def safe_centres(cells, resolution, radius):
    """Conservative disk erosion of known free; outside the map is blocked.

    Half a cell diagonal accounts for the spatial extent of blocked pixels.
    Unknown and uncertain pixels receive exactly the same erosion as walls.
    """
    radius = number(radius, 'robot_radius_m', .55, 1.5)
    extent = radius/resolution+math.sqrt(.5)
    border = math.ceil(extent)
    height, width = cells.shape
    padded = np.pad(cells == 0, border, constant_values=False)
    safe = np.ones(cells.shape, dtype=bool)
    for dy in range(-border, border+1):
        for dx in range(-border, border+1):
            if dx*dx+dy*dy <= extent*extent:
                safe &= padded[border+dy:border+dy+height, border+dx:border+dx+width]
    return safe


def reachable(safe, start):
    """Dijkstra over free centres. Disallow diagonal corner cutting."""
    height, width = safe.shape
    if not (0 <= start[0] < width and 0 <= start[1] < height) or not safe[start[1], start[0]]:
        return {}, {}
    distances, parents = {start: 0.}, {}
    queue = [(0., start)]
    while queue:
        cost, node = heapq.heappop(queue)
        if cost != distances[node]:
            continue
        x, y = node
        for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1), (1, 1), (1, -1), (-1, 1), (-1, -1)):
            nx, ny = x+dx, y+dy
            if not (0 <= nx < width and 0 <= ny < height and safe[ny, nx]):
                continue
            if dx and dy and not (safe[y, nx] and safe[ny, x]):
                continue
            distance = cost+math.hypot(dx, dy)
            if distance < distances.get((nx, ny), math.inf):
                distances[nx, ny], parents[nx, ny] = distance, node
                heapq.heappush(queue, (distance, (nx, ny)))
    return distances, parents


def astar(safe, start, goal):
    """A* with Euclidean admissible heuristic and the same collision contract."""
    height, width = safe.shape
    for x, y in (start, goal):
        if not (0 <= x < width and 0 <= y < height and safe[y, x]):
            return []
    scores, parents = {start: 0.}, {}
    queue = [(math.dist(start, goal), 0., start)]
    while queue:
        _, cost, node = heapq.heappop(queue)
        if cost != scores[node]:
            continue
        if node == goal:
            return path_to(parents, start, goal)
        x, y = node
        for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1), (1, 1), (1, -1), (-1, 1), (-1, -1)):
            nx, ny = x+dx, y+dy
            if not (0 <= nx < width and 0 <= ny < height and safe[ny, nx]):
                continue
            if dx and dy and not (safe[y, nx] and safe[ny, x]):
                continue
            candidate = cost+math.hypot(dx, dy)
            if candidate < scores.get((nx, ny), math.inf):
                scores[nx, ny], parents[nx, ny] = candidate, node
                heapq.heappush(queue, (candidate+math.dist((nx, ny), goal), candidate, (nx, ny)))
    return []


def path_to(parents, start, goal):
    result = [goal]
    while result[-1] != start:
        result.append(parents[result[-1]])
    return list(reversed(result))


def frontier_groups(cells):
    free, unknown = cells == 0, cells == -1
    neighbour = np.zeros(cells.shape, dtype=bool)
    neighbour[1:] |= unknown[:-1]; neighbour[:-1] |= unknown[1:]
    neighbour[:, 1:] |= unknown[:, :-1]; neighbour[:, :-1] |= unknown[:, 1:]
    pending = {(int(x), int(y)) for y, x in zip(*np.where(free & neighbour))}
    groups = []
    # Sort seeds once. Repeated min(set) is quadratic on fragmented frontiers.
    for first in sorted(pending):
        if first not in pending:
            continue
        pending.remove(first)
        group, todo = [first], [first]
        while todo:
            x, y = todo.pop()
            for n in ((x+1, y), (x-1, y), (x, y+1), (x, y-1)):
                if n in pending:
                    pending.remove(n); todo.append(n); group.append(n)
        groups.append(sorted(group))
    return groups


def plan(grid, *, goal=None, radius=.6):
    cells, spec = grid.cells(), grid.spec
    safe = safe_centres(cells, spec.resolution_m, radius)
    result = {'status': 'no_pose', 'path_m': [], 'frontiers': [], 'robot_radius_m': radius,
              'footprint_basis': 'rough_folded_body_not_measured', 'motion_enabled': False,
              'automatic_clearance': False, 'live_freshness_verified': False,
              'safe_centres': safe.astype(int).ravel().tolist()}
    if not grid.last_pose:
        return result
    start = spec.cell(grid.last_pose['x_m'], grid.last_pose['y_m'])
    distances, parents = reachable(safe, start)
    if not distances:
        result['status'] = 'start_not_in_eroded_free_space'
        return result
    if goal is not None:
        if not isinstance(goal, list) or len(goal) != 2:
            raise ValueError('goal_m: expected [x_m, y_m]')
        target = spec.cell(number(goal[0], 'goal.x_m', -1000, 1000), number(goal[1], 'goal.y_m', -1000, 1000))
        path = astar(safe, start, target)
        result.update(status='path_preview' if path else 'goal_unreachable', path_m=[spec.world(*p) for p in path])
        return result
    # Candidates must see a frontier through known free space. This avoids
    # rewarding unknown on the other side of a wall or an inaccessible room.
    viewing_cells = max(1, math.ceil(1.8/spec.resolution_m))
    groups = sorted(frontier_groups(cells), key=lambda group: (-len(group), group[0]))
    result['frontier_groups_detected'] = len(groups)
    result['frontier_search_truncated'] = len(groups) > 128
    for group in groups[:128]:
        best = None
        for fx, fy in group[::max(1, len(group)//20)]:
            for x in range(max(0, fx-viewing_cells), min(spec.width, fx+viewing_cells+1)):
                for y in range(max(0, fy-viewing_cells), min(spec.height, fy+viewing_cells+1)):
                    if (x, y) not in distances:
                        continue
                    separation = math.hypot(x-fx, y-fy)
                    if separation > viewing_cells:
                        continue
                    rank = (separation, distances[x, y], x, y)
                    if best and rank >= best[0]:
                        continue
                    if any(cells[cy, cx] != 0 for cx, cy in ray_cells((x, y), (fx, fy))):
                        continue
                    best = rank, (x, y), (fx, fy)
        if best:
            _, target, boundary = best
            cost = distances[target]*spec.resolution_m
            gain = len(group)*spec.resolution_m  # boundary length proxy, not room coverage
            result['frontiers'].append({'goal_m': spec.world(*target), 'boundary_m': spec.world(*boundary),
                                        'boundary_cells': len(group), 'path_length_m': cost,
                                        'score': gain-.35*cost})
    result['frontiers'].sort(key=lambda f: (-f['score'], f['goal_m']))
    if not result['frontiers']:
        result['status'] = 'no_reachable_frontier'
        return result
    target = spec.cell(*result['frontiers'][0]['goal_m'])
    result.update(status='frontier_preview', path_m=[spec.world(*p) for p in path_to(parents, start, target)])
    return result
