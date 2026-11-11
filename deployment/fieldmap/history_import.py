"""Adapters for existing private receipts. No hardware access or source writes."""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import re
import zipfile

MAX_JSON = 24 * 1024 * 1024
MAX_IMAGE = 20 * 1024 * 1024
MAX_ARCHIVE = 256 * 1024 * 1024
MAX_MEMBERS = 10000
RUN_ID = re.compile(r'^\d{8}-\d{6}$')
IMAGE = re.compile(r'^(.+)-cam(0|1|2|laptop)\.(jpg|jpeg|png)$', re.I)


def key(value):
    return hashlib.sha256(value.encode('utf-8')).hexdigest()[:32]


def timestamp(value):
    if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value):
        return round(value * 1000) if 0 < value < 10**11 else None
    if isinstance(value, str):
        try:
            date = datetime.fromisoformat(value.replace('Z', '+00:00'))
            if date.tzinfo is not None:
                return round(date.timestamp() * 1000)
        except ValueError:
            pass
    return None


def json_bytes(raw):
    if len(raw) > MAX_JSON:
        raise ValueError('JSON exceeds import byte budget')
    return json.loads(raw.decode('utf-8-sig'))


def read_json(path):
    if path.stat().st_size > MAX_JSON:
        raise ValueError('JSON exceeds import byte budget')
    return json_bytes(path.read_bytes())


def read_steps(path):
    if path.stat().st_size > MAX_JSON:
        raise ValueError('Step log exceeds byte budget')
    return lines(path.read_bytes())


def zip_json(bundle, name):
    info = bundle.getinfo(name)
    if info.file_size > MAX_JSON:
        raise ValueError('ZIP JSON exceeds import byte budget')
    return json_bytes(bundle.read(info))


def safe_member(name):
    parts = name.replace('\\', '/').split('/')
    return bool(name) and not name.startswith(('/', '\\')) and all(p not in ('', '.', '..') and ':' not in p for p in parts)


def finite(value, default=None):
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) else default


def wheels(rows):
    if not isinstance(rows, dict):
        return None
    result = {}
    for motor in ('9', '10'):
        value = rows.get(motor, rows.get(int(motor)))
        if isinstance(value, dict):
            value = value.get('position')
        value = finite(value)
        if value is None:
            return None
        result[motor] = round(value)
    return result


