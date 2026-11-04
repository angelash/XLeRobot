"""Synthetic ground-plane scenes: distances, masks, gating messages, HTTP surface."""
import time

import cv2
import numpy as np
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from deployment.perception.floor import DEFAULT_CONFIG, WORK, Calibration, FloorModel, analyse, near_region, polygon_mask, render
from deployment.perception.integration import install
from deployment.perception.service import PerceptionService

K = np.array([[330., 0, 320], [0, 330., 240], [0, 0, 1]])
HEIGHT, PITCH = 0.6, np.radians(50)
R = np.array([[0, -1, 0], [-np.sin(PITCH), 0, -np.cos(PITCH)], [np.cos(PITCH), 0, -np.sin(PITCH)]])
F2I = K @ np.column_stack([R[:, 0], R[:, 1], -R @ np.array([0, 0, HEIGHT])])
I2F = np.linalg.inv(F2I)
POSE = {'COM3:7': 2005, 'COM3:8': 2450}


def floor_px(x, y):
    p = F2I @ np.array([x, y, 1.0])
    return p[:2]/p[2]


def scene(obstacle=(0.8, 0.0), seed=1, gripper=True, color=(40, 50, 70), side=None):
    rng = np.random.default_rng(seed)
    bird = np.zeros((600, 600, 3), np.uint8)
    for r in range(0, 600, 50):
        for c in range(0, 600, 50):
            bird[r:r+50, c:c+50] = (120, 120, 120) if (r//50+c//50) % 2 else (150, 95, 45)
    bird = np.clip(bird.astype(np.int16)+rng.normal(0, 6, bird.shape), 0, 255).astype(np.uint8)
    A = np.array([[0, -1/200, 3.0], [-1/200, 0, 1.5], [0, 0, 1]])
    image = cv2.warpPerspective(bird, F2I @ A, (640, 480), borderValue=(235, 235, 235))
    if obstacle:
        x, y = obstacle
        a, b = floor_px(x, y-0.12), floor_px(x, y+0.12)
        box = np.array([a, b, b-[0, 110], a-[0, 110]], np.int32)
        cv2.fillPoly(image, [box], color)
    if side:
        # A white cabinet beside the corridor, low in the view.
        quad = np.array([floor_px(0.12, side), floor_px(0.35, side), floor_px(0.35, side+0.3), floor_px(0.12, side+0.3)], np.int32)
        cv2.fillPoly(image, [quad], (225, 225, 225))
    if gripper:
        cv2.fillPoly(image, [np.array([[0, 340], [150, 340], [190, 480], [0, 480]], np.int32)], (245, 245, 245))
    return image


def trained_model():
    model = FloorModel()
    for seed in (1, 2):
        hsv = cv2.cvtColor(cv2.resize(scene(obstacle=None, seed=seed), WORK, interpolation=cv2.INTER_AREA), cv2.COLOR_BGR2HSV)
        model.learn(hsv, near_region(DEFAULT_CONFIG, polygon_mask(DEFAULT_CONFIG['self_mask'])))
    return model


def test_obstacle_distance_from_ground_contact():
    model, calib = trained_model(), Calibration(I2F)
    result, layers = analyse(scene(), model, DEFAULT_CONFIG, calib)
    assert result['valid'] and result['metric'], result['reasons']
    assert result['clear_m'] == pytest.approx(0.8, abs=0.06)
    assert result['blind_m'] < 0.2 and result['obstacles'][0]['kind'] == 'floor_contact'
    view = render(scene(), result, layers, DEFAULT_CONFIG, calib)
    assert view.shape == (240, 320, 3)


def test_side_obstacle_and_self_mask_do_not_block_corridor():
    model, calib = trained_model(), Calibration(I2F)
    result, _ = analyse(scene(obstacle=(0.8, 0.6)), model, DEFAULT_CONFIG, calib)
    assert result['valid'] and result['clear_m'] > 1.3
    unmasked = {**DEFAULT_CONFIG, 'self_mask': []}
    blocked, _ = analyse(scene(obstacle=None), model, unmasked, calib)
    assert blocked['clear_m'] is None or blocked['clear_m'] < 0.3, 'an unmasked gripper must look like an obstacle'


def test_floor_is_learned_only_ahead_so_side_furniture_stays_an_obstacle():
    mask = polygon_mask(DEFAULT_CONFIG['self_mask'])
    learn = cv2.cvtColor(cv2.resize(scene(obstacle=None, side=0.32), WORK, interpolation=cv2.INTER_AREA), cv2.COLOR_BGR2HSV)
    model = FloorModel()
    model.learn(learn, near_region(DEFAULT_CONFIG, mask))
    model.learn(learn, near_region(DEFAULT_CONFIG, mask, Calibration(I2F)))
    white_ahead = scene(obstacle=(0.8, 0.0), color=(225, 225, 225))
    result, _ = analyse(white_ahead, model, DEFAULT_CONFIG, Calibration(I2F))
    assert result['valid'] and result['clear_m'] == pytest.approx(0.8, abs=0.06)


def test_uncalibrated_or_unlearned_floor_is_never_metric():
    result, layers = analyse(scene(), trained_model(), DEFAULT_CONFIG, None)
    assert not result['valid'] and not result['metric'] and result['clear_m'] is None and layers is not None
    empty, _ = analyse(scene(), FloorModel(), DEFAULT_CONFIG, Calibration(I2F))
    assert not empty['valid'] and '地面外观' in empty['reasons'][0]
    restored = FloorModel.from_json(trained_model().to_json())
    assert restored.ready


class Ledger:
    def calibrations(self, summary=False):
        return [{'id': 'g', 'kind': 'ground_homography', 'camera': 'head', 'status': 'adopted',
                 'data': {'H': I2F.tolist(), 'rms_m': 0.004, 'undistorted': False, 'head_pose': POSE}}]


def make_service(tmp_path, obstacle=(0.8, 0.0), pose=POSE, frames=True):
    jpeg = cv2.imencode('.jpg', scene(obstacle), [cv2.IMWRITE_JPEG_QUALITY, 92])[1].tobytes()

    def source(stop):
        while frames and not stop.is_set():
            yield jpeg
            time.sleep(0.03)
        if not frames:
            stop.wait(0.5)
            return
            yield
    sent = []
    service = PerceptionService(tmp_path, ledger_provider=lambda: Ledger(), frame_source=source,
                                publisher=sent.append, status_source=lambda: dict(pose))
    service.model = trained_model()
    return service, sent


def wait_for(predicate, timeout=6):
    end = time.monotonic()+timeout
    while time.monotonic() < end:
        if predicate():
            return True
        time.sleep(0.05)
    return False


def test_service_publishes_conservative_clearance_and_explicit_stop(tmp_path):
    service, sent = make_service(tmp_path)
    service.start()
    try:
        assert wait_for(lambda: any(m['valid'] for m in sent)), [m['reasons'] for m in sent[-3:]]
        good = [m for m in sent if m['valid']][-1]
        assert good['metric'] and good['head_pose_ok'] and good['clear_m'] == pytest.approx(0.8, abs=0.06)
        assert [m['seq'] for m in sent] == sorted(m['seq'] for m in sent) and len({m['stream'] for m in sent}) == 1
        assert service.learn_floor(True)['ready'] and service.overlay.startswith(b'\xff\xd8')
        with pytest.raises(ValueError):
            service.learn_floor(False)
    finally:
        service.stop()
    assert sent[-1]['valid'] is False and '停止' in sent[-1]['reasons'][0]


def test_head_pose_drift_and_missing_frames_block(tmp_path):
    service, sent = make_service(tmp_path, pose={'COM3:7': 2005, 'COM3:8': 2600})
    service.start()
    try:
        assert wait_for(lambda: any('头部偏离' in ' '.join(m['reasons'] or []) for m in sent))
        assert not any(m['valid'] for m in sent)
    finally:
        service.stop()
    blind, sent = make_service(tmp_path, frames=False)
    blind.start()
    try:
        assert wait_for(lambda: any('相机画面中断' in ' '.join(m['reasons'] or []) for m in sent))
    finally:
        blind.stop()


def test_config_validation_and_http_surface(tmp_path):
    service, _ = make_service(tmp_path)
    assert service.status()['people_detector'].startswith(('可用', '不可用'))
    with pytest.raises(ValueError):
        service.update_config({'controller': 'http://evil'})
    with pytest.raises(ValueError):
        service.update_config({'max_range_m': 9})
    assert service.update_config({'max_range_m': 1.2})['max_range_m'] == 1.2
    assert PerceptionService(tmp_path, ledger_provider=lambda: Ledger(), frame_source=lambda s: iter(()),
                             publisher=lambda m: None, status_source=dict).config['max_range_m'] == 1.2
    app = FastAPI()
    service.gate_view = lambda: {'present': False}
    install(app, data_dir=tmp_path, allowed_origins=('http://192.168.137.150:8770',), service=service)
    with TestClient(app) as client:
        assert client.get('/api/platform/perception/status').json()['running'] is False
        assert client.post('/api/platform/perception/start', json={}, headers={'Origin': 'http://evil.example'}).status_code == 403
        assert client.post('/api/platform/perception/start', json={}).json()['running']
        assert client.post('/api/platform/perception/learn-floor', json={'confirmed': False}).status_code == 422
        assert wait_for(lambda: service.overlay is not None)
        assert client.get('/api/platform/perception/frame.jpg').content.startswith(b'\xff\xd8')
        assert client.post('/api/platform/perception/stop', json={}).json()['running'] is False
        view = client.get('/perception/view')
        assert "script-src 'self'" in view.headers['content-security-policy'] and '/perception/assets/app.js' in view.text
        assert client.post('/api/platform/perception/drive', json={}).status_code == 404


def test_confirmed_floor_region_can_exclude_a_nearby_white_table(tmp_path):
    service, _ = make_service(tmp_path)
    region = [[0.56, 0.68], [0.96, 0.68], [0.96, 0.86], [0.56, 0.86]]
    assert service.update_config({'learn_region': region})['learn_region'] == region
    restored, _ = make_service(tmp_path)
    assert restored.config['learn_region'] == region
    mask = near_region(service.config, service.self_mask, None)
    assert not mask[180, 100] and mask[180, 210], 'Left table excluded; right floor sampled'
    for bad in [[], [[0.,0.],[1.,1.]], [[True,0.],[1.,0.],[1.,1.]], [[0.,0.],[1.1,0.],[1.,1.]]]:
        with pytest.raises(ValueError):
            service.update_config({'learn_region':bad})


def test_busy_camera_uses_fresh_shared_frames_without_replaying_identical_images():
    import httpx
    import threading
    from deployment.perception.service import mjpeg_frames
    stop = threading.Event()
    paths = []
    def handle(request):
        paths.append(request.url.path)
        if request.url.path.endswith('.mjpg'):
            return httpx.Response(409)
        n = len(paths)-1
        if n == 3:
            stop.set()
        return httpx.Response(200, content=b'a' if n < 3 else b'b', headers={'X-Captured-At':str(time.time())})
    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        frames=list(mjpeg_frames('http://controller/camera/0.mjpg',stop,client))
        assert [frame[0] for frame in frames] == [b'a',b'b']
        assert all(0 <= time.monotonic()-frame[1] < .4 for frame in frames)
    assert paths == ['/camera/0.mjpg']+['/api/camera/0/snapshot']*3


def test_busy_camera_rejects_stale_shared_snapshot():
    import httpx
    import threading
    from deployment.perception.service import mjpeg_frames
    def handle(request):
        return httpx.Response(409) if request.url.path.endswith('.mjpg') else httpx.Response(
            200,content=b'old-jpeg',headers={'X-Captured-At':str(time.time()-1.)})
    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        with pytest.raises(RuntimeError,match='过期'):
            list(mjpeg_frames('http://controller/camera/0.mjpg',threading.Event(),client))


def test_reader_preserves_shared_source_timestamp(tmp_path):
    service,_=make_service(tmp_path)
    stamp=time.monotonic()-.2
    def source(stop):
        yield b'jpeg',stamp
        stop.set()
    service.frame_source=source
    service._reader()
    assert service.frame_at == stamp and service.frame == b'jpeg'
