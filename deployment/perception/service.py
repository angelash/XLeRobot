"""Live perception loop inside the Studio process.

One reader subscribes to the controller's head-camera MJPEG (the controller
stays the only UVC owner); one processor analyses the newest frame at a fixed
rate and posts every result to the controller's obstacle gate. When frames,
calibration or head pose are missing, it still posts — an explicit block.
"""
from __future__ import annotations

import json
import threading
import time
import uuid
from collections import deque
from pathlib import Path

import cv2
import httpx
import numpy as np

from .floor import (CALIB, DEFAULT_CONFIG, WORK, Calibration, FloorModel, PeopleDetector, analyse, near_region,
                    polygon_mask, render)

EDITABLE = {'target_fps': (2, 15), 'floor_threshold': (0.2, 0.9), 'floor_coverage': (0.8, 0.995), 'corridor_half_width_m': (0.15, 0.5),
            'max_range_m': (0.6, 3.0), 'roi_top': (0.1, 0.8), 'near_region_top': (0.4, 0.9), 'min_run_px': (2, 20),
            'history_frames': (1, 10), 'max_occluded_fraction': (0.1, 0.9), 'head_tolerance_counts': (5, 80)}
FRAME_TIMEOUT_S = 0.6


def mjpeg_frames(url, stop, client=None):
    """Yield JPEG bytes from a multipart MJPEG response until stop is set."""
    own = client is None
    client = client or httpx.Client(trust_env=False, timeout=httpx.Timeout(5, read=5))
    try:
        with client.stream('GET', url) as response:
            if response.status_code == 409:
                # Studio skills and the live viewer may already own the stream.
                # Reuse the controller's timestamped frame instead of opening UVC
                # again or requiring the user to close another capability.
                response.close()
                base, camera = url.rsplit('/camera/', 1)
                camera = camera.split('.', 1)[0]
                last = None
                while not stop.is_set():
                    shared = client.get(f'{base}/api/camera/{camera}/snapshot', timeout=2)
                    shared.raise_for_status()
                    try:
                        age = time.time() - float(shared.headers['X-Captured-At'])
                    except (KeyError, ValueError):
                        raise RuntimeError('共享画面缺少可靠时间戳') from None
                    if not -0.1 <= age <= 0.35:
                        raise RuntimeError('共享画面已过期')
                    if shared.content != last:
                        last = shared.content
                        yield last, time.monotonic() - max(0.0, age)
                    stop.wait(0.07)
                return
            response.raise_for_status()
            buffer = bytearray()
            for chunk in response.iter_bytes():
                if stop.is_set():
                    return
                buffer.extend(chunk)
                while True:
                    start = buffer.find(b'\xff\xd8')
                    end = buffer.find(b'\xff\xd9', start+2) if start >= 0 else -1
                    if start < 0 or end < 0:
                        if len(buffer) > 3_000_000:
                            buffer.clear()
                        break
                    yield bytes(buffer[start:end+2])
                    del buffer[:end+2]
    finally:
        if own:
            client.close()


