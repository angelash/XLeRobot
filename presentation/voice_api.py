"""Exclusive short-lived browser capture lease on the existing Studio surface."""
from __future__ import annotations

import asyncio
from collections import OrderedDict
from hashlib import sha256
import ipaddress
import json
import secrets
import struct
import time
from urllib.parse import urlsplit
import uuid

import httpx
from fastapi import HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import Response
from pydantic import Field

from .state import Contract, Event
from .voice_engine import FRAME_BYTES, LocalVoiceEngine, VoiceError


def revision(profile):
    return sha256(json.dumps({'name': profile.name, 'persona': profile.persona,
                             'voice_input': profile.voice_input.model_dump()}, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:16]


class Claim(Contract):
    owner_id: str = Field(pattern=r'^[A-Za-z0-9_-]{8,100}$')
    takeover: bool = False


class Lease(Contract):
    owner_id: str = Field(pattern=r'^[A-Za-z0-9_-]{8,100}$')
    capture_epoch: str = Field(pattern=r'^[a-f0-9]{32}$')
    token: str = Field(min_length=20, max_length=100)


class Confirm(Lease):
    request_id: str = Field(pattern=r'^voice-[a-f0-9]{32}$')
    text: str = Field(min_length=1, max_length=4000)


def local_capture(request):
    """A remote workbench must never impersonate the laptop audio source."""
    host = request.client.host if request.client else ''
    try:
        local = ipaddress.ip_address(host).is_loopback
    except ValueError:
        local = host == 'testclient'
    target = request.url.hostname
    if not local or target not in ('127.0.0.1', 'localhost', '::1', 'testserver'):
        raise HTTPException(403, '请在笔记本打开 http://127.0.0.1:8770/presence/ 使用本机麦克风')
    origin = request.headers.get('origin')
    if origin:
        parsed = urlsplit(origin)
        if parsed.scheme != 'http' or parsed.netloc != request.url.netloc:
            raise HTTPException(403, '不接受跨站麦克风连接')


class VoiceInputService:
    def __init__(self, store, adapter, *, engine=None, clock=time.monotonic):
        self.store, self.adapter = store, adapter
        self.engine = engine or LocalVoiceEngine(store.root)
        self.clock = clock
        self.lock = asyncio.Lock()
        self.engine_lock = asyncio.Lock()
        self.lease = None
        self.socket = None
        self.error = None
        self.preparing = False
        self.capture_epoch = uuid.uuid4().hex
        self.profile_revision = None
        self.pending = OrderedDict()
        self.accepted = set()
        self.utterance_id = None
        self.latest_request = None
        self.ack_key = None
        self.ack_bytes = None
        self.ack_task = None
        self.last_input_state = None

    def valid(self, value):
        return self.lease and all(self.lease[k] == getattr(value, k) for k in ('owner_id', 'capture_epoch', 'token')) and self.clock() - self.lease['heartbeat'] < 3

    def status(self):
        lease = self.lease if self.lease and self.clock() - self.lease['heartbeat'] < 3 else None
        try:
            config = self.store.profile()
        except (ValueError, OSError):
            return {'enabled': False, 'active': False, 'state': 'fault', 'default_enabled': False,
                    'error': '展示配置无效，原文件已保留', 'capture_epoch': self.capture_epoch}
        return {'enabled': config.voice_input.enabled, 'active': bool(lease and self.socket),
                'state': 'preparing' if self.preparing else self.engine.state if lease else 'fault' if self.error else 'off',
                'owner_id': lease['owner_id'] if lease else None, 'capture_epoch': self.capture_epoch,
                'profile_revision': self.profile_revision, 'models_available': self.engine.available(),
                'ready': self.engine.ready, 'wake_ready': self.engine.wake_ready,
                'wake_names': self.engine.wake_names, 'unsupported_names': self.engine.unsupported,
                'error': self.error, 'sample_rate': 16000, 'frame_ms': 20, 'default_enabled': False,
                'local_url': 'http://127.0.0.1:8770/presence/', 'duplex': 'half',
                'ack_ready': bool(self.ack_bytes and self.ack_key == self._ack_identity(config))}

    def publish_status(self):
        return self.store.live.put('voice.status', self.status())

    async def infer(self, method, *args):
        async with self.engine_lock:
            task = asyncio.create_task(asyncio.to_thread(method, *args))
            try:
                return await asyncio.shield(task)
            except asyncio.CancelledError:
                # A Python cancellation cannot stop native ONNX inference.
                # Finish it before another stream/reset touches this recognizer.
                await task
                raise

    def _ack_identity(self, profile):
        return sha256((profile.voice_input.wake_ack.text + profile.speech.model_dump_json()).encode()).hexdigest()

    async def prepare_ack(self, profile):
        self.ack_bytes = None
        if not profile.speech.enabled or not profile.voice_input.wake_ack.audible or not profile.voice_input.wake_ack.text.strip():
            return
        key = self._ack_identity(profile)
        try:
            data, _ = await self.store.speech_service.synthesize(profile.voice_input.wake_ack.text, profile.speech)
            if key == self._ack_identity(self.store.profile()):
                self.ack_key, self.ack_bytes = key, data
        except Exception:
            pass  # A cache miss is visual feedback, never a synchronous wake TTS request.

    async def claim(self, value):
        async with self.lock:
            profile = self.store.profile()
            if not profile.voice_input.enabled:
                raise VoiceError('请先打开语音输入开关')
            if self.lease and self.clock() - self.lease['heartbeat'] < 3 and not value.takeover:
                raise VoiceError('另一个窗口正在采集；可显式接管')
            await self.release()
            self.preparing, self.error = True, None
            try:
                await self.infer(self.engine.prepare, profile)
            except Exception as exc:
                self.error = str(exc) if isinstance(exc, VoiceError) else '本地语音模型初始化失败，请检查模型与依赖'
                raise VoiceError(self.error) from None
            finally:
                self.preparing = False
            # Configuration might have changed while model loading was in flight.
            if revision(self.store.profile()) != revision(profile):
                raise VoiceError('角色或输入设置已变化，请重新启用')
            self.capture_epoch = uuid.uuid4().hex
            self.profile_revision = revision(profile)
            self.lease = {'owner_id': value.owner_id, 'capture_epoch': self.capture_epoch,
                          'token': secrets.token_urlsafe(32), 'heartbeat': self.clock()}
            if self.ack_task:
                self.ack_task.cancel()
            self.ack_task = asyncio.create_task(self.prepare_ack(profile))
            self.publish_status()
            return {k: self.lease[k] for k in ('owner_id', 'capture_epoch', 'token')} | {'status': self.status(), 'lease_ms': 3000}

    async def release(self):
        self.lease = None
        self.capture_epoch = uuid.uuid4().hex
        self.pending.clear()
        self.utterance_id = None
        socket, self.socket = self.socket, None
        if socket:
            try: await socket.close(code=1000, reason='采集已停止，请显式重新启用')
            except (RuntimeError, OSError): pass
        if self.engine.ready:
            await self.infer(self.engine.reset)
        self.publish_status()

    async def reconfigure(self, profile):
        if self.lease and revision(profile) != self.profile_revision:
            await self.release()
        if self.ack_key != self._ack_identity(profile):
            self.ack_bytes = None

    async def close(self):
        if self.ack_task:
            self.ack_task.cancel()
        await self.release()

    async def events(self, values, epoch):
        if not self.lease or epoch != self.capture_epoch:
            return
        for event in values:
            kind = event['kind']
            if kind == 'voice.wake':
                self.utterance_id = 'voice-' + uuid.uuid4().hex
                self.store.publish(Event(kind='system', title='听到角色名，正在倾听', source='voice-input',
                                         text=self.store.profile().name, phase='started'))
            event.update(capture_epoch=epoch, profile_revision=self.profile_revision, request_id=self.utterance_id)
            self.store.live.put(kind, event, 'transcript' if kind.startswith('asr.') else kind)
            if kind == 'asr.final' and event['text'].strip() and self.utterance_id:
                request_id = self.utterance_id
                if request_id in self.pending or request_id in self.accepted:
                    continue
                self.pending[request_id] = {'text': event['text'][:4000], 'profile': self.store.profile().model_copy(deep=True), 'epoch': epoch}
                while len(self.pending) > 2:
                    self.pending.popitem(last=False)
                self.store.publish(Event(kind='system', title='语音转写完成', text=event['text'][:4000],
                                         task_id=request_id, source='voice-input', phase='completed'))
                if not self.store.profile().voice_input.confirm_transcript:
                    await self.submit(request_id, event['text'], epoch)
            if kind in ('asr.final', 'voice.empty'):
                self.utterance_id = None
        if values or self.last_input_state != self.engine.state:
            self.last_input_state = self.engine.state
            self.publish_status()

    async def submit(self, request_id, text, epoch):
        if request_id in self.accepted and epoch == self.capture_epoch:
            return {'id': request_id, 'status': 'accepted'}
        value = self.pending.get(request_id)
        if not value or value['epoch'] != epoch or epoch != self.capture_epoch:
            raise VoiceError('转写已失效，请重新说话；旧指令不会重放')
        profile = value['profile']
        if revision(profile) != self.profile_revision:
            raise VoiceError('角色已改变，请重新说话')
        text = text.strip()
        command = text.rstrip('。.!！?？')
        commands = {'停止播报': 'stop-speech', '关闭麦克风': 'disable-input', '取消当前回答': 'cancel-reply',
                    '收起过程': 'hide-process', '展开过程': 'show-process'}
        if command in commands:
            action = commands[command]
            self.store.live.put('voice.command', {'action': action, 'request_id': request_id, 'capture_epoch': epoch})
            self.pending.pop(request_id, None)
            if action == 'disable-input':
                current = self.store.profile()
                current.voice_input.enabled = False
                self.store.save(current)
                await self.release()
            elif action == 'cancel-reply':
                active = next((v['request_id'] for v in reversed(self.store.replies.values.values()) if v['status'] == 'running'), None)
                if active:
                    await self.adapter.cancel(active)
            elif action in ('hide-process', 'show-process'):
                current = self.store.profile()
                current.layout.log_open = action == 'show-process'
                self.store.save(current)
            return {'id': request_id, 'status': 'local', 'action': action}
        if self.latest_request:
            terminal = any(e.get('task_id') == self.latest_request and e['phase'] in ('completed', 'failed', 'cancelled', 'interrupted') for e in self.store.journal.read()['events'])
            if not terminal:
                self.store.live.put('voice.error', {'text': '上一条语音仍在处理；本次转写已保留，请稍后确认发送', 'request_id': request_id})
                return {'id': request_id, 'status': 'waiting-confirmation'}
        # Mark once before awaiting HTTP: an uncertain reply is queried by ID,
        # never silently retried, even when the same final frame is delivered twice.
        self.accepted.add(request_id)
        if len(self.accepted) > 128:
            self.accepted = {request_id}
        self.latest_request = request_id
        self.pending.pop(request_id, None)
        try:
            result = await self.adapter.send(request_id, text, profile)
        except (httpx.HTTPError, ValueError):
            self.store.live.put('voice.error', {'text': '提交结果未确认，请用原请求 ID 查询，不要重复发送', 'request_id': request_id, 'uncertain': True})
            return {'id': request_id, 'status': 'uncertain'}
        self.store.live.put('voice.submitted', {'request_id': request_id, 'text': text, 'status': result['status']})
        return result

    async def connection(self, ws, lease):
        if not self.valid(lease) or self.socket:
            await ws.close(code=1008, reason='采集租约无效或已连接')
            return
        self.socket = ws
        epoch = self.capture_epoch
        frames = asyncio.Queue(maxsize=25)
        expected, received, began, last_frame = 0, 0, self.clock(), None
        await ws.send_json({'kind': 'voice.ready', **self.status()})
        self.publish_status()

        async def worker():
            while self.lease and epoch == self.capture_epoch:
                at, kind, payload = await frames.get()
                if not self.lease or epoch != self.capture_epoch:
                    return
                if self.clock() - at > .5:
                    raise VoiceError('音频处理落后，已放弃这条语音，请重说')
                if kind == 'audio':
                    values = await self.infer(self.engine.feed, payload)
                else:
                    if payload == 'begin':
                        self.pending.clear()
                        self.utterance_id = 'voice-' + uuid.uuid4().hex
                    if payload in ('cancel', 'pause', 'resume'):
                        self.utterance_id = None
                        self.pending.clear()
                    values = await self.infer(self.engine.control, payload)
                await self.events(values, epoch)

        async def receive():
            nonlocal expected, received, last_frame
            while self.valid(lease):
                try:
                    message = await asyncio.wait_for(ws.receive(), timeout=1)
                except asyncio.TimeoutError:
                    continue
                if not self.valid(lease):
                    raise VoiceError('采集租约已超时，请显式重新启用')
                if message['type'] == 'websocket.disconnect':
                    return
                if message.get('bytes') is not None:
                    raw = message['bytes']
                    if len(raw) != FRAME_BYTES + 4 or struct.unpack_from('<I', raw)[0] != expected:
                        raise VoiceError('音频格式或帧序号不连续，已放弃本条语音')
                    now = self.clock()
                    if last_frame is not None and now - last_frame > .6 and self.engine.state == 'listening':
                        raise VoiceError('音频中断，请重新说话')
                    last_frame = now
                    expected += 1
                    received += 1
                    if received > (now - began + 1) * 65:
                        raise VoiceError('音频发送超过实时速率')
                    try: frames.put_nowait((now, 'audio', raw[4:]))
                    except asyncio.QueueFull: raise VoiceError('音频缓冲已满，请重新说话') from None
                elif message.get('text') is not None:
                    if len(message['text']) > 4096:
                        raise VoiceError('控制消息过大')
                    value = json.loads(message['text'])
                    if not isinstance(value, dict):
                        raise VoiceError('采集控制消息必须为JSON对象')
                    command = value.get('op')
                    if command == 'heartbeat':
                        self.lease['heartbeat'] = self.clock()
                        await ws.send_json({'kind': 'heartbeat', 'capture_epoch': epoch})
                        continue
                    if command not in ('begin', 'end', 'cancel', 'pause', 'resume', 'ack-pause', 'ack-resume'):
                        raise VoiceError('未知的采集命令')
                    try: frames.put_nowait((self.clock(), 'control', command))
                    except asyncio.QueueFull: raise VoiceError('音频缓冲已满，请重新说话') from None
            raise VoiceError('采集心跳已超时，需显式重新启用')

        jobs = [asyncio.create_task(receive()), asyncio.create_task(worker())]
        try:
            done, _ = await asyncio.wait(jobs, return_when=asyncio.FIRST_COMPLETED)
            for job in done:
                job.result()
        except (VoiceError, ValueError, WebSocketDisconnect, RuntimeError) as exc:
            self.error = str(exc) if isinstance(exc, VoiceError) else '音频连接断开，请显式重新启用'
            self.store.live.put('voice.error', {'text': self.error, 'capture_epoch': epoch})
        finally:
            for job in jobs:
                job.cancel()
            if epoch == self.capture_epoch:
                # ASGI cancellation must not strand the lease while awaiting a
                # native inference thread or a WebSocket close handshake.
                cleanup = asyncio.create_task(self.release())
                try:
                    await asyncio.shield(cleanup)
                except asyncio.CancelledError:
                    # Cleanup owns a strong task reference until it finishes.
                    cleanup.add_done_callback(lambda task: task.exception() if not task.cancelled() else None)
                    raise
            await asyncio.gather(*jobs, return_exceptions=True)


def install_voice_input(app, router, store, adapter, guard):
    service = VoiceInputService(store, adapter)
    store.voice_input = service
    app.add_event_handler('shutdown', service.close)

    @router.get('/voice-input/status')
    async def status():
        return service.status()

    @router.post('/voice-input/claim')
    async def claim(value: Claim, request: Request):
        await guard(request)
        local_capture(request)
        try: return await service.claim(value)
        except VoiceError as exc: raise HTTPException(409, str(exc)) from None

    @router.post('/voice-input/release')
    async def release(value: Lease, request: Request):
        await guard(request)
        local_capture(request)
        if service.valid(value):
            await service.release()
        return {'state': 'off'}

    @router.post('/voice-input/confirm')
    async def confirm(value: Confirm, request: Request):
        await guard(request)
        local_capture(request)
        if not service.valid(value):
            raise HTTPException(409, '采集租约已失效')
        try: return await service.submit(value.request_id, value.text, value.capture_epoch)
        except VoiceError as exc: raise HTTPException(409, str(exc)) from None

    @router.get('/voice-input/ack')
    async def ack():
        profile = store.profile()
        if not profile.speech.enabled or not profile.voice_input.enabled or not profile.voice_input.wake_ack.audible or service.ack_key != service._ack_identity(profile) or not service.ack_bytes:
            return Response(status_code=204)
        return Response(service.ack_bytes, media_type='audio/wav', headers={'Cache-Control': 'no-store'})

    @router.websocket('/voice-input/stream')
    async def stream(ws: WebSocket):
        try:
            local_capture(ws)
        except HTTPException:
            await ws.close(code=1008)
            return
        await ws.accept()
        try:
            raw = await asyncio.wait_for(ws.receive_text(), timeout=3)
            if len(raw) > 4096:
                raise ValueError('oversized capture handshake')
            payload = json.loads(raw)
            lease = Lease.model_validate(payload)
            await service.connection(ws, lease)
        except (ValueError, asyncio.TimeoutError, WebSocketDisconnect):
            try: await ws.close(code=1008)
            except RuntimeError: pass
