"""Learning ledger: sessions, provenance-tagged samples, fitted models and reviews.

Models predict; they never command hardware or relax limits. Adoption is a
user decision recorded with the validation evidence that justified it.
"""
from __future__ import annotations

import json
import math
import re
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path

from . import protocols

TRUTH_KINDS = ('tape_measure', 'marker_vision', 'ground_homography', 'llm_judgment', 'user_judgment', 'encoder')
METRIC_TRUTH = ('tape_measure', 'marker_vision', 'ground_homography')
UNITS = ('m', 'deg', 'counts', 'px', 'bool', 'label', 's')
QUALITY = ('ok', 'outlier', 'invalid')
MODEL_KINDS = ('wheel_distance', 'wheel_rotation', 'wheel_speed', 'joint_step', 'gripper_width', 'stereo_range')
CALIBRATION_KINDS = {'intrinsics': ('rms_px', 0.8), 'stereo': ('rms_px', 1.0), 'ground_homography': ('rms_m', 0.01),
                     'hand_eye': (None, None)}
DESIGN_RADIUS_M = 0.0635
COUNTS_PER_REV = 4096
RUN_ID = re.compile(r'^\d{8}-\d{6}$')
ITEM_ID = re.compile(r'^[a-f0-9]{32}$')


def stamp():
    return datetime.now().astimezone().isoformat()


def _text(value, limit=500):
    return str(value or '')[:limit]


def linfit(x, y, intercept=True):
    """Least squares with residual RMSE and leave-one-out RMSE (small n)."""
    n = len(x)
    if n < (3 if intercept else 2) or n != len(y):
        raise ValueError('拟合样本不足')

    def solve(xs, ys):
        if intercept:
            mx, my = sum(xs)/len(xs), sum(ys)/len(ys)
            sxx = sum((a-mx)**2 for a in xs)
            if sxx <= 0:
                raise ValueError('自变量没有变化，无法拟合')
            slope = sum((a-mx)*(b-my) for a, b in zip(xs, ys))/sxx
            return slope, my-slope*mx
        sxx = sum(a*a for a in xs)
        if sxx <= 0:
            raise ValueError('自变量全为零，无法拟合')
        return sum(a*b for a, b in zip(xs, ys))/sxx, 0.0

    slope, offset = solve(x, y)
    residuals = [b-(slope*a+offset) for a, b in zip(x, y)]
    my = sum(y)/n
    total = sum((b-my)**2 for b in y)
    rmse = math.sqrt(sum(r*r for r in residuals)/n)
    loo = []
    for i in range(n):
        xs, ys = x[:i]+x[i+1:], y[:i]+y[i+1:]
        try:
            s, o = solve(xs, ys)
            loo.append(y[i]-(s*x[i]+o))
        except ValueError:
            pass
    return {'slope': slope, 'intercept': offset, 'n': n, 'rmse': rmse,
            'r2': 1-sum(r*r for r in residuals)/total if total > 0 else None,
            'loo_rmse': math.sqrt(sum(r*r for r in loo)/len(loo)) if loo else None,
            'max_abs_residual': max(abs(r) for r in residuals)}


def _wheel_counts(observed):
    delta = observed.get('wheel_delta_raw') or {}
    values = [delta.get(k, delta.get(int(k)) if k.isdigit() else None) for k in ('9', '10')]
    if any(not isinstance(v, (int, float)) for v in values):
        return None
    return values


def _counts_per_s(trace):
    points = []
    for row in trace or []:
        counts = row.get('counts') or {}
        values = [counts.get(k, counts.get(int(k))) for k in ('9', '10')]
        if all(isinstance(v, (int, float)) for v in values) and isinstance(row.get('t'), (int, float)):
            points.append((float(row['t']), sum(abs(v) for v in values)/2))
    if len(points) < 4:
        return None
    start = max(p[0] for p in points)*0.25
    steady = [p for p in points if p[0] >= start]
    if len(steady) < 3:
        return None
    try:
        return linfit([p[0] for p in steady], [p[1] for p in steady])['slope']
    except ValueError:
        return None