class PerceptionService:
    def __init__(self, data_dir, controller='http://127.0.0.1:8765', ledger_provider=None, frame_source=None,
                 publisher=None, status_source=None, clock=time.monotonic):
        self.root = Path(data_dir)
        self.root.mkdir(parents=True, exist_ok=True)
        self.controller = controller
        self.ledger_provider = ledger_provider
        self.frame_source = frame_source or (lambda stop: mjpeg_frames(f"{controller}/camera/{self.config['camera']}.mjpg", stop))
        self.http = httpx.Client(trust_env=False, timeout=0.5)
        self.publisher = publisher or self._publish
        self.status_source = status_source or self._controller_status
        self.clock = clock
        self.lock = threading.Lock()
        self.config = {**DEFAULT_CONFIG, **self._read('config.json', {})}
        self.model = FloorModel.from_json(self._read('floor-model.json', None))
        self.self_mask = polygon_mask(self.config['self_mask'])
        self.stop_event = threading.Event()
        self.threads = []
        self.running = False
        self.stream = None
        self.seq = 0
        self.frame = None
        self.frame_at = None
        self.frames_in = 0
        self.reader_error = None
        self.latest = None
        self.overlay = None
        self.overlay_seq = 0
        self.history = deque(maxlen=10)
        self.timings = deque(maxlen=50)
        self.publish_errors = 0
        self.published = 0
        self.head = {'ok': False, 'reason': '尚未读取头部姿态', 'positions': None, 'at': None}
        self.calibration, self.calibration_info, self.calibration_checked = None, {'status': 'none'}, -1e9
        # OpenCV 5.0 (the laptop build) no longer ships HOGDescriptor; floor contact still works.
        self.people, self.people_note = None, '已关闭'
        if self.config.get('people_detector'):
            if hasattr(cv2, 'HOGDescriptor'):
                self.people, self.people_note = PeopleDetector(), '可用（每0.5秒）'
            else:
                self.people_note = f'不可用：OpenCV {cv2.__version__} 未包含HOG行人检测'
        self.people_boxes, self.people_at = [], -1e9

    # ---- persistence -------------------------------------------------------
    def _read(self, name, default):
        path = self.root/name
        try:
            return json.loads(path.read_text(encoding='utf-8')) if path.exists() else default
        except (OSError, ValueError):
            return default

    def _write(self, name, data):
        tmp = (self.root/name).with_suffix('.tmp')
        tmp.write_text(json.dumps(data, ensure_ascii=False), encoding='utf-8')
        tmp.replace(self.root/name)

    # ---- lifecycle ---------------------------------------------------------
    def start(self):
        with self.lock:
            if self.running:
                return self.status()
            self.stop_event.clear()
            self.running = True
            self.stream = 'perception-'+uuid.uuid4().hex[:8]
            self.seq = 0
            self.frame = self.frame_at = None
            self.history.clear()
            self.threads = [threading.Thread(target=target, daemon=True, name=name) for target, name in
                            ((self._reader, 'perception-reader'), (self._processor, 'perception-processor'), (self._head_loop, 'perception-head'))]
            for thread in self.threads:
                thread.start()
        return self.status()

    def stop(self):
        with self.lock:
            if not self.running:
                return self.status()
            self.stop_event.set()
            threads, self.threads = self.threads, []
        for thread in threads:
            thread.join(timeout=6)
        with self.lock:
            self.running = False
        # A final explicit block; the controller would also expire the stream.
        self._send({'valid': False, 'metric': False, 'head_pose_ok': False, 'clear_m': None, 'reasons': ['视觉避障已停止']})
        return self.status()

    def _reader(self):
        while not self.stop_event.is_set():
            try:
                for source_frame in self.frame_source(self.stop_event):
                    # Shared snapshots retain their original receipt time;
                    # polling a frame is not another camera exposure.
                    jpeg, captured = source_frame if isinstance(source_frame, tuple) else (source_frame, self.clock())
                    with self.lock:
                        self.frame, self.frame_at = jpeg, captured
                        self.frames_in += 1
                        self.reader_error = None
                    if self.stop_event.is_set():
                        break
                else:
                    if not self.stop_event.is_set():
                        self.reader_error = '相机流结束'
            except Exception as exc:  # noqa: BLE001 - reported, retried
                self.reader_error = f'{type(exc).__name__}: {exc}'[:200]
            self.stop_event.wait(1.0)

    def _head_loop(self):
        while not self.stop_event.is_set():
            try:
                positions = self.status_source()
                pose = (self.calibration.head_pose if self.calibration else None) or None
                tolerance = self.config['head_tolerance_counts']
                if not pose:
                    head = {'ok': False, 'reason': '地面标定没有记录头部姿态'}
                else:
                    deltas = {k: positions.get(k, 1e9)-v for k, v in pose.items()}
                    bad = {k: d for k, d in deltas.items() if abs(d) > tolerance}
                    head = {'ok': not bad, 'reason': None if not bad else '头部偏离导航姿态：'+', '.join(f'{k} {d:+.0f}' for k, d in bad.items())}
                head.update(positions={k: positions.get(k) for k in ('COM3:7', 'COM3:8')}, at=self.clock())
            except Exception as exc:  # noqa: BLE001
                head = {'ok': False, 'reason': '无法读取头部姿态：'+type(exc).__name__, 'positions': None, 'at': self.clock()}
            self.head = head
            self.stop_event.wait(0.5)

    def _controller_status(self):
        data = self.http.get(self.controller+'/api/status', timeout=1.5).json()
        return {f"{p['port']}:{m['id']}": m['position'] for p in data.get('ports', []) for m in p.get('motors', [])}

    def _calibration(self):
        now = self.clock()
        if now-self.calibration_checked < 10:
            return self.calibration
        self.calibration_checked = now
        ledger = self.ledger_provider() if self.ledger_provider else None
        found = None
        if ledger is not None:
            items = ledger.calibrations(summary=False)
            adopted = lambda kind: next((c for c in items if c['kind'] == kind and c['camera'] == 'head' and c['status'] == 'adopted'), None)
            ground, intrinsics = adopted('ground_homography'), adopted('intrinsics')
            if ground:
                pose = ground['data'].get('head_pose')
                found = Calibration(ground['data']['H'], intrinsics['data'] if intrinsics and ground['data'].get('undistorted') else None,
                                    {k: v for k, v in (pose or {}).items() if k in ('COM3:7', 'COM3:8')}, {'id': ground['id']})
                self.calibration_info = {'status': 'adopted', 'id': ground['id'], 'rms_m': ground['data'].get('rms_m'), 'head_pose': pose}
            else:
                self.calibration_info = {'status': 'none', 'note': '尚无用户采用的头部地面单应标定（C2）'}
        self.calibration = found
        return found

    def _send(self, result):
        self.seq += 1
        message = {'stream': self.stream or 'perception-idle', 'seq': self.seq, **{k: result.get(k) for k in
                   ('valid', 'metric', 'head_pose_ok', 'clear_m', 'blind_m', 'processing_ms', 'frame_age_ms', 'reasons')},
                   'obstacles': (result.get('obstacles') or [])[:8]}
        try:
            self.publisher(message)
            self.published += 1
        except Exception:  # noqa: BLE001 - the controller expires silence anyway
            self.publish_errors += 1

    def _publish(self, message):
        self.http.post(self.controller+'/api/safety/perception', json=message, timeout=0.25).raise_for_status()

    def _processor(self):
        last_seen = None
        while not self.stop_event.is_set():
            tick = self.clock()
            period = 1/float(self.config['target_fps'])
            with self.lock:
                jpeg, at = self.frame, self.frame_at
            calibration = self._calibration()
            if jpeg is None or at is None or tick-at > FRAME_TIMEOUT_S:
                reason = self.reader_error or '等待头部相机画面'
                self.latest = {'valid': False, 'metric': calibration is not None, 'clear_m': None, 'reasons': ['相机画面中断：'+reason],
                               'head_pose_ok': self.head['ok'], 'at': time.time()}
                self._send(self.latest)
                self.stop_event.wait(0.2)
                continue
            if at == last_seen:
                self.stop_event.wait(0.01)
                continue
            last_seen = at
            started = time.perf_counter()
            bgr = cv2.imdecode(np.frombuffer(jpeg, np.uint8), cv2.IMREAD_REDUCED_COLOR_2)
            if bgr is None:
                continue
            if self.people and tick-self.people_at >= self.config['people_every_s']:
                self.people_boxes, self.people_at = self.people.detect(bgr), tick
            result, layers = analyse(bgr, self.model, self.config, calibration, self.self_mask, self.people_boxes)
            head = self.head
            result['head_pose_ok'] = bool(head['ok'])
            if calibration is not None and not head['ok']:
                result['reasons'].append(head['reason'] or '头部姿态未知')
                result['valid'] = False
            raw_clear = result['clear_m']
            self.history.append(raw_clear if result['valid'] else None)
            recent = list(self.history)[-int(self.config['history_frames']):]
            # Conservative: an obstacle seen in any recent frame keeps the gate closed.
            if result['valid']:
                if len(recent) < int(self.config['history_frames']) or any(v is None for v in recent):
                    result['valid'] = False
                    result['reasons'].append('等待连续有效帧')
                else:
                    result['clear_m'] = min(recent)
            result.update(raw_clear_m=raw_clear, frame_age_ms=round((self.clock()-at)*1000, 1), at=time.time(),
                          total_ms=round((time.perf_counter()-started)*1000, 2))
            if result['frame_age_ms'] > 350:
                result['valid'] = False
                result['reasons'].append('头部源画面已超过350ms')
            self.timings.append(result['total_ms'])
            self.latest = result
            self._send(result)
            if self.overlay_seq % 2 == 0 or self.overlay is None:
                ok, data = cv2.imencode('.jpg', render(bgr, result, layers, self.config, calibration), [cv2.IMWRITE_JPEG_QUALITY, 70])
                if ok:
                    self.overlay = data.tobytes()
            self.overlay_seq += 1
            self.stop_event.wait(max(0.0, period-(self.clock()-tick)))

    # ---- operator actions --------------------------------------------------------
    def learn_floor(self, confirmed):
        if confirmed is not True:
            raise ValueError('请先确认机器人前方近处地面没有障碍物')
        with self.lock:
            jpeg, at = self.frame, self.frame_at
        if not self.running or jpeg is None or self.clock()-at > 1.0:
            raise ValueError('需要正在运行且1秒内的头部画面')
        bgr = cv2.imdecode(np.frombuffer(jpeg, np.uint8), cv2.IMREAD_REDUCED_COLOR_2)
        hsv = cv2.cvtColor(bgr, cv2.COLOR_BGR2HSV)
        self.model.learn(hsv, near_region(self.config, self.self_mask, self._calibration()))
        self._write('floor-model.json', {**self.model.to_json(), 'updated_at': time.strftime('%Y-%m-%dT%H:%M:%S%z')})
        return {'samples': self.model.samples, 'ready': self.model.ready}

    def reset_floor(self, confirmed):
        if confirmed is not True:
            raise ValueError('需要确认重置地面模型')
        self.model = FloorModel()
        self._write('floor-model.json', self.model.to_json())
        return {'samples': 0, 'ready': False}

    def update_config(self, changes):
        if not isinstance(changes, dict):
            raise ValueError('配置需为对象')
        updated = dict(self.config)
        for key, value in changes.items():
            if key in ('self_mask', 'learn_region'):
                polygons = value if key == 'self_mask' else [value]
                if (not isinstance(polygons, list) or len(polygons) > 6 or any(not isinstance(p, list) or not 3 <= len(p) <= 12 for p in polygons)
                        or any(not (isinstance(pt, list) and len(pt) == 2 and all(type(v) in (int, float) and 0 <= v <= 1 for v in pt)) for p in polygons for pt in p)):
                    raise ValueError('遮罩/地面学习区域需为3–12点的归一化多边形；车体遮罩最多6个')
                updated[key] = value
            elif key in EDITABLE:
                low, high = EDITABLE[key]
                if isinstance(value, bool) or not isinstance(value, (int, float)) or not low <= value <= high:
                    raise ValueError(f'{key}需在{low}–{high}')
                updated[key] = int(value) if isinstance(low, int) and isinstance(high, int) else float(value)
            else:
                raise ValueError('不可修改的配置：'+str(key))
        self.config = updated
        self.self_mask = polygon_mask(self.config['self_mask'])
        self._write('config.json', {k: v for k, v in updated.items() if k in EDITABLE or k in ('self_mask', 'learn_region')})
        return self.config

    def gate_view(self):
        try:
            return self.http.get(self.controller+'/api/safety/perception', timeout=0.8).json()
        except Exception as exc:  # noqa: BLE001
            return {'present': False, 'error': type(exc).__name__}

    def status(self):
        latest = dict(self.latest or {})
        timings = sorted(self.timings)
        now = self.clock()
        self._calibration()
        return {'running': self.running, 'stream': self.stream, 'camera': self.config['camera'], 'frames_in': self.frames_in,
                'frame_age_ms': None if self.frame_at is None else round((now-self.frame_at)*1000), 'reader_error': self.reader_error,
                'published': self.published, 'publish_errors': self.publish_errors,
                'processing_ms_median': timings[len(timings)//2] if timings else None,
                'processing_ms_p90': timings[int(len(timings)*0.9)] if timings else None,
                'latest': latest, 'head': {k: v for k, v in self.head.items() if k != 'at'}, 'calibration': self.calibration_info,
                'floor_model': {'ready': self.model.ready, 'samples': self.model.samples}, 'config': self.config,
                'people_detector': self.people_note, 'work_size': list(WORK), 'calibration_size': list(CALIB)}
