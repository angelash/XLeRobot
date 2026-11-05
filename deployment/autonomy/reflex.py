"""Small local outcome hints learned from reviewed episodes, never motor permission.

Each physical attempt supplies exactly one pre-lift feature vector and an
independently reviewed full-cycle label. Sessions and episode IDs stay disjoint
across training/validation. No vision, force measurement or safety claim is
inferred from servo load. Models live in the existing learning ledger.
"""
from __future__ import annotations

import math
import time
import uuid

from .learning import stamp

FEATURES = ('closed_load', 'closed_lead', 'closed_position', 'torque_limit')
RANGES = ((0, 1023), (-900, 900), (0, 4095), (150, 750))


def vector(data):
    if not isinstance(data, dict) or set(data) != set(FEATURES):
        raise ValueError('需要完整的夹持特征：' + ', '.join(FEATURES))
    out = []
    for key, (low, high) in zip(FEATURES, RANGES):
        v = data[key]
        if type(v) not in (int, float) or not math.isfinite(v) or not low <= v <= high:
            raise ValueError('夹持特征无效：' + key)
        out.append(float(v))
    return out


def episodes(ledger, ids):
    if not isinstance(ids, list) or not 1 <= len(ids) <= 100 or len(set(ids)) != len(ids):
        raise ValueError('需要1–100个不同学习会话')
    out, seen = [], set()
    for sid in ids:
        session = ledger.session(sid)
        if session['status'] == 'open':
            raise ValueError('先结束样本会话再训练/验证')
        for sample in session['samples']:
            if sample.get('quality') != 'ok' or 'reflex_features' not in sample.get('observed', {}):
                continue
            truth = sample.get('truth') or {}
            if truth.get('kind') not in ('llm_judgment', 'user_judgment') or truth.get('unit') != 'label':
                raise ValueError('结果标签必须来自独立图像或现场复核')
            if truth.get('value') not in ('success', 'failed'):
                continue  # unconfirmed stays in ledger, cannot supply a binary training label
            inp = sample.get('inputs', {})
            eid, scope = inp.get('episode_id'), inp.get('reflex_scope')
            if not isinstance(eid, str) or not 1 <= len(eid) <= 160 or not isinstance(scope, str) or not scope:
                raise ValueError('需要物理episode_id和适用条件reflex_scope')
            if eid in seen:
                raise ValueError('同一物理尝试不能重复采样')
            seen.add(eid)
            out.append({'episode': eid, 'scope': scope, 'session': sid, 'sample': sample['id'],
                        'x': vector(sample['observed']['reflex_features']), 'y': int(truth['value'] == 'success')})
            if len(out) > 2000:
                raise ValueError('单次训练/验证最多2000个独立尝试')
    return out


def _hint(model, values, scope):
    if scope not in model['params']['scopes']:
        return {'hint': 'unknown', 'reason': '适用条件不匹配'}
    p = model['parameters']
    x = [(v-m)/s for v, m, s in zip(values, p['mean'], p['scale'])]
    pairs = sorted((math.dist(x, row['x']), row['y']) for row in p['neighbors'])
    if pairs[0][0] > 2.0:
        return {'hint': 'unknown', 'reason': '超出训练样本覆盖范围'}
    neighbors = pairs[:min(5, len(pairs))]
    weights = [1/(.15+d) for d, _ in neighbors]
    score = sum(w*y for w, (_, y) in zip(weights, neighbors))/sum(weights)
    return {'hint': 'likely_success' if score >= .8 else 'likely_failure' if score <= .2 else 'unknown',
            'success_vote': round(score, 5), 'nearest_distance': round(pairs[0][0], 5),
            'reason': '历史相似尝试的加权投票；不是概率标定或安全许可'}


