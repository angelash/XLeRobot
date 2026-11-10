"""Mount phone previews in the existing Studio application."""
import json
from pathlib import Path
from fastapi import APIRouter,HTTPException,Request
from fastapi.responses import FileResponse,RedirectResponse,Response
from fastapi.staticfiles import StaticFiles
from starlette.concurrency import run_in_threadpool
from .service import PhoneVision

STATIC=Path(__file__).with_name('static')
API='/api/platform/phone-vision'
CSP="default-src 'self'; img-src 'self' blob:; style-src 'self'; script-src 'self'; frame-ancestors 'self'; object-src 'none'; base-uri 'self'"


def install(app,*,repo,data_dir,allowed_origins=None,service=None):
    service=service or PhoneVision(repo,data_dir)
    app.state.phone_vision=service
    router=APIRouter(prefix=API)

    async def body(request):
        origin=request.headers.get('origin')
        allowed=allowed_origins() if callable(allowed_origins) else (allowed_origins or ())
        if origin and origin not in allowed:raise HTTPException(403,'只接受本平台的操作')
        if request.headers.get('content-type','').split(';',1)[0]!='application/json':raise HTTPException(415,'需要JSON请求')
        content=bytearray()
        async for chunk in request.stream():
            content.extend(chunk)
            if len(content)>1024:raise HTTPException(413,'请求过大')
        try:
            value=json.loads(content or b'{}')
            if not isinstance(value,dict):raise ValueError('需要JSON对象')
            return value
        except (ValueError,OSError) as exc:raise HTTPException(422,str(exc)) from None

    async def action(request,device,operation):
        value=await body(request)
        try:
            if value!={}:raise ValueError('操作不接收路径或执行命令')
            return getattr(service,operation)(device)
        except (ValueError,OSError) as exc:raise HTTPException(422,str(exc)) from None

    @router.get('/status')
    def status():return service.status()

    @router.post('/{device}/start')
    async def start(device:str,request:Request):return await action(request,device,'start')

    @router.post('/{device}/stop')
    async def stop(device:str,request:Request):return await action(request,device,'stop')

    @router.post('/android/view')
    async def configure_android(request:Request):
        value=await body(request)
        try:
            if set(value)!={'camera_id','rotation_deg'}:raise ValueError('只接受镜头和画面方向')
            return await run_in_threadpool(service.configure_android,value['camera_id'],value['rotation_deg'])
        except (ValueError,OSError) as exc:raise HTTPException(422,str(exc)) from None

    @router.get('/{device}/frame.jpg')
    def frame(device:str,session:str,seq:int,kind:str='rgb'):
        if kind not in ('rgb','depth'):raise HTTPException(422,'画面类型无效')
        try:
            value=service.frame(device,session,seq)
            data=value.rgb if kind=='rgb' else value.depth
            if not data:raise ValueError('该相机不提供深度画面')
            return Response(data,media_type='image/jpeg',headers={'Cache-Control':'no-store',
                            'X-Phone-Session':value.session,'X-Phone-Sequence':str(value.sequence)})
        except LookupError as exc:raise HTTPException(409,str(exc)) from None
        except ValueError as exc:raise HTTPException(422,str(exc)) from None

    @router.get('/{device}/depth')
    def depth(device:str,session:str,seq:int,x:int,y:int):
        try:return service.depth_at(device,x,y,session,seq)
        except LookupError as exc:raise HTTPException(409,str(exc)) from None
        except ValueError as exc:raise HTTPException(422,str(exc)) from None

    @router.get('/{device}/depth.raw')
    def raw_depth(device:str,session:str,seq:int):
        try:
            value=service.frame(device,session,seq)
            if not value.raw:raise ValueError('该相机不提供数值深度')
            return Response(value.raw,media_type='application/octet-stream',headers={
                'Cache-Control':'no-store','X-Phone-Session':value.session,
                'X-Phone-Sequence':str(value.sequence),'X-Depth-Format':'float32-le-meters',
                'X-Depth-Width':str(value.meta['width']),'X-Depth-Height':str(value.meta['height'])})
        except LookupError as exc:raise HTTPException(409,str(exc)) from None
        except ValueError as exc:raise HTTPException(422,str(exc)) from None

    app.include_router(router)
    app.mount('/phone-vision/assets',StaticFiles(directory=STATIC),name='phone-vision-assets')

    @app.get('/phone-vision/view',include_in_schema=False)
    def view():return FileResponse(STATIC/'index.html',headers={'Content-Security-Policy':CSP,'Cache-Control':'no-store'})

    @app.get('/phone-vision',include_in_schema=False)
    def entry():return RedirectResponse('/#phones')
    return service
