"""Mount phone previews in the existing Studio application."""
import json
from pathlib import Path
from fastapi import APIRouter,HTTPException,Request
from fastapi.responses import FileResponse,RedirectResponse,Response
from fastapi.staticfiles import StaticFiles
from starlette.concurrency import run_in_threadpool
from .service import PhoneVision
from .evidence import Evidence, geometry

STATIC=Path(__file__).with_name('static')
API='/api/platform/phone-vision'
CSP="default-src 'self'; img-src 'self' blob:; style-src 'self'; script-src 'self'; frame-ancestors 'self'; object-src 'none'; base-uri 'self'"


def install(app,*,repo,data_dir,allowed_origins=None,service=None):
    service=service or PhoneVision(repo,data_dir)
    app.state.phone_vision=service
    evidence=Evidence(Path(data_dir)/'evidence')
    app.state.phone_evidence=evidence
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

    @router.get('/captures')
    def captures(limit:int=48):
        try:return evidence.list('captures',limit)
        except ValueError as exc:raise HTTPException(422,str(exc)) from None

    @router.get('/models')
    def models():return evidence.list('models')

    @router.post('/captures')
    async def capture(request:Request):
        value=await body(request)
        if set(value)-{'device','session','sequence','label'} or not {'device','session','sequence'}<=set(value):raise HTTPException(422,'需指定设备和本次画面')
        if not isinstance(value['session'],str) or not 1<=len(value['session'])<=128 or type(value['sequence']) is not int or value['sequence']<1:raise HTTPException(422,'需指定确切画面会话和序号')
        try:
            frame=service.frame(value['device'],value['session'],value['sequence'])
            return await run_in_threadpool(evidence.capture,value['device'],frame,value.get('label',''))
        except LookupError as exc:raise HTTPException(409,str(exc)) from None
        except (ValueError,KeyError) as exc:raise HTTPException(422,str(exc)) from None

    @router.get('/evidence/{kind}/{identifier}/{name}')
    def evidence_file(kind:str,identifier:str,name:str):
        try:
            path=evidence.media(kind,identifier,name)
            media='application/json' if name.endswith('.json') else 'image/jpeg' if name.endswith('.jpg') else 'application/octet-stream'
            return FileResponse(path,media_type=media,headers={'Cache-Control':'no-store'})
        except KeyError:raise HTTPException(404,'图像或空间记录不存在') from None

    @router.get('/{device}/preview.jpg')
    def preview(device:str):
        try:
            value=service.frame(device)
            return Response(value.rgb,media_type='image/jpeg',headers={'Cache-Control':'no-store','X-Phone-Session':value.session,
                'X-Phone-Sequence':str(value.sequence),'X-Phone-Age-Ms':str(max(0,(service.clock()-value.received)*1000))})
        except LookupError as exc:raise HTTPException(409,str(exc)) from None
        except ValueError as exc:raise HTTPException(422,str(exc)) from None

    @router.get('/{device}/points')
    def points(device:str,session:str,seq:int,world:bool=False):
        try:return geometry(service.frame(device,session,seq),world=world)
        except LookupError as exc:raise HTTPException(409,str(exc)) from None
        except (ValueError,KeyError) as exc:raise HTTPException(422,str(exc)) from None

    @router.post('/{device}/start')
    async def start(device:str,request:Request):return await action(request,device,'start')

    @router.post('/{device}/stop')
    async def stop(device:str,request:Request):return await action(request,device,'stop')

    @router.post('/android/view')
    async def configure_android(request:Request):
        value=await body(request)
        try:
            if not {'camera_id','rotation_deg'}<=set(value) or set(value)-{'camera_id','rotation_deg','mode'}:raise ValueError('只接受镜头、画面方向和采集模式')
            return await run_in_threadpool(service.configure_android,value['camera_id'],value['rotation_deg'],value.get('mode'))
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
                'X-Depth-Semantics':value.meta.get('depth_source','usb-float-depth'),
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