class Reflex:
    def __init__(self, ledger):
        self.ledger = ledger

    def train(self, session_ids):
        rows = episodes(self.ledger, session_ids)
        if len({r['scope'] for r in rows}) != 1:
            raise ValueError('每个模型只学习一种适用条件，不能混合不同reflex_scope')
        if len(rows) < 8 or min(sum(r['y'] for r in rows), sum(1-r['y'] for r in rows)) < 3:
            raise ValueError('至少8个独立尝试，成功/失败各至少3个')
        mean = [sum(r['x'][i] for r in rows)/len(rows) for i in range(len(FEATURES))]
        scale = [max(floor, math.sqrt(sum((r['x'][i]-mean[i])**2 for r in rows)/len(rows)))
                 for i, floor in enumerate((20, 5, 80, 100))]
        model = {'id': uuid.uuid4().hex, 'kind': 'grasp_outcome_hint', 'created_at': stamp(), 'status': 'candidate',
                 'sessions': list(session_ids), 'samples': [r['session']+'/'+r['sample'] for r in rows],
                 'episode_ids': [r['episode'] for r in rows], 'features': list(FEATURES),
                 'params': {'scopes': sorted({r['scope'] for r in rows}), 'mode': 'shadow_only', 'motion_authority': False},
                 'parameters': {'mean': mean, 'scale': scale, 'neighbors': [
                     {'x': [(v-m)/s for v, m, s in zip(r['x'], mean, scale)], 'y': r['y']} for r in rows]},
                 'metrics': {'n': len(rows), 'successes': sum(r['y'] for r in rows)},
                 'validations': [], 'reviews': [],
                 'notes': ['仅本地旁路提示。闭合读数不能证明离桌；未知/未验证不能触发动作。']}
        self.ledger._write(self.ledger.root/'models'/(model['id']+'.json'), model)
        self.ledger._audit('reflex_trained', model=model['id'], n=len(rows))
        return model

    def _model(self, mid):
        m = self.ledger._read('models', mid)
        if m['kind'] != 'grasp_outcome_hint' or m['status'] == 'retired':
            raise ValueError('本地提示模型不可用')
        return m

    def predict(self, mid, features, scope):
        begun = time.perf_counter()
        model = self._model(mid)
        return {**_hint(model, vector(features), scope), 'model_id': mid, 'status': model['status'],
                'mode': 'shadow_only', 'motion_authority': False,
                'elapsed_ms': round((time.perf_counter()-begun)*1000, 4)}

    def validate(self, mid, session_ids):
        model = self._model(mid)
        if set(session_ids) & set(model['sessions']):
            raise ValueError('必须留出独立会话')
        rows = episodes(self.ledger, session_ids)
        if set(r['episode'] for r in rows) & set(model['episode_ids']):
            raise ValueError('同一物理尝试泄漏到验证集')
        if len(rows) < 5 or not {0, 1} <= {r['y'] for r in rows}:
            raise ValueError('验证至少5个独立尝试且包含成功/失败')
        correct, known, false_success, true_success = 0, 0, 0, 0
        for r in rows:
            h = _hint(model, r['x'], r['scope'])['hint']
            if h == 'unknown':
                continue
            prediction = int(h == 'likely_success')
            known += 1
            correct += prediction == r['y']
            false_success += prediction == 1 and r['y'] == 0
            true_success += prediction == 1 and r['y'] == 1
        agreement = correct/len(rows)  # abstentions do not inflate agreement
        success_count = sum(r['y'] for r in rows)
        success_recall = true_success/success_count
        passed = len(rows) >= 20 and agreement >= .95 and false_success == 0 and success_recall >= .8
        record = {'at': stamp(), 'sessions': session_ids, 'n': len(rows), 'known': known,
                  'coverage': known/len(rows), 'agreement': agreement, 'false_success': false_success,
                  'success_recall': success_recall, 'passed': passed}
        model['validations'].append(record)
        model['status'] = 'validated' if passed else 'candidate'
        self.ledger._write(self.ledger.root/'models'/(mid+'.json'), model)
        self.ledger._audit('reflex_validated', model=mid, **record)
        return model
