"""Surroundings review is a module of the existing port8770 Studio application."""
from __future__ import annotations

import json
import os
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request, Query
from fastapi.responses import FileResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles

from .store import Survey, STATIC
from .body import profile as body_profile, plan as body_plan
from .history import History

API = '/api/platform/fieldmap'
PAGE = '/fieldmap'
CSP = "default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; frame-ancestors 'self'; object-src 'none'; base-uri 'self'"


def data_directory(repo, platform_data):
    configured = os.environ.get('XLEROBOT_FIELDMAP_DATA')
    config_path = Path(repo)/'.local-deploy/studio-network.json'
    if not configured and config_path.exists():
        config = json.loads(config_path.read_text(encoding='utf-8-sig'))
        configured = config.get('fieldmap_data_dir')
    if configured:
        path = Path(configured)
        if not path.is_absolute():
            raise ValueError('fieldmap_data_dir must be an absolute local path')
        return path
    return Path(platform_data)/'fieldmap'


def history_configuration(repo):
    config_path = Path(repo)/'.local-deploy/studio-network.json'
    config = json.loads(config_path.read_text(encoding='utf-8-sig')) if config_path.exists() else {}
    fieldtests = Path(config.get('fieldtests_dir', Path(repo).parent/'FieldTests'))
    if not fieldtests.is_absolute():
        raise ValueError('fieldtests_dir must be absolute')
    direction_path = Path(repo)/'.local-deploy/chassis-direction.json'
    direction = json.loads(direction_path.read_text(encoding='utf-8-sig')) if direction_path.exists() else {}
    return {'history_evidence_root': Path(repo)/'fabrication/assembly/evidence',
            'history_fieldtests_root': fieldtests,
            'history_forward_sign': direction.get('camera_forward_sign') if direction.get('evidence') else None}


