"""Unified workbench. Proxies the existing controller; never opens serial ports."""
from __future__ import annotations
import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timezone
import importlib.metadata
import io
import json
import os
from pathlib import Path
import re
import time
import threading
import uuid
import zipfile

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.background import BackgroundTask

from .platform_catalog import REPO,GUIDES,CAPABILITIES,SOURCES,MEDIA,catalog
from .upstream_teleop import UpstreamTeleop,JOINTS

ROOT=Path(__file__).resolve().parent
STATIC=ROOT/'platform_static'
DATA=Path(os.environ.get('XLEROBOT_PLATFORM_DATA',str(ROOT/'platform-data')))
UPSTREAM='http://127.0.0.1:8765'
ID_RE=re.compile(r'^[a-f0-9]{32}$')
FILE_LOCK=threading.RLock()
STATE_LABELS={'integrated':'平台已接入','adaptation':'示例待适配','guide':'图文流程','reference':'社区参考'}


def stamp():return datetime.now().astimezone().isoformat()
def encode(data):return json.dumps(data,ensure_ascii=False,indent=2)
def safe_id(value):
    if not ID_RE.fullmatch(value):raise HTTPException(404,'未找到这条记录')
    return value

def atomic(path,data):
    # Windows denies replacement while another thread has an ordinary read handle.
    with FILE_LOCK:
        path.parent.mkdir(parents=True,exist_ok=True)
        tmp=path.with_suffix('.tmp');tmp.write_text(encode(data),encoding='utf-8')
        for attempt in range(4):
            try:tmp.replace(path);break
            except PermissionError:
                if attempt==3:raise
                time.sleep(.015)


def read_json(path):
    with FILE_LOCK:return json.loads(path.read_text(encoding='utf-8'))

def available_environments():
    result=[]
    for name in ['.venv-debug','.venv-robot','.venv-sim']:
        env=REPO/name
        site=env/'Lib/site-packages'
        if not site.exists():site=env/'lib'
        search_paths=[str(site)];inherited=False
        config=env/'pyvenv.cfg'
        if config.exists():
            values=dict(line.split('=',1) for line in config.read_text(encoding='utf-8').splitlines() if '=' in line)
            values={k.strip():v.strip() for k,v in values.items()}
            inherited=values.get('include-system-site-packages','').lower()=='true'
            if inherited and values.get('home'):search_paths.append(str(Path(values['home'])/'Lib/site-packages'))
        versions={}
        for path in search_paths:
            for d in importlib.metadata.distributions(path=[path]):versions.setdefault(d.metadata.get('Name','').lower().replace('_','-'),d.version)
        result.append({'name':name,'present':env.is_dir(),'includes_system_packages':inherited,'packages':{k:versions.get(k) for k in ['fastapi','httpx','lerobot','mujoco','ultralytics','torch','opencv-python','numpy']}})
    return result


class RecordRequest(BaseModel):
    name:str=Field(min_length=1,max_length=80)
    task:str=Field(default='',max_length=400)
    fps:int=Field(default=5,ge=2,le=10)

class PoseRequest(BaseModel):
    name:str=Field(min_length=1,max_length=80)
    note:str=Field(default='',max_length=500)
    source:str='runtime'

class ExperimentRequest(BaseModel):
    name:str=Field(min_length=1,max_length=80)
    policy:str='act'
    dataset:str=Field(pattern=r'^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$',max_length=100)
    steps:int=Field(default=20000,ge=100,le=1000000)
    batch_size:int=Field(default=4,ge=1,le=128)
    task:str=Field(default='',max_length=400)

class SandboxInput(BaseModel):
    axes:list[float]=Field(default_factory=lambda:[0]*6,min_length=6,max_length=6)
    buttons:list[int]=Field(default_factory=list,max_length=17)
    hat:list[int]=Field(default_factory=lambda:[0,0],min_length=2,max_length=2)
    dt:float=Field(default=.08,gt=0,le=.1)


