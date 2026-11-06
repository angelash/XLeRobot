"""Incremental private history index and bounded, read-only playback queries."""
from __future__ import annotations

from contextlib import contextmanager
from io import BytesIO
import json
import math
from pathlib import Path
import sqlite3
import threading
import time
import zipfile

from . import history_import as adapter

SCHEMA = '''
CREATE TABLE IF NOT EXISTS sources(id TEXT PRIMARY KEY,path TEXT,kind TEXT,stamp TEXT,state TEXT,error TEXT,run_id TEXT);
CREATE TABLE IF NOT EXISTS runs(id TEXT PRIMARY KEY,run_key TEXT,external_id TEXT,title TEXT,kind TEXT,start_ms INTEGER,end_ms INTEGER,status TEXT,priority INTEGER);
CREATE TABLE IF NOT EXISTS events(id TEXT PRIMARY KEY,run_id TEXT,ordinal INTEGER,start_ms INTEGER,end_ms INTEGER,op TEXT,status TEXT,body TEXT);
CREATE INDEX IF NOT EXISTS event_time ON events(end_ms,start_ms);
CREATE INDEX IF NOT EXISTS event_run ON events(run_id,ordinal);
CREATE TABLE IF NOT EXISTS photos(id TEXT PRIMARY KEY,run_id TEXT,camera TEXT,at_ms INTEGER,time_basis TEXT,phase TEXT,container TEXT,member TEXT,stamp TEXT,priority INTEGER);
CREATE INDEX IF NOT EXISTS photo_time ON photos(at_ms,camera);
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY,value TEXT);
'''


