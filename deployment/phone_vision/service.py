"""Studio-owned phone acquisition with isolated workers and expiring frames."""
from __future__ import annotations
from collections import deque
from dataclasses import dataclass
import json
import math
from pathlib import Path
import queue
import socket
import struct
import subprocess
import threading
import time
import uuid

from .protocol import read_message

DEVICES=('iphone','android')
FRESH_SECONDS=1.0


@dataclass(frozen=True)
class Frame:
    session: str
    sequence: int
    received: float
    meta: dict
    rgb: bytes
    depth: bytes
    raw: bytes


class PhoneVision:
    def __init__(self,repo,data_dir,*,clock=time.monotonic,launcher=subprocess.Popen):
        self.repo=Path(repo)
        self.root=Path(data_dir)
        self.clock=clock
        self.launcher=launcher
        self.lock=threading.RLock()
        self.settings_lock=threading.Lock()
        self.android_epoch=0
        self.config_path=self.repo/'.local-deploy/phone-vision.json'
        self.workers={}
        self.states={device:{'enabled':False,'phase':'disabled','reason':'启用后等待手机连接',
                             'session':None,'attempts':0,'frames':deque(maxlen=4)} for device in DEVICES}

    def config(self):
        default=self.repo/'.local-deploy/phone-vision-kit'
        cfg={'python':str(default/'python/python.exe'),'adb':str(default/'tools/platform-tools/adb.exe'),
             'server':str(default/'tools/scrcpy-server'),'camera_id':'0',
             'android_transport':'usb','android_endpoint':'','android_role':'observer','android_rotation':'0','android_mode':'camera',
             'android_cameras':[{'id':'0','label':'后置主摄'}],
             'iphone_transport':'usb','iphone_origin':'','iphone_python':''}
        if self.config_path.is_file():
            value=json.loads(self.config_path.read_text(encoding='utf-8-sig'))
            if not isinstance(value,dict):raise ValueError('Invalid phone vision configuration')
            for key in cfg:
                if key in value:
                    if key!='android_cameras' and (not isinstance(value[key],str) or (not value[key] and key not in ('android_endpoint','iphone_origin','iphone_python'))):raise ValueError('Invalid phone vision configuration')
                    cfg[key]=value[key]
        choices=cfg['android_cameras']
        if not isinstance(choices,list) or not 1<=len(choices)<=16:raise ValueError('Invalid camera choices')
        ids=set()
        for choice in choices:
            if not isinstance(choice,dict) or set(choice)!={'id','label'}:raise ValueError('Invalid camera choice')
            camera,label=choice['id'],choice['label']
            if not isinstance(camera,str) or not camera.isascii() or not camera.isdecimal() or len(camera)>3 or camera in ids:
                raise ValueError('Invalid camera ID')
            if not isinstance(label,str) or not 1<=len(label)<=60:raise ValueError('Invalid camera label')
            ids.add(camera)
        if cfg['android_rotation'] not in ('0','90','180','270'):raise ValueError('Invalid camera rotation')
        if cfg['android_mode'] not in ('camera','arcore'):raise ValueError('Invalid Android mode')
        if cfg['android_transport'] not in ('usb','wifi') or cfg['android_role'] not in ('observer','rear'):
            raise ValueError('Invalid Android camera configuration')
        if cfg['android_transport']=='wifi':
            from .android_network import wifi_endpoint
            wifi_endpoint(cfg['android_endpoint'])
        if cfg['iphone_transport'] not in ('usb','wifi'):raise ValueError('Invalid iPhone transport')
        if cfg['iphone_transport']=='wifi':
            from .iphone_wifi import origin
            cfg['iphone_origin']=origin(cfg['iphone_origin'])
        return cfg

    def status(self):
        cfg={}
        try:
            cfg=self.config()
            readiness={'worker_python':Path(cfg['python']).is_file(),
                       'iphone_worker_python':Path(cfg['iphone_python'] or cfg['python']).is_file(),
                       'android_tools':Path(cfg['adb']).is_file() and Path(cfg['server']).is_file()}
        except (OSError,ValueError):
            readiness={'worker_python':False,'android_tools':False}
        with self.lock:
            devices={}
            for device,state in self.states.items():
                frames=state['frames'];frame=frames[-1] if frames else None
                age=max(0,self.clock()-frame.received) if frame else None
                available=bool(state['enabled'] and state['phase']=='live' and frame and age<=FRESH_SECONDS)
                phase=state['phase']
                reason=state['reason']
                stopping=not state['enabled'] and device in self.workers
                if stopping:
                    phase='stopping';reason='正在退出采集会话，请稍候'
                if frame and phase=='live' and not available:
                    phase='stale';reason='画面已过期，正在等待新的采集帧'
                devices[device]={'enabled':state['enabled'],'phase':phase,'reason':reason,
                                 'transport':cfg.get('android_transport','usb') if device=='android' else cfg.get('iphone_transport','usb'),
                                 'role':cfg.get('android_role','observer') if device=='android' else 'depth',
                                 **({'camera_id':cfg.get('camera_id','0'),'rotation_deg':int(cfg.get('android_rotation','0')),'mode':cfg.get('android_mode','camera'),
                                     'camera_choices':cfg.get('android_cameras',[])} if device=='android' else {}),
                                 'attempts':state['attempts'],'session':state['session'],
                                 'stopping':stopping,
                                 'sequence':frame.sequence if frame else None,'age_ms':round(age*1000,1) if age is not None else None,
                                 'frame_available':available,'meta':frame.meta if available else None}
            return {'host':socket.gethostname(),'devices':devices,'readiness':readiness,
                    'freshness_limit_ms':int(FRESH_SECONDS*1000),'motion_enabled':False,'calibrated':False}

    def start(self,device,*,_settings=False):
        if device not in DEVICES:raise ValueError('Unknown phone')
        cfg=self.config()
        executable=(cfg['iphone_python'] or cfg['python']) if device=='iphone' else cfg['python']
        if not Path(executable).is_file():raise ValueError('手机采集环境未安装，请运行车载安装包')
        if device=='android' and not all(Path(cfg[key]).is_file() for key in ('adb','server')):
            raise ValueError('安卓采集工具未安装，请运行车载安装包')
        with self.lock:
            if device=='android' and self.settings_lock.locked() and not _settings:raise ValueError('镜头正在切换，请稍候')
            if device in self.workers:
                if self.states[device]['enabled']:return self.status()
                raise ValueError('上一采集会话正在退出，请稍后启用')
            stop=threading.Event()
            state=self.states[device]
            state.update(enabled=True,phase='starting',reason='正在连接手机',attempts=0,session=None)
            state['frames'].clear()
            thread=threading.Thread(target=self._supervise,args=(device,cfg,stop),daemon=True,name='phone-'+device)
            self.workers[device]={'stop':stop,'thread':thread,'process':None}
            thread.start()
        return self.status()

    def stop(self,device,*,_settings=False):
        if device not in DEVICES:raise ValueError('Unknown phone')
        with self.lock:
            if device=='android' and not _settings:self.android_epoch+=1
            state=self.states[device]
            state.update(enabled=False,phase='disabled',reason='已停止采集',session=None)
            state['frames'].clear()
            worker=self.workers.get(device)
            if worker:worker['stop'].set()
        return self.status()

    def configure_android(self,camera_id,rotation_deg,mode=None):
        cfg=self.config()
        mode=cfg['android_mode'] if mode is None else mode
        if mode not in ('camera','arcore'):raise ValueError('采集模式无效')
        if mode=='arcore':camera_id='0'
        if camera_id not in [choice['id'] for choice in cfg['android_cameras']]:raise ValueError('只能选择已配置并验证的镜头')
        if type(rotation_deg)!=int or rotation_deg not in (0,90,180,270):raise ValueError('画面方向只能是0、90、180或270度')
        if not self.settings_lock.acquire(blocking=False):raise ValueError('镜头正在切换，请稍候')
        old=None;updated=None;written=False;enabled=False
        try:
            old=self.config_path.read_bytes() if self.config_path.exists() else None
            with self.lock:
                enabled=self.states['android']['enabled']
                epoch=self.android_epoch
                worker=self.workers.get('android')
            self.stop('android',_settings=True)
            if worker:worker['thread'].join(timeout=22)
            with self.lock:
                if 'android' in self.workers:raise ValueError('旧镜头尚未退出，请稍后重试')
            current=self.config_path.read_bytes() if self.config_path.exists() else None
            if current!=old:raise ValueError('配置已被其他操作更新，请刷新')
            value=json.loads(old.decode('utf-8-sig')) if old else {}
            value.update(camera_id=camera_id,android_rotation=str(rotation_deg),android_mode=mode)
            updated=json.dumps(value,ensure_ascii=False,indent=2).encode('utf-8')
            self.config_path.parent.mkdir(parents=True,exist_ok=True)
            if old is not None:
                backup=self.config_path.parent/'phone-vision-backups'/('view-'+uuid.uuid4().hex+'.json')
                backup.parent.mkdir(parents=True,exist_ok=True);backup.write_bytes(old)
            temporary=self.config_path.with_name('phone-view-'+uuid.uuid4().hex+'.pending')
            temporary.write_bytes(updated);temporary.replace(self.config_path);written=True
            with self.lock:
                if enabled and epoch==self.android_epoch:self.start('android',_settings=True)
            return self.status()
        except Exception:
            if written and self.config_path.read_bytes()==updated:
                if old is None:self.config_path.unlink()
                else:self.config_path.write_bytes(old)
            with self.lock:
                if enabled and epoch==self.android_epoch and 'android' not in self.workers:
                    try:self.start('android',_settings=True)
                    except (ValueError,OSError):pass
            raise
        finally:self.settings_lock.release()

    def close(self):
        for device in DEVICES:self.stop(device)
        with self.lock:threads=[value['thread'] for value in self.workers.values()]
        for thread in threads:thread.join(timeout=18)

    def _publish(self,device,session,message,received=None):
        meta,rgb,depth,raw=message
        with self.lock:
            state=self.states[device]
            if not state['enabled'] or state['session']!=session:return
            if meta['kind']=='status':
                phase=meta.get('phase','waiting')
                if phase not in ('starting','waiting','error'):phase='waiting'
                state.update(phase=phase,reason=str(meta.get('reason','等待手机'))[:180])
                if phase!='starting':state['frames'].clear()
                return
            sequence=meta.get('sequence')
            if type(sequence)!=int or sequence<=0:raise ValueError('Invalid frame sequence')
            if state['frames'] and sequence<=state['frames'][-1].sequence:
                raise ValueError('Repeated phone frame sequence')
            if device=='iphone':
                wifi=meta.get('transport')=='wifi' and meta.get('depth_source')=='record3d_hue_video' and meta.get('sensor_raw_depth') is False
                if not raw or (not wifi and meta.get('camera_type')!=0):raise ValueError('TrueDepth or explicitly encoded Wi-Fi depth required')
            received=self.clock() if received is None else received
            acquired=meta.get('host_acquired_monotonic',received)
            if not isinstance(acquired,(int,float)) or not math.isfinite(acquired) or acquired>received+.1:
                raise ValueError('Invalid phone acquisition clock')
            received=min(received,acquired)
            if self.clock()-received>FRESH_SECONDS:
                state['frames'].clear()
                state.update(phase='stale',reason='接收积压已过期，等待新的画面')
                return
            state['frames'].append(Frame(session,sequence,received,meta,rgb,depth,raw))
            reason='正在接收彩色与深度' if device=='iphone' or meta.get('rgbd') else '正在接收后置彩色画面'
            if meta.get('android_mode')=='arcore' and not meta.get('rgbd'):reason=meta.get('depth_unavailable_reason','彩色已连接，等待新的 ARCore 深度')
            state.update(phase='live',reason=reason)

    def frame(self,device,session=None,sequence=None):
        if device not in DEVICES:raise ValueError('Unknown phone')
        with self.lock:
            state=self.states[device]
            if not state['enabled'] or state['phase']!='live':raise LookupError('手机画面未就绪')
            frame=next((f for f in reversed(state['frames']) if (session is None or f.session==session) and
                        (sequence is None or f.sequence==sequence)),None)
            if frame is None or self.clock()-frame.received>FRESH_SECONDS:
                raise LookupError('画面已过期或已进入新会话，请刷新')
            return frame

    def depth_at(self,device,x,y,session,sequence):
        frame=self.frame(device,session,sequence)
        if not frame.raw:raise ValueError('该相机不提供数值深度')
        width,height=frame.meta['width'],frame.meta['height']
        if not 0<=x<width or not 0<=y<height:raise ValueError('像素坐标超出画面')
        value=struct.unpack_from('<f',frame.raw,(y*width+x)*4)[0]
        valid=math.isfinite(value) and value>0
        encoded=frame.meta.get('depth_source')=='record3d_hue_video'
        arcore=frame.meta.get('android_mode')=='arcore'
        return {'session':frame.session,'sequence':frame.sequence,'x':x,'y':y,'depth_m':value if valid else None,
                'numeric_valid':valid,'calibrated':False,'confidence_available':frame.meta.get('confidence_available',False),
                'sensor_raw_depth':frame.meta.get('sensor_raw_depth',not encoded),'depth_source':frame.meta.get('depth_source','usb_float_depth'),
                'note':'ARCore算法深度，尚未做距离误差与机器人坐标标定' if arcore else 'Wi-Fi有损色相编码还原的近似深度，未做距离误差标定' if encoded else '深度原值，尚未做距离误差与机器人坐标标定'}

    def _cleanup_android(self,cfg,session,*,release_camera=True):
        """Recover only this session's ADB resources if a native worker died."""
        scid=f'{int(session[:8],16)&0x7fffffff:08x}'
        target='localabstract:scrcpy_'+scid
        ar_target='localabstract:xlerobot_ar_'+scid
        def adb(*args):
            return subprocess.run([cfg['adb'],*args],capture_output=True,timeout=4,
                                  creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        try:
            if cfg.get('android_mode')=='arcore':
                if release_camera:
                    target_args=['-s',cfg['android_endpoint']] if cfg.get('android_transport')=='wifi' else ['-d']
                    adb(*target_args,'shell','am','force-stop','org.xlerobot.arvision')
            output=adb('forward','--list').stdout.decode('utf-8','replace')
            for line in output.splitlines():
                row=line.split()
                if len(row)==3 and row[2] in (target,ar_target) and row[1].startswith('tcp:'):
                    adb('-s',row[0],'forward','--remove',row[1])
                    if row[2]==target:adb('-s',row[0],'shell','rm','/data/local/tmp/xlerobot-phone-'+scid+'.jar')
        except (OSError,subprocess.SubprocessError):pass

    def _supervise(self,device,cfg,stop):
        attempt=0
        try:
            self.root.mkdir(parents=True,exist_ok=True)
            while not stop.is_set():
                attempt+=1;session=uuid.uuid4().hex
                with self.lock:
                    state=self.states[device]
                    state.update(session=session,attempts=attempt,phase='starting',reason='正在连接手机')
                    state['frames'].clear()
                logfile=self.root/(device+'-worker.log')
                if logfile.exists() and logfile.stat().st_size>1_000_000:
                    logfile.replace(logfile.with_suffix('.previous.log'))
                executable=(cfg['iphone_python'] or cfg['python']) if device=='iphone' else cfg['python']
                args=[executable,str(Path(__file__).with_name('worker.py')),'--device',device,'--session',session]
                if device=='iphone':args+=['--iphone-transport',cfg['iphone_transport'],'--iphone-origin',cfg['iphone_origin']]
                if device=='android':args+=['--adb',cfg['adb'],'--server',cfg['server'],'--camera-id',cfg['camera_id'],
                                           '--android-transport',cfg['android_transport'],'--android-endpoint',cfg['android_endpoint'],
                                           '--android-role',cfg['android_role'],'--android-rotation',cfg['android_rotation']]
                if device=='android':args+=['--android-mode',cfg['android_mode']]
                process=None
                try:
                    with logfile.open('ab') as log:
                        process=self.launcher(args,stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=log,
                                              creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0),bufsize=0)
                        with self.lock:self.workers[device]['process']=process
                        messages=queue.Queue(maxsize=2)
                        def put(value,out=messages):
                            try:out.put_nowait(value)
                            except queue.Full:
                                try:out.get_nowait()
                                except queue.Empty:pass
                                try:out.put_nowait(value)
                                except queue.Full:pass
                        def read(proc=process,emit=put):
                            try:
                                while True:
                                    packet=read_message(proc.stdout)
                                    emit((self.clock(),packet))
                            except Exception as exc:emit(exc)
                        reader=threading.Thread(target=read,daemon=True,name='phone-pipe-'+device)
                        reader.start();last_packet=self.clock()
                        while not stop.is_set():
                            try:message=messages.get(timeout=.25)
                            except queue.Empty:
                                if self.clock()-last_packet>30:raise TimeoutError('Phone worker unresponsive')
                                continue
                            if isinstance(message,Exception):raise message
                            received,message=message
                            last_packet=received
                            self._publish(device,session,message,received)
                except Exception as exc:
                    with logfile.open('ab') as diagnostic:
                        diagnostic.write(('\nStudio pipe: '+type(exc).__name__+': '+str(exc)+'\n').encode('utf-8','replace'))
                    with self.lock:
                        if not stop.is_set():
                            if self.states[device]['phase'] not in ('waiting','error'):
                                self.states[device].update(phase='waiting',reason='连接中断，正在重试；请检查手机授权和采集状态')
                            self.states[device]['frames'].clear()
                finally:
                    if process:
                        try:
                            process.stdin.write(b'stop\n');process.stdin.flush()
                        except (BrokenPipeError,OSError,ValueError):pass
                        try:process.wait(timeout=13)
                        except subprocess.TimeoutExpired:process.kill();process.wait(timeout=3)
                        for pipe in (process.stdin,process.stdout):
                            if pipe:pipe.close()
                    if device=='android':self._cleanup_android(cfg,session,release_camera=stop.is_set())
                    with self.lock:
                        if device in self.workers:self.workers[device]['process']=None
                if not stop.is_set():stop.wait(min(2*attempt,10))
        finally:
            with self.lock:
                self.states[device]['frames'].clear()
                self.states[device].update(enabled=False,phase='disabled',session=None,reason='已停止采集')
                self.workers.pop(device,None)