class Workbench:
    def __init__(self,root=DATA,provider=None):
        self.root=Path(root);self.provider=provider;self.client=None
        self.recording=None;self.record_task=None;self.record_lock=asyncio.Lock()
        self.sandbox=UpstreamTeleop();self.environment=None;self.recording_error=None

    async def status(self):
        if self.provider:return await self.provider()
        try:
            response=await self.client.get(UPSTREAM+'/api/original/status',timeout=2.5)
            response.raise_for_status();return response.json()
        except (httpx.HTTPError,ValueError) as exc:
            raise HTTPException(503,'控制服务未连接：'+str(exc)) from exc

    def list_items(self,kind):
        result=[]
        folder=self.root/kind
        if not folder.exists():return result
        files=folder.glob('*/meta.json') if kind=='recordings' else folder.glob('*.json')
        for p in files:
            try:
                data=read_json(p)
                if kind=='recordings' and data.get('status')=='recording' and (not self.recording or data['id']!=self.recording['id']):
                    data['status']='interrupted';data['note']='上次服务结束，录制未正常封口；已有帧仍可回看'
                result.append(data)
            except (ValueError,OSError):continue
        return sorted(result,key=lambda x:x.get('created_at',''),reverse=True)

    def recording_meta(self,id):
        p=self.root/'recordings'/safe_id(id)/'meta.json'
        if not p.is_file():raise HTTPException(404,'轨迹不存在')
        data=read_json(p)
        if self.recording and self.recording['id']==id:return dict(self.recording)
        if data.get('status')=='recording':data['status']='interrupted'
        return data

    async def start_record(self,request):
        async with self.record_lock:
            if self.recording:raise HTTPException(409,'已有录制进行中，请先停止当前录制')
            state=await self.status()
            if not state.get('original') and not state.get('ports'):raise HTTPException(409,'没有可记录的状态')
            self.recording_error=None
            id=uuid.uuid4().hex
            self.recording={'id':id,'name':request.name.strip(),'task':request.task,'fps':request.fps,
                'created_at':stamp(),'status':'recording','frame_count':0,'errors':0,'duration_s':0.,
                'source_profiles':[],'contains_images':False,'format':'xlerobot-state-trajectory-v1',
                'native_lerobot_dataset':False,'note':'只记录状态与输入，未同步相机帧；回看不驱动实机'}
            folder=self.root/'recordings'/id;folder.mkdir(parents=True,exist_ok=True)
            atomic(folder/'meta.json',self.recording)
            self.record_task=asyncio.create_task(self.capture(id))
            return dict(self.recording)

    async def capture(self,id):
        start=time.monotonic();folder=self.root/'recordings'/id
        try:
            while self.recording and self.recording['id']==id:
                tick=time.monotonic()
                try:
                    state=await self.status()
                    profile=state.get('control_profile','unknown')
                    if profile not in self.recording['source_profiles']:self.recording['source_profiles'].append(profile)
                    frame={'t':round(time.monotonic()-start,4),'time':stamp(),'profile':profile,
                           'controller':state.get('controller',{}),'original':state.get('original'),
                           'ports':state.get('ports',[]),'gamepad':state.get('gamepad',{}),
                           'updated_at':state.get('updated_at'),'input_age_ms':state.get('input_age_ms')}
                    with (folder/'frames.jsonl').open('a',encoding='utf-8') as f:f.write(json.dumps(frame,ensure_ascii=False)+'\n')
                    self.recording['frame_count']+=1
                except (HTTPException,httpx.HTTPError,OSError):self.recording['errors']+=1
                self.recording['duration_s']=round(time.monotonic()-start,2)
                atomic(folder/'meta.json',self.recording)
                if self.recording['frame_count']>=10000 or time.monotonic()-start>1800:
                    self.recording['status']='stopped';self.recording['ended_at']=stamp();break
                await asyncio.sleep(max(.005,1/self.recording['fps']-(time.monotonic()-tick)))
        except asyncio.CancelledError:pass
        except OSError as exc:
            self.recording_error='录制存储失败：'+str(exc)
        finally:
            if self.recording and self.recording['id']==id:
                self.recording['status']='interrupted' if self.recording_error else 'stopped'
                self.recording['ended_at']=stamp()
                if self.recording_error:self.recording['note']=self.recording_error
                try:atomic(folder/'meta.json',self.recording)
                except OSError as exc:self.recording_error='录制封口失败，已有帧仍可读取：'+str(exc)
                finally:self.recording=None

    async def stop_record(self):
        async with self.record_lock:
            if not self.recording:raise HTTPException(409,'当前没有录制')
            id=self.recording['id']
            if self.record_task:
                self.record_task.cancel()
                try:await self.record_task
                except asyncio.CancelledError:pass
            # A task cancelled before its first event-loop turn cannot run finally.
            if self.recording and self.recording['id']==id:
                self.recording['status']='stopped';self.recording['ended_at']=stamp()
                atomic(self.root/'recordings'/id/'meta.json',self.recording)
                self.recording=None
            self.record_task=None
            return self.recording_meta(id)

    def frames(self,id):
        self.recording_meta(id);path=self.root/'recordings'/id/'frames.jsonl'
        frames=[]
        if path.exists():
            for line in path.read_text(encoding='utf-8').splitlines():
                try:frames.append(json.loads(line))
                except ValueError:continue
        return frames

    async def pose(self,request):
        if request.source=='sandbox':
            state={'control_profile':'sandbox','original':self.sandbox.snapshot(),'ports':[]}
        elif request.source=='runtime':state=await self.status()
        else:raise HTTPException(422,'姿态来源无效')
        id=uuid.uuid4().hex
        data={'id':id,'name':request.name.strip(),'note':request.note,'created_at':stamp(),
              'profile':state.get('control_profile'),'original':state.get('original'),
              'motors':[{'port':p['port'],'id':m['id'],'position':m['position'],'min':m['min'],'max':m['max'],'homing_raw':m.get('homing_raw')} for p in state.get('ports',[]) for m in p.get('motors',[])],
              'hardware_move_supported':False,'verified_reference_pose':False}
        atomic(self.root/'poses'/(id+'.json'),data);return data

    def sandbox_pose(self,id):
        path=self.root/'poses'/(safe_id(id)+'.json')
        if not path.is_file():raise HTTPException(404,'姿态不存在')
        data=read_json(path)
        if data.get('profile') not in ('sandbox','original_preview'):raise HTTPException(409,'实机未标定的计划姿态不能作为有效模型姿态载入；保留原始编码器供查看')
        arms=(data.get('original') or {}).get('arms',{})
        if not all(side in arms for side in ('left','right')):raise HTTPException(409,'这份记录没有模型姿态')
        q={s:{j:float(arms[s]['joints'][j]) for j in JOINTS} for s in arms}
        self.sandbox=UpstreamTeleop(q)
        original=data['original']
        self.sandbox.head=list(original.get('head_deg',[0.,0.]))
        self.sandbox.base=list(original.get('base_pose',[0.,0.,0.]))
        # Loading a pose has zero motion command even if it was saved while driving.
        self.sandbox.wheels=[0,0]
        return self.sandbox.snapshot()

    def experiment(self,request):
        if request.policy not in ('act','smolvla'):raise HTTPException(422,'本版只生成ACT与SmolVLA配置')
        id=uuid.uuid4().hex;job='experiment_'+id[:8]
        args=['lerobot-train','--dataset.repo_id='+request.dataset,'--output_dir=outputs/train/'+job,
              '--job_name='+job,'--policy.device=cuda','--batch_size='+str(request.batch_size),
              '--steps='+str(request.steps),'--wandb.enable=false','--policy.push_to_hub=false']
        args.append('--policy.type=act' if request.policy=='act' else '--policy.path=lerobot/smolvla_base')
        data={**request.model_dump(),'id':id,'created_at':stamp(),'status':'configuration_only',
              'command':' `\n  '.join(args),'args':args,'executed':False,
              'prerequisites':['原生LeRobot同步相机/动作数据','对应版本的lerobot-train与策略依赖','确认可用显存和独立评估集'],
              'note':'依据仓库教程生成，未启动训练、未下载权重、未上传数据。命令需在匹配的LeRobot环境执行。'}
        atomic(self.root/'experiments'/(id+'.json'),data);return data


