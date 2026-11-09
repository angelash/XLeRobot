"""Local XLeRobot hardware console: one bus owner and measured control feedback."""
from __future__ import annotations
from contextlib import asynccontextmanager
import asyncio
from pathlib import Path
import json
import subprocess
import time
import os
import threading
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from .control_runtime import ControlRuntime, LOG_DIR
from .control_mapping import CONTROL_MAP
from .original_mapping import ORIGINAL_MAP,JOINT_MAP,PRESET_MAP
from .original_runtime import OriginalRuntime, PreviewRuntime

ROOT = Path(__file__).resolve().parent
STATIC = ROOT / "static"
runtime = PreviewRuntime() if os.environ.get('XLEROBOT_PREVIEW_ONLY')=='1' else OriginalRuntime()

@asynccontextmanager
async def lifespan(app):
    runtime.start()
    yield
    runtime.close()

app = FastAPI(title="XLeRobot Hardware Desk v2", lifespan=lifespan)
app.mount('/static',StaticFiles(directory=STATIC),name='static')

@app.middleware('http')
async def local_requests(request: Request, call_next):
    origin=request.headers.get('origin')
    if request.method=='POST' and origin and origin not in ('http://127.0.0.1:8765','http://localhost:8765','http://127.0.0.1:8766','http://localhost:8766'):
        from fastapi.responses import JSONResponse
        return JSONResponse({'detail':'Only this local console may send commands'},status_code=403)
    response=await call_next(request)
    response.headers['Cache-Control']='no-store'
    return response

@app.get('/')
def index():
    return FileResponse(STATIC/'original.html')

@app.get('/diagnostic')
def diagnostic_index():
    return FileResponse(STATIC/'index.html')

@app.get('/api/status')
def status():
    return {**runtime.snapshot(),'server_pid':os.getpid(),'control_map':ORIGINAL_MAP if getattr(runtime,'control_profile','diagnostic')!='diagnostic' else CONTROL_MAP}

@app.get('/api/operator/state')
def operator_state():
    return runtime.operator_events.read()

@app.get('/api/operator/stream')
async def operator_stream(request:Request):
    async def generate():
        value=runtime.operator_events.read()
        yield 'event: operator\ndata: '+json.dumps(value,ensure_ascii=False)+'\n\n'
        while not await request.is_disconnected():
            next_value=await asyncio.to_thread(runtime.operator_events.wait,value['seq'],2)
            kind='operator' if next_value['seq']!=value['seq'] else 'heartbeat'
            value=next_value
            yield 'event: '+kind+'\ndata: '+json.dumps(value,ensure_ascii=False)+'\n\n'
    return StreamingResponse(generate(),media_type='text/event-stream',
        headers={'Cache-Control':'no-cache','X-Accel-Buffering':'no'})

@app.get('/original')
def original_index():
    return FileResponse(STATIC/'original.html')

@app.get('/api/original/status')
def original_status():
    # Status polling is not a hardware mutation. Resuming a previously verified
    # torque-off checkpoint only reopens observation after a clean restart.
    pending=None if isinstance(runtime,PreviewRuntime) else runtime.manual_calibration.recoverable
    # A hand-posing release is not a request to reopen collection.
    if pending and not pending.get('dismissed') and not runtime.manual_calibration.active and not getattr(runtime,'manual_pose_active',False):
        try:
            if runtime.controller['controller']=='offline' and not any(m['torque'] for p in runtime.report['ports'] for m in p['motors']):
                runtime.calibration_command('resume',{})
        except Exception:pass
    maps={'original_joint':JOINT_MAP,'original_preset':PRESET_MAP}
    return {**runtime.snapshot(),'control_map':maps.get(runtime.control_profile,ORIGINAL_MAP),
            'trial_control_map':JOINT_MAP,'preset_control_map':PRESET_MAP,'preview_only':isinstance(runtime,PreviewRuntime)}

class OriginalStartCommand(BaseModel):
    target:str='hardware'
    confirmed:bool=False

class WholeCalibrationCommand(BaseModel):
    confirmed:bool=False
    session_id:str|None=None
    step:str|None=None
    confirmed_keys:list[str]|None=None
    kind:str|None=None
    left_sign:int=-1
    right_sign:int=1
    forward_observed:bool=False
    left_observed:bool=False
    item:str|None=None
    wheels_verified:bool=False
    base_forward:str|None=None
    base_turn:str|None=None
    axis_overrides:dict[str,dict[str,float|int]]={}

class ControllerSettingsCommand(BaseModel):
    # Partial patch: omitted fields keep their saved values, so two panels
    # cannot overwrite each other with stale form state.
    arm_mapping:str|None=None
    joint_rate_deg_s:float|None=None
    grip_raw_s:int|None=None
    wheel_max_raw:int|None=None
    follow_delta_raw:int|None=None
    wheel_mapping:str|None=None
    base_forward:str|None=None
    base_turn:str|None=None
    axis_overrides:dict[str,dict[str,float|int]]|None=None

