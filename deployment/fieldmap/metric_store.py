"""Transactional, revision-checked replay sessions, separate from old surveys."""
from __future__ import annotations

from collections import OrderedDict
from contextlib import contextmanager
from dataclasses import asdict
from datetime import datetime, timezone
import copy
import hashlib
import json
import math
from pathlib import Path
import re
import sqlite3
import threading
import uuid

from .metric import GridSpec, Occupancy, exact, identifier, number, plan
from .vacuum_prior import valetudo_v2


MAX_SCANS = 256


class Conflict(ValueError):
    pass


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False, allow_nan=False)


def checked_id(value):
    if not isinstance(value, str) or not re.fullmatch(r'[a-f0-9]{32}', value):
        raise KeyError('mapping record not found')
    return value


def config_for(value, *, mode='sensor_replay'):
    if mode not in ('sensor_replay', 'synthetic_demo'):
        raise ValueError('unsupported session mode')
    exact(value, ('label', 'source', 'epoch', 'frame_id', 'calibration_id', 'grid', 'sensor_in_base'), 'session')
    label = value['label']
    if not isinstance(label, str) or not 1 <= len(label.strip()) <= 80:
        raise ValueError('label: expected 1..80 characters')
    result = {key: identifier(value[key], key) for key in ('source', 'epoch', 'frame_id', 'calibration_id')}
    result.update(label=label.strip(), grid=asdict(GridSpec.parse(value['grid'])), mode=mode)
    ext = value['sensor_in_base']
    exact(ext, ('x_m', 'y_m', 'yaw_rad'), 'sensor_in_base')
    result['sensor_in_base'] = {'x_m': number(ext['x_m'], 'sensor.x_m', -2, 2),
                                'y_m': number(ext['y_m'], 'sensor.y_m', -2, 2),
                                'yaw_rad': number(ext['yaw_rad'], 'sensor.yaw_rad', -math.pi, math.pi)}
    return result


