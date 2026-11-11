"""Storage shares Studio's process, origin policy, navigation and lifecycle."""
import json
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from .service import StorageMonitor

STATIC = Path(__file__).with_name('static')


def install(app, *, repo, data_dir, roots, allowed_origins=None, monitor=None):
    monitor = monitor or StorageMonitor(repo, data_dir, roots=roots)
    app.state.storage = monitor
    router = APIRouter(prefix='/api/platform/storage')

    @router.get('/status')
    def status():
        return monitor.status()

    @router.get('/summary')
    def summary():
        return monitor.summary()

    @router.post('/refresh')
    async def refresh(request: Request):
        allowed = allowed_origins() if callable(allowed_origins) else (allowed_origins or ())
        if request.headers.get('origin') and request.headers['origin'] not in allowed:
            raise HTTPException(403, '只接受已配置的平台来源')
        if request.headers.get('content-type', '').split(';')[0].strip() != 'application/json':
            raise HTTPException(415, '需要JSON请求')
        content = bytearray()
        async for chunk in request.stream():
            content.extend(chunk)
            if len(content) > 128:
                raise HTTPException(413, '请求过大')
        try:
            if json.loads(content) != {}:
                raise ValueError()
        except (ValueError, UnicodeDecodeError):
            raise HTTPException(422, '重新统计不接受路径或命令') from None
        return monitor.refresh()

    app.include_router(router)
    app.mount('/storage/assets', StaticFiles(directory=STATIC), name='storage-assets')

    @app.get('/storage/view', include_in_schema=False)
    def view():
        return FileResponse(STATIC / 'index.html', headers={
            'Cache-Control': 'no-store', 'Content-Security-Policy':
            "default-src 'self'; script-src 'self'; style-src 'self'; frame-ancestors 'self'; object-src 'none'; base-uri 'self'"})

    return monitor