def event(row, ordinal, run_key, fallback_ms):
    if not isinstance(row, dict):
        raise ValueError('Invalid event object')
    body = row.get('base_full') or row.get('rotation') or row.get('evidence') or row
    if not isinstance(body, dict):
        body = {}
    op = row.get('op') or ('travel' if 'base' in row or 'base_error' in row else 'arm')
    moving = op in ('travel', 'rotate', 'advance', 'drive', 'base')
    at = timestamp(row.get('at')) or fallback_ms
    if at is None:
        raise ValueError('Event has no timezone-aware timestamp')
    duration = max(0., finite(body.get('duration_s'), 0.))
    elapsed = max(0., finite(row.get('elapsed_ms'), 0.))
    trace = body.get('stop_trace') or body.get('readbacks') or []
    stop_ms = max((finite(t.get('after_zero_ms'), 0.) for t in trace if isinstance(t, dict)), default=0.)
    start = round(at - (elapsed or (duration * 1000 + stop_ms))) if moving else at
    drive_start = max(start, round(at - duration * 1000 - stop_ms))
    delta = wheels(body.get('wheel_delta_raw'))
    last = wheels(body.get('stop_readback')) or (wheels(trace[-1].get('wheels')) if trace and isinstance(trace[-1], dict) else None)
    first = wheels(body.get('wheel_start_raw'))
    command = wheels(body.get('command_raw'))
    # Only short, speed-bounded pulses justify a local half-turn unwrap.
    if delta is None and first and last and moving and 0 < duration <= .7 and command and max(abs(v) for v in command.values()) <= 160:
        delta = {i: (last[i] - first[i] + 2048) % 4096 - 2048 for i in first}
    if first is None and last and delta:
        first = {i: (last[i] - delta[i]) % 4096 for i in delta}
    if last is None and first and delta:
        last = {i: (first[i] + delta[i]) % 4096 for i in delta}
    if delta and any(abs(v) > 200000 for v in delta.values()):
        delta = None
    failed = bool(row.get('base_error') or row.get('status') == 'failed')
    stopped = body.get('stopped_by')
    if moving and stopped not in (None, 'target', 'duration'):
        failed = True
    count_trace = []
    commands = wheels(body.get('command_raw'))
    for t in body.get('travel_trace') or []:
        if not isinstance(t, dict):
            continue
        counts = wheels(t.get('counts'))
        seconds = finite(t.get('t'))
        if counts and commands and seconds is not None and 0 <= seconds <= duration + 2:
            count_trace.append({'ms': round(drive_start + seconds * 1000),
                                'delta': {i: counts[i] * (1 if commands[i] > 0 else -1) for i in counts}})
    # A bounded subset for the UI; keep both ends and the actual stop delta.
    if len(count_trace) > 500:
        count_trace = count_trace[::math.ceil(len(count_trace) / 500)] + count_trace[-1:]
    count_trace = sorted((p for p in count_trace if drive_start <= p['ms'] <= at), key=lambda p: p['ms'])
    sign = body.get('command_sign')
    sign = sign if type(sign) is int and sign in (-1, 1) else None
    distance = finite(body.get('wheel_distance_m_estimate'))
    radius = finite(body.get('radius_m_used'), .0635)
    if not .02 <= radius <= .2:
        radius = .0635
    reason = str(row.get('base_error') or row.get('message') or (row.get('contact') or {}).get('reason') or '')[:500]
    return {'id': key(f'{run_key}:{ordinal}:{op}'), 'ordinal': ordinal, 'op': op,
            'start_ms': start, 'end_ms': at, 'at_ms': at, 'drive_start_ms': drive_start,
            'time_basis': 'completion_and_elapsed_estimate' if moving else 'receipt_timestamp',
            'status': 'failed' if failed else 'done' if row.get('status') != 'running' else 'running',
            'label': str(row.get('label') or op)[:160], 'reason': reason, 'moving': moving,
            'delta': delta, 'wheel_start': first, 'wheel_end': last, 'trace': count_trace,
            'duration_s': duration, 'distance_m_estimate': distance,
            'requested_m': finite(body.get('requested_distance_m')),
            'radius_m': radius, 'track_m': .25, 'geometry_source': 'recorded_radius_and_design_track',
            'forward_sign': sign, 'direction_source': body.get('direction_source', 'historical_convention_unverified'),
            'stopped_by': stopped, 'read_glitches': len(body.get('read_glitches') or [])}


def photo(run_key, camera, at, basis, phase, path, member=None, priority=40):
    return {'id': key(f'{run_key}:photo:{member or path.name}'), 'camera': str(camera),
            'at_ms': at, 'time_basis': basis, 'phase': str(phase)[:120],
            'container': str(path.resolve()), 'member': member, 'priority': priority}


def _agent(result, rid, path, folder=None):
    run_key = 'agent:' + rid
    started = timestamp(result.get('started_at'))
    ended = timestamp(result.get('finished_at')) or started
    events, photos = [], []
    rows = result.get('results') or []
    if len(rows) > 10000:
        raise ValueError('Event budget exceeded')
    for n, row in enumerate(rows):
        events.append(event(row, row.get('step', n), run_key, ended))
        for shot in row.get('shots') or []:
            name = shot.get('file')
            if not isinstance(name, str) or not safe_member(name):
                continue
            at = timestamp(shot.get('captured_at')) or timestamp(row.get('capture_started_at')) or timestamp(row.get('at'))
            if at:
                photos.append(photo(run_key, shot.get('camera', 'unknown'), at,
                                    'frame_capture' if shot.get('captured_at') else 'capture_group_start_estimate',
                                    name, folder / name if folder else path, None if folder else name))
    return {'id': key(run_key), 'run_key': run_key, 'title': str(result.get('label') or rid)[:200],
            'external_id': rid, 'kind': 'agent', 'start_ms': started, 'end_ms': ended,
            'status': 'failed' if result.get('error') else 'done', 'events': events, 'photos': photos}


