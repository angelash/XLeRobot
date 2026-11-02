"""Serve a private visual survey and durable human corrections. No drive API.

Run: python -m deployment.fieldmap.server --data-dir D:/Robot/FieldMap
Camera evidence and the map itself stay outside the versioned source tree.
"""
from __future__ import annotations

import argparse
from datetime import datetime
import json
import mimetypes
import os
from pathlib import Path
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.error import URLError
from urllib.parse import urlsplit, unquote
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

    def load(self):
        path = self.root/'map.json'
        data = json.loads(path.read_text(encoding='utf-8-sig')) if path.exists() else {
            'schema': 1, 'id': 'awaiting-survey', 'title': '等待环境观察',
            'observations': [], 'features': [], 'scan': {'state': 'waiting'},
        }
        # Review never changes the original observation or unlocks a motion path.
        data['motion_enabled'] = False
        data['coordinate_system'] = 'schematic_relative_bearing_not_metric'
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

    def image(self, name):
        # Even an existing private file is unavailable unless the survey names it.
        if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]*\.(jpg|jpeg|png)', name):
            raise FileNotFoundError(name)
        names = {o['image'] for o in self.load()['observations']}
        if name not in names:
            raise FileNotFoundError(name)
        path = (self.root/'images'/name).resolve()
        if path.parent != (self.root/'images').resolve():
            raise FileNotFoundError(name)
        return path


def handler_for(survey):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def send(self, code, body, content_type='application/json; charset=utf-8'):
            if not isinstance(body, bytes):
                body = json.dumps(body, ensure_ascii=False).encode('utf-8')
            self.send_response(code)
            self.send_header('Content-Type', content_type)
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('Content-Security-Policy', "default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; frame-ancestors 'self'")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            path = unquote(urlsplit(self.path).path)
            try:
                if path == '/api/map':
                    self.send(200, survey.load())
                elif path == '/api/status':
                    self.send(200, survey.status())
                elif path == '/api/export':
                    self.send(200, survey.load())
                elif path.startswith('/images/'):
                    image = survey.image(path[len('/images/'):])
                    self.send(200, image.read_bytes(), mimetypes.guess_type(image.name)[0])
                elif path in ('/', '/index.html', '/app.js', '/style.css'):
                    file = STATIC/('index.html' if path == '/' else path[1:])
                    self.send(200, file.read_bytes(), mimetypes.guess_type(file.name)[0]+'; charset=utf-8')
                else:
                    self.send(404, {'error': '接口不存在'})
            except (FileNotFoundError, KeyError):
                self.send(404, {'error': '证据尚未就绪'})
            except (OSError, ValueError):
                self.send(503, {'error': '观察数据暂时不可读取'})

        def do_POST(self):
            # JSON only, no CORS. A foreign page cannot silently submit a review
            # or stop command to this private LAN service.
            origin = self.headers.get('Origin')
            if origin and origin != 'http://'+self.headers.get('Host', ''):
                return self.send(403, {'error': '只接受本页操作'})
            if self.headers.get_content_type() != 'application/json':
                return self.send(415, {'error': '需要JSON请求'})
            try:
                length = int(self.headers.get('Content-Length', '0'))
                if not 0 < length <= 32000:
                    raise ValueError('请求大小无效')
                payload = json.loads(self.rfile.read(length))
                if not isinstance(payload, dict):
                    raise ValueError('请求需为对象')
                if self.path == '/api/review':
                    return self.send(200, survey.review(payload))
                if self.path == '/api/stop':
                    failures = []
                    for path in ('/api/agent/stop', '/api/controller/stop'):
                        try:
                            survey.controller(path, {})
                        except (OSError, ValueError) as exc:
                            failures.append(str(exc))
                    survey.status_at = 0
                    status = survey.status()
                    return self.send(503 if failures else 200,
                                     {'requested': True, 'errors': failures, 'status': status})
                return self.send(404, {'error': '接口不存在'})
            except (ValueError, TypeError, KeyError) as exc:
                self.send(409, {'error': str(exc)})
            except OSError:
                self.send(503, {'error': '保存失败，请稍后重试'})

    return Handler


def parent_alive(pid):
    if os.name == 'nt':
        import ctypes
        kernel=ctypes.WinDLL('kernel32',use_last_error=True)
        kernel.OpenProcess.argtypes=[ctypes.c_uint32,ctypes.c_int,ctypes.c_uint32]
        kernel.OpenProcess.restype=ctypes.c_void_p
        kernel.WaitForSingleObject.argtypes=[ctypes.c_void_p,ctypes.c_uint32]
        kernel.CloseHandle.argtypes=[ctypes.c_void_p]
        handle=kernel.OpenProcess(0x00100000,False,pid)
        if not handle:return False
        try:return kernel.WaitForSingleObject(handle,0)==258
        finally:kernel.CloseHandle(handle)
    try:
        os.kill(pid,0)
        return True
    except ProcessLookupError:
        return False


def watch_parent(server,pid,probe=parent_alive):
    while probe(pid):time.sleep(.2)
    server.shutdown()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data-dir', required=True)
    parser.add_argument('--host', default='127.0.0.1')
    parser.add_argument('--port', type=int, default=8782)
    parser.add_argument('--parent-pid',type=int,default=0)
    args = parser.parse_args()
    server = ThreadingHTTPServer((args.host, args.port), handler_for(Survey(args.data_dir)))
    if args.parent_pid:
        threading.Thread(target=watch_parent,args=(server,args.parent_pid),daemon=True).start()
    try:
        server.serve_forever(poll_interval=.25)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == '__main__':
    main()