@app.post('/api/control-settings')
def update_control_settings(payload:ControllerSettingsCommand):
    try:return runtime.update_controller_settings(payload.model_dump(exclude_none=True))
    except Exception as exc:raise HTTPException(409,str(exc)) from exc

class LimitTestCommand(BaseModel):
    items:list[str]=[]
    confirmed:bool=False
    apply:bool=True

@app.post('/api/limit-test/start')
def start_limit_test(payload:LimitTestCommand):
    try:
        if isinstance(runtime,PreviewRuntime):raise ValueError('隔离预览不能运行实机自检')
        return runtime.limit_test.start(payload.items,payload.confirmed,payload.apply)
    except Exception as exc:
        runtime.log('limit_selftest','自检未开始：'+str(exc))
        raise HTTPException(409,str(exc)) from exc

@app.post('/api/limit-test/stop')
def stop_limit_test():
    return runtime.limit_test.stop('页面点击立即停止')

class UprightCommand(BaseModel):
    side:str
    action:str
    positions:dict[str,int]|None=None

@app.post('/api/limit-test/upright')
def limit_test_upright(payload:UprightCommand):
    try:
        if isinstance(runtime,PreviewRuntime):raise ValueError('隔离预览不操作实机')
        return runtime.limit_test.set_upright(payload.side,payload.action,payload.positions)
    except Exception as exc:
        runtime.log('limit_selftest','竖直姿态未修改：'+str(exc))
        raise HTTPException(409,str(exc)) from exc

# Assistant runs: called on the laptop itself over SSH, not proxied by Studio.
class AgentRunCommand(BaseModel):
    steps:list[dict]
    confirmed:bool=False
    label:str=''

@app.post('/api/agent/run')
def agent_run(payload:AgentRunCommand):
    try:
        if isinstance(runtime,PreviewRuntime):raise ValueError('隔离预览不操作实机')
        return runtime.agent.start(payload.steps,payload.confirmed,payload.label)
    except Exception as exc:
        runtime.log('agent','助手运行未开始：'+str(exc))
        raise HTTPException(409,str(exc)) from exc

@app.post('/api/agent/stop')
def agent_stop():
    runtime.limit_test.stop('助手运行被停止')
    return runtime.agent.snapshot()

@app.get('/api/agent/status')
def agent_status():
    return runtime.agent.snapshot()

class ManualPoseCommand(BaseModel):
    action:str
    motors:dict[str,list[int]]|None=None
    end_takeover:bool=False

@app.post('/api/manual-pose')
def manual_pose(payload:ManualPoseCommand):
    try:
        if isinstance(runtime,PreviewRuntime):raise ValueError('隔离预览不操作扭矩')
        return runtime.manual_pose(payload.action,payload.motors,payload.end_takeover)
    except Exception as exc:
        runtime.log('error','手动摆位未执行：'+str(exc))
        raise HTTPException(409,str(exc)) from exc

@app.post('/api/whole-calibration/{action}')
def whole_calibration_command(action:str,payload:WholeCalibrationCommand):
    try:
        result=runtime.calibration_command(action,payload.model_dump())
        warnings=getattr(result,'warnings',None)
        if warnings:return {**result,'operation_warnings':warnings}
        return result
    except Exception as exc:
        runtime.log('calibration_error','整机标定：'+str(exc))
        raise HTTPException(409,str(exc)) from exc

@app.post('/api/hardware/prepare')
def prepare_hardware(payload:OriginalStartCommand):
    try:return runtime.prepare_hardware(payload.confirmed)
    except Exception as exc:
        runtime.log('error','实机准备失败：'+str(exc))
        raise HTTPException(409,str(exc)) from exc

@app.post('/api/original/start')
def start_original(payload:OriginalStartCommand):
    try:
        if payload.target=='hardware':controller=runtime.begin_whole_robot(payload.confirmed)
        elif payload.target=='joint':controller=runtime.begin_trial(payload.confirmed)
        elif payload.target=='preset':controller=runtime.begin_preset(payload.confirmed)
        else:controller=runtime.begin_original(payload.target,payload.confirmed)
        return {'controller':controller}
    except Exception as exc:raise HTTPException(409,str(exc)) from exc

@app.post('/api/original/pause')
def pause_original():
    with runtime.io:
        if getattr(runtime,'control_profile','diagnostic')=='diagnostic':raise HTTPException(409,'当前为单关节调试，请使用该页面的停止操作')
        if runtime.controller['controller']!='online':raise HTTPException(409,'接管尚未开启')
        runtime._pause('页面暂停动作，接管保留')
        return {'controller':runtime.controller.copy()}