service=Workbench()

@asynccontextmanager
async def lifespan(app):
    service.client=httpx.AsyncClient(trust_env=False,timeout=3)
    service.environment=available_environments()
    yield
    if service.recording:await service.stop_record()
    await service.client.aclose()

app=FastAPI(title='XLeRobot Studio',lifespan=lifespan)
app.mount('/assets',StaticFiles(directory=STATIC),name='platform-assets')

@app.middleware('http')
async def local_only(request,call_next):
    origin=request.headers.get('origin')
    if request.method not in ('GET','HEAD') and origin and origin not in (
        'http://127.0.0.1:8770','http://localhost:8770',
        'http://127.0.0.1:18770','http://localhost:18770',  # SSH loopback tunnel
    ):
        return Response('Only this local workbench may change state',403)
    response=await call_next(request)
    response.headers['Cache-Control']='no-store'
    response.headers['X-Content-Type-Options']='nosniff'
    return response

@app.get('/')
def index():return FileResponse(STATIC/'index.html')

@app.get('/api/platform/catalog')
def get_catalog():return catalog()

@app.get('/api/platform/status')
async def status():
    try:state=await service.status();connected=True;error=None
    except HTTPException as exc:state={};connected=False;error=exc.detail
    return {'upstream_connected':connected,'upstream_url':UPSTREAM,'upstream_error':error,'runtime':state,
            'recording':service.recording,'recording_error':service.recording_error,'counts':{k:len(service.list_items(k)) for k in ['recordings','poses','experiments']},
            'version':'1.0-studio','server_time':stamp(),'environment':service.environment or available_environments()}