class History:
    def __init__(self, root, evidence_root=None, fieldtests_root=None, forward_sign=None):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.database = self.root / 'history.sqlite3'
        self.roots = [Path(p).resolve() for p in (evidence_root, fieldtests_root) if p]
        self.evidence_root, self.fieldtests_root = evidence_root, fieldtests_root
        self.forward_sign = forward_sign if forward_sign in (-1, 1) else None
        self.lock = threading.Lock()
        self.state_lock = threading.Lock()
        self.thumbnail_lock = threading.Lock()
        self.wake, self.closed = threading.Event(), threading.Event()
        self.thread = None
        self.running = False
        with self.connect() as db:
            db.executescript(SCHEMA)

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.database, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            db.execute('PRAGMA journal_mode=WAL')
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def safe_path(self, path):
        target = Path(path).resolve()
        if not any(target.is_relative_to(root) for root in self.roots):
            raise ValueError('Source escapes configured history roots')
        return target

    def start(self):
        if self.thread and self.thread.is_alive():
            return
        self.closed.clear()
        self.thread = threading.Thread(target=self._worker, name='field-history-index', daemon=True)
        self.thread.start()

    def stop(self):
        self.closed.set()
        self.wake.set()
        if self.thread:
            self.thread.join(timeout=2)

    def refresh(self):
        self.start()
        self.wake.set()
        return {'scheduled': True, 'motion_enabled': False}

    def _worker(self):
        while not self.closed.is_set():
            self.wake.clear()
            try:
                self.scan()
            except Exception as exc:
                # A malformed/temporarily locked source must not kill refresh.
                self.last_error = type(exc).__name__ + ': index scan will retry'
            self.wake.wait(30)

    def scan(self):
        if not self.lock.acquire(blocking=False):
            return {'already_running': True}
        with self.state_lock:
            self.running = True
        counts = {'changed': 0, 'unchanged': 0, 'skipped': 0, 'errors': 0}
        began = time.monotonic()
        try:
            sources, truncated, coverage = adapter.discover(self.evidence_root, self.fieldtests_root)
            for path, kind, priority in sources:
                if self.closed.is_set():
                    truncated = True
                    break
                source_id = adapter.key(str(path.resolve()))
                try:
                    for related in adapter.related_paths(path, kind):
                        self.safe_path(related)
                    stamp = 'v2|' + adapter.fingerprint(path, kind)
                    with self.connect() as db:
                        old = db.execute('SELECT stamp,state FROM sources WHERE id=?', (source_id,)).fetchone()
                    if old and old['stamp'] == stamp and old['state'] in ('indexed', 'skipped'):
                        counts['unchanged'] += 1
                        continue
                    value = adapter.PARSERS[kind](path)
                    if 'v2|' + adapter.fingerprint(path, kind) != stamp:
                        raise ValueError('Source changed during read; retry on next refresh')
                    with self.connect() as db:
                        if value:
                            self._save(db, value, priority, stamp)
                            counts['changed'] += 1
                        else:
                            counts['skipped'] += 1
                        db.execute('INSERT OR REPLACE INTO sources VALUES(?,?,?,?,?,?,?)',
                                   (source_id, str(path.resolve()), kind, stamp,
                                    'indexed' if value else 'skipped', value.get('warning') if value else 'Unlinked export; original retained',
                                    value['id'] if value else None))
                except (OSError, ValueError, KeyError, TypeError, AttributeError, OverflowError, RuntimeError, zipfile.BadZipFile) as exc:
                    counts['errors'] += 1
                    with self.connect() as db:
                        db.execute('INSERT OR REPLACE INTO sources VALUES(?,?,?,?,?,?,?)',
                                   (source_id, str(path.resolve()), kind, '', 'error', type(exc).__name__ + ': source could not be indexed', None))
                # Yield between files on a weak laptop. No sleeps or locks on HTTP.
                if counts['changed'] % 20 == 0:
                    time.sleep(.003)
            result = {**counts, **coverage, 'at_ms': round(time.time() * 1000), 'discovered': len(sources),
                      'discovery_truncated': truncated, 'elapsed_s': round(time.monotonic() - began, 3)}
            with self.connect() as db:
                db.execute('INSERT OR REPLACE INTO meta VALUES(?,?)', ('scan', json.dumps(result)))
            self.last_error = None
            return result
        finally:
            with self.state_lock:
                self.running = False
            self.lock.release()

    def _save(self, db, value, priority, stamp):
        rid = value['id']
        old = db.execute('SELECT priority FROM runs WHERE id=?', (rid,)).fetchone()
        if not old or priority >= old['priority']:
            if value['start_ms'] is None:
                value['start_ms'] = min((e['start_ms'] for e in value['events']), default=value['end_ms'])
            if value['end_ms'] is None:
                value['end_ms'] = max((e['end_ms'] for e in value['events']), default=value['start_ms'])
            if value['start_ms'] is None:
                raise ValueError('Recording has no usable time')
            db.execute('INSERT OR REPLACE INTO runs VALUES(?,?,?,?,?,?,?,?,?)',
                       tuple(value[k] for k in ('id', 'run_key', 'external_id', 'title', 'kind', 'start_ms', 'end_ms', 'status')) + (priority,))
            db.execute('DELETE FROM events WHERE run_id=?', (rid,))
            for e in value['events']:
                e['run_id'] = rid
                db.execute('INSERT OR REPLACE INTO events VALUES(?,?,?,?,?,?,?,?)',
                           (e['id'], rid, e['ordinal'], e['start_ms'], e['end_ms'], e['op'], e['status'], json.dumps(e)))
        for p in value['photos']:
            self.safe_path(p['container'])
            if p['member'] and not adapter.safe_member(p['member']):
                continue
            if not p['member'] and not Path(p['container']).is_file():
                continue
            old_photo = db.execute('SELECT priority FROM photos WHERE id=?', (p['id'],)).fetchone()
            if old_photo and old_photo['priority'] > p['priority']:
                continue
            db.execute('INSERT OR REPLACE INTO photos VALUES(?,?,?,?,?,?,?,?,?,?)',
                       (p['id'], rid, p['camera'], p['at_ms'], p['time_basis'], p['phase'], p['container'], p['member'], stamp, p['priority']))

    def status(self):
        with self.connect() as db:
            counts = {table: db.execute(f'SELECT count(*) FROM {table}').fetchone()[0] for table in ('runs', 'events', 'photos')}
            extent = db.execute('SELECT min(start_ms),max(end_ms) FROM runs').fetchone()
            scan = db.execute("SELECT value FROM meta WHERE key='scan'").fetchone()
            errors = db.execute("SELECT kind,state,count(*) n FROM sources WHERE state!='indexed' OR error IS NOT NULL GROUP BY kind,state").fetchall()
        scan_data = json.loads(scan[0]) if scan else None
        issues = [dict(r) for r in errors]
        if scan_data and scan_data.get('unlinked_archives'):
            issues.append({'kind': 'fieldtests_archive', 'state': 'unlinked', 'n': scan_data['unlinked_archives']})
        return {**counts, 'start_ms': extent[0], 'end_ms': extent[1], 'indexing': self.running,
                'scan': scan_data,
                'source_roots': {'evidence_available': bool(self.evidence_root and Path(self.evidence_root).is_dir()),
                                 'fieldtests_available': bool(self.fieldtests_root and Path(self.fieldtests_root).is_dir())},
                'coverage_issues': issues, 'motion_enabled': False,
                'error': getattr(self, 'last_error', None),
                'pose_source': 'wheel_odometry_estimate', 'geometry': {'radius_m': .0635, 'track_m': .25, 'source': 'published_design_unmeasured'}}

    @staticmethod
    def bounds(start, end):
        if (not isinstance(start, (int, float)) or not isinstance(end, (int, float))
                or abs(start) > 253402300799999 or abs(end) > 253402300799999
                or not math.isfinite(start) or not math.isfinite(end) or start > end):
            raise ValueError('Need a valid start/end time range')
        return round(start), round(end)

    def runs(self, start, end, offset=0, limit=100):
        start, end = self.bounds(start, end)
        if not 0 <= offset or not 1 <= limit <= 200:
            raise ValueError('Invalid recording pagination')
        with self.connect() as db:
            total = db.execute('SELECT count(*) FROM runs WHERE end_ms>=? AND start_ms<=?', (start, end)).fetchone()[0]
            rows = db.execute('SELECT * FROM runs WHERE end_ms>=? AND start_ms<=? ORDER BY start_ms,id LIMIT ? OFFSET ?', (start, end, limit, offset)).fetchall()
        return {'items': [{k: r[k] for k in ('id', 'external_id', 'title', 'kind', 'start_ms', 'end_ms', 'status')} for r in rows],
                'total': total, 'next_offset': offset + len(rows) if offset + len(rows) < total else None}

    def query(self, start, end, run_id=None, event_limit=2000, point_limit=6000):
        start, end = self.bounds(start, end)
        if not 10 <= event_limit <= 4000 or not 100 <= point_limit <= 12000:
            raise ValueError('Invalid playback budget')
        with self.connect() as db:
            # Integrate earlier motion to preserve each episode's origin when
            # the user narrows its time window. Never move the origin on drag.
            records = db.execute('SELECT body FROM events WHERE start_ms<=? ORDER BY start_ms,end_ms,id LIMIT 50001', (end,)).fetchall()
            titles = {r['id']: r['title'] for r in db.execute('SELECT id,title FROM runs')}
        coverage_truncated = len(records) > 50000
        all_events = [json.loads(r['body']) for r in records[:50000]]
        groups = build_tracks(all_events, self.forward_sign)
        selected = [e for e in all_events if e['end_ms'] >= start and (not run_id or e['run_id'] == run_id)]
        total = len(selected)
        visible = selected[:event_limit]
        chosen_ids = {e['id'] for e in visible}
        tracks = []
        budget = point_limit
        for group in groups:
            samples = clip_points([p for p in group['points'] if p['event_id'] in chosen_ids], start, end)
            if not samples:
                continue
            tracks.append({**{k: v for k, v in group.items() if k != 'points'}, 'points': samples})
        npoints = sum(len(t['points']) for t in tracks)
        if npoints > budget:
            stride = math.ceil(npoints / max(1, budget - 2 * len(tracks)))
            for t in tracks:
                t['points'] = t['points'][::stride] + ([t['points'][-1]] if len(t['points']) > 1 else [])
        for e in visible:
            e['title'] = titles.get(e['run_id'], '')
            e.pop('trace', None)
        motion = [e for e in selected if e['moving']]
        return {'start_ms': start, 'end_ms': end, 'events': visible, 'tracks': tracks,
                'total_events': total, 'truncated': total > event_limit or coverage_truncated,
                'points_downsampled': npoints > budget, 'summary': {'motion_events': len(motion),
                    'translations': sum(e['op'] != 'rotate' for e in motion),
                    'rotations': sum(e['op'] == 'rotate' for e in motion),
                    'failed_events': sum(e['status'] == 'failed' for e in selected),
                    'distance_m_estimate': sum(abs(e.get('distance_m_estimate') or 0) for e in motion)},
                'coordinate_system': 'each_episode_x_forward_y_left_m_estimate',
                'motion_enabled': False, 'notice': '轮估会漂移；不同轨迹段各有独立起点，未与示意环境标定对齐。'}

    def photos(self, start, end, camera=None, run_id=None, offset=0, limit=36, at=None):
        start, end = self.bounds(start, end)
        if not 0 <= offset or not 1 <= limit <= 100:
            raise ValueError('Invalid photo pagination')
        terms, args = ['at_ms BETWEEN ? AND ?'], [start, end]
        if camera is not None:
            terms.append('camera=?'); args.append(str(camera))
        if run_id:
            terms.append('run_id=?'); args.append(run_id)
        where = ' AND '.join(terms)
        with self.connect() as db:
            total = db.execute('SELECT count(*) FROM photos WHERE ' + where, args).fetchone()[0]
            if at is None:
                rows = db.execute('SELECT * FROM photos WHERE ' + where + ' ORDER BY at_ms,id LIMIT ? OFFSET ?', [*args, limit, offset]).fetchall()
            else:
                rows = db.execute('SELECT * FROM photos WHERE ' + where + ' ORDER BY abs(at_ms-?),at_ms,id LIMIT ?', [*args, round(at), limit]).fetchall()
        def public(p):
            return {k: p[k] for k in ('id', 'run_id', 'camera', 'at_ms', 'time_basis', 'phase')} | {
                'url': f'/fieldmap/history/photos/{p["id"]}',
                'thumbnail_url': f'/fieldmap/history/photos/{p["id"]}?thumbnail=true',
                **({'offset_ms': p['at_ms'] - round(at)} if at is not None else {})}
        return {'items': [public(p) for p in rows], 'total': total,
                'next_offset': offset + len(rows) if at is None and offset + len(rows) < total else None}

    def image(self, photo_id, thumbnail=False):
        if not isinstance(photo_id, str) or len(photo_id) != 32 or any(c not in 'abcdef0123456789' for c in photo_id):
            raise FileNotFoundError('Unknown photo')
        with self.connect() as db:
            p = db.execute('SELECT * FROM photos WHERE id=?', (photo_id,)).fetchone()
        if not p:
            raise FileNotFoundError('Unknown photo')
        path = self.safe_path(p['container'])
        cache_key = adapter.key(photo_id + p['stamp'])
        cached = self.root / 'thumbnails' / (cache_key + '.jpg')
        if thumbnail and cached.is_file():
            return cached.read_bytes(), 'image/jpeg'
        if p['member']:
            if path.stat().st_size > adapter.MAX_ARCHIVE:
                raise ValueError('Archive exceeds byte budget')
            if not adapter.safe_member(p['member']):
                raise FileNotFoundError('Invalid indexed member')
            with zipfile.ZipFile(path) as bundle:
                info = bundle.getinfo(p['member'])
                if info.file_size > adapter.MAX_IMAGE:
                    raise ValueError('Image exceeds byte budget')
                raw = bundle.read(info)
        else:
            if path.stat().st_size > adapter.MAX_IMAGE:
                raise ValueError('Image exceeds byte budget')
            raw = path.read_bytes()
        from PIL import Image
        with Image.open(BytesIO(raw)) as im:
            if im.format not in ('JPEG', 'PNG') or im.width * im.height > 20_000_000:
                raise ValueError('Invalid indexed image dimensions or format')
            if not thumbnail:
                im.verify()
                return raw, 'image/png' if im.format == 'PNG' else 'image/jpeg'
            im.thumbnail((320, 240))
            output = BytesIO(); im.convert('RGB').save(output, format='JPEG', quality=78)
        data = output.getvalue()
        with self.thumbnail_lock:
            cached.parent.mkdir(exist_ok=True)
            temporary = cached.with_suffix('.tmp'); temporary.write_bytes(data); temporary.replace(cached)
        return data, 'image/jpeg'


