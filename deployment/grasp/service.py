"""Live fast-grasp service inside the Studio process.

Camera feeds come from the controller (the only UVC owner): the wrist stream is
claimed when free, otherwise its shared newest frame is polled. A tracker thread
follows the boxed object on every new wrist frame; operations (survey, pick and
place) run one at a time in a worker thread and move the arm only through the
controller's bounded servo session. Each operation writes keyframes from all
cameras plus an event log into its own run folder.
"""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path

import cv2
import httpx
import numpy as np

from .controller import DEFAULTS, EDITABLE, GraspController, GraspFailure
from .knowledge import Knowledge, iou
from .tracker import TemplateTracker
from .vision import draw, propose

FRAME_STALE_S = 1.5
CAMERAS = (2, 1, 0)
# Defaults of the first releases (464ffb9–eadd5b0), which saved every editable value.
LEGACY_DEFAULTS = {'survey_height': 6.0, 'clear_bottom': 335.0}


class CameraFeed:
    """Newest JPEG from one controller camera: its MJPEG stream if free, else the shared snapshot."""
    def __init__(self, camera, controller, client_factory=None, clock=time.monotonic):
        self.camera, self.controller, self.clock = camera, controller, clock
        self.client_factory = client_factory or (lambda: httpx.Client(trust_env=False, timeout=httpx.Timeout(5, read=5)))
        self.jpeg = None; self.t = None; self.seq = 0; self.mode = 'off'; self.error = None
        self.stop_event = threading.Event(); self.thread = None

    def start(self):
        if self.thread and self.thread.is_alive():
            return
        self.stop_event.clear()
        self.thread = threading.Thread(target=self._run, daemon=True, name=f'grasp-camera-{self.camera}')
        self.thread.start()

    def stop(self):
        self.stop_event.set()
        if self.thread:
            self.thread.join(timeout=6)
        self.mode = 'off'

    def _publish(self, jpeg):
        self.jpeg, self.t = jpeg, self.clock(); self.seq += 1; self.error = None

    def _run(self):
        while not self.stop_event.is_set():
            client = self.client_factory()
            try:
                with client.stream('GET', f'{self.controller}/camera/{self.camera}.mjpg') as response:
                    if response.status_code == 409:
                        self.mode = 'shared'
                        response.close()
                        self._poll(client)
                        continue
                    response.raise_for_status()
                    self.mode = 'stream'
                    buffer = bytearray()
                    for chunk in response.iter_bytes():
                        if self.stop_event.is_set():
                            return
                        buffer.extend(chunk)
                        while True:
                            start = buffer.find(b'\xff\xd8')
                            end = buffer.find(b'\xff\xd9', start + 2) if start >= 0 else -1
                            if start < 0 or end < 0:
                                if len(buffer) > 3_000_000:
                                    buffer.clear()
                                break
                            self._publish(bytes(buffer[start:end + 2]))
                            del buffer[:end + 2]
                    self.error = '相机流结束'
            except Exception as exc:  # noqa: BLE001 - reported and retried
                self.error = f'{type(exc).__name__}: {exc}'[:160]
            finally:
                client.close()
            self.stop_event.wait(1.0)

    def _poll(self, client):
        """Another consumer owns the device: reuse its newest frame (about 7 per second)."""
        deadline = self.clock() + 20
        while not self.stop_event.is_set() and self.clock() < deadline:
            try:
                r = client.get(f'{self.controller}/api/camera/{self.camera}/snapshot', timeout=2)
                if r.status_code == 200 and r.content != self.jpeg:
                    self._publish(r.content)
                elif r.status_code != 200:
                    self.error = f'共享画面不可用（{r.status_code}）'
                    return
            except httpx.HTTPError as exc:
                self.error = type(exc).__name__
                return
            self.stop_event.wait(0.14)

    def latest(self, max_age=FRAME_STALE_S):
        if self.jpeg is None or self.t is None or self.clock() - self.t > max_age:
            return None
        return self.jpeg, self.t, self.seq

    def status(self):
        return {'camera': self.camera, 'mode': self.mode, 'seq': self.seq, 'error': self.error,
                'age_ms': None if self.t is None else round((self.clock() - self.t)*1000)}


