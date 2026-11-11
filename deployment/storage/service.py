"""Bounded metadata inventory and independent free-space monitoring; never deletes files."""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import ctypes
import json
import os
from pathlib import Path
import shutil
import socket
import stat
import subprocess
import threading
import time

GIB = 1024 ** 3
DEFAULT_THRESHOLDS = {'warning_free_gib': 20, 'warning_free_percent': 15,
                      'critical_free_gib': 5, 'critical_free_percent': 5}
RANK = {'ok': 0, 'unknown': 1, 'warning': 2, 'critical': 3}
IMAGE = {'.jpg', '.jpeg', '.png', '.webp', '.bmp', '.gif', '.heic', '.tif', '.tiff'}
VIDEO = {'.mp4', '.mkv', '.avi', '.mov', '.webm', '.h264', '.h265'}
DOCUMENT = {'.md', '.pdf', '.doc', '.docx', '.ppt', '.pptx', '.xls', '.xlsx', '.html'}
SENSOR = {'.npy', '.npz', '.ply', '.pcd', '.depth'}


def stamp():
    return datetime.now(timezone.utc).isoformat()


def absolute(path):
    # Do not resolve/follow junctions when deciding inventory boundaries.
    return Path(os.path.abspath(path))


def linked(info):
    return stat.S_ISLNK(info.st_mode) or bool(getattr(info, 'st_file_attributes', 0) & 0x400)


def disjoint_roots(paths):
    result = []
    for path in sorted({absolute(p) for p in paths}, key=lambda p: (len(p.parts), str(p))):
        if not any(path == root or root in path.parents for root in result):
            result.append(path)
    return result


def kind(path):
    ext = path.suffix.lower()
    if ext in IMAGE:
        return 'images'
    if ext in VIDEO:
        return 'videos'
    if ext in DOCUMENT:
        return 'documents'
    if ext in SENSOR:
        return 'sensor_data'
    if ext in {'.json', '.jsonl', '.log', '.csv', '.db', '.sqlite', '.sqlite3', '.txt'}:
        return 'records'
    return 'other'


def purpose(path, root):
    parts = {p.lower() for p in path.relative_to(root).parts[:-1]}
    if any(p.startswith('.venv') or p in {'node_modules', 'site-packages'} for p in parts):
        return 'environments'
    if '.local-deploy' in parts or root.name.lower() == 'robottransfer' or 'phone-vision-car-kit-20261109-v2' in parts:
        return 'deployment'
    if parts & {'fieldtests', 'evidence'} or root.name.lower() in {'fieldtests', 'evidence'}:
        return 'evidence'
    if 'fieldmap' in parts or root.name.lower() == 'fieldmap':
        return 'maps'
    if 'platform-data' in parts:
        return 'studio'
    return 'project'


