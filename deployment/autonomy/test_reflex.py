import pytest

from deployment.autonomy.learning import Ledger
from deployment.autonomy.reflex import Reflex, vector


def examples(ledger, prefix, count=12):
    s = ledger.create_session('G3')
    for i in range(count):
        ok = bool(i % 2)
        ledger.add_sample(s['id'], {'inputs': {'episode_id': f'{prefix}-{i}', 'reflex_scope': 'adapter-v1'},
            'observed': {'reflex_features': {'closed_load': 100+i if ok else 20+i,
                'closed_lead': 25 if ok else 5, 'closed_position': 1600, 'torque_limit': 600}},
            'truth': {'kind': 'llm_judgment', 'unit': 'label', 'value': 'success' if ok else 'failed'}})
    ledger.close_session(s['id'])
    return s['id']


def test_real_training_persistence_and_no_motion_permission(tmp_path):
    ledger = Ledger(tmp_path)
    train = examples(ledger, 'train')
    model = Reflex(ledger).train([train])
    result = Reflex(Ledger(tmp_path)).predict(model['id'], dict(zip(
        ('closed_load', 'closed_lead', 'closed_position', 'torque_limit'), (105, 25, 1600, 600))), 'adapter-v1')
    assert result['hint'] == 'likely_success'
    assert result['motion_authority'] is False and result['status'] == 'candidate'
    assert model['id'] in [x['id'] for x in ledger.models()]


def test_holdout_is_by_physical_attempt_not_ledger_id(tmp_path):
    l = Ledger(tmp_path)
    a, duplicate = examples(l, 'same'), examples(l, 'same')
    reflex = Reflex(l)
    model = reflex.train([a])
    with pytest.raises(ValueError, match='独立会话'):
        reflex.validate(model['id'], [a])
    with pytest.raises(ValueError, match='泄漏'):
        reflex.validate(model['id'], [duplicate])
    with pytest.raises(ValueError, match='重复采样'):
        reflex.train([a, duplicate])


def test_out_of_domain_and_small_validation_do_not_promote(tmp_path):
    l = Ledger(tmp_path)
    a, b = examples(l, 'train'), examples(l, 'holdout')
    r = Reflex(l)
    m = r.train([a])
    f = {'closed_load': 100, 'closed_lead': 25, 'closed_position': 1600, 'torque_limit': 600}
    assert r.predict(m['id'], f, 'different-pads')['hint'] == 'unknown'
    assert r.predict(m['id'], f | {'closed_load': 900}, 'adapter-v1')['hint'] == 'unknown'
    validated = r.validate(m['id'], [b])
    assert validated['status'] == 'candidate', 'Twelve samples do not establish the >=20 validation requirement'
    assert validated['validations'][0]['coverage'] <= 1


def test_open_bad_or_unreviewed_data_rejected(tmp_path):
    l = Ledger(tmp_path)
    s = l.create_session('G3')
    with pytest.raises(ValueError, match='先结束'):
        Reflex(l).train([s['id']])
    for data in ({}, {'closed_load': float('nan'), 'closed_lead': 1, 'closed_position': 1500, 'torque_limit': 600}):
        with pytest.raises(ValueError):
            vector(data)


def test_different_hardware_scopes_cannot_mix_in_one_model(tmp_path):
    l = Ledger(tmp_path)
    first = examples(l, 'old-pads')
    other = examples(l, 'new-pads')
    session = l.session(other)
    for sample in session['samples']:
        sample['inputs']['reflex_scope'] = 'different-pads'
    l._save_session(session)
    with pytest.raises(ValueError, match='适用条件'):
        Reflex(l).train([first, other])


def test_majority_failure_accuracy_cannot_promote_a_useless_model(tmp_path):
    l = Ledger(tmp_path)
    train = examples(l, 'train')
    hold = l.create_session('G3')['id']
    for i in range(20):
        l.add_sample(hold, {'inputs': {'episode_id': f'hold-{i}', 'reflex_scope': 'adapter-v1'},
            'observed': {'reflex_features': {'closed_load': 21, 'closed_lead': 5,
                'closed_position': 1600, 'torque_limit': 600}},
            'truth': {'kind': 'llm_judgment', 'unit': 'label', 'value': 'success' if i == 0 else 'failed'}})
    l.close_session(hold)
    r = Reflex(l)
    m = r.validate(r.train([train])['id'], [hold])
    assert m['validations'][-1]['agreement'] == .95
    assert m['validations'][-1]['success_recall'] == 0
    assert m['status'] == 'candidate'


def test_http_endpoints_train_and_predict_without_hardware(tmp_path):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from deployment.autonomy.integration import install
    app = FastAPI()
    install(app, data_dir=tmp_path, start=False)
    ledger = app.state.autonomy['ledger']
    sid = examples(ledger, 'api')
    c = TestClient(app)
    model = c.post('/api/platform/autonomy/learning/reflex/train', json={'sessions': [sid]}).json()
    result = c.post('/api/platform/autonomy/learning/reflex/'+model['id']+'/predict', json={
        'scope': 'adapter-v1', 'features': {'closed_load': 108, 'closed_lead': 25, 'closed_position': 1600, 'torque_limit': 600}})
    assert result.status_code == 200 and result.json()['motion_authority'] is False