@app.get('/api/events')
def events(after: int=0):
    with runtime.event_lock:
        return {'last_seq':runtime.event_id,
                'events':[dict(x) for x in runtime.events if x['seq']>(0 if after>runtime.event_id else after)]}

@app.get('/api/log/download')
def log_download():
    path=LOG_DIR/'control-events.jsonl'
    if not path.is_file():raise HTTPException(404,'No events yet')
    return FileResponse(path,filename='xlerobot-control-events.jsonl')

class StartCommand(BaseModel):
    confirmed: bool=False

@app.post('/api/controller/start')
def start_controller(payload:StartCommand):
    try:return {'status':'ready','message':'接管已启动，等待新的LB按下','controller':runtime.begin(payload.confirmed)}
    except Exception as exc:
        runtime.log('error','启动失败：'+str(exc))
        raise HTTPException(409,str(exc)) from exc

@app.post('/api/controller/stop')
def stop_controller():
    return {'status':'stopped','controller':runtime.stop()}

@app.post('/api/controller/resume')
def resume_controller():
    try:return {'status':'ready','controller':runtime.resume(),'message':'已恢复，等待重新按住LB'}
    except Exception as exc:raise HTTPException(409,str(exc)) from exc

@app.post('/api/controller/heartbeat')
def heartbeat():
    runtime.ui_heartbeat=time.monotonic()
    return {'ok':True}

class SelectCommand(BaseModel):
    port:str
    id:int

@app.post('/api/controller/select')
def select_joint(payload:SelectCommand):
    try:
        runtime.select(payload.port,payload.id)
        return {'status':'ok'}
    except Exception as exc:raise HTTPException(409,str(exc)) from exc

class SettingsCommand(BaseModel):
    step_raw:int=Field(16,ge=4,le=32)
    speed_raw:int=Field(100,ge=40,le=150)

@app.post('/api/controller/settings')
def settings(payload:SettingsCommand):
    with runtime.io:
        if runtime.controller['armed']:raise HTTPException(409,'先松开LB再修改步长/速度')
        runtime.step_raw=payload.step_raw;runtime.speed_raw=payload.speed_raw
        runtime.log('settings','更新小步参数',**payload.model_dump())
    return {'status':'ok','settings':payload.model_dump()}

class TorqueCommand(BaseModel):
    enabled:bool
    scope:str='all'
    port:str|None=None
    id:int|None=None

@app.post('/api/torque')
def torque(payload:TorqueCommand):
    if payload.scope not in ('all','single'):raise HTTPException(400,'Unknown scope')
    if payload.scope=='single' and (payload.port is None or payload.id is None):raise HTTPException(400,'Missing motor')
    selected=None if payload.scope=='all' else {payload.port:[payload.id]}
    try:return runtime.torque(payload.enabled,selected)
    except Exception as exc:
        runtime.log('error','扭矩操作失败：'+str(exc))
        raise HTTPException(409,str(exc)) from exc

@app.post('/api/calibration/read')
def calibration_read():
    with runtime.io:
        if runtime.controller['armed']:raise HTTPException(409,'先松开LB')
        return runtime.capture_profile()

camera_health_lock = threading.Lock()


def set_camera(camera,state,frames=None):
    # Serial preparation may hold runtime.io for seconds. Camera publication
    # must continue during that time so safety observers receive fresh frames.
    with camera_health_lock:
        current={**runtime.camera_health[camera],'state':state}
        if frames is not None:current['frames']=frames
        runtime.camera_health[camera]=current

from .camera_snapshot import CameraSnapshots
camera_snapshots = CameraSnapshots()
from .obstacle_gate import PerceptionGate
runtime.perception_gate = PerceptionGate()
runtime.agent.shared_frame = lambda camera: camera_snapshots.latest(camera, max_age=1.0).jpeg


@app.post('/api/safety/perception')
async def safety_perception(request: Request):
    # Loopback-only service; the Studio perception process posts one message per frame.
    try:
        payload = await request.json()
        return runtime.perception_gate.update(payload)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@app.get('/api/safety/perception')
def safety_perception_status():
    return runtime.perception_gate.snapshot()


# Vision-guided skills (Studio /grasp): bounded steps inside this serial owner.
from .servo_session import ServoSession


def servo():
    session = getattr(runtime, 'servo', None)
    if session is None or session.runtime is not runtime:
        session = runtime.servo = ServoSession(runtime, LOG_DIR/'servo-sessions', authorization_root=LOG_DIR.parent)
    return session


class ServoStartCommand(BaseModel):
    label: str = ''
    confirmed: bool = False
    lease_s: float = 4.0
    stationary_base: bool = False


class ServoStepCommand(BaseModel):
    session_id: str
    targets: dict[str, float]
    torque: int | None = None
    speed_deg: float = 12
    label: str = ''


