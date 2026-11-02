"""Presence HTTP/SSE surface. Fixed peers, explicit media, no actuator routes."""
from __future__ import annotations

import asyncio
import ipaddress
import json
from pathlib import Path
from urllib.parse import urlsplit
import uuid

import httpx
from fastapi import APIRouter, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import Field, ValidationError
from starlette.background import BackgroundTask

from .adapters import OnboardAdapter
from .state import Contract, DEFAULT_DATA, Event, MAX_AVATAR, MAX_IMAGE, Profile, ROOT, Store, now, quality


def local_peer(url):
    if not url:
        return None
    parsed = urlsplit(url)
    if parsed.scheme != 'http' or parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path not in ('', '/'):
        raise ValueError('上游必须为本机 HTTP 服务，不接受路径或凭据')
    if parsed.hostname != 'localhost' and not ipaddress.ip_address(parsed.hostname or '').is_loopback:
        raise ValueError('展示适配只连接本机服务')
    return url.rstrip('/')


class Analysis(Contract):
    media_id: str = Field(pattern=r'^[a-f0-9]{32}\.png$')


class Message(Contract):
    id: str = Field(pattern=r'^[A-Za-z0-9_-]{1,100}$')
    message: str = Field(min_length=1, max_length=12000)


async def bounded_body(request, limit):
    data = bytearray()
    async for chunk in request.stream():
        data.extend(chunk)
        if len(data) > limit:
            raise HTTPException(413, '上传超过大小限制')
    return bytes(data)


