"""Generated files and injected capacity samples only; never fills a real disk."""
import json
import os
from pathlib import Path
import stat
import threading
import time

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from .integration import install
from . import service as mod


def monitor(tmp_path, **kwargs):
    return mod.StorageMonitor(tmp_path, tmp_path / 'state', roots=[tmp_path / 'archive'],
                              volumes=lambda: ['test-volume'], hardware=lambda: {'disks': []}, **kwargs)


@pytest.mark.parametrize('total,free,level,reason', [
    (200, 21, 'warning', 'free_percent'),  # Plenty of bytes, low percentage.
    (50, 19, 'warning', 'free_bytes'),     # Plenty of percentage, low bytes.
    (1000, 50, 'critical', 'free_percent'),
    (50, 5, 'critical', 'free_bytes'),
    (100, 20, 'warning', 'free_bytes'),
    (100, 21, 'ok', None),
])
def test_capacity_thresholds(total, free, level, reason):
    actual, reasons = mod.capacity_level(total * mod.GIB, free * mod.GIB, mod.DEFAULT_THRESHOLDS)
    assert actual == level
    if reason:
        assert reason in reasons


def test_inventory_reconciles_nested_roots_and_types(tmp_path):
    evidence = tmp_path / 'FieldTests' / 'run'
    evidence.mkdir(parents=True)
    (evidence / 'camera.JPG').write_bytes(b'p' * 31)
    (evidence / 'state.json').write_bytes(b'j' * 13)
    (tmp_path / 'guide.md').write_bytes(b'd' * 7)
    (tmp_path / 'video.mp4').write_bytes(b'v' * 59)
    result = mod.scan_inventory([tmp_path, evidence, tmp_path])
    assert result['complete']
    assert result['files'] == 4
    assert result['bytes'] == 110
    assert result['images'] == 1 and result['image_bytes'] == 31
    assert len(result['roots']) == 1
    for field in ('kinds', 'purposes', 'directories'):
        assert sum(r['bytes'] for r in result[field]) == result['bytes']
        assert sum(r['files'] for r in result[field]) == result['files']
    assert next(r for r in result['purposes'] if r['id'] == 'evidence')['bytes'] == 44
    assert result['largest_files'][0]['bytes'] == 59


def test_missing_paths_and_limits_are_never_complete(tmp_path):
    assert not mod.scan_inventory([tmp_path / 'missing'])['complete']
    for name in ('one.jpg', 'two.jpg'):
        (tmp_path / name).write_bytes(b'photo')
    result = mod.scan_inventory([tmp_path], max_files=1)
    assert not result['complete'] and result['limit'] == 'files' and result['files'] == 1
    stopped = threading.Event()
    stopped.set()
    assert mod.scan_inventory([tmp_path], stop=stopped)['limit'] == 'stopped'


def test_access_failure_retains_partial_totals(tmp_path, monkeypatch):
    blocked = tmp_path / 'blocked'
    blocked.mkdir()
    (tmp_path / 'visible.jpg').write_bytes(b'visible')
    original = os.scandir

    def scandir(path):
        if Path(path) == blocked:
            raise PermissionError('denied')
        return original(path)

    monkeypatch.setattr(mod.os, 'scandir', scandir)
    result = mod.scan_inventory([tmp_path])
    assert not result['complete'] and result['error_count'] == 1
    assert result['bytes'] == 7 and result['images'] == 1


def test_reparse_root_is_skipped_without_resolving(tmp_path, monkeypatch):
    original = Path.lstat

    class ReparseInfo:
        st_mode = stat.S_IFDIR
        st_file_attributes = 0x400

    def lstat(path, *args, **kwargs):
        return ReparseInfo() if path == tmp_path else original(path, *args, **kwargs)

    monkeypatch.setattr(Path, 'lstat', lstat)
    result = mod.scan_inventory([tmp_path])
    assert result['bytes'] == 0 and result['skipped_links'] == 1
    assert not result['complete']


