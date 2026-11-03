"""Mount the fast-grasp skill inside the port-8770 Studio (page /grasp, API /api/platform/grasp)."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, RedirectResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from starlette.concurrency import run_in_threadpool

from .service import GraspService

API = '/api/platform/grasp'
PAGE = '/grasp'
STATIC = Path(__file__).with_name('static')
CSP = "default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; frame-ancestors 'self'; object-src 'none'; base-uri 'self'"


def install(app, *, data_dir, allowed_origins=None, controller='http://127.0.0.1:8765', service=None):
    service = service or GraspService(data_dir, controller)
    app.state.grasp = service
    router = APIRouter(prefix=API)
    streams = {'open': 0}

    async def body(request):
        origin = request.headers.get('origin')
        allowed = allowed_origins() if callable(allowed_origins) else (allowed_origins or ())
        if origin and origin not in allowed:
            raise HTTPException(403, '只接受已配置的平台来源')
        if request.headers.get('content-type', '').split(';', 1)[0].strip() != 'application/json':
            raise HTTPException(415, '需要JSON请求')
        content = await request.body()
        if len(content) > 8000:
            raise HTTPException(413, '请求大小超限')
        try:
            value = json.loads(content or b'{}')
        except ValueError as exc:
            raise HTTPException(422, '请求需为JSON对象') from exc
        if not isinstance(value, dict):
            raise HTTPException(422, '请求需为JSON对象')
        return value

    async def call(function, *args):
        try:
            return await run_in_threadpool(function, *args)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from None
        except KeyError as exc:
            raise HTTPException(404, str(exc).strip("'")) from None
        except OSError as exc:
            raise HTTPException(503, '本地文件暂时不可写：' + str(exc)) from None

    @router.get('/status')
    async def status():
        return await run_in_threadpool(service.status)

    @router.post('/cameras/start')
    async def cameras_start(request: Request):
        await body(request)
        return await call(service.start_cameras)

    @router.post('/cameras/stop')
    async def cameras_stop(request: Request):
        await body(request)
        return await call(service.stop_cameras)

    @router.post('/select')
    async def select(request: Request):
        payload = await body(request)
        return await call(service.select, payload.get('bbox'))

    @router.post('/propose')
    async def propose(request: Request):
        await body(request)
        return await call(service.propose)

    @router.post('/clear')
    async def clear(request: Request):
        await body(request)
        return await call(service.clear)

    @router.post('/survey')
    async def survey(request: Request):
        payload = await body(request)
        return await call(service.survey, payload.get('confirmed'))

    @router.post('/run')
    async def run(request: Request):
        payload = await body(request)
        return await call(service.run, payload.get('confirmed'), payload.get('options') or {})

    @router.post('/stop')
    async def stop(request: Request):
        await body(request)
        return await call(service.stop)

    @router.post('/config')
    async def config(request: Request):
        payload = await body(request)
        return await call(service.update_config, payload.get('changes'))

    @router.get('/runs')
    async def runs():
        return await call(service.runs)

    @router.get('/runs/{run_id}/{name}')
    async def run_file(run_id: str, name: str):
        path = await call(service.run_file, run_id, name)
        media = 'image/jpeg' if name.endswith('.jpg') else 'application/json'
        return FileResponse(path, media_type=media)

    @router.get('/frame.jpg')
    def frame():
        if not service.overlay:
            raise HTTPException(404, '尚无画面')
        return Response(service.overlay, media_type='image/jpeg')

    @app.get(PAGE + '/stream.mjpg', include_in_schema=False)
    async def stream(request: Request):
        if streams['open'] >= 3:
            raise HTTPException(429, '实时画面连接过多')

        async def frames():
            streams['open'] += 1
            last, idle = None, 0
            try:
                for _ in range(10*900):
                    if await request.is_disconnected():
                        break
                    data = service.overlay
                    if data is not None and data is not last:
                        last, idle = data, 0
                        yield b'--frame\r\nContent-Type: image/jpeg\r\nContent-Length: ' + str(len(data)).encode() + b'\r\n\r\n' + data + b'\r\n'
                    else:
                        idle += 1
                        if idle > 50 and not service.cameras_on:
                            break
                    await asyncio.sleep(0.1)
            finally:
                streams['open'] -= 1
        return StreamingResponse(frames(), media_type='multipart/x-mixed-replace; boundary=frame')

    app.include_router(router)
    app.mount(PAGE + '/assets', StaticFiles(directory=STATIC), name='grasp-assets')

    @app.get(PAGE + '/view', include_in_schema=False)
    def view():
        return FileResponse(STATIC/'index.html', headers={'Content-Security-Policy': CSP})

    @app.get(PAGE, include_in_schema=False)
    @app.get(PAGE + '/', include_in_schema=False)
    def page():
        return RedirectResponse('/#grasp')

    return service
