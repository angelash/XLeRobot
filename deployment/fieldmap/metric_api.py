"""Known-pose mapping is hosted exclusively in the existing Studio application."""
from __future__ import annotations

import json
from pathlib import Path
import sqlite3

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, Response
from starlette.concurrency import run_in_threadpool

from .metric import exact
from .metric_store import Conflict, MappingStore
from . import metric_demo


PREFIX = '/api/platform/fieldmap/metric'
STATIC = Path(__file__).with_name('static')


def install(app, *, data_dir, allowed_origins=None):
    store = MappingStore(Path(data_dir)/'metric')
    app.state.metric_mapping = store
    router = APIRouter(prefix=PREFIX)

    async def payload(request, limit=256000):
        allowed = allowed_origins() if callable(allowed_origins) else (allowed_origins or ())
        origin = request.headers.get('origin')
        if origin and origin not in allowed:
            raise HTTPException(403, '只接受已配置的Studio来源')
        if request.headers.get('content-type', '').split(';', 1)[0].strip() != 'application/json':
            raise HTTPException(415, '需要JSON请求')
        raw = bytearray()
        async for chunk in request.stream():
            raw.extend(chunk)
            if len(raw) > limit:
                raise HTTPException(413, '建图请求超过大小预算')
        try:
            value = json.loads(raw)
        except (ValueError, UnicodeDecodeError):
            raise HTTPException(422, '需要有效的JSON对象') from None
        if not isinstance(value, dict):
            raise HTTPException(422, '需要JSON对象')
        return value

    async def call(function, *args, **kwargs):
        try:
            return await run_in_threadpool(function, *args, **kwargs)
        except Conflict as exc:
            raise HTTPException(409, str(exc)) from None
        except KeyError:
            raise HTTPException(404, '建图记录不存在') from None
        except (ValueError, TypeError) as exc:
            raise HTTPException(422, str(exc)) from None
        except (OSError, sqlite3.DatabaseError):
            raise HTTPException(503, '建图数据读取或写入失败，原始数据保留') from None

    def fields(value, names):
        try:
            exact(value, names, 'request')
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None

    @router.get('/status')
    async def status():
        return {'stage': 'known_pose_replay', 'localization_backend': 'not_connected',
                'live_sensor_connected': False, 'llm_calls': 0, 'motion_enabled': False,
                'automatic_clearance': False, 'scan_budget_per_session': 256,
                'required_next': ['sensor_calibration', 'continuous_localization', 'protected_robot_validation']}

    @router.get('/sessions')
    async def listing():
        return await call(store.listing)

    @router.post('/sessions', status_code=201)
    async def create(request: Request):
        return await call(store.create, await payload(request, 8000))

    @router.post('/demo', status_code=201)
    async def demo(request: Request):
        value = await payload(request, 1000)
        fields(value, ())

        def build():
            record = store.create(metric_demo.configuration(), mode='synthetic_demo')
            for packet in metric_demo.scans():
                result = store.append(record['id'], packet['sequence'], packet)
            return store.read(result['id'])
        return await call(build)

    @router.get('/sessions/{sid}')
    async def read(sid: str):
        return await call(store.read, sid)

    @router.post('/sessions/{sid}/scan')
    async def scan(sid: str, request: Request):
        value = await payload(request)
        fields(value, ('expected_revision', 'scan'))
        return await call(store.append, sid, value['expected_revision'], value['scan'])

    @router.post('/sessions/{sid}/plan')
    async def preview(sid: str, request: Request):
        value = await payload(request, 2000)
        if set(value)-{'expected_revision', 'goal_m', 'robot_radius_m'} or 'expected_revision' not in value:
            raise HTTPException(422, '需要地图revision，可选目标坐标及整车预览半径')
        return await call(store.preview, sid, value['expected_revision'], value.get('goal_m'), value.get('robot_radius_m', .6))

    @router.post('/sessions/{sid}/priors', status_code=201)
    async def import_prior(sid: str, request: Request):
        value = await payload(request, 512000)
        fields(value, ('expected_revision', 'format', 'map'))
        if value['format'] != 'valetudo_v2':
            raise HTTPException(422, '当前只接受Valetudo v2二维先验')
        return await call(store.import_prior, sid, value['expected_revision'], value['map'])

    @router.get('/sessions/{sid}/priors/{pid}')
    async def prior(sid: str, pid: str):
        return await call(store.prior, sid, pid)

    @router.get('/sessions/{sid}/export')
    async def archive(sid: str):
        value = await call(store.archive, sid)
        return Response(json.dumps(value, ensure_ascii=False, allow_nan=False), media_type='application/json',
                        headers={'Content-Disposition': f'attachment; filename="mapping-{sid}.json"'})

    @router.get('/sessions/{sid}/occupancy.pgm')
    async def pgm(sid: str):
        return Response(await call(store.pgm, sid), media_type='image/x-portable-graymap',
                        headers={'Content-Disposition': f'attachment; filename="mapping-{sid}.pgm"'})

    @router.get('/sessions/{sid}/occupancy.yaml')
    async def yaml(sid: str):
        value = await call(store.read, sid)
        grid = value['grid']
        text = (f'# Replay export; not cleared for navigation\nimage: mapping-{sid}.pgm\n'
                f'resolution: {grid["resolution_m"]}\norigin: [{grid["origin_x_m"]}, {grid["origin_y_m"]}, 0.0]\n'
                'negate: 0\noccupied_thresh: 0.65\nfree_thresh: 0.196\n')
        return Response(text, media_type='application/yaml',
                        headers={'Content-Disposition': f'attachment; filename="mapping-{sid}.yaml"'})

    app.include_router(router)

    @app.get('/fieldmap/metric/view', include_in_schema=False)
    async def view():
        return FileResponse(STATIC/'metric-view.html', headers={'Content-Security-Policy':
            "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; object-src 'none'; frame-ancestors 'self'; base-uri 'self'"})

    return store