class ServoRefCommand(BaseModel):
    session_id: str
    reason: str = '完成'


class ServoBaseCommand(BaseModel):
    session_id: str
    distance_m: float
    authorization: str
    max_raw: int = 160
    label: str = ''
    forward_clear: bool = False


class ServoStopCommand(BaseModel):
    reason: str = '页面点击立即停止'


class ReverseGrantCommand(BaseModel):
    authorization: str
    distance_m: float
    confirmed: bool = False
    previous_distance_m: float | None = None
    extension_authorization: str = ''


@app.post('/api/servo/base-authorization')
def servo_base_authorization(payload: ReverseGrantCommand):
    try:
        if isinstance(runtime, PreviewRuntime):
            raise ValueError('隔离预览不登记实机授权')
        return servo().grant_reverse(payload.authorization, payload.distance_m, payload.confirmed,
                                     payload.previous_distance_m, payload.extension_authorization)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc


@app.post('/api/servo/session')
def servo_start(payload: ServoStartCommand):
    try:
        if isinstance(runtime, PreviewRuntime):
            raise ValueError('隔离预览不操作实机')
        return servo().start(payload.label, payload.confirmed, payload.lease_s, payload.stationary_base)
    except Exception as exc:
        runtime.log('servo', '抓取会话未开始：'+str(exc))
        raise HTTPException(409, str(exc)) from exc


@app.post('/api/servo/step')
def servo_step(payload: ServoStepCommand):
    try:
        return servo().step(payload.session_id, payload.targets, payload.torque, payload.speed_deg, payload.label)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc


@app.post('/api/servo/hold')
def servo_hold(payload: ServoRefCommand):
    try:
        return servo().hold(payload.session_id)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc


@app.post('/api/servo/base')
def servo_base(payload: ServoBaseCommand):
    try:
        return servo().base(payload.session_id, payload.distance_m, payload.authorization, payload.max_raw, payload.label,
                            payload.forward_clear)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc


@app.get('/api/servo/base-budget')
def servo_base_budget(authorization: str):
    return servo().reverse_budget(authorization)


@app.post('/api/servo/end')
def servo_end(payload: ServoRefCommand):
    try:
        return servo().end(payload.session_id, payload.reason)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc


@app.post('/api/servo/stop')
def servo_stop(payload: ServoStopCommand | None = None):
    return servo().stop((payload.reason if payload else '') or '页面点击立即停止')


@app.get('/api/servo/status')
def servo_status():
    return servo().snapshot()


@app.get('/api/camera/{camera}/snapshot')
def camera_snapshot(camera: int):
    from fastapi.responses import Response
    if camera not in (0, 1, 2):
        raise HTTPException(404, '相机编号无效')
    try:
        frame = camera_snapshots.latest(camera)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    return Response(frame.jpeg, media_type='image/jpeg', headers={
        'Cache-Control': 'no-store', 'X-Captured-At': str(frame.captured_at),
        'X-Frame-Sequence': str(frame.sequence),
    })


def mjpeg(camera: int):
    cmd = [
        "ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error",
        "-f", "dshow", "-video_device_number", str(camera),
        "-video_size", "640x480", "-framerate", "15",
        "-i", "video=USB2.0_CAM1", "-an", "-c:v", "mjpeg", "-q:v", "5",
        "-fps_mode", "passthrough",
        "-f", "image2pipe", "pipe:1",
    ]
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0)
    except Exception:
        camera_snapshots.release(camera)
        raise
    buffer = bytearray()
    frames = 0
    set_camera(camera, "连接中")
    try:
        while True:
            chunk = proc.stdout.read(8192) if proc.stdout else b""
            if not chunk:
                break
            buffer.extend(chunk)
            while True:
                start = buffer.find(b"\xff\xd8")
                end = buffer.find(b"\xff\xd9", start + 2) if start >= 0 else -1
                if start < 0 or end < 0:
                    if len(buffer) > 2_000_000:
                        buffer.clear()
                    break
                jpeg = bytes(buffer[start:end + 2])
                del buffer[:end + 2]
                frames += 1
                if not camera_snapshots.publish(camera, jpeg, frames):
                    continue
                set_camera(camera, "在线", frames)
                yield b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: " + str(len(jpeg)).encode() + b"\r\n\r\n" + jpeg + b"\r\n"
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            proc.kill()
        set_camera(camera, "离线", frames)
        camera_snapshots.release(camera)


@app.get("/camera/{camera}.mjpg")
def camera(camera: int):
    if camera not in (0, 1, 2):
        return {"error": "camera must be 0, 1 or 2"}
    if not camera_snapshots.claim(camera):
        raise HTTPException(409, '该相机已有采集者；观察请使用共享快照接口')
    return StreamingResponse(mjpeg(camera), media_type="multipart/x-mixed-replace; boundary=frame")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8765, log_level="warning")
