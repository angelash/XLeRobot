"""Local survey data and reviews, hosted by the unified Studio application."""
from __future__ import annotations

from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import threading
import time
from urllib.error import URLError
from urllib.request import Request, urlopen

STATIC = Path(__file__).with_name('static')
KINDS = {'blocked', 'candidate', 'unknown'}


def stamp():
    return datetime.now().astimezone().isoformat()


def atomic_json(path, value):
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')
    os.replace(temporary, path)


class Survey:
    def __init__(self, root, upstream='http://127.0.0.1:8765'):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.upstream = upstream.rstrip('/')
        if self.upstream != 'http://127.0.0.1:8765':
            raise ValueError('Only the existing local controller is supported')
        self.lock = threading.RLock()
        self.status_lock = threading.Lock()
        self.cached_status = None
        self.status_at = 0.

    def snapshot_paths(self):
        return {'current': self.root/'map.json', **{
            hashlib.sha256(p.name.encode()).hexdigest()[:32]: p
            for p in sorted((self.root/'history').glob('*.json'))[:500]}}

    def snapshots(self):
        items, unreadable = [], 0
        for sid, path in self.snapshot_paths().items():
            try:
                data = self.read_map(path)
                if not data or 'features' not in data:
                    continue
                items.append({'id': sid, 'map_id': data['id'], 'title': data.get('title', data['id']),
                              'at': data.get('updated_at') or data.get('created_at'), 'readonly': sid != 'current'})
            except (OSError, ValueError, KeyError):
                unreadable += 1
        return {'items': items, 'unreadable': unreadable}

    def read_map(self, path):
        if not path.resolve().is_relative_to(self.root) or path.stat().st_size > 12 * 1024 * 1024:
            raise ValueError('Map outside data root or exceeds budget')
        return json.loads(path.read_text(encoding='utf-8-sig'))

    def preserve(self):
        """Content-addressed checkpoint; never replace or delete an older map."""
        with self.lock:
            path=self.root/'map.json'
            if not path.exists():return None
            raw=path.read_bytes();self.read_map(path)
            folder=self.root/'history';folder.mkdir(exist_ok=True)
            digest=hashlib.sha256(raw).hexdigest();target=folder/('retained-'+digest+'.json')
            if target.exists():
                if target.read_bytes()!=raw:raise ValueError('地图快照哈希冲突')
            else:
                with target.open('xb') as f:f.write(raw)
            return {'sha256':digest,'snapshot':hashlib.sha256(target.name.encode()).hexdigest()[:32]}

    def save(self,data,expected_sha256):
        with self.lock:
            path=self.root/'map.json';old=path.read_bytes() if path.exists() else b''
            if hashlib.sha256(old).hexdigest()!=expected_sha256:raise ValueError('地图已更新，请按新版本合并；旧图保留')
            if not isinstance(data,dict) or not isinstance(data.get('id'),str) or not isinstance(data.get('observations'),list) or not isinstance(data.get('features'),list):raise ValueError('无效地图')
            if len(json.dumps(data).encode())>12*1024*1024:raise ValueError('地图超过容量')
            self.preserve();atomic_json(path,data);self.preserve();return self.load()

    def load(self, snapshot='current'):
        paths = self.snapshot_paths()
        if snapshot not in paths:
            raise KeyError('Unknown map snapshot')
        path = paths[snapshot]
        data = self.read_map(path) if path.exists() else {
            'schema': 1, 'id': 'awaiting-survey', 'title': '等待环境观察',
            'observations': [], 'features': [], 'scan': {'state': 'waiting'},
        }
        # Review never changes the original observation or unlocks a motion path.
        data['motion_enabled'] = False
        data['coordinate_system'] = 'schematic_relative_bearing_not_metric'
        data['snapshot_id'] = snapshot
        data['readonly'] = snapshot != 'current'
        data['review'] = self.review_data(data['id'])
        return data

    def review_data(self, survey_id):
        path = self.root/'reviews.json'
        data = json.loads(path.read_text(encoding='utf-8')) if path.exists() else {}
        return data.get(survey_id, {'revision': 0, 'items': {}, 'history': []})

    def review(self, payload):
        with self.lock:
            survey = self.load()
            if payload.get('survey_id') != survey['id']:
                raise ValueError('地图已更新，请刷新后重新标注')
            current = survey['review']
            if type(payload.get('revision')) is not int or payload['revision'] != current['revision']:
                raise ValueError('标注已被更新，请刷新后重试')
            key = payload.get('feature_id')
            if key not in {f['id'] for f in survey['features']}:
                raise ValueError('标注对象不存在')
            kind = payload.get('kind')
            if kind not in KINDS:
                raise ValueError('区域分类无效')
            note = payload.get('note', '')
            if not isinstance(note, str) or len(note) > 1000:
                raise ValueError('备注需不超过1000字')
            if type(payload.get('confirmed')) is not bool:
                raise ValueError('需要明确是否确认')
            item = {'feature_id': key, 'kind': kind, 'note': note,
                    'confirmed': payload['confirmed'], 'at': stamp()}
            mobility=payload.get('mobility')
            if mobility is not None:
                if mobility not in ('fixed','movable','unknown','dynamic'):raise ValueError('物体可移动性分类无效')
                item['mobility']=mobility
            current['revision'] += 1
            current['items'][key] = item
            current['history'].append(item)
            path = self.root/'reviews.json'
            all_reviews = json.loads(path.read_text(encoding='utf-8')) if path.exists() else {}
            all_reviews[survey['id']] = current
            atomic_json(path, all_reviews)
            return current

    def controller(self, path, body=None):
        request = Request(self.upstream+path, data=json.dumps(body).encode() if body is not None else None,
                          headers={'Content-Type': 'application/json'})
        with urlopen(request, timeout=2.5) as response:
            return json.load(response)

    def status(self):
        with self.status_lock:
            if self.cached_status is not None and time.monotonic()-self.status_at < 2:
                return self.cached_status
            try:
                state = self.controller('/api/status')
                motors = [m for p in state.get('ports', []) for m in p['motors']]
                wheels = [m for p in state.get('ports', []) if p['port'] == 'COM4'
                          for m in p['motors'] if m['id'] in (9, 10)]
                value = {'online': True, 'at': stamp(), 'sample_at': state.get('updated_at'),
                         'motor_count': len(motors), 'gamepad': bool(state.get('gamepad', {}).get('connected')),
                         'controller': state.get('controller', {}).get('controller'),
                         'agent_running': bool(state.get('agent_run', {}).get('running')),
                         'wheel_drive_disabled': len(wheels)==2 and all(m.get('torque')==0 and m.get('goal_velocity')==0 for m in wheels),
                         'wheels_stopped': len(wheels) == 2 and all(
                             m.get('goal_velocity') == 0 and (m.get('speed', 65535) & 0x7fff) <= 3 for m in wheels),
                         'voltage': [min(m['voltage'] for m in motors), max(m['voltage'] for m in motors)] if motors else None,
                         'temperature_max': max((m['temperature'] for m in motors), default=None),
                         'error': state.get('error')}
            except (OSError, URLError, ValueError, KeyError) as exc:
                value = {'online': False, 'at': stamp(), 'error': str(exc),
                         'wheels_stopped': None, 'motor_count': 0, 'gamepad': False}
            self.cached_status = value
            self.status_at = time.monotonic()
            return value

    def image(self, name, snapshot='current'):
        # Even an existing private file is unavailable unless the survey names it.
        if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]*\.(jpg|jpeg|png)', name):
            raise FileNotFoundError(name)
        names = {o['image'] for o in self.load(snapshot)['observations']}
        if name not in names:
            raise FileNotFoundError(name)
        path = (self.root/'images'/name).resolve()
        if path.parent != (self.root/'images').resolve():
            raise FileNotFoundError(name)
        return path