class Ledger:
    def __init__(self, root, evidence_root=None):
        self.root = Path(root)
        for name in ('sessions', 'models', 'calibrations'):
            (self.root/name).mkdir(parents=True, exist_ok=True)
        self.evidence_root = Path(evidence_root) if evidence_root else None
        self.lock = threading.RLock()

    # ---- storage ---------------------------------------------------------
    def _write(self, path, data):
        with self.lock:
            tmp = path.with_suffix('.tmp')
            tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
            for attempt in range(5):
                try:
                    tmp.replace(path)
                    return
                except PermissionError:
                    if attempt == 4:
                        raise
                    time.sleep(0.02)

    def _read(self, folder, item_id):
        if not ITEM_ID.fullmatch(str(item_id)):
            raise KeyError('编号无效')
        path = self.root/folder/(item_id+'.json')
        if not path.is_file():
            raise KeyError('记录不存在')
        with self.lock:
            return json.loads(path.read_text(encoding='utf-8'))

    def _all(self, folder):
        items = []
        with self.lock:
            for path in (self.root/folder).glob('*.json'):
                try:
                    items.append(json.loads(path.read_text(encoding='utf-8')))
                except (OSError, ValueError):
                    continue
        return sorted(items, key=lambda x: x.get('created_at', ''), reverse=True)

    def _audit(self, event, **details):
        with self.lock, (self.root/'audit.jsonl').open('a', encoding='utf-8') as handle:
            handle.write(json.dumps({'at': stamp(), 'event': event, **details}, ensure_ascii=False)+'\n')

    # ---- sessions --------------------------------------------------------
    def create_session(self, protocol_id, operator='assistant', environment=None, note=''):
        protocol = protocols.get(str(protocol_id))
        environment = environment or {}
        if not isinstance(environment, dict) or len(environment) > 20:
            raise ValueError('环境说明需为不超过20项的对象')
        session = {'id': uuid.uuid4().hex, 'protocol': protocol['id'], 'title': protocol['title'], 'created_at': stamp(),
                   'operator': _text(operator, 40), 'environment': {_text(k, 40): _text(v, 200) for k, v in environment.items()},
                   'note': _text(note), 'status': 'open', 'samples': [], 'runs': [], 'photos': []}
        self._write(self.root/'sessions'/(session['id']+'.json'), session)
        self._audit('session_created', session=session['id'], protocol=protocol['id'])
        return session

    def session(self, session_id):
        return self._read('sessions', session_id)

    def sessions(self):
        return [{k: s.get(k) for k in ('id', 'protocol', 'title', 'created_at', 'operator', 'status', 'note')}
                | {'samples': len(s.get('samples', [])), 'with_truth': sum(1 for x in s.get('samples', []) if x.get('truth')),
                   'runs': len(s.get('runs', [])), 'photos': len(s.get('photos', []))} for s in self._all('sessions')]

    def _save_session(self, session):
        self._write(self.root/'sessions'/(session['id']+'.json'), session)

    def _open(self, session_id):
        session = self.session(session_id)
        if session['status'] != 'open':
            raise ValueError('会话已结束，不能再添加或修改样本')
        return session

    @staticmethod
    def _truth(truth):
        if truth is None:
            return None
        if not isinstance(truth, dict) or truth.get('kind') not in TRUTH_KINDS or truth.get('unit') not in UNITS:
            raise ValueError('真值需包含kind（'+'/'.join(TRUTH_KINDS)+'）与unit（'+'/'.join(UNITS)+'）')
        value = truth.get('value')
        if truth['unit'] in ('label',):
            value = _text(value, 200)
        elif truth['unit'] == 'bool':
            if not isinstance(value, bool):
                raise ValueError('布尔真值必须是true/false')
        else:
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
                raise ValueError('数值真值必须是有限数字')
            value = float(value)
        return {'kind': truth['kind'], 'unit': truth['unit'], 'value': value, 'note': _text(truth.get('note')),
                'recorded_at': stamp(), 'by': _text(truth.get('by', 'assistant'), 40)}

    def add_sample(self, session_id, sample):
        with self.lock:
            session = self._open(session_id)
            if not isinstance(sample, dict):
                raise ValueError('样本需为对象')
            inputs, observed = sample.get('inputs', {}), sample.get('observed', {})
            if not isinstance(inputs, dict) or not isinstance(observed, dict):
                raise ValueError('inputs与observed需为对象')
            if len(json.dumps([inputs, observed])) > 60000:
                raise ValueError('单个样本过大')
            quality = sample.get('quality', 'ok')
            if quality not in QUALITY:
                raise ValueError('质量标记无效')
            entry = {'id': f"s{len(session['samples'])+1:03d}", 'created_at': stamp(), 'inputs': inputs, 'observed': observed,
                     'truth': self._truth(sample.get('truth')), 'source_run': sample.get('source_run'),
                     'quality': quality, 'quality_reason': _text(sample.get('quality_reason')), 'note': _text(sample.get('note'))}
            session['samples'].append(entry)
            self._save_session(session)
            return entry

    def _sample(self, session, sample_id):
        for sample in session['samples']:
            if sample['id'] == sample_id:
                return sample
        raise KeyError('样本不存在')

    def set_truth(self, session_id, sample_id, truth):
        with self.lock:
            session = self._open(session_id)
            sample = self._sample(session, sample_id)
            if sample.get('truth'):
                sample.setdefault('truth_history', []).append(sample['truth'])
            sample['truth'] = self._truth(truth)
            self._save_session(session)
            return sample

    def set_quality(self, session_id, sample_id, quality, reason=''):
        if quality not in QUALITY:
            raise ValueError('质量标记无效')
        with self.lock:
            session = self._open(session_id)
            sample = self._sample(session, sample_id)
            sample.setdefault('quality_history', []).append({'quality': sample['quality'], 'reason': sample.get('quality_reason')})
            sample.update(quality=quality, quality_reason=_text(reason))
            self._save_session(session)
            return sample

    def close_session(self, session_id, status='closed', reason=''):
        if status not in ('closed', 'interrupted'):
            raise ValueError('结束状态只能是closed或interrupted')
        with self.lock:
            session = self._open(session_id)
            session.update(status=status, closed_at=stamp(), close_reason=_text(reason))
            self._save_session(session)
            self._audit('session_closed', session=session_id, status=status)
            return session

    def _run_dir(self, run_id):
        if not RUN_ID.fullmatch(str(run_id)):
            raise ValueError('运行编号格式应为YYYYMMDD-HHMMSS')
        if not self.evidence_root:
            raise ValueError('未配置车载运行证据目录')
        matches = sorted(self.evidence_root.glob(f'*/agent-runs/{run_id}/result.json'))
        if not matches:
            raise KeyError('没有找到该运行回执')
        return matches[-1].parent

    def import_run(self, session_id, run_id):
        """Copy facts from an assistant run receipt; truth is left for later."""
        folder = self._run_dir(run_id)
        result = json.loads((folder/'result.json').read_text(encoding='utf-8'))
        plan = {}
        if (folder/'plan.json').is_file():
            plan = json.loads((folder/'plan.json').read_text(encoding='utf-8'))
        steps = plan.get('steps', [])
        with self.lock:
            session = self._open(session_id)
            if run_id in session['runs']:
                raise ValueError('该运行已导入本会话')
            added = []
            for row in result.get('results', []):
                index = row.get('step')
                step = steps[index] if isinstance(index, int) and 0 <= index < len(steps) else {}
                op = row.get('op')
                source = {'run_id': run_id, 'step': index, 'op': op, 'run_error': result.get('error')}
                if op in ('travel', 'advance', 'rotate'):
                    detail = row.get('rotation') or {}
                    trace = row.get('travel_trace') or detail.get('travel_trace')
                    observed = {'status': row.get('status'), 'message': row.get('message'),
                                'wheel_delta_raw': row.get('wheel_delta_raw'), 'duration_s': row.get('duration_s'),
                                'command_raw': row.get('command_raw'), 'wheel_distance_m_estimate': row.get('wheel_distance_m_estimate'),
                                'yaw_delta_deg_estimate': row.get('yaw_delta_deg_estimate'), 'counts_per_s': _counts_per_s(trace),
                                'stop_settle_ms': (row.get('stop_trace') or [{}])[-1].get('after_zero_ms'),
                                'fault': detail.get('fault')}
                    if observed['wheel_delta_raw'] is None and trace:
                        last = trace[-1].get('counts', {})
                        observed['partial_counts'] = last
                    inputs = {k: step.get(k) for k in ('op', 'distance_m', 'max_raw', 'duration', 'direction') if k in step}
                    inputs.update(target_counts=row.get('target_counts'), requested_distance_m=row.get('requested_distance_m'))
                    entry = {'id': f"s{len(session['samples'])+1:03d}", 'created_at': stamp(), 'inputs': inputs,
                             'observed': observed, 'truth': None, 'source_run': source,
                             'quality': 'ok' if row.get('status') == 'done' else 'invalid',
                             'quality_reason': '' if row.get('status') == 'done' else _text(row.get('message')), 'note': ''}
                    session['samples'].append(entry)
                    added.append(entry['id'])
                elif op == 'move':
                    entry = {'id': f"s{len(session['samples'])+1:03d}", 'created_at': stamp(),
                             'inputs': {'op': 'move', 'targets': step.get('targets'), 'torque': step.get('torque'),
                                        'speed_deg': step.get('speed_deg')},
                             'observed': {'status': row.get('status'), 'positions': row.get('positions'), 'contact': row.get('contact'),
                                          'message': row.get('message')},
                             'truth': None, 'source_run': source,
                             'quality': 'ok' if row.get('status') == 'done' else 'invalid', 'quality_reason': _text(row.get('message')), 'note': ''}
                    session['samples'].append(entry)
                    added.append(entry['id'])
                elif op == 'snapshot':
                    session['photos'].append({'run_id': run_id, 'step': index, 'at': row.get('at'),
                                              'capture_started_at': row.get('capture_started_at'),
                                              'files': [s for s in row.get('shots', []) if 'file' in s],
                                              'positions': row.get('positions'), 'wheel_positions': row.get('wheel_positions')})
            session['runs'].append(run_id)
            self._save_session(session)
            self._audit('run_imported', session=session_id, run=run_id, samples=added)
            return {'run_id': run_id, 'samples': added, 'photos': len(session['photos']), 'run_error': result.get('error')}

    # ---- models ----------------------------------------------------------
    def _samples(self, session_ids):
        if not isinstance(session_ids, list) or not session_ids:
            raise ValueError('至少选择一个会话')
        rows = []
        for session_id in session_ids:
            session = self.session(session_id)
            rows += [(session_id, s) for s in session['samples'] if s.get('quality') == 'ok']
        return rows

    def _adopted(self, kind):
        for model in self._all('models'):
            if model['kind'] == kind and model['status'] == 'adopted':
                return model
        return None

    def _radius(self, params):
        if params.get('radius_m'):
            return float(params['radius_m']), 'parameter'
        adopted = self._adopted('wheel_distance')
        if adopted:
            return adopted['parameters']['radius_m'], 'adopted:'+adopted['id']
        return DESIGN_RADIUS_M, 'design_value_unmeasured'

    def _dataset(self, kind, rows, params):
        """Return [(x, y, sample_ref, extra)] for a model kind."""
        data = []
        for session_id, s in rows:
            truth, observed, inputs = s.get('truth'), s.get('observed', {}), s.get('inputs', {})
            ref = f"{session_id[:8]}/{s['id']}"
            if kind in ('wheel_distance', 'wheel_rotation'):
                counts = _wheel_counts(observed)
                if not counts or not truth or truth['kind'] not in METRIC_TRUTH+('user_judgment',):
                    continue
                if (kind == 'wheel_distance') != (truth['unit'] == 'm') or truth['unit'] not in ('m', 'deg'):
                    continue
                theta = (abs(counts[0])+abs(counts[1]))/2*2*math.pi/COUNTS_PER_REV
                data.append((theta, abs(truth['value']), ref, {'sign': 1 if truth['value'] >= 0 else -1,
                                                                'asymmetry': (abs(counts[0])-abs(counts[1]))/max(1e-9, (abs(counts[0])+abs(counts[1]))/2)}))
            elif kind == 'wheel_speed':
                raw = inputs.get('max_raw') or max((abs(v) for v in (observed.get('command_raw') or {}).values()), default=None)
                if isinstance(raw, (int, float)) and isinstance(observed.get('counts_per_s'), (int, float)):
                    data.append((float(raw), float(observed['counts_per_s']), ref, {}))
            elif kind == 'gripper_width':
                if truth and truth['unit'] == 'm' and isinstance(observed.get('position'), (int, float)):
                    data.append((float(observed['position']), truth['value'], ref, {}))
            elif kind == 'stereo_range':
                if truth and truth['unit'] == 'm' and isinstance(observed.get('measured_distance_m'), (int, float)):
                    data.append((truth['value'], observed['measured_distance_m']-truth['value'], ref, {}))
            elif kind == 'joint_step':
                target, achieved = inputs.get('target_delta_counts'), observed.get('achieved_delta_counts')
                if isinstance(target, (int, float)) and isinstance(achieved, (int, float)) and inputs.get('joint'):
                    data.append((float(target), float(achieved)-float(target), ref, {'joint': str(inputs['joint'])}))
        return data

    def fit(self, kind, session_ids, params=None):
        if kind not in MODEL_KINDS:
            raise ValueError('模型类型只能是 '+'、'.join(MODEL_KINDS))
        params = params or {}
        data = self._dataset(kind, self._samples(session_ids), params)
        if len(data) < 3:
            raise ValueError(f'可用于{kind}的有效样本只有{len(data)}个，至少需要3个带真值的样本')
        x = [d[0] for d in data]
        y = [d[1] for d in data]
        parameters, metrics, notes = {}, {}, []
        if kind == 'wheel_distance':
            overall = linfit(x, y, intercept=False)
            parameters = {'radius_m': overall['slope'], 'counts_per_5cm': 0.05/overall['slope']/(2*math.pi)*COUNTS_PER_REV,
                          'mean_asymmetry': sum(d[3]['asymmetry'] for d in data)/len(data)}
            for name, sign in (('forward', 1), ('backward', -1)):
                part = [d for d in data if d[3]['sign'] == sign]
                if len(part) >= 2:
                    sub = linfit([d[0] for d in part], [d[1] for d in part], intercept=False)
                    parameters[f'radius_{name}_m'] = sub['slope']
                    metrics[f'{name}_rmse_m'] = sub['rmse']
            metrics.update(rmse_m=overall['rmse'], loo_rmse_m=overall['loo_rmse'], n=overall['n'],
                           max_abs_residual_m=overall['max_abs_residual'])
            notes.append('有效轮径来自计数与地面真值，仅适用于采样时的地面与速度范围')
        elif kind == 'wheel_rotation':
            radius, source = self._radius(params)
            arcs = [t*radius for t in x]
            yaw = [math.radians(v) for v in y]
            fitted = linfit(arcs, yaw, intercept=False)
            parameters = {'track_m': 2/fitted['slope'], 'radius_m_used': radius, 'radius_source': source}
            metrics = {'rmse_deg': math.degrees(fitted['rmse']), 'loo_rmse_deg': math.degrees(fitted['loo_rmse'] or 0),
                       'n': fitted['n']}
        elif kind == 'wheel_speed':
            fitted = linfit(x, y)
            parameters = {'counts_per_s_per_raw': fitted['slope'], 'intercept_counts_per_s': fitted['intercept'],
                          'deadband_raw': -fitted['intercept']/fitted['slope'] if fitted['slope'] else None}
            metrics = {'r2': fitted['r2'], 'rmse_counts_per_s': fitted['rmse'], 'n': fitted['n']}
        elif kind == 'gripper_width':
            fitted = linfit(x, y)
            parameters = {'m_per_count': fitted['slope'], 'width_at_zero_m': fitted['intercept']}
            metrics = {'rmse_m': fitted['rmse'], 'loo_rmse_m': fitted['loo_rmse'], 'n': fitted['n']}
        elif kind == 'stereo_range':
            fitted = linfit(x, y)
            errors = y
            parameters = {'bias_m': sum(errors)/len(errors), 'error_slope_per_m': fitted['slope']}
            metrics = {'rmse_m': math.sqrt(sum(e*e for e in errors)/len(errors)), 'max_abs_error_m': max(abs(e) for e in errors),
                       'range_m': [min(x), max(x)], 'n': len(errors)}
        elif kind == 'joint_step':
            groups = {}
            for target, error, _, extra in data:
                groups.setdefault((extra['joint'], 'up' if target > 0 else 'down'), []).append(error)
            parameters = {f'{joint}:{direction}': {'mean_error_counts': sum(v)/len(v), 'n': len(v),
                                                   'std_counts': math.sqrt(sum((e-sum(v)/len(v))**2 for e in v)/len(v))}
                          for (joint, direction), v in groups.items()}
            metrics = {'max_abs_mean_error_counts': max(abs(p['mean_error_counts']) for p in parameters.values()), 'n': len(data)}
        model = {'id': uuid.uuid4().hex, 'kind': kind, 'created_at': stamp(), 'status': 'candidate', 'parameters': parameters,
                 'metrics': metrics, 'sessions': session_ids, 'samples': [d[2] for d in data], 'params': params, 'notes': notes,
                 'validations': [], 'reviews': []}
        self._write(self.root/'models'/(model['id']+'.json'), model)
        self._audit('model_fitted', model=model['id'], kind=kind, n=len(data))
        return model

    def _predict_errors(self, model, data):
        p, kind = model['parameters'], model['kind']
        if kind == 'wheel_distance':
            def radius(sign):
                return p.get('radius_forward_m' if sign > 0 else 'radius_backward_m') or p['radius_m']
            return [d[1]-radius(d[3]['sign'])*d[0] for d in data]
        if kind == 'wheel_rotation':
            return [d[1]-math.degrees(2*d[0]*p['radius_m_used']/p['track_m']) for d in data]
        if kind == 'wheel_speed':
            return [d[1]-(p['counts_per_s_per_raw']*d[0]+p['intercept_counts_per_s']) for d in data]
        if kind == 'gripper_width':
            return [d[1]-(p['m_per_count']*d[0]+p['width_at_zero_m']) for d in data]
        if kind == 'stereo_range':
            return [d[1] for d in data]
        if kind == 'joint_step':
            return [d[1]-p.get(f"{d[3]['joint']}:{'up' if d[0] > 0 else 'down'}", {}).get('mean_error_counts', 0) for d in data]
        raise ValueError('未知模型类型')

    def validate(self, model_id, session_ids):
        with self.lock:
            model = self._read('models', model_id)
            if set(session_ids) & set(model['sessions']):
                raise ValueError('验证必须使用拟合以外的会话')
            data = self._dataset(model['kind'], self._samples(session_ids), model.get('params', {}))
            if len(data) < 2:
                raise ValueError('验证样本不足（至少2个）')
            errors = self._predict_errors(model, data)
            rmse = math.sqrt(sum(e*e for e in errors)/len(errors))
            rule = protocols.VALIDATION.get(model['kind'], {})
            if model['kind'] == 'wheel_speed':
                ys = [d[1] for d in data]
                mean = sum(ys)/len(ys)
                total = sum((v-mean)**2 for v in ys)
                r2 = 1-sum(e*e for e in errors)/total if total else None
                passed = r2 is not None and r2 >= rule.get('min_r2', 0.98)
                record = {'r2': r2}
            elif model['kind'] == 'wheel_rotation':
                passed = rmse <= rule['max_rmse_deg']
                record = {'rmse_deg': rmse}
            elif model['kind'] == 'joint_step':
                worst = max(abs(e) for e in errors)
                passed = worst <= rule['max_abs_error_counts']
                record = {'max_abs_error_counts': worst}
            else:
                passed = rmse <= rule['max_rmse_m']
                record = {'rmse_m': rmse}
            validation = {'at': stamp(), 'sessions': session_ids, 'n': len(data), **record, 'rule': rule, 'passed': passed}
            model['validations'].append(validation)
            if passed and model['status'] == 'candidate':
                model['status'] = 'validated'
            self._write(self.root/'models'/(model_id+'.json'), model)
            self._audit('model_validated', model=model_id, passed=passed)
            return model

    def review(self, model_id, action, reviewer, note=''):
        with self.lock:
            model = self._read('models', model_id)
            if action == 'adopt':
                if reviewer != 'user':
                    raise ValueError('只有用户可以采用模型')
                if model['status'] != 'validated':
                    raise ValueError('只有通过验证的模型才能采用')
                previous = self._adopted(model['kind'])
                if previous and previous['id'] != model_id:
                    previous['status'] = 'retired'
                    previous['reviews'].append({'at': stamp(), 'action': 'superseded', 'by': model_id})
                    self._write(self.root/'models'/(previous['id']+'.json'), previous)
                model['status'] = 'adopted'
            elif action == 'retire':
                model['status'] = 'retired'
            else:
                raise ValueError('审核动作只能是adopt或retire')
            model['reviews'].append({'at': stamp(), 'action': action, 'reviewer': _text(reviewer, 40), 'note': _text(note)})
            self._write(self.root/'models'/(model_id+'.json'), model)
            self._audit('model_reviewed', model=model_id, action=action, reviewer=reviewer)
            return model

    def models(self):
        return self._all('models')

    # ---- calibrations ------------------------------------------------------
    def save_calibration(self, kind, camera, data, source=None, note=''):
        if kind not in CALIBRATION_KINDS:
            raise ValueError('标定类型只能是 '+'、'.join(CALIBRATION_KINDS))
        if not isinstance(data, dict) or len(json.dumps(data)) > 400000:
            raise ValueError('标定数据无效或过大')
        metric, limit = CALIBRATION_KINDS[kind]
        value = data.get(metric) if metric else None
        item = {'id': uuid.uuid4().hex, 'kind': kind, 'camera': _text(camera, 40), 'created_at': stamp(), 'status': 'candidate',
                'data': data, 'source': source or {}, 'note': _text(note), 'metric': metric, 'metric_value': value,
                'threshold': limit, 'meets_threshold': None if metric is None or value is None else bool(value <= limit),
                'reviews': []}
        self._write(self.root/'calibrations'/(item['id']+'.json'), item)
        self._audit('calibration_saved', calibration=item['id'], kind=kind, camera=item['camera'])
        return item

    def review_calibration(self, item_id, action, reviewer, note=''):
        with self.lock:
            item = self._read('calibrations', item_id)
            if action == 'adopt':
                if reviewer != 'user':
                    raise ValueError('只有用户可以采用标定')
                if item['meets_threshold'] is False:
                    raise ValueError('未达到门槛的标定不能采用')
                for other in self._all('calibrations'):
                    if other['id'] != item_id and other['kind'] == item['kind'] and other['camera'] == item['camera'] and other['status'] == 'adopted':
                        other['status'] = 'retired'
                        other['reviews'].append({'at': stamp(), 'action': 'superseded', 'by': item_id})
                        self._write(self.root/'calibrations'/(other['id']+'.json'), other)
                item['status'] = 'adopted'
            elif action == 'retire':
                item['status'] = 'retired'
            else:
                raise ValueError('审核动作只能是adopt或retire')
            item['reviews'].append({'at': stamp(), 'action': action, 'reviewer': _text(reviewer, 40), 'note': _text(note)})
            self._write(self.root/'calibrations'/(item_id+'.json'), item)
            self._audit('calibration_reviewed', calibration=item_id, action=action)
            return item

    def calibrations(self, summary=True):
        items = self._all('calibrations')
        if summary:
            return [{k: v for k, v in item.items() if k != 'data'} for item in items]
        return items

    def calibration(self, item_id):
        return self._read('calibrations', item_id)
