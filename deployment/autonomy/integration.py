"""Mount compute routing and the learning ledger inside the port-8770 Studio."""
from __future__ import annotations

import base64
import json
import re
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.concurrency import run_in_threadpool

from . import protocols, resources
from .compute import AuthError, ComputeFabric
from .learning import Ledger
from .reflex import Reflex

API = '/api/platform/autonomy'
PAGE = '/autonomy'
STATIC = Path(__file__).with_name('static')
PRINT_ASSETS = Path(__file__).resolve().parents[2]/'docs'/'autonomy'/'assets'
CSP = "default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; frame-ancestors 'self'; object-src 'none'; base-uri 'self'"
RUN_ID = re.compile(r'^\d{8}-\d{6}$')
RUN_FILE = re.compile(r'^[A-Za-z0-9_.-]{1,96}\.(jpg|jpeg|png)$')
TOKEN_HEADER = 'x-autonomy-token'


def install(app, *, data_dir, evidence_root=None, allowed_origins=None, start=True):
    data_dir = Path(data_dir)
    fabric = ComputeFabric(data_dir/'compute', start=start)
    ledger = Ledger(data_dir/'learning', evidence_root)
    reflex = Reflex(ledger)
    app.state.autonomy = {'fabric': fabric, 'ledger': ledger}
    router = APIRouter(prefix=API)
    evidence = Path(evidence_root) if evidence_root else None

    async def body(request, limit, worker=False):
        if worker:
            try:
                fabric.check_token(request.headers.get(TOKEN_HEADER))
            except AuthError as exc:
                raise HTTPException(401, str(exc)) from None
        else:
            origin = request.headers.get('origin')
            allowed = allowed_origins() if callable(allowed_origins) else (allowed_origins or ())
            if origin and origin not in allowed:
                raise HTTPException(403, '只接受已配置的平台来源')
        if request.headers.get('content-type', '').split(';', 1)[0].strip() != 'application/json':
            raise HTTPException(415, '需要JSON请求')
        content = bytearray()
        async for chunk in request.stream():
            content.extend(chunk)
            if len(content) > limit:
                raise HTTPException(413, '请求大小超限')
        try:
            value = json.loads(content or b'{}')
        except (ValueError, UnicodeDecodeError) as exc:
            raise HTTPException(422, '请求需为JSON对象') from exc
        if not isinstance(value, dict):
            raise HTTPException(422, '请求需为JSON对象')
        return value

    def run_image(reference):
        """Read an image saved by an assistant run on this laptop, by name only."""
        run_id, name = str(reference.get('run_id', '')), str(reference.get('file', ''))
        if not evidence or not RUN_ID.fullmatch(run_id) or not RUN_FILE.fullmatch(name):
            raise ValueError('运行图片引用无效')
        matches = sorted(evidence.glob(f'*/agent-runs/{run_id}/{name}'))
        if not matches:
            raise ValueError(f'运行{run_id}中没有{name}')
        return base64.b64encode(matches[-1].read_bytes()).decode('ascii')

    def resolve(value, depth=0):
        if depth > 4:
            return value
        if isinstance(value, dict):
            if set(value) == {'run_id', 'file'}:
                return run_image(value)
            return {k: resolve(v, depth+1) for k, v in value.items()}
        if isinstance(value, list):
            return [resolve(v, depth+1) for v in value]
        return value

    def guard(function, *args):
        try:
            return function(*args)
        except KeyError as exc:
            raise HTTPException(404, str(exc).strip("'")) from None
        except (ValueError, TypeError) as exc:
            raise HTTPException(422, str(exc)) from None
        except OSError as exc:
            raise HTTPException(503, '本地数据暂时不可读写：'+str(exc)) from None

    # ---- compute ---------------------------------------------------------
    @router.get('/status')
    def status():
        return fabric.status(resources.snapshot())

    @router.get('/jobs')
    def jobs():
        return {'jobs': fabric.recent()}

    @router.get('/jobs/{job_id}')
    def job(job_id: str):
        return guard(fabric.get, job_id)

    @router.post('/jobs')
    async def submit(request: Request):
        payload = await body(request, 8_000_000)
        wait_s = float(payload.get('wait_s', 0) or 0)

        def work():
            job = fabric.submit(payload.get('capability'), resolve(payload.get('payload') or {}), payload.get('route', 'auto'),
                                payload.get('deadline_s', 30), source=str(payload.get('source', 'api')))
            return fabric.wait(job['id'], wait_s) if wait_s > 0 else job
        return await run_in_threadpool(guard, work)

    @router.post('/worker/register')
    async def register(request: Request):
        payload = await body(request, 200_000, worker=True)
        address = request.client.host if request.client else None
        return guard(fabric.register, payload, address)

    @router.post('/worker/heartbeat')
    async def heartbeat(request: Request):
        payload = await body(request, 200_000, worker=True)
        return guard(fabric.heartbeat, str(payload.get('node_id', '')), payload.get('resources'), payload.get('running'))

    @router.post('/worker/poll')
    async def poll(request: Request):
        payload = await body(request, 20_000, worker=True)
        job = await run_in_threadpool(guard, fabric.poll, str(payload.get('node_id', '')), payload.get('wait_s', 15))
        return {'job': job}

    @router.post('/worker/result')
    async def result(request: Request):
        payload = await body(request, 6_000_000, worker=True)
        return guard(fabric.complete, str(payload.get('node_id', '')), payload.get('job_id'), bool(payload.get('ok')),
                     payload.get('result'), payload.get('error'), payload.get('run_ms'), bool(payload.get('retryable')))

    # ---- learning ----------------------------------------------------------
    @router.get('/learning')
    def learning():
        return {'protocols': protocols.PROTOCOLS, 'validation': protocols.VALIDATION, 'sessions': ledger.sessions(),
                'models': ledger.models(), 'calibrations': ledger.calibrations(),
                'print_assets': sorted(p.name for p in PRINT_ASSETS.glob('*') if p.suffix in ('.pdf', '.png'))}

    @router.get('/learning/sessions/{session_id}')
    def session(session_id: str):
        return guard(ledger.session, session_id)

    @router.post('/learning/sessions')
    async def create(request: Request):
        p = await body(request, 50_000)
        return await run_in_threadpool(guard, ledger.create_session, p.get('protocol'), p.get('operator', 'assistant'),
                                       p.get('environment'), p.get('note', ''))

    @router.post('/learning/sessions/{session_id}/samples')
    async def sample(session_id: str, request: Request):
        p = await body(request, 200_000)
        return await run_in_threadpool(guard, ledger.add_sample, session_id, p.get('sample'))

    @router.post('/learning/sessions/{session_id}/import-run')
    async def import_run(session_id: str, request: Request):
        p = await body(request, 10_000)
        return await run_in_threadpool(guard, ledger.import_run, session_id, p.get('run_id'))

    @router.post('/learning/sessions/{session_id}/truth')
    async def truth(session_id: str, request: Request):
        p = await body(request, 20_000)
        return await run_in_threadpool(guard, ledger.set_truth, session_id, p.get('sample_id'), p.get('truth'))

    @router.post('/learning/sessions/{session_id}/quality')
    async def quality(session_id: str, request: Request):
        p = await body(request, 20_000)
        return await run_in_threadpool(guard, ledger.set_quality, session_id, p.get('sample_id'), p.get('quality'), p.get('reason', ''))

    @router.post('/learning/sessions/{session_id}/close')
    async def close(session_id: str, request: Request):
        p = await body(request, 10_000)
        return await run_in_threadpool(guard, ledger.close_session, session_id, p.get('status', 'closed'), p.get('reason', ''))

    @router.post('/learning/fit')
    async def fit(request: Request):
        p = await body(request, 20_000)
        return await run_in_threadpool(guard, ledger.fit, p.get('kind'), p.get('sessions'), p.get('params'))

    @router.post('/learning/reflex/train')
    async def train_reflex(request: Request):
        p = await body(request, 20_000)
        return await run_in_threadpool(guard, reflex.train, p.get('sessions'))

    @router.post('/learning/reflex/{model_id}/predict')
    async def predict_reflex(model_id: str, request: Request):
        p = await body(request, 20_000)
        return await run_in_threadpool(guard, reflex.predict, model_id, p.get('features'), p.get('scope'))

    @router.post('/learning/reflex/{model_id}/validate')
    async def validate_reflex(model_id: str, request: Request):
        p = await body(request, 20_000)
        return await run_in_threadpool(guard, reflex.validate, model_id, p.get('sessions'))

    @router.post('/learning/models/{model_id}/validate')
    async def validate(model_id: str, request: Request):
        p = await body(request, 20_000)
        return await run_in_threadpool(guard, ledger.validate, model_id, p.get('sessions'))

    @router.post('/learning/models/{model_id}/review')
    async def review(model_id: str, request: Request):
        p = await body(request, 20_000)
        return await run_in_threadpool(guard, ledger.review, model_id, p.get('action'), p.get('reviewer'), p.get('note', ''))

    @router.get('/calibrations/{item_id}')
    def calibration(item_id: str):
        return guard(ledger.calibration, item_id)

    @router.post('/calibrations')
    async def save_calibration(request: Request):
        p = await body(request, 600_000)

        def work():
            data, source = p.get('data'), {'note': str(p.get('source_note', ''))[:200]}
            if p.get('job_id'):
                job = fabric.get(str(p['job_id']))
                if job['status'] != 'done':
                    raise ValueError('只能保存已成功完成的计算结果')
                data, source = job['result'], {'job_id': job['id'], 'capability': job['capability'], 'node': job['node'],
                                               'finished_at': job.get('finished_at')}
            return ledger.save_calibration(p.get('kind'), p.get('camera'), data, source, p.get('note', ''))
        return await run_in_threadpool(guard, work)

    @router.post('/calibrations/{item_id}/review')
    async def review_calibration(item_id: str, request: Request):
        p = await body(request, 20_000)
        return await run_in_threadpool(guard, ledger.review_calibration, item_id, p.get('action'), p.get('reviewer'), p.get('note', ''))

    app.include_router(router)
    app.mount(PAGE+'/assets', StaticFiles(directory=STATIC), name='autonomy-assets')

    @app.get(PAGE+'/print/{name}', include_in_schema=False)
    def print_asset(name: str):
        path = PRINT_ASSETS/name
        if not re.fullmatch(r'[A-Za-z0-9_.-]+\.(pdf|png)', name) or not path.is_file():
            raise HTTPException(404, '没有这份打印资产')
        return FileResponse(path, headers={'X-Content-Type-Options': 'nosniff'})

    @app.get(PAGE+'/view', include_in_schema=False)
    def view():
        return FileResponse(STATIC/'index.html', headers={'Content-Security-Policy': CSP})

    @app.get(PAGE, include_in_schema=False)
    @app.get(PAGE+'/', include_in_schema=False)
    def page():
        return RedirectResponse('/#autonomy')

    return fabric, ledger