class HttpExecutor:
    """The controller's /api/servo contract; refusals surface as GraspFailure."""
    def __init__(self, controller, client=None):
        self.url = controller
        self.http = client or httpx.Client(trust_env=False, timeout=5)
        self.session_id = None
        self.active = False

    def _post(self, path, body, timeout=8.0):
        try:
            r = self.http.post(self.url + path, json=body, timeout=timeout)
        except httpx.HTTPError as exc:
            self.active = False
            raise GraspFailure('控制服务无响应：' + type(exc).__name__) from None
        if r.status_code == 409:
            detail = r.json().get('detail', '') if r.headers.get('content-type', '').startswith('application/json') else r.text
            raise GraspFailure('控制服务拒绝：' + str(detail)[:200])
        r.raise_for_status()
        return r.json()

    def start(self, label=''):
        data = self._post('/api/servo/session', {'label': label, 'confirmed': True, 'lease_s': 4.0}, timeout=20)
        self.session_id, self.active = data['session_id'], True
        return self.session_id

    def step(self, targets, torque=None, speed=12.0, label=''):
        body = {'session_id': self.session_id, 'targets': targets, 'speed_deg': speed, 'label': label}
        if torque is not None:
            body['torque'] = int(torque)
        entry = self._post('/api/servo/step', body, timeout=40)
        if entry.get('ended'):
            self.active = False
        return entry

    def hold(self):
        return self._post('/api/servo/hold', {'session_id': self.session_id})

    def base(self, distance_m, authorization, label=''):
        entry = self._post('/api/servo/base', {'session_id': self.session_id, 'distance_m': float(distance_m),
                                               'authorization': authorization, 'label': label}, timeout=20)
        if entry.get('ended'):
            self.active = False
        return entry

    def end(self, reason='完成'):
        if self.active:
            try:
                self._post('/api/servo/end', {'session_id': self.session_id, 'reason': reason[:100]})
            except Exception:  # noqa: BLE001 - the lease ends it anyway
                pass
        self.active = False

    def stop(self, reason='停止'):
        try:
            self.http.post(self.url + '/api/servo/stop', json={'reason': reason}, timeout=4)
        except httpx.HTTPError:
            pass
        self.active = False

    def status(self):
        try:
            return self.http.get(self.url + '/api/servo/status', timeout=1.5).json()
        except Exception as exc:  # noqa: BLE001
            return {'reachable': False, 'error': type(exc).__name__}


class RunRecorder:
    def __init__(self, root, service, operation):
        self.service = service
        self.dir = Path(root)/(time.strftime('%Y%m%d-%H%M%S') + '-' + operation)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.count = 0
        self.keyframes = []

    def event(self, item):
        try:
            with open(self.dir/'events.jsonl', 'a', encoding='utf-8') as f:
                f.write(json.dumps(item, ensure_ascii=False, default=str) + '\n')
        except OSError:
            pass

    def keyframe(self, tag, phase=''):
        self.count += 1
        saved = []
        for camera, feed in self.service.feeds.items():
            frame = feed.latest(max_age=3.0)
            if frame is None:
                continue
            name = f'{self.count:02d}-{tag}-cam{camera}.jpg'
            (self.dir/name).write_bytes(frame[0]); saved.append(name)
        overlay = self.service.overlay
        if overlay:
            name = f'{self.count:02d}-{tag}-cam2-overlay.jpg'
            (self.dir/name).write_bytes(overlay); saved.append(name)
        self.keyframes.append({'n': self.count, 'tag': tag, 'phase': phase, 'files': saved, 'at': time.strftime('%H:%M:%S')})

    def finish(self, result, status):
        try:
            (self.dir/'result.json').write_text(json.dumps({'result': result, 'keyframes': self.keyframes, **status},
                                                           ensure_ascii=False, default=str, indent=1), encoding='utf-8')
        except OSError:
            pass