def clip_points(points, start, end):
    """Clip per-event samples, interpolating window endpoints without bridging gaps."""
    result = []
    for i, p in enumerate(points):
        if i:
            a = points[i - 1]
            if a['event_id'] == p['event_id'] and a['ms'] < p['ms']:
                for boundary in (start, end):
                    if a['ms'] < boundary < p['ms']:
                        f = (boundary - a['ms']) / (p['ms'] - a['ms'])
                        result.append({**p, 'ms': boundary, **{k: a[k] + (p[k] - a[k]) * f for k in ('x_m', 'y_m', 'yaw_rad')}, 'interpolated': True})
        if start <= p['ms'] <= end:
            result.append(p)
    return sorted(result, key=lambda p: p['ms'])


def build_tracks(events, configured_sign):
    groups, current, previous = [], None, None
    x = y = yaw = 0.
    for e in events:
        if not e['moving']:
            continue
        delta, first, last = e.get('delta'), e.get('wheel_start'), e.get('wheel_end')
        sign = e.get('forward_sign') or configured_sign
        if not delta or not first or not last or sign not in (-1, 1):
            current = previous = None
            e['pose_unavailable_reason'] = '缺少可确认的双轮位移或历史方向约定'
            continue
        continuity = previous and 0 <= e['start_ms'] - previous['end_ms'] <= 600000 and all(
            abs((first[i] - previous['wheel_end'][i] + 2048) % 4096 - 2048) <= 4 for i in ('9', '10'))
        signature = (sign, e['radius_m'], e['track_m'])
        if not current or not continuity or current['signature'] != signature:
            x = y = yaw = 0.
            current = {'id': adapter.key('episode:' + e['id']), 'start_ms': e['start_ms'],
                       'end_ms': e['end_ms'], 'points': [], 'signature': signature,
                       'geometry_source': e['geometry_source'],
                       'direction_source': e['direction_source'] if e.get('forward_sign') else 'current_configuration_assumed_for_history',
                       'origin': 'local_episode_start_not_room_position'}
            groups.append(current)
        e['episode_id'] = current['id']
        e['pose_start'] = {'x_m': x, 'y_m': y, 'yaw_rad': yaw}
        samples = [{'ms': e['drive_start_ms'], 'delta': {'9': 0, '10': 0}},
                   *(e.get('trace') or []), {'ms': e['end_ms'], 'delta': delta}]
        last_delta = {'9': 0, '10': 0}
        for sample in samples:
            differences = {i: sample['delta'][i] - last_delta[i] for i in last_delta}
            meters_per_count = 2 * math.pi * e['radius_m'] / 4096
            ds = sign * (differences['10'] - differences['9']) / 2 * meters_per_count
            dyaw = (differences['9'] + differences['10']) * meters_per_count / e['track_m']
            x += ds * math.cos(yaw + dyaw / 2)
            y += ds * math.sin(yaw + dyaw / 2)
            yaw += dyaw
            current['points'].append({'ms': sample['ms'], 'x_m': x, 'y_m': y, 'yaw_rad': yaw,
                                      'event_id': e['id'], 'status': e['status']})
            last_delta = sample['delta']
        e['pose_end'] = {'x_m': x, 'y_m': y, 'yaw_rad': yaw}
        if e.get('distance_m_estimate') is None and e['op'] != 'rotate':
            e['distance_m_estimate'] = sign * (delta['10'] - delta['9']) / 2 * 2 * math.pi * e['radius_m'] / 4096
        current['end_ms'] = e['end_ms']
        previous = e
    for group in groups:
        group.pop('signature', None)
    return groups