def install(app, *, data_dir, allowed_origins=None, history_evidence_root=None,
            history_fieldtests_root=None, history_forward_sign=None):
    survey = Survey(data_dir)
    app.state.fieldmap = survey
    history = History(Path(data_dir)/'history', history_evidence_root, history_fieldtests_root, history_forward_sign)
    app.state.fieldmap_history = history
    router = APIRouter(prefix=API)

    async def body(request):
        origin = request.headers.get('origin')
        allowed = allowed_origins() if callable(allowed_origins) else (allowed_origins or ())
        if origin and origin not in allowed:
            raise HTTPException(403, '只接受已配置的平台来源')
        if request.headers.get('content-type', '').split(';', 1)[0].strip() != 'application/json':
            raise HTTPException(415, '需要JSON请求')
        content = bytearray()
        async for chunk in request.stream():
            content.extend(chunk)
            if len(content) > 32000:
                raise HTTPException(413, '请求大小超限')
        try:
            value = json.loads(content)
        except (ValueError, UnicodeDecodeError) as exc:
            raise HTTPException(422, '请求需为JSON对象') from exc
        if not isinstance(value, dict):
            raise HTTPException(422, '请求需为JSON对象')
        return value

    def load(snapshot='current'):
        try:
            return survey.load(snapshot)
        except KeyError as exc:
            raise HTTPException(404, '地图快照不存在') from exc
        except (OSError, ValueError, KeyError) as exc:
            raise HTTPException(503, '观察数据暂时不可读取，原文件保留') from exc

    @router.get('/map')
    def map_data(snapshot: str = 'current'):
        return load(snapshot)

    @router.get('/snapshots')
    def snapshots():
        return survey.snapshots()

    @router.get('/export')
    def export():
        return load()

    @router.get('/status')
    def status():
        return survey.status()

    @router.get('/body')
    def robot_body():
        return body_profile()

    @router.get('/history/status')
    def history_status():
        return history.status()

    @router.post('/history/refresh')
    async def history_refresh(request: Request):
        await body(request)
        return history.refresh()

    @router.get('/history/runs')
    def history_runs(start_ms: int, end_ms: int, offset: int = Query(0, ge=0), limit: int = Query(100, ge=1, le=200)):
        try:
            return history.runs(start_ms, end_ms, offset, limit)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc

    @router.get('/history/query')
    def history_query(start_ms: int, end_ms: int, run_id: str | None = None):
        try:
            return history.query(start_ms, end_ms, run_id)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc

    @router.get('/history/photos')
    def history_photos(start_ms: int, end_ms: int, camera: str | None = None, run_id: str | None = None,
                       offset: int = Query(0, ge=0), limit: int = Query(36, ge=1, le=100), at_ms: int | None = None):
        try:
            return history.photos(start_ms, end_ms, camera, run_id, offset, limit, at_ms)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc

    @router.get('/history/nearest')
    def history_nearest(start_ms: int, end_ms: int, at_ms: int, run_id: str | None = None):
        try:
            return {'cameras': {c: history.photos(start_ms, end_ms, c, run_id, limit=1, at=at_ms)['items'] for c in ('0', '1', '2')}}
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc

    @router.post('/body/plan')
    async def robot_body_plan(request: Request):
        payload = await body(request)
        try:
            return body_plan(payload)
        except (ValueError, TypeError) as exc:
            raise HTTPException(422, str(exc)) from exc

    @router.post('/review')
    async def review(request: Request):
        payload = await body(request)
        # File replacement and locks run in a worker, not Studio's event loop.
        from starlette.concurrency import run_in_threadpool
        try:
            return await run_in_threadpool(survey.review, payload)
        except (ValueError, TypeError, KeyError) as exc:
            raise HTTPException(409, str(exc)) from exc
        except OSError as exc:
            raise HTTPException(503, '复核保存失败，请稍后重试') from exc

    @router.post('/stop')
    async def stop(request: Request):
        await body(request)
        from starlette.concurrency import run_in_threadpool
        def stop_actions():
            failures = []
            for path in ('/api/agent/stop', '/api/controller/stop'):
                try:
                    survey.controller(path, {})
                except (OSError, ValueError) as exc:
                    failures.append(str(exc))
            survey.status_at = 0
            return {'requested': True, 'errors': failures, 'status': survey.status()}
        result = await run_in_threadpool(stop_actions)
        return Response(json.dumps(result, ensure_ascii=False), status_code=503 if result['errors'] else 200,
                        media_type='application/json')

    app.include_router(router)
    app.mount(PAGE+'/assets', StaticFiles(directory=STATIC), name='fieldmap-assets')

    @app.get(PAGE+'/images/{name}', include_in_schema=False)
    def image(name: str, snapshot: str = 'current'):
        try:
            path = survey.image(name, snapshot)
            if not path.is_file():
                raise FileNotFoundError(name)
            return FileResponse(path, headers={'X-Content-Type-Options': 'nosniff'})
        except (FileNotFoundError, KeyError):
            raise HTTPException(404, '照片不在本次观察中') from None
        except (OSError, ValueError) as exc:
            raise HTTPException(503, '照片证据暂时不可读取') from exc

    @app.get(PAGE+'/history/photos/{photo_id}', include_in_schema=False)
    def history_image(photo_id: str, thumbnail: bool = False):
        try:
            raw, media = history.image(photo_id, thumbnail)
            return Response(raw, media_type=media, headers={'X-Content-Type-Options': 'nosniff', 'Cache-Control': 'private, max-age=300'})
        except (FileNotFoundError, KeyError):
            raise HTTPException(404, '历史照片不存在或原始文件已不在原处') from None
        except (OSError, ValueError, RuntimeError) as exc:
            raise HTTPException(503, '历史图片损坏或超过读取预算') from exc

    @app.get(PAGE+'/view', include_in_schema=False)
    def view():
        return FileResponse(STATIC/'index.html', headers={'Content-Security-Policy': CSP})

    @app.get(PAGE, include_in_schema=False)
    @app.get(PAGE+'/', include_in_schema=False)
    def page():
        return RedirectResponse('/#fieldmap')

    return survey
