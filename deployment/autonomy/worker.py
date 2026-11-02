"""Workstation compute node: connects out to the robot's 8770 Studio.

Run on the workstation (no inbound port):
    python -m deployment.autonomy.worker --server http://192.168.137.150:8770
The token file is copied from the laptop's private platform data over SSH.
This process never talks to the robot controller or its serial ports.
"""
from __future__ import annotations

import argparse
import base64
import io
import json
import os
import platform
import re
import subprocess
import sys
import threading
import time
import wave
from pathlib import Path

import httpx

from . import capabilities as caps
from . import resources

VERSION = 'autonomy-worker-1'
DEFAULT_TOKEN = Path(os.environ.get('LOCALAPPDATA', str(Path.home())))/'XLeRobotRemote'/'autonomy-token.txt'
# A missing cuDNN DLL aborts the whole process inside native code, so CUDA is
# first proven in a disposable child process before this node relies on it.
CUDA_PROBE = '''
import sys, numpy as np
from faster_whisper import WhisperModel
model = WhisperModel(sys.argv[1], device="cuda", compute_type="float16")
segments, _ = model.transcribe(np.zeros(8000, np.float32), language="zh")
list(segments)
print("cuda-ok")
'''


class Providers:
    """Optional workstation-only capabilities, enabled only when really available."""

    def __init__(self, llm_url=None, whisper_model='small', allow_download=False, timeout=2.0, whisper_device='auto'):
        self.llm_url = llm_url.rstrip('/') if llm_url else None
        self.whisper_model_name = whisper_model
        self.allow_download = allow_download
        self.whisper_device_mode = whisper_device
        self.whisper = None
        self.whisper_lock = threading.Lock()
        self.cuda_checked = threading.Event()
        self.cuda_ok = False
        self.details = {}
        self.available = {}
        self._probe(timeout)

    def _probe(self, timeout):
        import cv2
        if hasattr(cv2, 'calibrateHandEye'):
            self.available['vision.hand_eye'] = self.hand_eye
        try:
            from faster_whisper.utils import download_model
            path = download_model(self.whisper_model_name, local_files_only=not self.allow_download)
            self.whisper_path = path
            self.details['speech.transcribe'] = {'model': self.whisper_model_name, 'cached': True, 'device': 'probing'}
            threading.Thread(target=self._cuda_probe, daemon=True).start()
            self.available['speech.transcribe'] = self.transcribe
        except Exception as exc:  # noqa: BLE001 - optional capability
            self.details['speech.transcribe'] = {'available': False, 'reason': f'{type(exc).__name__}: {exc}'[:200]}
        if self.llm_url:
            try:
                with httpx.Client(trust_env=False, timeout=timeout) as client:
                    models = client.get(self.llm_url+'/models').raise_for_status().json().get('data', [])
                self.llm_models = [m.get('id') for m in models if m.get('id')]
                if self.llm_models:
                    self.available['llm.chat'] = self.chat
                    self.details['llm.chat'] = {'url': self.llm_url, 'models': self.llm_models[:10]}
                else:
                    self.details['llm.chat'] = {'available': False, 'reason': '服务没有可用模型'}
            except Exception as exc:  # noqa: BLE001
                self.details['llm.chat'] = {'available': False, 'reason': f'{type(exc).__name__}: {exc}'[:200]}

    def _cuda_probe(self):
        try:
            if self.whisper_device_mode == 'cpu':
                reason = '按参数使用CPU'
            else:
                done = subprocess.run([sys.executable, '-c', CUDA_PROBE, str(self.whisper_path)], capture_output=True, text=True,
                                      timeout=180, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
                self.cuda_ok = done.returncode == 0 and 'cuda-ok' in done.stdout
                tail = (done.stderr or done.stdout or '').strip().splitlines()[-2:]
                reason = 'CUDA可用' if self.cuda_ok else 'CUDA不可用，改用CPU：'+' '.join(tail)[:200]
        except (OSError, subprocess.SubprocessError) as exc:
            reason = f'CUDA探测失败，改用CPU：{type(exc).__name__}'
        finally:
            self.cuda_checked.set()
        self.details['speech.transcribe'].update(device='cuda' if self.cuda_ok else 'cpu', device_reason=reason)

    def hand_eye(self, payload):
        from .stereo import hand_eye
        return hand_eye(payload['gripper_rotations'], payload['gripper_translations'],
                        payload['target_rotations'], payload['target_translations'])

    def _load_whisper(self):
        with self.whisper_lock:
            if self.whisper is None:
                from faster_whisper import WhisperModel
                self.cuda_checked.wait(200)
                attempts = [('cuda', 'float16'), ('cpu', 'int8')] if self.cuda_ok else [('cpu', 'int8')]
                last = None
                for device, compute_type in attempts:
                    try:
                        started = time.perf_counter()
                        self.whisper = WhisperModel(self.whisper_path, device=device, compute_type=compute_type)
                        self.whisper_device = device
                        self.whisper_load_s = time.perf_counter()-started
                        break
                    except Exception as exc:  # noqa: BLE001 - try the next device
                        last = exc
                if self.whisper is None:
                    raise RuntimeError(f'识别模型加载失败：{last}')
            return self.whisper

    def transcribe(self, payload):
        audio = base64.b64decode(payload['audio_wav_b64'], validate=True)
        with wave.open(io.BytesIO(audio)) as handle:
            seconds = handle.getnframes()/float(handle.getframerate())
        if seconds > 120:
            raise ValueError('单段音频最长120秒')
        model = self._load_whisper()
        started = time.perf_counter()
        options = {'language': payload.get('language') or None, 'beam_size': max(1, min(8, int(payload.get('beam_size', 1)))),
                   'vad_filter': bool(payload.get('vad', False))}
        # Names such as the robot persona are easily replaced by homophones.
        if payload.get('initial_prompt'):
            options['initial_prompt'] = str(payload['initial_prompt'])[:200]
        if payload.get('hotwords'):
            options['hotwords'] = str(payload['hotwords'])[:200]
        segments, info = model.transcribe(io.BytesIO(audio), **options)
        parts = [{'start': round(s.start, 2), 'end': round(s.end, 2), 'text': s.text.strip(),
                  'avg_logprob': round(s.avg_logprob, 3)} for s in segments]
        elapsed = time.perf_counter()-started
        return {'text': ''.join(p['text'] for p in parts), 'segments': parts, 'language': info.language,
                'language_probability': round(info.language_probability, 3), 'audio_s': round(seconds, 2),
                'compute_s': round(elapsed, 3), 'real_time_factor': round(elapsed/max(seconds, 1e-6), 3),
                'device': self.whisper_device, 'model': self.whisper_model_name, 'prompted': 'initial_prompt' in options or 'hotwords' in options,
                'model_load_s': round(getattr(self, 'whisper_load_s', 0), 2)}

    def chat(self, payload):
        messages = payload.get('messages')
        if not isinstance(messages, list) or not messages:
            raise ValueError('messages需为非空列表')
        model = payload.get('model') or self.llm_models[0]
        body = {'model': model, 'messages': messages, 'max_tokens': max(1, min(1024, int(payload.get('max_tokens', 256)))),
                'temperature': float(payload.get('temperature', 0.2)), 'stream': False}
        started = time.perf_counter()
        with httpx.Client(trust_env=False, timeout=float(payload.get('timeout_s', 60))) as client:
            data = client.post(self.llm_url+'/chat/completions', json=body).raise_for_status().json()
        return {'content': data['choices'][0]['message'].get('content'), 'model': data.get('model', model),
                'usage': data.get('usage'), 'latency_s': round(time.perf_counter()-started, 3)}


class Worker:
    def __init__(self, server, token, node_id, name, providers, parallel=2, log=print):
        self.server = server.rstrip('/')+'/api/platform/autonomy'
        self.headers = {'X-Autonomy-Token': token}
        self.node_id, self.name = node_id, name
        self.providers = providers
        self.parallel = max(1, min(8, int(parallel)))
        self.log = log
        self.stop = threading.Event()
        self.running = {}
        self.registered = threading.Event()
        self.lock = threading.Lock()
        self.completed = 0

    def capabilities(self):
        return sorted(set(caps.REGISTRY) | set(self.providers.available))

    def _post(self, client, path, payload, timeout):
        response = client.post(self.server+path, json=payload, headers=self.headers, timeout=timeout)
        if response.status_code == 401:
            self.stop.set()
            raise PermissionError('令牌被拒绝，节点停止；请重新复制笔记本私有令牌')
        if response.status_code == 404 and path != '/worker/register':
            self.registered.clear()
        response.raise_for_status()
        return response.json()

    def register(self, client):
        info = {'node_id': self.node_id, 'name': self.name, 'capabilities': self.capabilities(), 'version': VERSION,
                'max_parallel': self.parallel, 'resources': resources.snapshot(include_gpu=True),
                'details': {'providers': self.providers.details, 'python': platform.python_version()}}
        reply = self._post(client, '/worker/register', info, 10)
        self.registered.set()
        self.log(f'已注册：{self.node_id}，能力{len(info["capabilities"])}项')
        return reply

    def execute(self, job):
        name, payload = job['capability'], job['payload']
        started = time.perf_counter()
        if name not in self.providers.available and name not in caps.REGISTRY:
            return {'ok': False, 'error': f'工作端不支持{name}', 'retryable': True, 'run_ms': 0.0}
        try:
            if name in self.providers.available:
                result = self.providers.available[name](payload)
            else:
                result = caps.run(name, payload)
            return {'ok': True, 'result': result, 'run_ms': (time.perf_counter()-started)*1000}
        except (ImportError, RuntimeError, OSError) as exc:
            return {'ok': False, 'error': f'{type(exc).__name__}: {exc}'[:600], 'retryable': True,
                    'run_ms': (time.perf_counter()-started)*1000}
        except Exception as exc:  # noqa: BLE001 - deterministic input error
            return {'ok': False, 'error': f'{type(exc).__name__}: {exc}'[:600], 'retryable': False,
                    'run_ms': (time.perf_counter()-started)*1000}

    def poll_loop(self, index):
        backoff = 1.0
        with httpx.Client(trust_env=False) as client:
            while not self.stop.is_set():
                try:
                    if not self.registered.is_set():
                        with self.lock:
                            if not self.registered.is_set():
                                self.register(client)
                    reply = self._post(client, '/worker/poll', {'node_id': self.node_id, 'wait_s': 15}, 30)
                    backoff = 1.0
                    job = reply.get('job')
                    if not job:
                        continue
                    self.running[job['job_id']] = job['capability']
                    try:
                        outcome = self.execute(job)
                    finally:
                        self.running.pop(job['job_id'], None)
                    answer = self._post(client, '/worker/result', {'node_id': self.node_id, 'job_id': job['job_id'], **outcome}, 30)
                    self.completed += 1
                    self.log(f"{job['capability']} → {'完成' if outcome['ok'] else '失败'} {outcome['run_ms']:.0f}ms"
                             + ('' if answer.get('accepted') else f"（服务端未采用：{answer.get('reason')}）"))
                except PermissionError as exc:
                    self.log(str(exc))
                    return
                except (httpx.HTTPError, ValueError) as exc:
                    self.log(f'连接问题，{backoff:.0f}秒后重试：{type(exc).__name__}')
                    self.stop.wait(backoff)
                    backoff = min(10.0, backoff*2)

    def heartbeat_loop(self):
        with httpx.Client(trust_env=False) as client:
            while not self.stop.wait(2.0):
                if not self.registered.is_set():
                    continue
                try:
                    self._post(client, '/worker/heartbeat', {'node_id': self.node_id, 'resources': resources.snapshot(include_gpu=True),
                                                            'running': list(self.running)}, 5)
                except PermissionError:
                    return
                except (httpx.HTTPError, ValueError):
                    pass

    def run(self, duration=None):
        threads = [threading.Thread(target=self.poll_loop, args=(i,), daemon=True) for i in range(self.parallel)]
        threads.append(threading.Thread(target=self.heartbeat_loop, daemon=True))
        for thread in threads:
            thread.start()
        end = time.monotonic()+duration if duration else None
        try:
            while not self.stop.is_set() and (end is None or time.monotonic() < end):
                time.sleep(0.5)
        except KeyboardInterrupt:
            pass
        finally:
            self.stop.set()
            for thread in threads:
                thread.join(timeout=35)
        return self.completed


def main(argv=None):
    parser = argparse.ArgumentParser(description='XLeRobot workstation compute node')
    parser.add_argument('--server', default='http://192.168.137.150:8770')
    parser.add_argument('--token-file', default=str(DEFAULT_TOKEN))
    parser.add_argument('--node-id', default=re.sub(r'[^A-Za-z0-9_.-]', '-', platform.node())[:64] or 'workstation')
    parser.add_argument('--parallel', type=int, default=2)
    parser.add_argument('--llm-url', default='http://127.0.0.1:11434/v1', help='OpenAI兼容本地模型服务；不可用时自动不注册')
    parser.add_argument('--whisper-model', default='small')
    parser.add_argument('--whisper-device', choices=('auto', 'cpu'), default='auto')
    parser.add_argument('--cuda-dll-dir', action='append', default=[], help='cuDNN/cuBLAS所在目录，可多次指定')
    parser.add_argument('--allow-model-download', action='store_true')
    parser.add_argument('--duration', type=float, default=None, help='运行指定秒数后退出（测试用）')
    args = parser.parse_args(argv)
    for folder in args.cuda_dll_dir:
        os.environ['PATH'] = folder+os.pathsep+os.environ.get('PATH', '')
        if hasattr(os, 'add_dll_directory'):
            os.add_dll_directory(folder)
    token = Path(args.token_file).read_text(encoding='ascii').strip()
    providers = Providers(args.llm_url or None, args.whisper_model, args.allow_model_download, whisper_device=args.whisper_device)
    worker = Worker(args.server, token, args.node_id, platform.node(), providers, args.parallel,
                    log=lambda m: print(time.strftime('%H:%M:%S'), m, flush=True))
    print(json.dumps({'node_id': args.node_id, 'capabilities': worker.capabilities(), 'providers': providers.details},
                     ensure_ascii=False), flush=True)
    worker.run(args.duration)


if __name__ == '__main__':
    sys.exit(main())
