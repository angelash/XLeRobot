"""Allowlisted readback from the observation mailbox; never a robot executor."""
from __future__ import annotations

import asyncio
import json
import time

import httpx

from .state import Event


class OnboardAdapter:
    def __init__(self, store, url=None):
        self.store = store
        self.url = url.rstrip('/') if url else None
        self.last_poll = 0
        self.connection = {'state': 'unconfigured', 'label': '智能服务未配置'}
        self.lock = asyncio.Lock()
        self.task = None

    async def start(self):
        if self.url and not self.task:
            self.task = asyncio.create_task(self.listen())

    async def stop(self):
        if self.task:
            self.task.cancel()
            try: await self.task
            except asyncio.CancelledError: pass
            self.task = None

    async def listen(self):
        while True:
            try:
                async with httpx.AsyncClient(trust_env=False, timeout=httpx.Timeout(10, read=20)) as client:
                    async with client.stream('GET', self.url + '/v1/presentation/stream') as response:
                        response.raise_for_status()
                        async for line in response.aiter_lines():
                            if not line.startswith('data: '):
                                continue
                            batch = json.loads(line[6:])
                            for value in batch.get('messages', [])[-32:]:
                                self.receive_live(value, baseline=batch.get('reset', False))
            except (httpx.HTTPError, ValueError, KeyError, TypeError):
                await self.refresh()
                await asyncio.sleep(1)

    def receive_live(self, value, baseline=False):
        status = value.get('status')
        text = value.get('reply', '')
        if status == 'running' and text:
            self.store.replies.update(value['id'], text, turn_id=value.get('turn_id'))
        elif status in ('completed', 'cancelled', 'failed', 'interrupted'):
            # Historical terminal receipts do not enter the live speech registry.
            if not baseline or self.store.replies.get(value['id']):
                old = self.store.replies.get(value['id'])
                self.store.replies.update(value['id'], text or (old or {}).get('text', ''),
                                          status=status, turn_id=value.get('turn_id'))
        if status != 'running' or not text:
            self.receive(value)

    async def refresh(self):
        if not self.url or time.monotonic() - self.last_poll < .8:
            return self.connection
        async with self.lock:
            if time.monotonic() - self.last_poll < .8:
                return self.connection
            self.last_poll = time.monotonic()
            try:
                async with httpx.AsyncClient(trust_env=False, timeout=1.5) as client:
                    response = await client.get(self.url + '/v1/presentation')
                    if response.status_code == 404:
                        health = await client.get(self.url + '/health')
                        health.raise_for_status()
                        self.connection = {'state': 'legacy', 'label': '会话桥在线 · 展示回执待升级'}
                        return self.connection
                    response.raise_for_status()
                    payload = response.json()
                self.connection = {'state': 'fault' if payload.get('fault') else 'online',
                                   'label': '智能服务异常' if payload.get('fault') else '智能服务在线',
                                   'motion_enabled': False}
                for message in payload.get('messages', [])[-32:]:
                    self.receive(message)
            except (httpx.HTTPError, ValueError, KeyError, TypeError):
                self.connection = {'state': 'offline', 'label': '智能服务未连接'}
            return self.connection

    def receive(self, value):
        request_id = str(value['id'])[:100]
        status = value['status']
        phases = {'queued': ('progress', '任务已排队'), 'running': ('started', '正在处理任务'),
                  'completed': ('completed', '任务完成'), 'failed': ('failed', '任务失败'),
                  'cancelled': ('cancelled', '任务已取消'), 'interrupted': ('interrupted', '任务被中断')}
        if status not in phases:
            return
        phase, title = phases[status]
        reply = value.get('reply', '')
        if isinstance(reply, dict):
            reply = reply.get('text', '')
        text = str(reply if status == 'completed' else value.get('message', ''))[:8000]
        if status != 'completed' and '\n用户消息：\n' in text:
            text = text.rsplit('\n用户消息：\n', 1)[-1]
        if status in ('failed', 'cancelled', 'interrupted'):
            text = '本次任务没有完成。请检查车载会话桥；不会自动重放。'
        event = Event(event_id=f'onboard-{request_id}-{status}', kind='message' if status == 'completed' else 'task',
                      phase=phase, title=title, text=text, task_id=request_id, source='onboard', evidence='reported')
        try:
            self.store.publish(event)
        except ValueError:
            pass  # An unchanged status is idempotent; never rewrite an earlier event.

    async def send(self, request_id, message, profile):
        if not self.url:
            raise ValueError('未配置车载智能服务；可以先播放演示或分析本地图片')
        async with httpx.AsyncClient(trust_env=False, timeout=5) as client:
            response = await client.post(self.url + '/v1/messages',
                                         json={'id': request_id, 'sender': 'presence', 'message': message,
                                               'presentation': {'name': profile.name, 'persona': profile.persona}})
            if 400 <= response.status_code < 500:
                raise ValueError('会话桥拒绝本次请求，请检查其队列或故障状态')
            response.raise_for_status()
            try:
                value = response.json()
            except ValueError:
                raise httpx.RemoteProtocolError('提交回执不是有效 JSON') from None
        self.receive(value)
        return {'id': request_id, 'status': value.get('status', 'queued')}

    async def cancel(self, request_id):
        if not self.url:
            raise ValueError('会话桥未连接')
        async with httpx.AsyncClient(trust_env=False, timeout=8) as client:
            response = await client.post(self.url + '/v1/messages/' + request_id + '/cancel', json={})
            response.raise_for_status()
            return response.json()