def parse_native_agent(path):
    rid = path.parent.name
    if not RUN_ID.fullmatch(rid):
        raise ValueError('Invalid native run ID')
    return _agent(read_json(path), rid, path, path.parent)


def _servo(session, rows, result):
    if len(rows) > 10000:
        raise ValueError('Event budget exceeded')
    sid = str(session.get('session_id', ''))
    if not re.fullmatch('[a-f0-9]{8,40}', sid):
        raise ValueError('Invalid servo session ID')
    run_key = 'servo:' + sid
    start = timestamp(session.get('started_at'))
    end = timestamp(result.get('finished_at')) or start
    events = [event(r, r.get('n', n), run_key, end) for n, r in enumerate(rows)]
    if events:
        end = max(end or 0, max(e['end_ms'] for e in events))
    return {'id': key(run_key), 'run_key': run_key, 'external_id': sid,
            'title': str(session.get('label') or sid)[:200], 'kind': 'servo',
            'start_ms': start, 'end_ms': end, 'status': 'done' if result.get('ended') == '完成' else 'partial',
            'events': events, 'photos': []}


def lines(raw):
    if len(raw) > MAX_JSON:
        raise ValueError('Step log exceeds import byte budget')
    rows = []
    partial = False
    for line in raw.decode('utf-8-sig').splitlines():
        if not line.strip():
            continue
        try:
            rows.append(json.loads(line))
        except ValueError:
            partial = True
    return rows, partial


def parse_native_servo(path):
    folder = path.parent
    session = read_json(path)
    steps = folder / 'steps.jsonl'
    rows, partial = read_steps(steps) if steps.exists() else ([], False)
    result = read_json(folder / 'result.json') if (folder / 'result.json').exists() else {}
    value = _servo(session, rows, result)
    if partial:
        value['warning'] = 'Incomplete step lines retained for a later refresh'
    return value


def parse_journal(path):
    data=read_json(path);run_key='journal:'+data['id'];events=[];photos=[]
    for n,row in enumerate(data.get('events',[])):
        # Native agent/servo receipts already describe assistant movement. Keep
        # passive observations without counting the same wheel path twice.
        if row.get('op') in ('travel','rotate') and row.get('actor') not in ('manual_web','manual_gamepad'):
            row={**row,'op':'observed_motion'}
        item=event(row,n,run_key,timestamp(data['updated_at']));item['actor']=row.get('actor','observed');events.append(item)
    for row in data.get('photos',[]):
        if not safe_member(row['file']):continue
        p=path.parent/row['file'];at=timestamp(row['captured_at'])
        if at:photos.append(photo(run_key,row['camera'],at,row.get('time_basis','frame_capture'),row.get('phase','spatial memory'),p))
    return {'id':key(run_key),'run_key':run_key,'external_id':data['id'],'title':data.get('title','空间连续记录'),
            'kind':'spatial_journal','start_ms':timestamp(data['started_at']),'end_ms':timestamp(data.get('finished_at') or data['updated_at']),
            'status':data['state'],'events':events,'photos':photos}


def parse_control_zip(path):
    if path.stat().st_size > MAX_ARCHIVE:
        raise ValueError('Archive exceeds byte budget')
    with zipfile.ZipFile(path) as bundle:
        if len(bundle.infolist()) > MAX_MEMBERS:
            raise ValueError('Archive member budget exceeded')
        session = zip_json(bundle, 'session.json')
        if 'steps.jsonl' in bundle.namelist() and bundle.getinfo('steps.jsonl').file_size > MAX_JSON:
            raise ValueError('Step log exceeds byte budget')
        rows, partial = lines(bundle.read('steps.jsonl')) if 'steps.jsonl' in bundle.namelist() else ([], False)
        result = zip_json(bundle, 'result.json') if 'result.json' in bundle.namelist() else {}
        value = _servo(session, rows, result)
    paired = path.with_name(path.name[:-len('-control.zip')] + '.zip')
    if paired.is_file():
        if paired.stat().st_size > MAX_ARCHIVE:
            raise ValueError('Paired archive exceeds byte budget')
        with zipfile.ZipFile(paired) as bundle:
            if len(bundle.infolist()) > MAX_MEMBERS:
                raise ValueError('Archive member budget exceeded')
            log = zip_json(bundle, 'log.json')
            for capture in log.get('captures') or []:
                for frame in capture.get('frames') or []:
                    camera = str(frame.get('camera', 'unknown'))
                    name = str(capture.get('name')) + '-cam' + camera + '.jpg'
                    at = timestamp(frame.get('captured_at'))
                    if at and name in bundle.namelist() and safe_member(name):
                        value['photos'].append(photo(value['run_key'], camera, at, 'frame_capture',
                                                     capture['name'], paired, name, 80))
    if partial:
        value['warning'] = 'Incomplete archive step lines'
    return value