class MappingStore:
    def __init__(self, root):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.path = self.root/'metric.sqlite3'
        self.lock = threading.RLock()
        self.cache = OrderedDict()
        with self.connect() as connection:
            connection.executescript('''
                CREATE TABLE IF NOT EXISTS sessions (
                    id TEXT PRIMARY KEY, config TEXT NOT NULL,
                    revision INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS scans (
                    session_id TEXT NOT NULL REFERENCES sessions(id), sequence INTEGER NOT NULL,
                    packet TEXT NOT NULL, sha256 TEXT NOT NULL, PRIMARY KEY(session_id, sequence));
                CREATE TABLE IF NOT EXISTS priors (
                    id TEXT PRIMARY KEY, session_id TEXT NOT NULL REFERENCES sessions(id),
                    source TEXT NOT NULL, source_sha256 TEXT NOT NULL, converted TEXT NOT NULL);
            ''')

    @contextmanager
    def connect(self):
        connection = sqlite3.connect(self.path, timeout=3.)
        connection.row_factory = sqlite3.Row
        connection.execute('PRAGMA foreign_keys=ON')
        try:
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def create(self, value, *, mode='sensor_replay'):
        config = config_for(value, mode=mode)
        sid = uuid.uuid4().hex
        with self.connect() as connection:
            connection.execute('BEGIN IMMEDIATE')
            if connection.execute('SELECT COUNT(*) FROM sessions').fetchone()[0] >= 100:
                raise ValueError('session budget reached; export before creating more sessions')
            connection.execute('INSERT INTO sessions VALUES (?,?,0,?)',
                               (sid, canonical(config), datetime.now(timezone.utc).isoformat()))
        return self.read(sid)

    def listing(self):
        with self.connect() as connection:
            records = connection.execute('''SELECT s.*, (SELECT COUNT(*) FROM scans WHERE session_id=s.id) frames,
                (SELECT COUNT(*) FROM priors WHERE session_id=s.id) prior_count FROM sessions s ORDER BY created_at DESC''').fetchall()
        return {'items': [{'id': r['id'], 'revision': r['revision'], 'created_at': r['created_at'],
                           'frames': r['frames'], 'prior_count': r['prior_count'],
                           'label': json.loads(r['config'])['label'], 'mode': json.loads(r['config'])['mode']}
                          for r in records], 'motion_enabled': False}

    def _load(self, connection, sid):
        record = connection.execute('SELECT * FROM sessions WHERE id=?', (checked_id(sid),)).fetchone()
        if record is None:
            raise KeyError('mapping session not found')
        key = sid, record['revision']
        if key in self.cache:
            self.cache.move_to_end(key)
            return record, self.cache[key]
        try:
            config = json.loads(record['config'])
            # Revalidate persisted contracts. Damaged state must never become empty.
            config_for({k: v for k, v in config.items() if k != 'mode'}, mode=config['mode'])
            grid = Occupancy(config)
        except (ValueError, KeyError, TypeError, AttributeError) as exc:
            raise OSError('stored mapping configuration is invalid; original database retained') from exc
        for row in connection.execute('SELECT * FROM scans WHERE session_id=? ORDER BY sequence', (sid,)):
            if hashlib.sha256(row['packet'].encode()).hexdigest() != row['sha256']:
                raise OSError('stored scan integrity check failed; original database retained')
            if grid.scan_count >= MAX_SCANS:
                raise OSError('stored replay exceeds frame budget')
            if row['sequence'] != grid.scan_count:
                raise OSError('stored sequence has a gap; original database retained')
            try:
                grid.integrate(json.loads(row['packet']))
            except (ValueError, KeyError, TypeError) as exc:
                raise OSError('stored scan contract is invalid; original database retained') from exc
        self.cache[key] = grid
        while len(self.cache) > 4:
            self.cache.popitem(last=False)
        return record, grid

    def _read(self, sid):
        with self.connect() as connection:
            connection.execute('BEGIN')  # consistent snapshot across metadata and frame reads
            record, grid = self._load(connection, sid)
            priors = [{'id': r['id'], 'format': 'valetudo_v2', 'source_sha256': r['source_sha256'],
                       'registration': 'unregistered', 'navigation_use': False}
                      for r in connection.execute('SELECT id,source_sha256 FROM priors WHERE session_id=? ORDER BY id', (sid,))]
            return record, grid, priors

    def read(self, sid):
        with self.lock:
            record, grid, priors = self._read(sid)
            return {'id': sid, 'revision': record['revision'], 'created_at': record['created_at'],
                    'config': grid.config, 'priors': priors, **grid.export()}

    @staticmethod
    def revision(actual, expected):
        if type(expected) is not int or expected != actual:
            raise Conflict('mapping revision changed; reload before applying this request')

    def append(self, sid, expected_revision, packet):
        with self.lock, self.connect() as connection:
            connection.execute('BEGIN IMMEDIATE')
            record, grid = self._load(connection, sid)
            self.revision(record['revision'], expected_revision)
            if grid.scan_count >= MAX_SCANS:
                raise ValueError('scan budget reached; preserve this session and start a separate session')
            candidate = copy.deepcopy(grid)
            evidence = candidate.integrate(packet)
            raw = canonical(packet)
            connection.execute('INSERT INTO scans VALUES (?,?,?,?)',
                               (sid, packet['sequence'], raw, hashlib.sha256(raw.encode()).hexdigest()))
            connection.execute('UPDATE sessions SET revision=revision+1 WHERE id=?', (sid,))
            connection.commit()
            self.cache[(sid, expected_revision+1)] = candidate
            while len(self.cache) > 4:
                self.cache.popitem(last=False)
            return {'id': sid, 'revision': expected_revision+1, 'scan_count': candidate.scan_count,
                    'update': evidence, 'motion_enabled': False}

    def preview(self, sid, expected_revision, goal=None, radius=.6):
        with self.lock:
            record, grid, _ = self._read(sid)
            self.revision(record['revision'], expected_revision)
            return {'id': sid, 'revision': record['revision'], **plan(grid, goal=goal, radius=radius)}

    def import_prior(self, sid, expected_revision, value):
        raw = canonical(value)
        if len(raw.encode()) > 500000:
            raise ValueError('prior source exceeds 500 KB budget')
        converted = valetudo_v2(value)
        pid = uuid.uuid4().hex
        digest = hashlib.sha256(raw.encode()).hexdigest()
        with self.lock, self.connect() as connection:
            connection.execute('BEGIN IMMEDIATE')
            record, _ = self._load(connection, sid)
            self.revision(record['revision'], expected_revision)
            if connection.execute('SELECT COUNT(*) FROM priors WHERE session_id=?', (sid,)).fetchone()[0] >= 10:
                raise ValueError('prior budget reached')
            connection.execute('INSERT INTO priors VALUES (?,?,?,?,?)', (pid, sid, raw, digest, canonical(converted)))
            connection.execute('UPDATE sessions SET revision=revision+1 WHERE id=?', (sid,))
        return {'id': pid, 'session_id': sid, 'revision': expected_revision+1, 'source_sha256': digest,
                'registration': 'unregistered', 'navigation_use': False, 'motion_enabled': False}

    def prior(self, sid, pid):
        with self.connect() as connection:
            row = connection.execute('SELECT * FROM priors WHERE id=? AND session_id=?', (checked_id(pid), checked_id(sid))).fetchone()
        if row is None:
            raise KeyError('external prior not found')
        if hashlib.sha256(row['source'].encode()).hexdigest() != row['source_sha256']:
            raise OSError('prior integrity check failed; original source retained')
        # Re-decode the preserved original, rather than trusting cached conversion.
        return {'id': pid, 'source_sha256': row['source_sha256'], **valetudo_v2(json.loads(row['source']))}

    def archive(self, sid):
        with self.lock, self.connect() as connection:
            connection.execute('BEGIN')
            record, grid = self._load(connection, sid)
            scans = [json.loads(r['packet']) for r in connection.execute('SELECT packet FROM scans WHERE session_id=? ORDER BY sequence', (sid,))]
            priors = []
            for row in connection.execute('SELECT source,source_sha256 FROM priors WHERE session_id=? ORDER BY id', (sid,)):
                if hashlib.sha256(row['source'].encode()).hexdigest() != row['source_sha256']:
                    raise OSError('stored prior integrity check failed; original database retained')
                priors.append(json.loads(row['source']))
        return {'schema': 1, 'id': sid, 'revision': record['revision'], 'config': grid.config,
                'scans': scans, 'external_priors': priors, 'motion_enabled': False}

    def pgm(self, sid):
        data = self.read(sid)
        width, height = data['grid']['width'], data['grid']['height']
        pixels = bytearray()
        for y in range(height-1, -1, -1):
            pixels.extend(254 if n == 0 else 0 if n == 100 else 205 for n in data['cells'][y*width:(y+1)*width])
        return f'P5\n# replay only; no navigation clearance\n{width} {height}\n255\n'.encode()+pixels