def test_directory_link_is_not_traversed(tmp_path):
    outside = tmp_path / 'outside'
    outside.mkdir()
    (outside / 'secret.jpg').write_bytes(b'x' * 100)
    root = tmp_path / 'root'
    root.mkdir()
    try:
        (root / 'linked').symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip('Creating symlinks requires Windows developer mode')
    result = mod.scan_inventory([root])
    assert result['bytes'] == 0 and result['skipped_links'] == 1


def test_unreadable_capacity_recovery_and_staleness(tmp_path):
    values = iter([(100*mod.GIB, 97*mod.GIB, 3*mod.GIB), OSError(),
                   (100*mod.GIB, 70*mod.GIB, 30*mod.GIB)])

    def usage(_):
        value = next(values)
        if isinstance(value, Exception):
            raise value
        return value

    service = monitor(tmp_path, usage=usage)
    assert service.check_capacity()['level'] == 'critical'
    assert service.check_capacity()['level'] == 'unknown'
    assert 'free_bytes' not in service.summary()['volumes'][0]
    assert service.check_capacity()['level'] == 'ok'
    service.sample['checked_at'] = '2000-01-01T00:00:00+00:00'
    assert service.summary()['stale'] and service.summary()['level'] == 'unknown'
    assert (tmp_path / 'state' / 'snapshot.json').exists()


def test_scan_does_not_block_capacity_worker(tmp_path, monkeypatch):
    archive = tmp_path / 'archive'
    archive.mkdir()
    entered, release = threading.Event(), threading.Event()
    original = mod.scan_inventory
    samples = []

    def slow(*args, **kwargs):
        entered.set()
        release.wait(2)
        return original(*args, **kwargs)

    def usage(_):
        samples.append(True)
        return (100*mod.GIB, 50*mod.GIB, 50*mod.GIB)

    monkeypatch.setattr(mod, 'scan_inventory', slow)
    service = monitor(tmp_path, usage=usage, capacity_interval=.02)
    service.start()
    try:
        assert entered.wait(1)
        deadline = time.monotonic() + 1
        while len(samples) < 3 and time.monotonic() < deadline:
            time.sleep(.01)
        assert len(samples) >= 3
        assert service.status()['scanning']
        for _ in range(30):
            service.refresh()
        assert len(service.threads) == 2
    finally:
        release.set()
        service.close()
    assert not any(t.is_alive() for t in service.threads)


def test_http_refresh_has_no_path_or_command_input(tmp_path):
    service = monitor(tmp_path)
    app = FastAPI()
    install(app, repo=tmp_path, data_dir=tmp_path, roots=[],
            allowed_origins=['http://localhost:8770'], monitor=service)
    with TestClient(app) as client:
        assert client.get('/api/platform/storage/status').json()['read_only']
        assert client.get('/api/platform/storage/summary').json()['level'] == 'unknown'
        assert client.post('/api/platform/storage/refresh', json={}, headers={'Origin':'https://foreign.invalid'}).status_code == 403
        assert client.post('/api/platform/storage/refresh', json={'path':'C:/'}).status_code == 422
        assert client.post('/api/platform/storage/refresh', content='{}').status_code == 415
        assert client.post('/api/platform/storage/refresh', content='x'*129, headers={'Content-Type':'application/json'}).status_code == 413
        assert client.post('/api/platform/storage/refresh', json={}).status_code == 200
        assert service.scan_event.is_set()
        assert 'frame-ancestors' in client.get('/storage/view').headers['Content-Security-Policy']
        assert client.get('/storage/assets/storage.js').status_code == 200


def test_persisted_inventory_and_write_failure_are_visible(tmp_path, monkeypatch):
    archive = tmp_path / 'archive'
    archive.mkdir()
    (archive / 'picture.jpg').write_bytes(b'abc')
    service = monitor(tmp_path)
    service.scan()
    restored = monitor(tmp_path)
    assert restored.status()['inventory']['from_previous_run']
    assert restored.status()['inventory']['bytes'] == 3
    original = Path.mkdir

    def denied(path, *args, **kwargs):
        if path == service.data_dir:
            raise PermissionError()
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, 'mkdir', denied)
    service._persist()
    assert service.status()['persistence_error'] == 'PermissionError'
