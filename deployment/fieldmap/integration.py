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
from .exploration import Exploration
from .journal import SpatialJournal

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
    journal_root=Path(data_dir)/'journal'
    history = History(Path(data_dir)/'history', history_evidence_root, history_fieldtests_root, history_forward_sign,journal_root=journal_root)
    app.state.fieldmap_history = history
    exploration=Exploration(Path(data_dir)/'exploration')
    app.state.exploration=exploration
    journal=SpatialJournal(journal_root,survey,lambda:(app.state.phone_vision,app.state.phone_evidence,app.state.phone_reconstruction),forward_sign=history_forward_sign)
    app.state.spatial_journal=journal
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

    @router.get('/journal/status')
    def journal_status():return journal.status()

    @router.post('/journal/configure')
    async def journal_configure(request:Request):
        value=await body(request)
        if set(value)!={'enabled'}:raise HTTPException(422,'需要明确记录状态')
        try:
            journal.configure(value['enabled']);journal.start();return journal.status()
        except ValueError as exc:raise HTTPException(422,str(exc)) from None

    @router.post('/checkpoint')
    async def checkpoint(request:Request):
        value=await body(request)
        if value:raise HTTPException(422,'快照不接收动作参数')
        from starlette.concurrency import run_in_threadpool
        return await run_in_threadpool(survey.preserve)

    @router.get('/export')
    def export():
        return load()

    @router.get('/status')
    def status():
        return survey.status()

    @router.get('/phone-space')
    def phone_space():
        evidence=getattr(app.state,'phone_evidence',None)
        if evidence is None:raise HTTPException(503,'手机空间记录尚未接入')
        models=evidence.list('models',100)
        captures=evidence.list('captures',48)
        return {'models':models['items'],'captures':captures['items'],
                'model_limit':100,'capture_limit':48,'motion_enabled':False,
                'retention':'original_sources_preserved_no_automatic_deletion'}

    @router.get('/phone-space/coverage')
    def phone_coverage(kind:str,identifier:str):
        coverage=getattr(app.state,'phone_coverage',None)
        if coverage is None:raise HTTPException(503,'手机点阵分析尚未接入')
        try:return coverage.read(kind,identifier)
        except KeyError:raise HTTPException(404,'手机空间记录不存在') from None
        except ValueError as exc:raise HTTPException(422,str(exc)) from None
        except OSError:raise HTTPException(503,'原始空间记录暂不可读，保留原文件') from None

    @router.get('/phone-space/live')
    def phone_space_live(session:str,seq:int):
        from deployment.phone_vision.coverage import frame_coverage
        service=getattr(app.state,'phone_vision',None)
        if service is None:raise HTTPException(503,'手机尚未接入')
        try:
            frame=service.frame('android',session,seq)
            report=frame_coverage(frame.meta,frame.raw) if frame.raw else None
            return {'coverage':report,'meta':frame.meta,'session':frame.session,'sequence':frame.sequence,
                    'remaining_fresh_ms':max(0,1000-(service.clock()-frame.received)*1000),
                    'historical':False,'motion_enabled':False}
        except LookupError as exc:raise HTTPException(409,str(exc)) from None
        except ValueError as exc:raise HTTPException(422,str(exc)) from None

    @router.get('/exploration')
    def exploration_status():return exploration.status()

    @router.post('/exploration/start')
    async def exploration_start(request:Request):
        value=await body(request)
        if set(value)-{'authorization','confirmed'}:raise HTTPException(422,'无效的探索启动请求')
        try:return exploration.start(value.get('authorization'),value.get('confirmed'))
        except ValueError as exc:raise HTTPException(422,str(exc)) from None

    @router.post('/exploration/update')
    async def exploration_update(request:Request):
        value=await body(request)
        allowed={'session_id','phase','note','translation_legs','turn_attempts','wheel_path_m'}
        if set(value)-allowed or not {'session_id','phase','note'}<=set(value):raise HTTPException(422,'无效的探索状态更新')
        try:return exploration.update(**value)
        except ValueError as exc:raise HTTPException(409,str(exc)) from None

    @router.post('/exploration/stop')
    async def exploration_stop(request:Request):
        value=await body(request)
        if value:raise HTTPException(422,'停止请求不接收动作参数')
        from starlette.concurrency import run_in_threadpool
        result=await run_in_threadpool(exploration.stop,survey.controller)
        return Response(json.dumps(result,ensure_ascii=False),media_type='application/json',status_code=503 if result['errors'] else 200)

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
            # Latch exploration off before the existing controller stop calls.
            if exploration.status()['enabled']:
                result=exploration.stop(survey.controller);failures.extend(result['errors'])
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