def scan_inventory(roots, *, time_budget=60, max_files=500000, stop=None, clock=time.monotonic):
    """Count apparent file bytes. Live snapshot, no content reads, no reparse traversal."""
    started = clock()
    result = {'started_at': stamp(), 'completed_at': None, 'complete': True, 'limit': None,
              'bytes': 0, 'files': 0, 'images': 0, 'image_bytes': 0,
              'errors': [], 'error_count': 0, 'skipped_links': 0, 'roots': [],
              'kinds': [], 'purposes': [], 'directories': [], 'largest_files': []}
    types, purposes, directories = {}, {}, {}
    largest = []

    def add(bucket, key, size, image):
        row = bucket.setdefault(key, {'id': key, 'bytes': 0, 'files': 0, 'images': 0, 'image_bytes': 0})
        row['bytes'] += size
        row['files'] += 1
        if image:
            row['images'] += 1
            row['image_bytes'] += size

    def error(path, exc):
        result['complete'] = False
        result['error_count'] += 1
        if len(result['errors']) < 12:
            result['errors'].append({'path': str(path), 'error': type(exc).__name__})

    exhausted = False
    for root in disjoint_roots(roots):
        row = {'path': str(root), 'bytes': 0, 'files': 0, 'images': 0, 'image_bytes': 0,
               'state': 'pending'}
        result['roots'].append(row)
        if exhausted:
            continue
        try:
            info = root.lstat()
            if linked(info):
                result['skipped_links'] += 1
                row['state'] = 'link_skipped'
                result['complete'] = False
                continue
            if not stat.S_ISDIR(info.st_mode):
                raise NotADirectoryError(str(root))
        except OSError as exc:
            row['state'] = 'unavailable'
            error(root, exc)
            continue
        row['state'] = 'complete'
        stack = [root]
        before_errors = result['error_count']
        while stack and not exhausted:
            folder = stack.pop()
            try:
                with os.scandir(folder) as entries:
                    for entry in entries:
                        if (stop and stop.is_set()) or clock() - started >= time_budget or result['files'] >= max_files:
                            result['limit'] = 'stopped' if stop and stop.is_set() else 'time' if clock() - started >= time_budget else 'files'
                            result['complete'] = False
                            row['state'] = 'partial'
                            exhausted = True
                            break
                        try:
                            info = entry.stat(follow_symlinks=False)
                            if linked(info):
                                result['skipped_links'] += 1
                                continue
                            if stat.S_ISDIR(info.st_mode):
                                stack.append(Path(entry.path))
                                continue
                            if not stat.S_ISREG(info.st_mode):
                                continue
                            path, size = Path(entry.path), info.st_size
                            category = kind(path)
                            image = category == 'images'
                            for target in (result, row):
                                target['bytes'] += size
                                target['files'] += 1
                                if image:
                                    target['images'] += 1
                                    target['image_bytes'] += size
                            add(types, category, size, image)
                            add(purposes, purpose(path, root), size, image)
                            # Disjoint first-level rows reconcile exactly to each root.
                            relative = path.relative_to(root)
                            group = root / relative.parts[0] if len(relative.parts) > 1 else root / '(根目录文件)'
                            add(directories, str(group), size, image)
                            largest.append({'path': str(path), 'bytes': size})
                            if len(largest) > 80:
                                largest.sort(key=lambda x: x['bytes'], reverse=True)
                                del largest[30:]
                        except OSError as exc:
                            error(entry.path, exc)
            except OSError as exc:
                error(folder, exc)
        if result['error_count'] > before_errors and row['state'] == 'complete':
            row['state'] = 'partial'
    result.update(completed_at=stamp(), duration_s=round(clock() - started, 3),
                  kinds=sorted(types.values(), key=lambda x: x['bytes'], reverse=True),
                  purposes=sorted(purposes.values(), key=lambda x: x['bytes'], reverse=True),
                  directories=sorted(directories.values(), key=lambda x: x['bytes'], reverse=True)[:40],
                  largest_files=sorted(largest, key=lambda x: x['bytes'], reverse=True)[:20])
    return result


def volume_paths():
    if os.name != 'nt':
        return [Path('/')]
    kernel = ctypes.windll.kernel32
    kernel.GetDriveTypeW.argtypes = [ctypes.c_wchar_p]
    kernel.GetDriveTypeW.restype = ctypes.c_uint
    mask = kernel.GetLogicalDrives()
    return [Path(f'{chr(65 + i)}:\\') for i in range(26)
            if mask & (1 << i) and kernel.GetDriveTypeW(f'{chr(65 + i)}:\\') == 3]