@app.get('/api/platform/sources/{id}')
def source(id:str):
    if id not in SOURCES:raise HTTPException(404,'文档未在公开源文件索引内')
    path=REPO/SOURCES[id]
    return {'id':id,'path':SOURCES[id],'content':path.read_text(encoding='utf-8-sig'),'kind':path.suffix[1:]}

@app.get('/api/platform/media/{id}')
def media(id:str):
    if id not in MEDIA or not (REPO/MEDIA[id]).is_file():raise HTTPException(404,'图片未归档')
    return FileResponse(REPO/MEDIA[id])

@app.get('/api/platform/library')
def library():return {k:service.list_items(k) for k in ['recordings','poses','experiments']}

@app.post('/api/platform/recordings/start')
async def record_start(payload:RecordRequest):return await service.start_record(payload)

@app.post('/api/platform/recordings/stop')
async def record_stop():return await service.stop_record()

@app.get('/api/platform/recordings/{id}')
def record_data(id:str):return {'meta':service.recording_meta(id),'frames':service.frames(id)}

@app.get('/api/platform/recordings/{id}/export')
def export_record(id:str):
    meta=service.recording_meta(id);frames=service.frames(id)
    archive=io.BytesIO()
    with zipfile.ZipFile(archive,'w',zipfile.ZIP_DEFLATED) as z:
        z.writestr('manifest.json',encode(meta));z.writestr('frames.jsonl','\n'.join(json.dumps(f,ensure_ascii=False) for f in frames)+'\n')
        z.writestr('README.txt','状态轨迹；不含相机帧；不是原生LeRobot数据集。网页回看不向实机发指令。\n')
    return Response(archive.getvalue(),media_type='application/zip',headers={'Content-Disposition':f'attachment; filename="trajectory-{id[:8]}.zip"'})

