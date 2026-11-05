"""Whole-body sweep must cover intermediate angles and never authorise motion."""
import math
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from deployment.fieldmap.body import profile, plan, rotation_bounds
from deployment.fieldmap.integration import install, API


def test_intermediate_corner_hits_wall_despite_clear_endpoint_boxes():
    b = profile()['box_m']
    bounds = rotation_bounds(b, 90., 0.)
    assert bounds['x_max'] == pytest.approx(.5)
    assert bounds['y_max'] == pytest.approx(.5)
    # At both 0 and 90deg x_max <= .4; a corner sweeps as far as .5 mid-turn.
    p = plan({'posture': 'folded', 'turn_deg': 90,
              'obstacles': [{'x_min': .49, 'x_max': .6, 'y_min': -.1, 'y_max': .1}]})
    assert p['known_intersections'] == [{'index': 0, 'stages': ['rotation']}]
    assert p['assessment'] == 'possible_collision' and not p['motion_enabled']


@pytest.mark.parametrize('turn', [-180., -90., -37., 0., 37., 90., 180.])
def test_analytic_sweep_encloses_every_sampled_corner(turn):
    b = profile()['box_m']; bounds = rotation_bounds(b, turn, .05)
    for k in range(101):
        angle = math.radians(turn*k/100)
        for x in (b['x_min'], b['x_max']):
            for y in (b['y_min'], b['y_max']):
                u, v = x*math.cos(angle)-y*math.sin(angle), x*math.sin(angle)+y*math.cos(angle)
                assert bounds['x_min'] <= u-.049999 <= u+.049999 <= bounds['x_max']
                assert bounds['y_min'] <= v-.049999 <= v+.049999 <= bounds['y_max']


def test_translate_before_turn_keeps_path_and_rear_swing():
    p = plan({'posture': 'folded', 'forward_m': .1, 'turn_deg': 180.})
    assert p['translation_aabb_m']['x_min'] == pytest.approx(-.45)
    assert p['rotation_aabb_m']['x_min'] == pytest.approx(-.45)
    assert p['combined_aabb_m']['x_max'] == pytest.approx(.65)
    assert p['full_turn_radius_m'] == pytest.approx(.55)
    assert p['assessment'] == 'clearance_unconfirmed'
    assert not p['automatic_clearance']


@pytest.mark.parametrize('payload', [
    {}, {'posture': 'extended'}, {'posture': 'folded', 'turn_deg': float('nan')},
    {'posture': 'folded', 'forward_m': True}, {'posture': 'folded', 'turn_deg': 181},
    {'posture': 'folded', 'obstacles': [{'x_min': 1, 'x_max': 0, 'y_min': 0, 'y_max': 1}]},
])
def test_unknown_posture_and_invalid_geometry_are_rejected(payload):
    with pytest.raises(ValueError): plan(payload)


def test_studio_routes_are_read_only_previews_and_reject_foreign_origin(tmp_path):
    app = FastAPI();install(app, data_dir=tmp_path, allowed_origins=('http://local',))
    with TestClient(app) as c:
        assert c.get(API+'/body').json()['status'] == 'engineering_assumption_not_measured'
        assert c.post(API+'/body/plan', json={'posture': 'folded', 'turn_deg': 30}).json()['motion_enabled'] is False
        assert c.post(API+'/body/plan', json={'posture': 'extended'}).status_code == 422
        assert c.post(API+'/body/plan', json={'posture': 'folded'}, headers={'Origin': 'http://foreign'}).status_code == 403