def disk_environment():
    if os.name != 'nt':
        return {'checked_at': stamp(), 'disks': [], 'error': '仅Windows提供物理磁盘信息'}
    # Fixed command, no user strings or filenames interpolated into PowerShell.
    script = """$ErrorActionPreference='Stop';[Console]::OutputEncoding=[Text.Encoding]::UTF8;
$items=@(Get-Disk | Where-Object {$_.Size -gt 0} | ForEach-Object {
 $disk=$_;$letters=@(Get-Partition -DiskNumber $disk.Number | Where-Object {$_.DriveLetter} | ForEach-Object {[string]$_.DriveLetter+':'})
 [pscustomobject]@{number=$disk.Number;model=$disk.FriendlyName;bus=$disk.BusType.ToString();size_bytes=$disk.Size;health=$disk.HealthStatus.ToString();volumes=$letters}
});ConvertTo-Json -InputObject $items -Depth 4 -Compress"""
    try:
        result = subprocess.run(['powershell.exe', '-NoProfile', '-NonInteractive', '-Command', script],
                                capture_output=True, timeout=15, check=True,
                                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        rows = json.loads(result.stdout.decode('utf-8-sig'))
        if not isinstance(rows, list):
            raise ValueError('Invalid disk metadata')
        return {'checked_at': stamp(), 'disks': rows, 'error': None}
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        return {'checked_at': stamp(), 'disks': [], 'error': type(exc).__name__}


def capacity_level(total, free, thresholds):
    if total <= 0 or free < 0 or free > total:
        raise ValueError('Invalid disk capacity')
    percent = free / total * 100
    for level in ('critical', 'warning'):
        reasons = []
        if free <= thresholds[f'{level}_free_gib'] * GIB:
            reasons.append('free_bytes')
        if percent <= thresholds[f'{level}_free_percent']:
            reasons.append('free_percent')
        if reasons:
            return level, reasons
    return 'ok', []


class StorageMonitor:
    def __init__(self, repo, data_dir, *, roots=None, volumes=None, usage=shutil.disk_usage,
                 thresholds=None, capacity_interval=60, inventory_interval=900, scan_seconds=60,
                 hardware=disk_environment):
        self.repo, self.data_dir = absolute(repo), Path(data_dir)
        self.thresholds = dict(DEFAULT_THRESHOLDS if thresholds is None else thresholds)
        self.roots = disjoint_roots(roots if roots is not None else [repo])
        self.volumes_provider = volumes or volume_paths
        self.usage = usage
        self.hardware_provider = hardware
        self.hardware = {'checked_at': None, 'disks': [], 'error': None}
        self.capacity_interval, self.inventory_interval = capacity_interval, inventory_interval
        self.scan_seconds = scan_seconds
        self.lock, self.persist_lock = threading.RLock(), threading.Lock()
        self.stop_event, self.scan_event = threading.Event(), threading.Event()
        self.threads = []
        self.sample = {'checked_at': None, 'volumes': [], 'level': 'unknown', 'alerts': []}
        self.inventory, self.scanning, self.last_error = None, False, None
        self.history = []
        self.persistence_error = None
        try:
            cached = json.loads((self.data_dir / 'snapshot.json').read_text(encoding='utf-8'))
            if cached.get('host') == socket.gethostname():
                self.history = cached.get('history', [])[-1440:]
                self.inventory = cached.get('inventory')
                if self.inventory:
                    self.inventory['from_previous_run'] = True
        except (OSError, ValueError, TypeError, AttributeError):
            pass

    def start(self):
        if any(thread.is_alive() for thread in self.threads):
            return
        self.threads = []
        self.stop_event.clear()
        for target in (self._capacity_loop, self._inventory_loop):
            thread = threading.Thread(target=target, daemon=True, name='studio-storage-' + target.__name__)
            self.threads.append(thread)
            thread.start()

    def close(self):
        self.stop_event.set()
        self.scan_event.set()
        for thread in self.threads:
            thread.join(timeout=2)

    def check_capacity(self):
        rows, alerts = [], []
        try:
            paths = list(self.volumes_provider())
        except OSError:
            paths = []
        for path in paths:
            row = {'path': str(path)}
            try:
                total, used, free = self.usage(path)
                level, reasons = capacity_level(total, free, self.thresholds)
                row.update(total_bytes=total, used_bytes=used, free_bytes=free,
                           free_percent=round(free / total * 100, 2), level=level, reasons=reasons)
            except (OSError, ValueError):
                row.update(level='unknown', reasons=['unreadable'])
            rows.append(row)
            if row['level'] != 'ok':
                alerts.append({'volume': row['path'], 'level': row['level'], 'reasons': row['reasons']})
        if not rows:
            alerts.append({'volume': None, 'level': 'unknown', 'reasons': ['no_volumes']})
        value = {'checked_at': stamp(), 'volumes': rows,
                 'level': max((r['level'] for r in rows), key=RANK.get, default='unknown'), 'alerts': alerts}
        with self.lock:
            self.sample = value
            self.history.append({'at': value['checked_at'], 'volumes': [
                {'path': r['path'], 'free_bytes': r.get('free_bytes')} for r in rows]})
            self.history = self.history[-1440:]
        self._persist()
        return value

    def refresh(self):
        # Coalesce requests; there is always only one inventory worker.
        self.scan_event.set()
        return self.status()

    def scan(self):
        with self.lock:
            if self.scanning:
                return
            self.scanning = True
        try:
            hardware = self.hardware_provider()
            value = scan_inventory(self.roots, time_budget=self.scan_seconds, stop=self.stop_event)
            with self.lock:
                self.hardware = hardware
                self.inventory, self.last_error = value, None
        except Exception as exc:
            with self.lock:
                self.last_error = type(exc).__name__
        finally:
            with self.lock:
                self.scanning = False
        self._persist()

    def summary(self):
        with self.lock:
            value = deepcopy(self.sample)
        age = None if not value['checked_at'] else (datetime.now(timezone.utc) - datetime.fromisoformat(value['checked_at'])).total_seconds()
        value['stale'] = age is None or age > self.capacity_interval * 3
        if value['stale']:
            value['level'] = max((value['level'], 'unknown'), key=RANK.get)
        value['host'] = socket.gethostname()
        value['capacity_interval_s'] = self.capacity_interval
        return value

    def status(self):
        with self.lock:
            return {**self.summary(), 'thresholds': dict(self.thresholds),
                    'inventory': deepcopy(self.inventory), 'scanning': self.scanning,
                    'inventory_error': self.last_error, 'persistence_error': self.persistence_error,
                    'inventory_interval_s': self.inventory_interval,
                    'scan_budget_s': self.scan_seconds, 'roots': list(map(str, self.roots)),
                    'hardware': deepcopy(self.hardware),
                    'history': deepcopy(self.history[-120:]), 'read_only': True}

    def _capacity_loop(self):
        while not self.stop_event.is_set():
            try:
                self.check_capacity()
            except Exception as exc:
                with self.lock:
                    self.sample = {**self.sample, 'level': 'unknown', 'alerts': [
                        {'volume': None, 'level': 'unknown', 'reasons': [type(exc).__name__]}]}
            self.stop_event.wait(self.capacity_interval)

    def _inventory_loop(self):
        while not self.stop_event.is_set():
            self.scan_event.clear()
            self.scan()
            self.scan_event.wait(self.inventory_interval)

    def _persist(self):
        # Serialize writers and snapshot inside that lock to avoid old-over-new replacement.
        with self.persist_lock:
            try:
                with self.lock:
                    value = {'host': socket.gethostname(), 'history': deepcopy(self.history),
                             'inventory': deepcopy(self.inventory)}
                self.data_dir.mkdir(parents=True, exist_ok=True)
                dest = self.data_dir / 'snapshot.json'
                temp = self.data_dir / 'snapshot.tmp'
                temp.write_text(json.dumps(value, ensure_ascii=False), encoding='utf-8')
                temp.replace(dest)
                self.persistence_error = None
            except OSError as exc:
                self.persistence_error = type(exc).__name__


def install_roots(repo, platform_data, fieldmap_data, fieldtests):
    repo = absolute(repo)
    parent = repo.parent
    roots = [parent if parent.name.lower() == 'robot' else repo,
             platform_data, fieldmap_data, fieldtests]
    transfer = parent.parent / 'RobotTransfer'
    if parent.name.lower() == 'robot' and transfer.is_dir():
        roots.append(transfer)
    tools = parent.parent / 'RobotTools'
    if parent.name.lower() == 'robot' and tools.is_dir():
        roots.append(tools)
    return disjoint_roots(roots)