@app.post('/api/platform/poses')
async def save_pose(payload:PoseRequest):return await service.pose(payload)

@app.post('/api/platform/sandbox/pose/{id}')
def sandbox_pose(id:str):return service.sandbox_pose(id)

@app.get('/api/platform/sandbox')
def sandbox():return service.sandbox.snapshot()

@app.post('/api/platform/sandbox/reset')
def sandbox_reset():service.sandbox=UpstreamTeleop();return service.sandbox.snapshot()

@app.post('/api/platform/sandbox/step')
def sandbox_step(payload:SandboxInput):
    if any(not -1<=x<=1 for x in payload.axes) or any(i not in range(17) for i in payload.buttons) or any(i not in (-1,0,1) for i in payload.hat):raise HTTPException(422,'输入超出范围')
    b=[i in payload.buttons for i in range(17)]
    return service.sandbox.step({'connected':True,'axes':payload.axes,'buttons':b,'hat':payload.hat},payload.dt)

@app.get('/api/platform/sandbox/demo/{id}')
def sandbox_demo(id:str):
    routines={
        'reach':[([0,-.8,0,-.8,0,0],[],[0,0],30),([.8,0,.8,0,0,0],[],[0,0],30),([.5,-.4,.5,-.4,0,0],[4,5],[0,0],30),([0,0,0,0,1,1],[],[0,0],25)],
        'drive':[([0]*6,[],[0,-1],25),([0]*6,[],[-1,0],30),([0]*6,[],[0,1],25)],
    }
    if id not in routines:raise HTTPException(404,'演示不存在')
    e=UpstreamTeleop();frames=[];t=0.
    for axes,buttons,hat,count in routines[id]:
        for _ in range(count):
            state=e.step({'connected':True,'axes':axes,'buttons':[i in buttons for i in range(17)],'hat':hat},.08)
            frames.append({'t':round(t,3),'profile':'sandbox_demo','original':state});t+=.08
    return {'meta':{'name':'双臂伸取示例' if id=='reach' else '差速驾驶示例','duration_s':round(t,2),'frame_count':len(frames),'source_profiles':['sandbox_demo']},'frames':frames}

@app.post('/api/platform/experiments')
def experiment(payload:ExperimentRequest):return service.experiment(payload)

@app.post('/api/platform/vision/quality')
async def quality(request:Request):
    body=await request.body()
    if len(body)>8_000_000:raise HTTPException(413,'图片需小于8MB')
    try:
        from PIL import Image
        import numpy as np
        import cv2
        with Image.open(io.BytesIO(body)) as image:
            if image.width*image.height>20_000_000:raise ValueError('图片像素过大')
            im=np.asarray(image.convert('RGB'))
        gray=cv2.cvtColor(im,cv2.COLOR_RGB2GRAY)
        bright=float(gray.mean());sharp=float(cv2.Laplacian(gray,cv2.CV_64F).var())
        dark=float((gray<20).mean());over=float((gray>240).mean())
        notes=[]
        if bright<55:notes.append('整体偏暗，建议增加柔和照明')
        if over>.2:notes.append('高亮像素较多，检查反光和过曝')
        if sharp<60:notes.append('高频细节较少，检查对焦、运动模糊或低纹理')
        if not notes:notes.append('当前统计未见明显异常，仍需检查目标是否完整入镜')
        return {'width':im.shape[1],'height':im.shape[0],'brightness':round(bright,1),'laplacian_variance':round(sharp,1),'dark_fraction':round(dark,3),'bright_fraction':round(over,3),'notes':notes,
                'method':'像素统计与Laplacian方差；阈值为启发式，非识别/抓取成功判断','saved':False}
    except (ImportError,ValueError,OSError) as exc:raise HTTPException(422,'图片无法分析：'+str(exc)) from exc

