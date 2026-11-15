"""Independent geometric and persistence contracts for replay-only mapping."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
import hashlib
import math
from pathlib import Path
import sqlite3

import numpy as np
import pytest

from .metric import GridSpec, Occupancy, astar, plan, safe_centres
from .metric_demo import configuration, scans
from .metric_store import Conflict, MappingStore
from .vacuum_prior import valetudo_v2


def config():
    value = configuration()
    value['grid'] = asdict(GridSpec(40, 40, .1, -2, -2))
    return value


def packet(sequence=0, **changes):
    result = next(scans())
    result.update(sequence=sequence, stamp_s=sequence+1., range_max_m=5.,
                  rays=[{'angle_rad': 0., 'range_m': 1., 'hit': True}],
                  pose={'x_m': 0., 'y_m': 0., 'yaw_rad': 0., 'stamp_s': sequence+1.,
                        'quality': 'tracked', 'position_sigma_m': .02, 'yaw_sigma_rad': .01})
    result.update(changes)
    return result


def vacuum_map():
    return {'__class': 'ValetudoMap', 'metaData': {'version': 2},
            'size': {'x': 51200, 'y': 51200}, 'pixelSize': 5,
            'layers': [{'type': 'floor', 'compressedPixels': [5000, 6000, 4, 5000, 6001, 4]},
                       {'type': 'wall', 'pixels': [5000, 6000]}],
            'entities': [{'type': 'robot_position', 'points': [25000, 30000]}]}


def test_wall_hit_occlusion_and_duplicate_beams_have_independent_evidence():
    one, many = Occupancy(config()), Occupancy(config())
    p = packet()
    one.integrate(p)
    p['rays'] *= 100
    many.integrate(p)
    assert np.array_equal(one.odds, many.odds)
    assert one.cells()[20, 30] == 100  # x=1 m wall
    assert one.cells()[20, 31] == -1  # behind wall stays unknown
    assert one.cells()[20, 25] == 50  # one pass is still uncertain
    one.integrate(packet(1))
    assert one.cells()[20, 25] == 0


def test_missing_measurement_never_becomes_a_clearing_ray():
    grid = Occupancy(config())
    p = packet()
    p['rays'].append({'angle_rad': math.pi/2, 'range_m': None, 'hit': False})
    evidence = grid.integrate(p)
    assert evidence['missing_rays'] == 1
    assert grid.cells()[30, 20] == -1
    p['rays'] = [p['rays'][-1]]
    p['sequence'] = 1
    with pytest.raises(ValueError):
        grid.integrate(p)
    assert grid.scan_count == 1


def test_sensor_extrinsic_is_rotated_from_base_pose_not_ignored():
    cfg = config(); cfg['sensor_in_base']['x_m'] = .5
    grid = Occupancy(cfg)
    p = packet(); p['pose']['yaw_rad'] = math.pi/2
    grid.integrate(p)
    assert grid.cells()[35, 20] == 100  # sensor at (0,.5), hit at (0,1.5)
    assert grid.cells()[20, 20] == -1  # do not manufacture a free robot footprint
    assert grid.last_pose['x_m'] == grid.last_pose['y_m'] == 0.


def test_clipped_ray_does_not_invent_boundary_wall_and_odds_saturate():
    grid = Occupancy(config())
    grid.integrate(packet(rays=[{'angle_rad': 0., 'range_m': 4., 'hit': True}]))
    assert not np.any(grid.cells() == 100)
    other = Occupancy(config())
    for i in range(30):
        other.integrate(packet(i))
    assert other.odds[20, 30] == 400 and other.odds[20, 25] == -400


@pytest.mark.parametrize('mutation', [
    lambda p: p.update(epoch='new-reference'),
    lambda p: p.update(source='another-sensor'),
    lambda p: p.update(frame_id='phone_ar'),
    lambda p: p.update(calibration_id='different-installation'),
    lambda p: p.update(sequence=True),
    lambda p: p.update(stamp_s=float('nan')),
    lambda p: p['pose'].update(quality='lost'),
    lambda p: p['pose'].update(position_sigma_m=.11),
    lambda p: p['pose'].update(yaw_sigma_rad=.2),
    lambda p: p['pose'].update(stamp_s=1.3),
    lambda p: p['rays'].append({'angle_rad': 0, 'range_m': float('inf'), 'hit': False}),
    lambda p: p['rays'].append({'angle_rad': 0, 'range_m': None, 'hit': True}),
    lambda p: p.update(drive=True),
])
def test_invalid_scan_is_rejected_before_any_partial_update(mutation):
    grid = Occupancy(config()); p = packet(); mutation(p)
    with pytest.raises(ValueError):
        grid.integrate(p)
    assert grid.scan_count == 0 and not grid.seen.any()


def test_reordered_time_and_unexplained_pose_jump_need_new_epoch():
    grid = Occupancy(config()); grid.integrate(packet())
    p = packet(1); p['stamp_s'] = p['pose']['stamp_s'] = 1.
    with pytest.raises(ValueError, match='reversed'):
        grid.integrate(p)
    p = packet(1); p['stamp_s'] = p['pose']['stamp_s'] = 1.01; p['pose']['x_m'] = 1.
    with pytest.raises(ValueError, match='discontinuity'):
        grid.integrate(p)
    assert grid.scan_count == 1


def test_inflation_blocks_narrow_door_unknown_uncertain_and_map_edges():
    cells = np.zeros((40, 60), dtype=int)
    cells[:, 30] = 100; cells[15:25, 30] = 0  # 1 m door, body diameter >=1.1 m
    safe = safe_centres(cells, .1, .55)
    assert astar(safe, (12, 20), (48, 20)) == []
    assert not safe[0, 10]  # map exterior is unknown
    cells[20, 12] = -1
    assert not safe_centres(cells, .1, .55)[20, 12]
    cells[20, 12] = 50
    assert not safe_centres(cells, .1, .55)[20, 12]
    with pytest.raises(ValueError):
        safe_centres(cells, .1, .1)


def test_astar_cannot_cut_a_diagonal_corner_but_finds_real_gap():
    assert astar(np.array([[True, False], [False, True]]), (0, 0), (1, 1)) == []
    safe = np.ones((9, 9), dtype=bool); safe[:7, 4] = False
    path = astar(safe, (1, 1), (7, 1))
    assert path[0] == (1, 1) and path[-1] == (7, 1)
    assert any(y >= 7 for _, y in path)
    assert all(safe[y, x] for x, y in path)


def test_frontiers_are_reachable_viewpoints_on_known_side_not_remote_room():
    cfg = config(); cfg['grid'] = asdict(GridSpec(80, 40, .1, -4, -2))
    grid = Occupancy(cfg)
    grid.seen[5:35, 5:35] = True; grid.odds[5:35, 5:35] = -100
    grid.seen[5:35, 50:75] = True; grid.odds[5:35, 50:75] = -100
    world = grid.spec.world(20, 20)
    grid.last_pose = {'x_m': world[0], 'y_m': world[1], 'yaw_rad': 0.}
    result = plan(grid)
    assert result['status'] == 'frontier_preview' and result['frontiers']
    safe = np.array(result['safe_centres']).reshape(grid.odds.shape)
    for x, y in result['path_m']:
        cx, cy = grid.spec.cell(x, y)
        assert cx < 35 and safe[cy, cx]
    assert all(f['goal_m'][0] < 0 for f in result['frontiers'])
    assert not result['motion_enabled'] and not result['automatic_clearance']


def test_two_writers_cannot_apply_same_revision_and_restart_replays(tmp_path):
    first, second = MappingStore(tmp_path), MappingStore(tmp_path)
    sid = first.create(config())['id']
    def append(store):
        try:
            return store.append(sid, 0, packet())['revision']
        except Conflict:
            return 'conflict'
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(append, (first, second)))
    assert sorted(map(str, results)) == ['1', 'conflict']
    data = MappingStore(tmp_path).read(sid)
    assert data['scan_count'] == 1 and data['revision'] == 1 and data['counts']['occupied'] == 1
    before = data['cells']
    with pytest.raises(ValueError):
        second.append(sid, 1, packet(1, epoch='restarted'))
    assert first.read(sid)['cells'] == before


def test_damaged_scan_fails_closed_without_repairing_or_overwriting(tmp_path):
    store = MappingStore(tmp_path); sid = store.create(config())['id']; store.append(sid, 0, packet())
    with sqlite3.connect(store.path) as c:
        c.execute('UPDATE scans SET packet=? WHERE session_id=?', ('{}', sid))
    before = hashlib.sha256(store.path.read_bytes()).hexdigest()
    with pytest.raises(OSError, match='integrity'):
        MappingStore(tmp_path).read(sid)
    assert hashlib.sha256(store.path.read_bytes()).hexdigest() == before


def test_corrupt_persisted_configuration_is_a_storage_failure(tmp_path):
    store = MappingStore(tmp_path); sid = store.create(config())['id']
    with sqlite3.connect(store.path) as c:
        c.execute('UPDATE sessions SET config=? WHERE id=?', ('{}', sid))
    with pytest.raises(OSError, match='configuration'):
        MappingStore(tmp_path).read(sid)


def test_vacuum_prior_crops_large_canvas_converts_cm_flips_y_and_wall_wins():
    data = valetudo_v2(vacuum_map())
    assert data['grid'] == {'width': 4, 'height': 2, 'resolution_m': .05,
                            'origin_x_m': 250., 'origin_y_m': 211.9}
    assert data['cells'] == [0, 0, 0, 0, 100, 0, 0, 0]
    assert data['registration'] == 'unregistered' and not data['navigation_use']
    value = vacuum_map(); value['layers'][0]['compressedPixels'] = [10239, 0, 2]
    with pytest.raises(ValueError, match='bounds'):
        valetudo_v2(value)


def test_imported_free_space_never_changes_robot_occupancy_or_pose(tmp_path):
    store = MappingStore(tmp_path); sid = store.create(config())['id']
    before = store.read(sid)
    prior = store.import_prior(sid, 0, vacuum_map())
    after = MappingStore(tmp_path).read(sid)
    assert before['cells'] == after['cells'] and after['last_pose'] is None
    assert after['revision'] == 1 and after['scan_count'] == 0
    assert not store.prior(sid, prior['id'])['navigation_use']
    assert store.preview(sid, 1)['status'] == 'no_pose'
    assert store.archive(sid)['external_priors'] == [vacuum_map()]
    store.append(sid, 1, packet())  # scan sequence remains 0 despite prior revision
    with pytest.raises(KeyError):
        store.prior(sid, '../private')


def test_demo_is_repeatable_and_export_keeps_unknowns(tmp_path):
    outputs = []
    for name in ('a', 'b'):
        store = MappingStore(tmp_path/name)
        sid = store.create(configuration(), mode='synthetic_demo')['id']
        for p in scans():
            store.append(sid, p['sequence'], p)
        data = store.read(sid); preview = store.preview(sid, data['revision'])
        assert preview['status'] == 'frontier_preview' and preview['path_m']
        assert data['scan_count'] == 24 and data['counts']['unknown'] > 0 and data['counts']['occupied'] > 0
        assert data['llm_calls'] == 0 and not data['live_freshness_verified']
        pgm = store.pgm(sid)
        assert len(pgm.split(b'\n', 4)[4]) == 160*120 and 205 in pgm.split(b'\n', 4)[4]
        outputs.append((data['cells'], preview['path_m']))
    assert outputs[0] == outputs[1]


def test_exported_replay_restores_to_new_volume_with_source_hash(tmp_path):
    from .metric_cli import run
    first = run(tmp_path/'outputs')
    archive = Path(first['output'])/'mapping.json'
    original = archive.read_bytes()
    restored = run(tmp_path/'outputs', archive)
    assert restored['session_id'] != first['session_id']
    assert first['grid_sha256'] == restored['grid_sha256']
    assert restored['provenance']['source_sha256'] == hashlib.sha256(original).hexdigest()
    assert archive.read_bytes() == original and restored['motion_commands'] == 0