def install(app, *, data_dir=DEFAULT_DATA, onboard_url=None, controller_url=None, allowed_origins=None):
    store = Store(data_dir)
    adapter = OnboardAdapter(store, local_peer(onboard_url))
    controller = local_peer(controller_url)
    router = APIRouter(prefix='/api/presence')
    app.state.presence = store

    @router.api_route('/{rest:path}', methods=['OPTIONS'], include_in_schema=False)
    async def no_cors(rest: str):
        raise HTTPException(403, '不接受跨站调用')

    async def guard(request, producer=False):
        origin = request.headers.get('origin')
        allowed = allowed_origins() if callable(allowed_origins) else (allowed_origins or ())
        if origin and origin not in allowed:
            raise HTTPException(403, '此来源不能修改展示状态')
        if producer:
            host = request.client.host if request.client else ''
            if host not in ('127.0.0.1', '::1', 'testclient') or origin or request.headers.get('x-presence-producer') != '1':
                raise HTTPException(403, '事件生产者必须从本机显式发布')

    def profile():
        try:
            return store.profile()
        except (OSError, ValueError, ValidationError) as exc:
            raise HTTPException(503, '展示配置损坏；原文件保留，请修复 profile.json') from exc

    @router.get('/config')
    async def get_config():
        return profile()

    @router.put('/config')
    async def put_config(value: Profile, request: Request):
        await guard(request)
        try:
            return store.save(value)
        except (OSError, ValueError) as exc:
            raise HTTPException(422, str(exc)) from exc

    @router.get('/state')
    async def state():
        connection = await adapter.refresh()
        return {'version': 1, 'profile': profile(), 'connection': connection,
                'camera_available': bool(controller), 'server_time': now(), **store.journal.read()}

    @router.get('/stream')
    async def stream(request: Request, after: int = 0, epoch: str | None = None):
        try:
            cursor = int(request.headers.get('last-event-id', str(after)))
        except ValueError:
            raise HTTPException(422, '事件游标无效') from None
        async def generate():
            nonlocal cursor, epoch
            heartbeat = 0
            while not await request.is_disconnected():
                batch = store.journal.read(cursor, epoch)
                if batch['reset']:
                    yield 'event: reset\ndata: ' + json.dumps(batch, ensure_ascii=False) + '\n\n'
                    cursor = batch['seq']
                else:
                    for event in batch['events']:
                        yield f'id: {event["seq"]}\nevent: progress\ndata: ' + json.dumps(event, ensure_ascii=False) + '\n\n'
                        cursor = event['seq']
                epoch = batch['epoch']
                heartbeat += 1
                if heartbeat >= 30:
                    yield ': keepalive\n\n'
                    heartbeat = 0
                await asyncio.sleep(.3)
        return StreamingResponse(generate(), media_type='text/event-stream',
                                 headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'})

    @router.post('/events')
    async def publish(value: Event, request: Request):
        await guard(request, producer=True)
        try:
            return store.publish(value)
        except FileNotFoundError as exc:
            raise HTTPException(404, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc

    @router.post('/media')
    @router.post('/avatar')
    async def upload(request: Request):
        await guard(request)
        avatar = request.url.path.endswith('/avatar')
        data = await bounded_body(request, MAX_AVATAR if avatar else MAX_IMAGE)
        try:
            return await asyncio.to_thread(store.put, data, avatar)
        except (ValueError, OSError) as exc:
            raise HTTPException(422, str(exc)) from exc

    @router.get('/media/{asset_id}')
    async def media(asset_id: str):
        try:
            path = store.asset(asset_id)
        except (ValueError, FileNotFoundError):
            raise HTTPException(404, '资源不存在') from None
        return FileResponse(path, media_type='model/gltf-binary' if path.suffix == '.glb' else 'image/png',
                            headers={'X-Content-Type-Options': 'nosniff'})

    @router.post('/analyze')
    async def analyze(value: Analysis, request: Request):
        await guard(request)
        try:
            path = store.asset(value.media_id)
        except (ValueError, FileNotFoundError):
            raise HTTPException(404, '图片不存在') from None
        task = uuid.uuid4().hex
        common = {'kind': 'vision', 'source': 'pixel-analysis', 'evidence': 'computed',
                  'task_id': task, 'frame_id': task, 'media_id': value.media_id}
        store.publish(Event(**common, phase='started', title='开始分析输入图片', text='计算亮度、曝光比例和清晰度；此步骤不做目标识别。'))
        try:
            metrics = await asyncio.to_thread(quality, path)
        except (OSError, ValueError):
            store.publish(Event(**common, phase='failed', title='图片分析失败', text='无法读取该图片。'))
            raise HTTPException(422, '图片无法分析') from None
        text = (f'亮度 {metrics["brightness"]}/255 · 暗部 {metrics["dark_fraction"]:.1%} · '
                f'高亮 {metrics["bright_fraction"]:.1%} · 清晰度方差 {metrics["laplacian_variance"]}\n'
                '像素统计与 Laplacian 方差为启发式结果，不代表语义识别、物体检测或抓取可用。')
        event = store.publish(Event(**common, phase='completed', title='图片质量分析完成', text=text))
        return {'event': event, 'metrics': metrics}

    @router.post('/messages')
    async def messages(value: Message, request: Request):
        await guard(request)
        try:
            return await adapter.send(value.id, value.message, profile())
        except httpx.HTTPError:
            raise HTTPException(504, '提交结果不确定。请用原请求 ID 查询；不要重复发送。') from None
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc

    @router.get('/messages/{request_id}')
    async def receipt(request_id: str):
        import re
        if not re.fullmatch(r'[A-Za-z0-9_-]{1,100}', request_id) or not adapter.url:
            raise HTTPException(404, '未找到请求')
        try:
            async with httpx.AsyncClient(trust_env=False, timeout=3) as client:
                response = await client.get(adapter.url + '/v1/messages/' + request_id)
                response.raise_for_status()
                value = response.json()
            adapter.receive(value)
            return {'id': request_id, 'status': value['status']}
        except (httpx.HTTPError, ValueError, KeyError):
            raise HTTPException(503, '暂时无法读回原请求；原 ID 已保留') from None

    @router.get('/camera/{camera}')
    async def camera(camera: int, request: Request):
        if camera not in (0, 1, 2) or not controller:
            raise HTTPException(404, '相机未配置')
        client = httpx.AsyncClient(trust_env=False, timeout=httpx.Timeout(5, read=12))
        try:
            upstream = await client.send(client.build_request('GET', controller + f'/camera/{camera}.mjpg'), stream=True)
            upstream.raise_for_status()
        except asyncio.CancelledError:
            await client.aclose()
            raise
        except httpx.HTTPError:
            await client.aclose()
            raise HTTPException(503, '相机流不可用') from None
        async def close():
            await upstream.aclose()
            await client.aclose()
        async def frames():
            try:
                async for chunk in upstream.aiter_bytes():
                    if await request.is_disconnected():
                        break
                    yield chunk
            finally:
                await close()
        return StreamingResponse(frames(), media_type=upstream.headers.get('content-type', 'multipart/x-mixed-replace; boundary=frame'),
                                 background=BackgroundTask(close))

    app.include_router(router)
    app.mount('/presence/assets', StaticFiles(directory=ROOT / 'static'), name='presence-assets')

    @app.get('/presence/', include_in_schema=False)
    async def page():
        return FileResponse(ROOT / 'static/index.html', headers={
            'X-Content-Type-Options': 'nosniff',
            # GLTFLoader's ImageBitmapLoader fetches embedded images as blob URLs.
            'Content-Security-Policy': "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' blob: data:; connect-src 'self' blob:; worker-src 'self' blob:; object-src 'none'; base-uri 'self'"})

    @app.get('/presence', include_in_schema=False)
    async def page_redirect():
        return RedirectResponse('/presence/')

    return store


def create_app(data_dir=DEFAULT_DATA, onboard_url=None, controller_url=None, port=8780):
    app = FastAPI(title='XLeRobot Presence', version='1.0')
    origins = (f'http://127.0.0.1:{port}', f'http://localhost:{port}', f'http://[::1]:{port}')
    install(app, data_dir=data_dir, onboard_url=onboard_url, controller_url=controller_url, allowed_origins=origins)

    @app.get('/', include_in_schema=False)
    async def root():
        return RedirectResponse('/presence/')

    return app
