import pytest
from fastapi.testclient import TestClient
from fabrication.assembly.debug_console.camera_snapshot import CameraSnapshots


def test_owner_exclusion_freshness_and_release(monkeypatch):
    from fabrication.assembly.debug_console import camera_snapshot as mod
    clock = [10.]
    monkeypatch.setattr(mod.time, 'monotonic', lambda: clock[0])
    cache = CameraSnapshots()
    assert cache.claim(0) and not cache.claim(0)
    with pytest.raises(ValueError, match='尚无'):
        cache.latest(0)
    cache.publish(0, b'frame', 1)
    assert cache.latest(0).jpeg == b'frame'
    clock[0] += 3
    with pytest.raises(ValueError, match='过期'):
        cache.latest(0)
    cache.release(0)
    assert cache.claim(0)
    with pytest.raises(ValueError, match='尚无'):
        cache.latest(0)


def test_api_returns_existing_frame_without_opening_camera(monkeypatch):
    from fabrication.assembly.debug_console import app as web
    cache = CameraSnapshots()
    monkeypatch.setattr(web, 'camera_snapshots', cache)
    client = TestClient(web.app)
    assert client.get('/api/camera/0/snapshot').status_code == 409
    cache.claim(0); cache.publish(0, b'jpeg', 7)
    response = client.get('/api/camera/0/snapshot')
    assert response.content == b'jpeg'
    assert response.headers['x-frame-sequence'] == '7'
    assert response.headers['cache-control'] == 'no-store'
    assert client.get('/camera/0.mjpg').status_code == 409
    assert client.get('/api/camera/8/snapshot').status_code == 404
