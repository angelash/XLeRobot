"""Facts, uncertainty, storage and no-motion boundaries of grip assessment."""
import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from deployment.grasp.force_policy import assess_grip, normalize_profile
from deployment.grasp.integration import install
from deployment.grasp.knowledge import Knowledge
from deployment.grasp.service import GraspService


def test_unknown_mass_does_not_invent_newtons_or_motor_target():
    a = assess_grip({'material': 'rigid_plastic', 'source': 'visual_estimate', 'confidence': .8})
    assert a['mass_band'] == 'unknown' and a['per_jaw_normal_force_N_estimate'] is None
    assert a['closing_target_raw'] is None and not a['auto_apply']
    assert a['torque_ladder_raw'] == [450, 600]


def test_more_weight_does_not_raise_fragile_object_torque():
    fragile = {'material': 'glass_ceramic', 'fragility': 'fragile', 'mass_source': 'user'}
    assert assess_grip({**fragile, 'mass_g': [20, 30]})['torque_ladder_raw'] == [250, 350]
    a = assess_grip({**fragile, 'mass_g': [500, 800]})
    assert a['torque_ladder_raw'] == [250, 350] and a['mass_band'] == 'capacity_review_required'


def test_theoretical_force_never_becomes_pwm_calibration():
    a = assess_grip({'mass_g': [100, 200], 'mass_source': 'measured', 'friction_range': [.2, .5],
                    'friction_source': 'catalog', 'contact_condition': 'specified pad and material pair'})
    assert a['per_jaw_normal_force_N_estimate'] == pytest.approx([1.962, 9.81])
    assert not a['force_to_servo_calibration_available'] and not a['auto_apply']


@pytest.mark.parametrize('profile', [
    {'mass_g': [True, 20], 'mass_source': 'user'}, {'mass_g': [100, 20], 'mass_source': 'user'},
    {'mass_g': [10, 20]}, {'confidence': float('nan')}, {'confidence': True},
    {'friction_range': [0, .5]}, {'material': 'silk-steel'}, {'squeeze_torque': 1000},
    {'friction_range': [.2, .5]}, {'friction_range': [.2, .5], 'friction_source': 'catalog'},
])
def test_invalid_or_command_fields_are_rejected(profile):
    with pytest.raises(ValueError):
        normalize_profile(profile)


def test_profile_history_preserves_templates_statistics_and_prior_conditions(tmp_path):
    k = Knowledge(tmp_path)
    k.record('plug', True, 'operator')
    before = json.loads(json.dumps(k.data['objects']['plug']))
    k.set_profile('plug', {'material': 'rigid_plastic', 'source': 'visual_estimate', 'confidence': .7})
    k.set_profile('plug', {'material': 'rigid_plastic', 'source': 'user', 'confidence': 1,
                           'mass_g': [60, 65], 'mass_source': 'measured'})
    again = Knowledge(tmp_path)
    obj = again.data['objects']['plug']
    assert obj['templates'] == before['templates'] and obj['stats'] == before['stats']
    assert len(obj['profile_history']) == 2
    assert obj['profile_history'][1]['previous']['mass_g'] is None
    assert again.profile('plug')['profile']['mass_g'] == [60, 65]


def test_failed_profile_save_does_not_replace_in_memory_facts(tmp_path, monkeypatch):
    k = Knowledge(tmp_path); k.set_profile('plug', {'source': 'user'})
    old = json.loads(json.dumps(k.data))
    monkeypatch.setattr(k, '_save', lambda: (_ for _ in ()).throw(OSError('disk unavailable')))
    with pytest.raises(OSError):
        k.set_profile('plug', {'material': 'metal'})
    assert k.data == old and Knowledge(tmp_path).data == old


def test_profile_api_is_same_platform_and_never_starts_motion(tmp_path):
    service = GraspService(tmp_path)
    config = dict(service.config)
    app = FastAPI(); install(app, data_dir=None, service=service, allowed_origins=('http://studio',))
    client = TestClient(app); endpoint = '/api/platform/grasp/objects/plug/profile'
    assert client.get(endpoint).json()['assessment']['mass_band'] == 'unknown'
    profile = {'category': 'charger', 'material': 'rigid_plastic', 'source': 'visual_estimate', 'confidence': .8}
    assert client.post(endpoint, json={'profile': profile}, headers={'Origin': 'http://other'}).status_code == 403
    assert client.post(endpoint, json={'profile': profile}).status_code == 200
    assert service.config == config and service.op_thread is None and not service.executor.active
    client.post(endpoint, json={'profile': {**profile, 'hazards': ['live_power']}})
    with pytest.raises(ValueError, match='危险项'):
        service.run(True, {'object': 'plug'})
    assert service.op_thread is None and not service.executor.active
