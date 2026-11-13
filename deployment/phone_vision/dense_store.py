"""Persistent local surfaces and bounded, observation-only fixed-view jobs."""
from __future__ import annotations
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import threading
import time
import uuid

from .dense import atomic, stamp
from .evidence import IDENTIFIER

OUTPUTS = {'surface.json': 12*1024*1024, 'report.json': 200000, 'support.png': 2000000,
           'manifest.json': 10000, 'preview.jpg': 4000000,
           'surface.ply': 32000000, 'surface-full.ply': 128000000}


class DenseStore:
    def __init__(self, root, service):
        self.root, self.service = Path(root), service
        self.lock = threading.Lock()
        self.active = None
        self.stop = threading.Event()
        self.thread = None

    def _folder(self, identifier):
        if not isinstance(identifier, str) or not IDENTIFIER.fullmatch(identifier):
            raise KeyError('Unknown reconstruction')
        folder = (self.root/identifier).resolve()
        if folder.parent != self.root.resolve() or not folder.is_dir():
            raise KeyError('Unknown reconstruction')
        if any((folder/name).resolve() != folder/name for name in ('input', 'result')):
            raise KeyError('Linked reconstruction folder')
        return folder

    def _read(self, path, budget):
        if path.resolve() != path or not path.is_file() or path.stat().st_size > budget:
            raise ValueError('Reconstruction metadata unavailable')
        return json.loads(path.read_text('utf8'))

    def media(self, identifier, name):
        folder = self._folder(identifier)
        if re.fullmatch(r'\d{4}\.jpg', name):
            sources = self._read(folder/'input/frames.json', 500000)['frames']
            matches = [r for r in sources if r['rgb_file'] == name]
            if len(matches) != 1:
                raise KeyError(name)
            path, budget, expected = folder/'input'/name, 4000000, matches[0]['rgb_sha256']
        elif name in OUTPUTS:
            path, budget = folder/'result'/name, OUTPUTS[name]
            manifest = self._read(folder/'result/manifest.json', OUTPUTS['manifest.json'])
            expected = manifest[name]['sha256'] if name != 'manifest.json' else None
            if name != 'manifest.json' and path.stat().st_size != manifest[name]['bytes']:
                raise ValueError('Reconstruction size mismatch')
        else:
            raise KeyError(name)
        if path.resolve() != path or not path.is_file() or path.stat().st_size > budget:
            raise KeyError(name)
        if expected and hashlib.sha256(path.read_bytes()).hexdigest() != expected:
            raise ValueError('Reconstruction hash mismatch')
        return path

    def read(self, identifier):
        folder = self._folder(identifier)
        report = self._read(self.media(identifier, 'report.json'), OUTPUTS['report.json'])
        base = '/api/platform/phone-vision/dense/'+identifier+'/'
        sources = self._read(folder/'input/frames.json', 500000)['frames']
        accepted = {r['index'] for r in report['accepted']}
        return {'id': identifier, 'report': report, 'surface_url': base+'surface.json',
                'mesh_url': base+'surface.ply', 'report_url': base+'report.json',
                'preview_url': base+'preview.jpg', 'support_url': base+'support.png' if report['pose_mode']=='fixed' else None,
                'sources': [{'sequence': r['sequence'], 'at': r['at'], 'accepted': r['index'] in accepted,
                             'rgb_url': base+r['rgb_file']} for r in sources]}

    def listing(self):
        items = []
        if self.root.is_dir():
            for folder in sorted(self.root.iterdir(), key=lambda p: p.name, reverse=True):
                if not IDENTIFIER.fullmatch(folder.name) or not (folder/'result/manifest.json').is_file():
                    continue
                try:
                    detail = self.read(folder.name)
                    r = detail['report']
                    items.append({'id': folder.name, 'finished_at': r['finished_at'],
                        'vertices': r['surface_vertices'], 'triangles': r['surface_triangles'],
                        'accepted_frames': len(r['accepted']), 'input_frames': r['input_frames'],
                        'pose_mode': r['pose_mode'], 'depth_fraction': r['measured_depth_fraction_median']})
                except (OSError, KeyError, ValueError, TypeError):
                    continue  # Keep corrupt/incomplete originals, never promote them.
        items.sort(key=lambda r: r['finished_at'], reverse=True)
        worker = self.service.repo/'.venv-dense/Scripts/python.exe'
        return {'items': items[:40], 'limit': 40, 'job': self.active,
                'worker_ready': worker.is_file(), 'motion_enabled': False,
                'retention': 'original_rgbd_and_results_preserved'}

    def begin(self, device):
        if device != 'iphone':
            raise ValueError('当前固定视野融合已验证iPhone RGB-D；安卓继续独立保留')
        worker = self.service.repo/'.venv-dense/Scripts/python.exe'
        if not worker.is_file():
            raise ValueError('尚未安装独立稠密重建环境')
        self.service.frame(device)  # Same original one-second freshness guard.
        with self.lock:
            if self.active and self.active['phase'] in ('capturing', 'fusing'):
                raise ValueError('已有稠密重建在进行')
            self.stop.clear()
            identifier = uuid.uuid4().hex
            folder = self.root/identifier
            (folder/'input').mkdir(parents=True)
            self.active = {'id': identifier, 'phase': 'capturing', 'frames': 0,
                           'started_at': stamp(), 'motion_commands': 0}
            atomic(folder/'job.json', self.active)
            self.thread = threading.Thread(target=self._run, args=(device, worker, folder), daemon=True,
                                           name='phone-dense-observation')
            self.thread.start()
            return dict(self.active)

    def _update(self, folder, **fields):
        with self.lock:
            self.active = {**self.active, **fields}
            atomic(folder/'job.json', self.active)

    def _run(self, device, worker, folder):
        rows, session, sequence, process = [], None, None, None
        try:
            deadline = time.monotonic()+20
            while len(rows) < 30 and time.monotonic() < deadline:
                if self.stop.wait(.15):
                    raise InterruptedError('重建已停止，已有数据保留')
                try:
                    frame = self.service.frame(device)
                except LookupError:
                    continue
                if session is not None and session != frame.session:
                    raise ValueError('采集中手机重新连接；保留本段，重新采集')
                session = frame.session
                if sequence == frame.sequence:
                    continue
                if not frame.raw or not frame.meta.get('rgbd'):
                    raise ValueError('当前镜头没有数值深度')
                if len(frame.raw) > 4194304 or len(frame.rgb) > 4000000:
                    raise ValueError('RGB-D超过有界采集大小')
                sequence = frame.sequence
                stem = f'{len(rows):04d}'
                (folder/'input'/f'{stem}.jpg').write_bytes(frame.rgb)
                (folder/'input'/f'{stem}.f32').write_bytes(frame.raw)
                rows.append({'index': len(rows), 'session': frame.session, 'sequence': sequence,
                    'at': stamp(), 'meta': frame.meta, 'rgb_file': stem+'.jpg', 'depth_file': stem+'.f32',
                    'source_age_ms': max(0, (self.service.clock()-frame.received)*1000),
                    'rgb_sha256': hashlib.sha256(frame.rgb).hexdigest(),
                    'depth_sha256': hashlib.sha256(frame.raw).hexdigest()})
                atomic(folder/'input/frames.json', {'frames': rows, 'motion_commands': 0})
                self._update(folder, frames=len(rows))
            if len(rows) < 3:
                raise ValueError('未收到足够的新鲜RGB-D帧；已有数据保留')
            self._update(folder, phase='fusing')
            env = dict(os.environ, OMP_NUM_THREADS='1', OPENBLAS_NUM_THREADS='1', MKL_NUM_THREADS='1')
            with (folder/'worker.log').open('wb') as log:
                process = subprocess.Popen([str(worker), '-m', 'deployment.phone_vision.dense',
                    '--input', str(folder/'input'), '--output', str(folder/'result'), '--pose-mode', 'fixed'],
                    cwd=str(self.service.repo), env=env, stdout=log, stderr=subprocess.STDOUT,
                    creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
                deadline = time.monotonic()+150
                while process.poll() is None:
                    if self.stop.wait(.2) or time.monotonic() > deadline:
                        raise InterruptedError('重建停止或超时；原始数据保留')
                if process.returncode:
                    raise ValueError('融合未完成；原始数据和失败回执保留在本次记录')
            self.read(folder.name)  # Publish only after its manifest verifies.
            self._update(folder, phase='complete', finished_at=stamp())
        except (OSError, ValueError, KeyError, InterruptedError) as exc:
            (folder/'failure.txt').write_text(repr(exc), encoding='utf8')
            reason='本次文件读写失败，已保存数据保留；请稍后重试' if isinstance(exc, OSError) else str(exc)
            self._update(folder, phase='failed', reason=reason, finished_at=stamp())
        finally:
            if process is not None and process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()

    def close(self):
        self.stop.set()
        if self.thread is not None and self.thread is not threading.current_thread():
            self.thread.join(timeout=8)