@app.get('/api/platform/camera/{camera}/frame')
async def camera_frame(camera:int):
    if camera not in (0,1,2):raise HTTPException(404,'相机编号无效')
    data=bytearray()
    try:
        async with service.client.stream('GET',UPSTREAM+f'/camera/{camera}.mjpg',timeout=4) as response:
            response.raise_for_status()
            async for chunk in response.aiter_bytes():
                data.extend(chunk);start=data.find(b'\xff\xd8');end=data.find(b'\xff\xd9',start+2) if start>=0 else -1
                if start>=0 and end>=0:return Response(bytes(data[start:end+2]),media_type='image/jpeg')
                if len(data)>4_000_000:break
    except httpx.HTTPError as exc:raise HTTPException(503,'当前相机没有可读取画面') from exc
    raise HTTPException(503,'相机未返回有效图像，请检查设备连接')

# A fixed route allowlist prevents the gateway becoming a generic HTTP proxy.
PROXY_GET={'original/status','status','events','log/download'}
PROXY_POST={'original/start','original/pause','controller/start','controller/stop','controller/resume','controller/heartbeat','controller/select','controller/settings','torque','calibration/read'}

async def proxy(request,path,stream=False):
    try:
        if stream:
            req=service.client.build_request('GET',UPSTREAM+path,timeout=httpx.Timeout(8,read=20))
            response=await service.client.send(req,stream=True)
            return StreamingResponse(response.aiter_bytes(),status_code=response.status_code,
                media_type=response.headers.get('content-type'),background=BackgroundTask(response.aclose))
        response=await service.client.request(request.method,UPSTREAM+path,content=await request.body(),
            headers={'Content-Type':request.headers.get('content-type','application/json'),'Origin':UPSTREAM},timeout=8)
        headers={k:response.headers[k] for k in ('content-disposition',) if k in response.headers}
        return Response(response.content,status_code=response.status_code,media_type=response.headers.get('content-type'),headers=headers)
    except httpx.HTTPError as exc:raise HTTPException(503,'控制服务未响应，接管状态请以控制器为准') from exc

@app.api_route('/api/{path:path}',methods=['GET','POST'])
async def controller_proxy(path:str,request:Request):
    allowed=PROXY_GET if request.method=='GET' else PROXY_POST
    if path not in allowed:raise HTTPException(404,'接口未开放')
    suffix='?'+request.url.query if request.url.query else ''
    return await proxy(request,'/api/'+path+suffix)

@app.get('/camera/{camera}.mjpg')
async def camera_proxy(camera:int,request:Request):
    if camera not in (0,1,2):raise HTTPException(404,'相机编号无效')
    return await proxy(request,f'/camera/{camera}.mjpg',True)

@app.get('/static/{path:path}')
async def runtime_static(path:str):
    # Only the exact shipped runtime files needed by the two console pages.
    if path not in ('mapping.css','original.css','original.js','app.js','style.css','bringup.css'):raise HTTPException(404)
    if path=='mapping.css':
        css=(ROOT/'static'/path).read_text(encoding='utf-8')
        # The workbench remains usable offline; inherited Google Fonts are optional.
        return Response('\n'.join(line for line in css.splitlines() if not line.lstrip().startswith('@import')),media_type='text/css')
    return FileResponse(ROOT/'static'/path)

@app.get('/runtime/{name}')
async def runtime_page(name:str):
    if name not in ('original','diagnostic'):raise HTTPException(404)
    content=(ROOT/'static'/('original.html' if name=='original' else 'index.html')).read_text(encoding='utf-8')
    content=content.replace('</head>','<link rel="stylesheet" href="/assets/embed.css"></head>')
    content=content.replace('href="http://127.0.0.1:8765/diagnostic"','href="/runtime/diagnostic"').replace('href="/original"','href="/runtime/original"')
    return HTMLResponse(content)

if __name__=='__main__':
    import uvicorn
    uvicorn.run('fabrication.assembly.debug_console.platform_app:app',host='127.0.0.1',port=8770,log_level='warning')
