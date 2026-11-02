"""Route compute jobs between the laptop and optional workstation nodes.

Workstation nodes connect outward to the Studio (register → long-poll →
result), so no inbound port is opened anywhere. Every job has a deadline;
results that arrive after it, or from a node that no longer owns the job, are
discarded. Nothing here can command robot hardware.
"""
from __future__ import annotations

import hmac
import json
import re
import secrets
import threading
import time
import uuid
from collections import defaultdict, deque
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

from . import capabilities as caps

ROUTES = ('auto', 'local', 'prefer_worker', 'worker_only')
FINAL = frozenset(('done', 'failed', 'expired', 'cancelled'))
NODE_TIMEOUT_S = 6.0
QUEUE_GRACE_S = 2.0
NODE_ID = re.compile(r'^[A-Za-z0-9_.-]{1,64}$')
HISTORY = 300


def stamp():
    return datetime.now().astimezone().isoformat()


def _limit_threads(count):
    try:
        import cv2
        cv2.setNumThreads(int(count))
    except Exception:  # noqa: BLE001 - optional tuning only
        pass


class AuthError(Exception):
    pass


class ComputeFabric:
    def __init__(self, root, *, local_threads=2, clock=time.monotonic, sweep_interval=0.25, start=True,
                 node_timeout=NODE_TIMEOUT_S, queue_grace=QUEUE_GRACE_S):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.clock = clock
        self.node_timeout = node_timeout
        self.queue_grace = queue_grace
        self.cond = threading.Condition()
        self.jobs = {}
        self.order = deque()
        self.nodes = {}
        self.executor = ThreadPoolExecutor(1, 'autonomy-local', initializer=_limit_threads, initargs=(local_threads,))
        self.stats = defaultdict(lambda: {'ok': 0, 'failed': 0, 'run_ms': deque(maxlen=50)})
        self.token_path = self.root/'worker-token.txt'
        self.token = self._load_token()
        self.log_path = self.root/'jobs.jsonl'
        self.closed = threading.Event()
        if start:
            threading.Thread(target=self._loop, args=(sweep_interval,), daemon=True, name='autonomy-sweep').start()

    # ---- security -------------------------------------------------------
    def _load_token(self):
        if self.token_path.exists():
            value = self.token_path.read_text(encoding='ascii').strip()
            if len(value) >= 32:
                return value
        value = secrets.token_urlsafe(32)
        self.token_path.write_text(value, encoding='ascii')
        return value

    def check_token(self, value):
        if not isinstance(value, str) or not hmac.compare_digest(value.encode(), self.token.encode()):
            raise AuthError('算力节点令牌无效')

    # ---- nodes ----------------------------------------------------------
    def register(self, info, address=None):
        node_id = str(info.get('node_id', ''))
        if not NODE_ID.fullmatch(node_id):
            raise ValueError('节点编号只能包含字母数字和 _.-')
        offered = info.get('capabilities') or []
        if not isinstance(offered, list) or len(offered) > 64:
            raise ValueError('能力列表无效')
        capabilities = sorted({str(c)[:64] for c in offered if isinstance(c, str) and re.fullmatch(r'[a-z0-9_.]{3,64}', c)})
        with self.cond:
            now = self.clock()
            previous = self.nodes.get(node_id, {})
            self.nodes[node_id] = {
                'node_id': node_id, 'name': str(info.get('name', node_id))[:80], 'capabilities': capabilities,
                'details': {k: v for k, v in (info.get('details') or {}).items() if isinstance(k, str)} if isinstance(info.get('details'), dict) else {},
                'resources': info.get('resources') if isinstance(info.get('resources'), dict) else {},
                'version': str(info.get('version', ''))[:40], 'max_parallel': max(1, min(8, int(info.get('max_parallel', 1)))),
                'address': address, 'registered_at': stamp(), 'last_seen': now, 'polling': previous.get('polling', 0),
                'session': uuid.uuid4().hex}
            self.cond.notify_all()
            return {'node_id': node_id, 'session': self.nodes[node_id]['session'], 'heartbeat_s': 2.0,
                    'node_timeout_s': self.node_timeout, 'max_poll_s': 20.0}

    def heartbeat(self, node_id, resources=None, running=None):
        with self.cond:
            node = self.nodes.get(node_id)
            if not node:
                raise KeyError('节点未注册')
            node['last_seen'] = self.clock()
            if isinstance(resources, dict):
                node['resources'] = resources
            if isinstance(running, list):
                node['running'] = [str(r)[:40] for r in running][:16]
            return {'ok': True}

    def _online(self, node, now):
        return node['polling'] > 0 or now-node['last_seen'] <= self.node_timeout

    def _inflight(self, node_id):
        return sum(1 for j in self.jobs.values() if j.get('node') == node_id and j['status'] == 'assigned')

    def _workers_for(self, capability, now):
        return [n for n in self.nodes.values() if capability in n['capabilities'] and self._online(n, now)]

    # ---- jobs -----------------------------------------------------------
    def _tier(self, capability):
        if capability in caps.REGISTRY:
            return caps.REGISTRY[capability]['tier']
        return 'worker'

    def submit(self, capability, payload, route='auto', deadline_s=30.0, source='api'):
        if not isinstance(capability, str) or not re.fullmatch(r'[a-z0-9_.]{3,64}', capability):
            raise ValueError('能力名称无效')
        if route not in ROUTES:
            raise ValueError('路由只能是 ' + '、'.join(ROUTES))
        if not isinstance(payload, dict):
            raise ValueError('任务参数必须是JSON对象')
        deadline_s = float(deadline_s)
        if not 0.2 <= deadline_s <= 900:
            raise ValueError('截止时间需在0.2–900秒')
        with self.cond:
            now = self.clock()
            known = capability in caps.REGISTRY or capability in caps.WORKER_ONLY or any(capability in n['capabilities'] for n in self.nodes.values())
            if not known:
                raise ValueError('未知能力：'+capability)
            if route == 'auto':
                route = {'laptop': 'local', 'either': 'prefer_worker'}.get(self._tier(capability), 'worker_only')
            job = {'id': uuid.uuid4().hex, 'capability': capability, 'route': route, 'source': str(source)[:40],
                   'status': 'new', 'node': None, 'created_at': stamp(), 'created': now, 'deadline': now+deadline_s,
                   'deadline_s': deadline_s, 'payload': payload, 'attempts': [], 'fallback': None, 'result': None,
                   'error': None, 'input': self._summarise(payload)}
            self.jobs[job['id']] = job
            self.order.append(job['id'])
            self._trim()
            self._dispatch(job, now)
            return self.public(job)

    @staticmethod
    def _summarise(payload):
        summary = {}
        for key, value in list(payload.items())[:20]:
            if isinstance(value, str):
                summary[key] = f'text:{len(value)}'
            elif isinstance(value, list):
                summary[key] = f'list:{len(value)}'
            elif isinstance(value, dict):
                summary[key] = 'object'
            else:
                summary[key] = value if isinstance(value, (int, float, bool)) or value is None else type(value).__name__
        return summary

    def _trim(self):
        while len(self.order) > HISTORY:
            oldest = self.order[0]
            job = self.jobs.get(oldest)
            if job and job['status'] not in FINAL:
                break
            self.order.popleft()
            self.jobs.pop(oldest, None)

    def _dispatch(self, job, now):
        capability, route = job['capability'], job['route']
        local_ok = capability in caps.REGISTRY
        workers = self._workers_for(capability, now)
        if route == 'local':
            if local_ok:
                return self._queue_local(job, None)
            return self._finish(job, 'failed', error='笔记本不支持该能力')
        if workers:
            job.update(status='queued', target='worker', queued_at=now)
            self.cond.notify_all()
            return None
        if route == 'prefer_worker' and local_ok:
            return self._queue_local(job, '没有在线的工作端节点')
        return self._finish(job, 'failed', error='没有在线的工作端节点提供'+capability)

    def _queue_local(self, job, reason):
        job.update(status='queued_local', node='local', target='local')
        if reason:
            job['fallback'] = reason
        self.executor.submit(self._run_local, job['id'])

    def _run_local(self, job_id):
        with self.cond:
            job = self.jobs.get(job_id)
            if not job or job['status'] != 'queued_local':
                return
            now = self.clock()
            if now > job['deadline']:
                self._finish(job, 'expired', error='排队超过截止时间，未执行')
                return
            job.update(status='running', started=now)
            payload = job['payload']
        started = time.perf_counter()
        result = error = None
        try:
            result = caps.run(job['capability'], payload)
        except Exception as exc:  # noqa: BLE001 - reported to the caller
            error = f'{type(exc).__name__}: {exc}'[:600]
        run_ms = (time.perf_counter()-started)*1000
        with self.cond:
            if job['status'] != 'running' or job['node'] != 'local':
                return
            if self.clock() > job['deadline']:
                self._finish(job, 'expired', error='计算完成晚于截止时间，结果已丢弃', run_ms=run_ms)
            else:
                self._finish(job, 'failed' if error else 'done', result=result, error=error, run_ms=run_ms)

    def _finish(self, job, status, result=None, error=None, run_ms=None):
        now = self.clock()
        job.update(status=status, result=result if status == 'done' else None, error=error, finished=now,
                   finished_at=stamp(), total_ms=round((now-job['created'])*1000, 1),
                   run_ms=None if run_ms is None else round(run_ms, 1))
        job.pop('payload', None)
        key = f"{job.get('node') or '-'}|{job['capability']}"
        if status == 'done':
            self.stats[key]['ok'] += 1
            if run_ms is not None:
                self.stats[key]['run_ms'].append(run_ms)
        elif status in ('failed', 'expired'):
            self.stats[key]['failed'] += 1
        self._log(job)
        self.cond.notify_all()

    def _log(self, job):
        entry = {k: job.get(k) for k in ('id', 'capability', 'route', 'source', 'status', 'node', 'created_at', 'finished_at',
                                         'total_ms', 'run_ms', 'fallback', 'error', 'input')}
        try:
            if self.log_path.exists() and self.log_path.stat().st_size > 2_000_000:
                self.log_path.replace(self.log_path.with_suffix('.jsonl.1'))
            with self.log_path.open('a', encoding='utf-8') as handle:
                handle.write(json.dumps(entry, ensure_ascii=False)+'\n')
        except OSError:
            pass

    def _fallback_or_fail(self, job, reason, now):
        if job['route'] == 'prefer_worker' and job['capability'] in caps.REGISTRY and job['deadline']-now > 0.2 and 'payload' in job:
            job['node'] = None
            self._queue_local(job, reason)
        else:
            self._finish(job, 'failed', error=reason)

    # ---- worker protocol ---------------------------------------------------
    def poll(self, node_id, wait_s=15.0):
        wait_s = max(0.0, min(20.0, float(wait_s)))
        with self.cond:
            node = self.nodes.get(node_id)
            if not node:
                raise KeyError('节点未注册')
            end = self.clock()+wait_s
            node['polling'] += 1
            try:
                while True:
                    now = self.clock()
                    node['last_seen'] = now
                    if self._inflight(node_id) < node['max_parallel']:
                        for job_id in self.order:
                            job = self.jobs.get(job_id)
                            if (job and job['status'] == 'queued' and job['capability'] in node['capabilities']
                                    and now < job['deadline']):
                                job.update(status='assigned', node=node_id, assigned=now, lease_until=job['deadline'])
                                job['attempts'].append({'node': node_id, 'at': stamp()})
                                return {'job_id': job['id'], 'capability': job['capability'], 'payload': job['payload'],
                                        'deadline_in_s': round(job['deadline']-now, 3)}
                    remaining = end-now
                    if remaining <= 0 or self.closed.is_set():
                        return None
                    self.cond.wait(min(remaining, 0.5))
            finally:
                node['polling'] -= 1
                node['last_seen'] = self.clock()

    def complete(self, node_id, job_id, ok, result=None, error=None, run_ms=None, retryable=False):
        with self.cond:
            now = self.clock()
            node = self.nodes.get(node_id)
            if node:
                node['last_seen'] = now
            job = self.jobs.get(str(job_id))
            if not job or job.get('node') != node_id or job['status'] != 'assigned':
                return {'accepted': False, 'reason': '任务已回退、过期或不属于该节点，结果丢弃'}
            if now > job['deadline']:
                self._finish(job, 'expired', error='工作端结果晚于截止时间，已丢弃', run_ms=run_ms)
                return {'accepted': False, 'reason': 'late'}
            if not ok and retryable:
                self._fallback_or_fail(job, '工作端无法执行：'+str(error)[:200], now)
                return {'accepted': True, 'fallback': job['status'] == 'queued_local'}
            self._finish(job, 'done' if ok else 'failed', result=result if ok else None,
                         error=None if ok else str(error)[:600], run_ms=run_ms)
            return {'accepted': True}

    # ---- supervision -------------------------------------------------------
    def sweep(self):
        with self.cond:
            now = self.clock()
            for job in [self.jobs[i] for i in list(self.order) if i in self.jobs]:
                status = job['status']
                if status in FINAL or status == 'running':
                    continue
                if now > job['deadline']:
                    self._finish(job, 'expired', error='超过截止时间')
                elif status == 'queued':
                    if not self._workers_for(job['capability'], now):
                        self._fallback_or_fail(job, '工作端离线', now)
                    elif job['route'] == 'prefer_worker' and now-job['queued_at'] > self.queue_grace and job['capability'] in caps.REGISTRY:
                        self._fallback_or_fail(job, '工作端繁忙，转笔记本执行', now)
                elif status == 'assigned':
                    node = self.nodes.get(job['node'])
                    if not node or not self._online(node, now):
                        self._fallback_or_fail(job, '工作端在执行中失联', now)

    def _loop(self, interval):
        while not self.closed.wait(interval):
            try:
                self.sweep()
            except Exception:  # noqa: BLE001 - keep supervising
                pass

    def close(self):
        self.closed.set()
        with self.cond:
            self.cond.notify_all()
        self.executor.shutdown(wait=False, cancel_futures=True)

    # ---- views ---------------------------------------------------------------
    def wait(self, job_id, wait_s):
        end = self.clock()+max(0.0, min(60.0, float(wait_s)))
        with self.cond:
            job = self.jobs.get(job_id)
            if not job:
                raise KeyError('任务不存在')
            while job['status'] not in FINAL:
                remaining = end-self.clock()
                if remaining <= 0:
                    break
                self.cond.wait(min(remaining, 0.5))
            return self.public(job)

    def public(self, job, include_result=True):
        now = self.clock()
        view = {k: v for k, v in job.items() if k not in ('payload', 'created', 'deadline', 'queued_at', 'assigned',
                                                           'started', 'finished', 'lease_until', 'result')}
        view['deadline_in_s'] = round(job['deadline']-now, 3) if job['status'] not in FINAL else None
        if include_result:
            view['result'] = job.get('result')
        return view

    def get(self, job_id):
        with self.cond:
            job = self.jobs.get(job_id)
            if not job:
                raise KeyError('任务不存在')
            return self.public(job)

    def recent(self, limit=40):
        with self.cond:
            ids = list(self.order)[-int(limit):]
            return [self.public(self.jobs[i], include_result=False) for i in reversed(ids) if i in self.jobs]

    def status(self, local_resources=None):
        with self.cond:
            now = self.clock()
            nodes = []
            for node in self.nodes.values():
                nodes.append({k: node.get(k) for k in ('node_id', 'name', 'capabilities', 'details', 'resources', 'version',
                                                       'max_parallel', 'address', 'registered_at', 'running')}
                             | {'online': self._online(node, now), 'heartbeat_age_s': round(now-node['last_seen'], 1),
                                'inflight': self._inflight(node['node_id'])})
            counts = defaultdict(int)
            for job in self.jobs.values():
                counts[job['status']] += 1
            stats = []
            for key, value in self.stats.items():
                node, capability = key.split('|', 1)
                samples = sorted(value['run_ms'])
                stats.append({'node': node, 'capability': capability, 'ok': value['ok'], 'failed': value['failed'],
                              'median_run_ms': round(samples[len(samples)//2], 1) if samples else None})
            capabilities = []
            for item in caps.describe():
                where = (['local'] if item['name'] in caps.REGISTRY else [])+[n['node_id'] for n in nodes if n['online'] and item['name'] in n['capabilities']]
                capabilities.append({**item, 'available_on': where})
            return {'nodes': nodes, 'local': {'resources': local_resources, 'executor_threads': 1},
                    'jobs': dict(counts), 'stats': stats, 'capabilities': capabilities, 'tiers': caps.TIERS,
                    'routes': {'auto': '按能力层级选择', 'local': '只在笔记本', 'prefer_worker': '优先工作端，离线/繁忙/超时回退笔记本',
                               'worker_only': '只在工作端，离线即失败'},
                    'limits': {'node_timeout_s': self.node_timeout, 'queue_grace_s': self.queue_grace}}