def parse_agent_zip(path):
    # Exporters record the native ID in the adjacent receipt. No guessing from
    # the label or the archive's filesystem time, which causes duplicate runs.
    adjacent = path.with_suffix('.json')
    if not adjacent.exists():
        return None
    receipt = read_json(adjacent)
    rid = str(receipt.get('run_id', ''))
    if not RUN_ID.fullmatch(rid):
        return None
    if path.stat().st_size > MAX_ARCHIVE:
        raise ValueError('Archive exceeds byte budget')
    with zipfile.ZipFile(path) as bundle:
        if len(bundle.infolist()) > MAX_MEMBERS or 'result.json' not in bundle.namelist():
            return None
        result = zip_json(bundle, 'result.json')
        return _agent(result, rid, path)


def related_paths(path, kind):
    related = [path]
    if kind == 'servo':
        related += [path.parent / 'steps.jsonl', path.parent / 'result.json']
    elif kind == 'control_zip':
        related += [path.with_name(path.name[:-len('-control.zip')] + '.zip')]
    elif kind == 'agent_zip':
        related += [path.with_suffix('.json')]
    return [p for p in related if p.exists()]


def fingerprint(path, kind):
    return '|'.join(f'{p.name}:{p.stat().st_mtime_ns}:{p.stat().st_size}' for p in related_paths(path, kind))


def discover(evidence_root, fieldtests_root, budget=6000, *, journal_root=None):
    """Bound discovery to the documented layout, skipping nested backup ZIPs."""
    found = []
    if journal_root and Path(journal_root).is_dir():found += [(p,'spatial_journal',90) for p in Path(journal_root).glob('*/run.json')]
    coverage = {'fieldtest_zip_count': 0, 'paired_photo_archives': 0,
                'ignored_aggregate_archives': 0, 'unlinked_archives': 0}
    if evidence_root and Path(evidence_root).is_dir():
        base = Path(evidence_root)
        found += [(p, 'agent', 100) for p in base.glob('*/agent-runs/*/result.json')]
        found += [(p, 'servo', 100) for p in base.glob('*/servo-sessions/*/session.json')]
    if fieldtests_root and Path(fieldtests_root).is_dir():
        for p in Path(fieldtests_root).glob('*/*/*.zip'):
            coverage['fieldtest_zip_count'] += 1
            if p.name.endswith('-control.zip'):
                found.append((p, 'control_zip', 50))
            elif p.with_name(p.stem+'-control.zip').exists():
                coverage['paired_photo_archives'] += 1
            elif any(s in p.stem for s in ('receipts', 'release', 'evidence', 'docs', 'analysis')):
                coverage['ignored_aggregate_archives'] += 1
            elif p.with_suffix('.json').exists() and not any(s in p.stem for s in ('receipts', 'release', 'evidence', 'docs', 'analysis')):
                found.append((p, 'agent_zip', 30))
            else:
                coverage['unlinked_archives'] += 1
    # Most recent recordings first if a very large installation hits its budget.
    found.sort(key=lambda row: row[0].stat().st_mtime, reverse=True)
    return found[:budget], len(found) > budget, coverage


PARSERS = {'agent': parse_native_agent, 'servo': parse_native_servo,
           'control_zip': parse_control_zip, 'agent_zip': parse_agent_zip,'spatial_journal':parse_journal}