class GraspService:
    def __init__(self, data_dir, controller='http://127.0.0.1:8765', executor=None, feeds=None, clock=time.monotonic,
                 fabric_provider=None):
        self.root = Path(data_dir)
        self.root.mkdir(parents=True, exist_ok=True)
        (self.root/'runs').mkdir(exist_ok=True)
        self.controller = controller
        self.clock = clock
        self.executor = executor or HttpExecutor(controller)
        self.feeds = feeds or {c: CameraFeed(c, controller, clock=clock) for c in CAMERAS}
        self.config = {**DEFAULTS, **self._load_overrides()}
        self.tracker = TemplateTracker()
        self.lock = threading.Lock()
        self.cond = threading.Condition()
        self.track = None
        self.track_gray = None
        self.overlay = None
        self.cameras_on = False
        self.track_thread = None
        self.track_stop = threading.Event()
        self.op = None
        self.op_thread = None
        self.op_stop = threading.Event()
        self.brain = None
        self.recorder = None
        self.last_result = None
        self.timings = []
        self.knowledge = Knowledge(self.root/'knowledge')
        self.selection = None
        self.fabric_provider = fabric_provider

    def _read(self, name, default):
        try:
            path = self.root/name
            return json.loads(path.read_text(encoding='utf-8')) if path.exists() else default
        except (OSError, ValueError):
            return default

    # ---- cameras and tracking ------------------------------------------------
    def start_cameras(self):
        for feed in self.feeds.values():
            feed.start()
        if not (self.track_thread and self.track_thread.is_alive()):
            self.track_stop.clear()
            self.track_thread = threading.Thread(target=self._track_loop, daemon=True, name='grasp-tracker')
            self.track_thread.start()
        self.cameras_on = True
        return self.status()

    def stop_cameras(self):
        if self.op_thread and self.op_thread.is_alive():
            raise ValueError('抓取进行中，先停止再关闭相机')
        self.track_stop.set()
        for feed in self.feeds.values():
            feed.stop()
        if self.track_thread:
            self.track_thread.join(timeout=5)
        self.cameras_on = False
        return self.status()

    def _decode(self, jpeg):
        return cv2.imdecode(np.frombuffer(jpeg, np.uint8), cv2.IMREAD_COLOR)

    def _track_loop(self):
        last_seq, last_overlay = -1, 0.0
        while not self.track_stop.is_set():
            frame = self.feeds[2].latest()
            if frame is None or frame[2] == last_seq:
                self.track_stop.wait(0.01)
                continue
            jpeg, t, seq = frame
            last_seq = seq
            bgr = self._decode(jpeg)
            if bgr is None:
                continue
            gray = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)
            started = time.perf_counter()
            with self.lock:
                state = self.tracker.update(gray) if self.tracker.active else self.tracker.state()
            self.timings = (self.timings + [round((time.perf_counter() - started)*1000, 1)])[-50:]
            with self.cond:
                self.track = {**state, 't': t, 'seq': seq}
                self.track_gray = gray
                self.cond.notify_all()
            if self.clock() - last_overlay > 0.12:
                phase = self.brain.phase if self.brain else ''
                ok, data = cv2.imencode('.jpg', draw(bgr, state, self.config, phase), [cv2.IMWRITE_JPEG_QUALITY, 70])
                if ok:
                    self.overlay = data.tobytes()
                last_overlay = self.clock()

    def observe(self, after, timeout=2.5):
        """First tracker result on a frame captured after 'after' (None if the wrist feed stalls)."""
        deadline = self.clock() + timeout
        with self.cond:
            while self.track is None or self.track['t'] < after:
                remaining = deadline - self.clock()
                if remaining <= 0 or self.track_stop.is_set():
                    return None
                self.cond.wait(min(0.1, remaining))
            return {**self.track, 'gray': self.track_gray}

    def _fresh_gray(self):
        frame = self.feeds[2].latest(max_age=1.0)
        if frame is None:
            raise ValueError('需要1秒内的腕部画面：先开启相机')
        bgr = self._decode(frame[0])
        if bgr is None:
            raise ValueError('腕部画面无法解码')
        return cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)

    def select(self, bbox, source='operator'):
        if not isinstance(bbox, (list, tuple)) or len(bbox) != 4 or any(isinstance(v, bool) or not isinstance(v, (int, float)) for v in bbox):
            raise ValueError('目标框需为[x, y, 宽, 高]（640×480画面像素）')
        if self.op_thread and self.op_thread.is_alive():
            raise ValueError('抓取进行中，不能更换目标')
        gray = self._fresh_gray()
        with self.lock:
            state = self.tracker.init(gray, bbox)
        self.selection = {'bbox': [float(v) for v in bbox], 'gray': gray, 'source': str(source)[:40], 'at': self.clock()}
        return {'selected': state, 'source': self.selection['source']}

    def locate(self, name=None, select=True):
        """Local finder (v2 R6): stored views instead of a large-model box. First the
        workstation's image-guided detector through the compute fabric, then the
        classical template matcher; either abstains below its score."""
        if not self.knowledge.data['objects']:
            raise ValueError('知识库还没有任何物体模板：先由大模型框选并成功抓取一次')
        hit = self._locate_by_model(name)
        if hit is None or (not hit['ok'] and hit.get('finder_error')):
            classical = self.knowledge.locate(self._fresh_gray(), name)
            classical['finder'] = 'template'
            if hit is not None:
                classical['model_error'] = hit.get('finder_error')
            hit = classical
        if not hit['ok']:
            raise ValueError(f'本地没有认出目标（{hit.get("finder")}最高分{hit["score"]}），请交回大模型框选'
                             + (f'；模型不可用：{hit["model_error"]}' if hit.get('model_error') else ''))
        if select:
            self.select(hit['bbox'], source=f'local:{hit.get("finder")}:{hit["object"]}')
        return {'located': hit}

    def _locate_by_model(self, name):
        fabric = self.fabric_provider() if self.fabric_provider else None
        if fabric is None:
            return None
        import base64
        obj_name = Knowledge.name(name) if name else next(iter(self.knowledge.data['objects']))
        obj = self.knowledge.data['objects'].get(obj_name) or {}
        files = [self.knowledge.root/'templates'/t['file'] for t in reversed(obj.get('templates', []))][:3]
        frames, last = [], None
        for _ in range(3):                      # three distinct recent frames, about 0.2 s apart
            frame = self.feeds[2].latest(max_age=1.0)
            if frame is None:
                raise ValueError('需要1秒内的腕部画面：先开启相机')
            if frame[2] != last:
                frames.append(base64.b64encode(frame[0]).decode()); last = frame[2]
            time.sleep(0.2)
        payload = {'images_b64': frames,
                   'queries_b64': [base64.b64encode(p.read_bytes()).decode() for p in files if p.is_file()]}
        try:
            job = fabric.submit('vision.find_by_example', payload, route='worker_only', deadline_s=25, source='grasp')
            if job['status'] not in ('done', 'failed', 'expired'):
                job = fabric.wait(job['id'], 25)
        except (ValueError, KeyError) as exc:
            return {'ok': False, 'score': None, 'finder': 'owlvit', 'finder_error': str(exc)[:120]}
        result = job.get('result') if job.get('status') == 'done' else None
        if not result:
            return {'ok': False, 'score': None, 'finder': 'owlvit', 'finder_error': (job.get('error') or job.get('status') or '无结果')[:120]}
        return {**result, 'object': obj_name, 'finder': 'owlvit', 'node': job.get('node'), 'total_ms': job.get('total_ms')}

    def propose(self):
        box = propose(self._fresh_gray())
        if box is None:
            raise ValueError('没有找到明显的目标轮廓，请手动框选')
        return {'proposal': box, 'note': '仅为建议框：确认后再点“使用建议框”'}

    def clear(self):
        if self.op_thread and self.op_thread.is_alive():
            raise ValueError('抓取进行中，不能清除目标')
        with self.lock:
            self.tracker.reset()
        self.selection = None
        return self.status()

    # ---- operations ------------------------------------------------------------
    def _launch(self, name, confirmed, work):
        if confirmed is not True:
            raise ValueError('请确认手臂运动范围内无人、桌面工作区已清理，且可随时点立即停止')
        if self.op_thread and self.op_thread.is_alive():
            raise ValueError('已有操作在进行：' + str(self.op))
        if not self.cameras_on or self.feeds[2].latest() is None:
            raise ValueError('需要腕部相机实时画面：先开启相机')
        self.op_stop.clear()
        self.recorder = RunRecorder(self.root/'runs', self, name)
        self.brain = GraspController(self.executor, self, self.recorder, dict(self.config), self.op_stop, self.clock)
        self.op = name

        def runner():
            begun = self.clock()
            try:
                result = work(self.brain)
            except Exception as exc:  # noqa: BLE001 - recorded
                result = {'ok': False, 'error': f'{type(exc).__name__}: {exc}'}
            try:
                result = {**(result or {}), **self._learn(name, result or {}, self.clock() - begun)}
            except Exception as exc:  # noqa: BLE001 - learning never hides the motion result
                result = {**(result or {}), 'learn_error': f'{type(exc).__name__}: {exc}'}
            self.last_result = {'operation': name, **(result or {}), 'dir': self.recorder.dir.name}
            self.recorder.finish(result, self.brain.status())
            self.op = None
        self.op_thread = threading.Thread(target=runner, daemon=True, name='grasp-operation')
        self.op_thread.start()
        return self.status()

    def _learn(self, name, result, seconds):
        """After a real pick-and-place: keep what worked and count who chose the target (v2 R6–R8)."""
        meta = getattr(self, 'run_meta', None) or {}
        if name != 'pick-place' or result.get('rehearsal') or not meta:
            return {}
        obj, sel = meta['object'], meta.get('selection') or {}
        ok = bool(result.get('ok'))
        out = {'object': obj, 'select_source': sel.get('source'), 'llm_calls': meta.get('llm_calls', 0),
               'local_agreement': meta.get('agreement')}
        self.knowledge.record(obj, ok, sel.get('source', 'operator'), run=self.recorder.dir.name, agreement=meta.get('agreement'),
                              seconds=round(seconds, 1), steps=result.get('steps'), llm_calls=meta.get('llm_calls', 0),
                              note=result.get('error', ''))
        if ok and sel.get('gray') is not None:
            c = self.brain.cfg if self.brain else self.config
            params = {k: c.get(k) for k in ('desk_z', 'approach_clearance', 'survey_height', 'insert_cm', 'squeeze_lead', 'squeeze_torque')}
            out['learned'] = self.knowledge.learn(obj, sel['gray'], sel['bbox'], sel.get('source', 'operator'), params, self.recorder.dir.name)
        return out

    def survey(self, confirmed):
        return self._launch('survey', confirmed, lambda brain: brain.run_survey())

    def reposition(self, confirmed, back_cm, authorization):
        if isinstance(back_cm, bool) or not isinstance(back_cm, (int, float)) or not 0.5 <= back_cm <= 20:
            raise ValueError('后退距离需在0.5–20cm')
        if len(str(authorization or '').strip()) < 8:
            raise ValueError('后退需要附上用户授权原文')
        def work(brain):
            brain.cfg['reverse_authorization'] = str(authorization).strip()
            result = brain.run_reposition(float(back_cm))
            for leg in result.get('legs') or []:
                self.knowledge.note_base(-leg['requested_cm']/100, -leg['estimate_cm']/100, leg['stopped_by'], run=self.recorder.dir.name)
            return result
        return self._launch('reposition', confirmed, work)

    def run(self, confirmed, options=None):
        options = options or {}
        obj = Knowledge.name(options.get('object', 'plug'))
        meta = {'object': obj, 'llm_calls': 0, 'agreement': None}
        if options.get('auto_select'):
            self.locate(obj)                      # L2: the local finder chooses the target
        sel = self.selection or {}
        if not str(sel.get('source', 'operator')).startswith('local'):
            meta['llm_calls'] = 1                 # the box came from the large model or the operator
            if self.knowledge.data['objects'].get(obj, {}).get('templates') and sel.get('gray') is not None:
                hit = self.knowledge.locate(sel['gray'], obj)
                meta['agreement'] = round(iou(hit['bbox'], sel['bbox']), 3) if hit['bbox'] else 0.0
        meta['selection'] = sel
        with self.lock:
            ready = self.tracker.active and self.tracker.lost == 0
        if not ready:
            raise ValueError('先在腕部画面中框选目标，并确认跟踪正常')
        overrides = {}
        for key in ('place_lateral_cm', 'place_forward_cm'):
            if key in options:
                low, high = EDITABLE[key]
                value = options[key]
                if isinstance(value, bool) or not isinstance(value, (int, float)) or not low <= value <= high:
                    raise ValueError(f'{key}需在{low}–{high}')
                overrides[key] = float(value)
        if 'end_pose' in options:
            if options['end_pose'] not in ('upright', 'survey'):
                raise ValueError('结束姿态只能是竖直或观察位')
            overrides['end_pose'] = options['end_pose']
        if options.get('stop_after') not in (None, 'inserted'):
            raise ValueError('演练只支持送入两指后停止')
        overrides['stop_after'] = options.get('stop_after')
        if options.get('reverse_authorization'):
            if len(str(options['reverse_authorization']).strip()) < 8:
                raise ValueError('后退授权需写明用户原话')
            overrides['reverse_authorization'] = str(options['reverse_authorization']).strip()
        learned = self.knowledge.params(obj)
        def work(brain):
            brain.cfg.update(overrides)
            return brain.run_pick_place()
        self.run_meta = meta
        status = self._launch('pick-place', confirmed, work)
        return {**status, 'object': obj, 'select_source': sel.get('source'), 'learned_params': learned}

    def stop(self):
        self.op_stop.set()
        self.executor.stop('页面点击立即停止')
        return self.status()

    def _load_overrides(self):
        """Only operator changes are kept, so a new default is not shadowed by an
        old saved value. Files written before 2026-11-03 15:40 hold every editable
        value; there an entry equal to that release's default is not a change."""
        saved = self._read('config.json', {})
        if isinstance(saved.get('overrides'), dict):
            over = saved['overrides']
        else:
            over = {k: v for k, v in saved.items() if v != LEGACY_DEFAULTS.get(k, DEFAULTS.get(k))}
        return {k: v for k, v in over.items() if k in EDITABLE or k == 'end_pose'}

    def update_config(self, changes):
        if not isinstance(changes, dict):
            raise ValueError('配置需为对象')
        updated = dict(self.config)
        for key, value in changes.items():
            if key not in EDITABLE:
                raise ValueError('不可修改的配置：' + str(key))
            low, high = EDITABLE[key]
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not low <= value <= high:
                raise ValueError(f'{key}需在{low}–{high}')
            updated[key] = int(value) if isinstance(low, int) and isinstance(high, int) else float(value)
        self.config = updated
        tmp = self.root/'config.tmp'
        overrides = {k: v for k, v in updated.items() if k in EDITABLE and v != DEFAULTS.get(k)}
        tmp.write_text(json.dumps({'overrides': overrides}, ensure_ascii=False), encoding='utf-8')
        tmp.replace(self.root/'config.json')
        return {k: self.config[k] for k in EDITABLE}

    def runs(self, limit=12):
        out = []
        for d in sorted((self.root/'runs').iterdir(), reverse=True)[:limit]:
            data = {}
            try:
                data = json.loads((d/'result.json').read_text(encoding='utf-8'))
            except (OSError, ValueError):
                pass
            out.append({'id': d.name, 'result': data.get('result'), 'keyframes': data.get('keyframes', [])})
        return out

    def run_file(self, run_id, name):
        import re
        if not re.fullmatch(r'\d{8}-\d{6}-[a-z-]{1,20}', run_id) or not re.fullmatch(r'[A-Za-z0-9_.-]{1,80}\.(jpg|json|jsonl)', name):
            raise ValueError('文件引用无效')
        path = self.root/'runs'/run_id/name
        if not path.is_file():
            raise KeyError('文件不存在')
        return path

    def status(self):
        with self.lock:
            track = dict(self.track or {}) if self.tracker.active else {'active': False}
        track.pop('gray', None)
        timings = sorted(self.timings)
        return {'cameras_on': self.cameras_on, 'cameras': [f.status() for f in self.feeds.values()],
                'track': track, 'tracking_ms_median': timings[len(timings)//2] if timings else None,
                'operation': self.op, 'brain': self.brain.status() if self.brain else None, 'last_result': self.last_result,
                'executor': self.executor.status(), 'config': {k: self.config[k] for k in EDITABLE},
                'selection_source': (self.selection or {}).get('source'), 'knowledge': self.knowledge.summary()}
