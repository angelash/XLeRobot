"""Private camera worker; binary pipe only, no listener or robot commands."""
from __future__ import annotations
import argparse
from collections import deque
from fractions import Fraction
import hashlib
import json
import os
from pathlib import Path
import secrets
import socket
import subprocess
import sys
import threading
import time

for name in ('OPENBLAS_NUM_THREADS','OMP_NUM_THREADS','MKL_NUM_THREADS'):
    os.environ[name]='1'

sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
from deployment.phone_vision.protocol import write_message


def dependencies():
    import av,cv2,numpy,record3d
    return {'python':sys.version.split()[0],'av':av.__version__,'opencv':cv2.__version__,
            'numpy':numpy.__version__,'record3d':__import__('importlib.metadata').metadata.version('record3d')}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--device',choices=('android','iphone'))
    parser.add_argument('--adb')
    parser.add_argument('--server')
    parser.add_argument('--camera-id',default='0')
    parser.add_argument('--android-transport',choices=('usb','wifi'),default='usb')
    parser.add_argument('--android-endpoint',default='')
    parser.add_argument('--android-role',choices=('observer','rear'),default='observer')
    parser.add_argument('--session')
    parser.add_argument('--check',action='store_true')
    args=parser.parse_args()
    if args.check:
        print(json.dumps(dependencies()))
        return
    # Complete native imports before a background pipe reader is started.
    # Windows CRT/DLL startup and blocked stdin must not race during import.
    import cv2,numpy
    if args.device=='iphone':import record3d
    else:import av
    # Keep native library printf output out of the framed binary pipe.
    wire=os.fdopen(os.dup(sys.stdout.fileno()),'wb',buffering=0)
    os.dup2(sys.stderr.fileno(),sys.stdout.fileno())
    stop=threading.Event()
    def commands():
        try:
            pending=b''
            while True:
                # A buffered reader held by a daemon thread aborts CPython at
                # shutdown when a missing phone makes the main worker return.
                chunk=os.read(sys.stdin.fileno(),256)
                if not chunk:
                    stop.set();return
                pending+=chunk
                while b'\n' in pending:
                    line,pending=pending.split(b'\n',1)
                    if line.strip()==b'stop':stop.set();return
                if len(pending)>1024:stop.set();return
        except OSError:
            stop.set()
    threading.Thread(target=commands,daemon=True).start()
    def status(phase,reason):
        write_message(wire,{'kind':'status','phase':phase,'reason':reason})
    status('starting','正在检查手机连接')
    try:
        if args.device=='iphone':iphone(wire,stop,status)
        else:android(args,wire,stop,status)
    except (BrokenPipeError,EOFError):
        return
    except Exception as exc:
        # Do not copy device identifiers or app logs into the browser.
        print(type(exc).__name__+': '+str(exc),file=sys.stderr,flush=True)
        status('waiting','连接未就绪或已中断，请检查 USB 连接与手机授权')
        raise SystemExit(2)


def iphone(wire,stop,status):
    import cv2,numpy as np,record3d
    stream=record3d.Record3DStream()
    lock=threading.Lock();ready=threading.Event();latest={};times=deque(maxlen=120)
    def frame():
        try:
            rgb=np.asarray(stream.get_rgb_frame()).copy()
            depth=np.asarray(stream.get_depth_frame()).copy()
            intrinsics=stream.get_intrinsic_mat()
            meta={'intrinsics':{k:float(getattr(intrinsics,k)) for k in ('fx','fy','tx','ty')},
                  'camera_type':int(stream.get_device_type()),
                  'confidence_available':bool(np.asarray(stream.get_confidence_frame()).size)}
            with lock:
                arrived=time.monotonic();times.append(arrived);latest.update(rgb=rgb,depth=depth,meta=meta,received=arrived)
            ready.set()
        except Exception:
            stop.set()
    stream.on_new_frame=frame;stream.on_stream_stopped=stop.set
    devices=stream.get_connected_devices()
    if len(devices)!=1:
        status('waiting','未找到唯一的 iPhone，请接上 USB、解锁并信任这台电脑')
        return
    if not stream.connect(devices[0]):
        status('waiting','请打开 Record3D，开启 USB Streaming 并开始前置采集')
        return
    sequence=0;last_publish=0.;last_notice=0.;last_received=time.monotonic()
    try:
        while not stop.is_set():
            if not ready.wait(.2):
                now=time.monotonic()
                if now-last_notice>1:
                    status('waiting','请在 Record3D 点开始采集，保持前置 TrueDepth 和 App 前台')
                    last_notice=now
                if now-last_received>12:
                    return
                continue
            ready.clear();now=time.monotonic();last_received=now
            if now-last_publish<.125:
                continue
            with lock:
                values=latest.copy();arrivals=list(times)
            rgb,depth,meta=values['rgb'],values['depth'],values['meta']
            if meta['camera_type']!=0:
                status('waiting','当前不是前置 TrueDepth，请切换镜头');return
            if rgb.ndim!=3 or depth.ndim!=2 or rgb.shape[:2]!=depth.shape:
                status('error','彩色与深度尺寸不一致，暂不提供测距');return
            valid=np.isfinite(depth)&(depth>0)
            image=np.zeros(depth.shape,dtype=np.uint8)
            image[valid]=np.round(np.clip((depth[valid]-.2)/1.3,0,1)*254+1).astype(np.uint8)
            color=cv2.applyColorMap(255-image,cv2.COLORMAP_TURBO);color[~valid]=0
            ok,rgb_jpeg=cv2.imencode('.jpg',cv2.cvtColor(rgb,cv2.COLOR_RGB2BGR),[cv2.IMWRITE_JPEG_QUALITY,78])
            ok2,depth_jpeg=cv2.imencode('.jpg',color,[cv2.IMWRITE_JPEG_QUALITY,78])
            if not ok or not ok2:raise ValueError('JPEG encoding failed')
            sequence+=1
            meta.update(kind='frame',width=int(rgb.shape[1]),height=int(rgb.shape[0]),sequence=sequence,
                        host_acquired_monotonic=values['received'],
                        rgbd=True,positive_depth_fraction=float(valid.mean()),depth_unit='m',
                        depth_display_m=[.2,1.5],depth_aligned='unverified',calibrated=False,
                        fps=round((len(arrivals)-1)/(arrivals[-1]-arrivals[0]),2) if len(arrivals)>1 and arrivals[-1]>arrivals[0] else None)
            write_message(wire,meta,rgb_jpeg.tobytes(),depth_jpeg.tobytes(),np.asarray(depth,dtype='<f4').tobytes())
            last_publish=now
    finally:
        stream.disconnect()


def android(args,wire,stop,status):
    import av,cv2
    from deployment.phone_vision.android_usb import receive_exact,receive_packet
    from deployment.phone_vision.android_network import select_device,wifi_endpoint,VideoClock
    if not args.adb or not args.server or not Path(args.adb).is_file() or not Path(args.server).is_file():
        status('error','手机采集工具尚未安装，请运行车载安装包');return
    if not args.camera_id.isascii() or not args.camera_id.isdecimal():raise ValueError('Invalid camera ID')
    def adb(*values):
        return subprocess.run([args.adb,*values],capture_output=True,check=True,timeout=8,
                              creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0)).stdout.decode('utf-8','replace').strip()
    transport=getattr(args,'android_transport','usb');endpoint=getattr(args,'android_endpoint','')
    if transport=='wifi':
        try:adb('connect',wifi_endpoint(endpoint))
        except subprocess.CalledProcessError:pass  # Report the exact authorization state below.
    rows=[row.split() for row in adb('devices').splitlines()[1:] if row.strip()]
    serial,reason=select_device(rows,transport,endpoint)
    if serial is None:status('waiting',reason);return
    if args.session and (len(args.session)!=32 or any(c not in '0123456789abcdef' for c in args.session)):
        raise ValueError('Invalid worker session')
    scid=f'{int(args.session[:8],16)&0x7fffffff:08x}' if args.session else f'{secrets.randbits(31):08x}'
    remote='/data/local/tmp/xlerobot-phone-'+scid+'.jar'
    channel=None;process=None;port=None
    try:
        adb('-s',serial,'push',args.server,remote)
        port=int(adb('-s',serial,'forward','tcp:0','localabstract:scrcpy_'+scid))
        options=['5.0.1','scid='+scid,'video_source=camera','camera_id='+args.camera_id,
                 'camera_size=1280x720','camera_fps=15','video_codec=h264','video_bit_rate=4000000',
                 'audio=false','control=false','tunnel_forward=true','cleanup=false',
                 'send_device_meta=false','send_stream_meta=false','send_dummy_byte=true','send_frame_meta=true']
        process=subprocess.Popen([args.adb,'-s',serial,'shell','CLASSPATH='+remote,'app_process','/',
                                  'com.genymobile.scrcpy.Server',*options],stdout=sys.stderr,stderr=sys.stderr,
                                  creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        deadline=time.monotonic()+8
        while not stop.is_set():
            try:
                channel=socket.create_connection(('127.0.0.1',port),timeout=2)
                if receive_exact(channel,1)!=b'\0':raise ValueError('Invalid scrcpy handshake')
                break
            except (ConnectionRefusedError,EOFError):
                if channel:channel.close();channel=None
                if time.monotonic()>deadline:raise TimeoutError('Camera handshake timed out')
                stop.wait(.15)
        if stop.is_set():return
        channel.settimeout(2)
        decoder=av.CodecContext.create('h264','r');config=b'';sequence=0;previous_pts=None
        last_publish=0.;arrivals=deque(maxlen=60);video_clock=VideoClock();last_backlog_notice=0.
        while not stop.is_set():
            pts,is_config,raw=receive_packet(channel)
            if pts is None:continue
            if is_config:config+=raw;continue
            if previous_pts is not None and pts<=previous_pts:raise ValueError('Non-monotonic camera timestamp')
            previous_pts=pts
            packet=av.Packet(config+raw);config=b'';packet.pts=packet.dts=pts;packet.time_base=Fraction(1,1000000)
            for frame in decoder.decode(packet):
                now=time.monotonic();arrivals.append(now)
                acquired,lag=video_clock.observe(frame.pts if frame.pts is not None else pts,now)
                if lag>.5:
                    if now-last_backlog_notice>1:
                        status('waiting','画面传输积压，正在等待新的后视画面');last_backlog_notice=now
                    continue
                if now-last_publish<.125:continue
                bgr=frame.to_ndarray(format='bgr24')
                ok,jpeg=cv2.imencode('.jpg',bgr,[cv2.IMWRITE_JPEG_QUALITY,78])
                if not ok:raise ValueError('JPEG encoding failed')
                sequence+=1
                meta={'kind':'frame','width':frame.width,'height':frame.height,'sequence':sequence,'rgbd':False,
                      'host_acquired_monotonic':acquired,'relative_backlog_ms':round(lag*1000,1),
                      'absolute_latency_verified':False,'transport':transport,'role':getattr(args,'android_role','observer'),
                      'source_pts_us':pts,'calibrated':False,'fps':round((len(arrivals)-1)/(arrivals[-1]-arrivals[0]),2)
                      if len(arrivals)>1 and arrivals[-1]>arrivals[0] else None}
                write_message(wire,meta,jpeg.tobytes());last_publish=now
    finally:
        if channel:channel.close()
        if process:
            try:process.wait(timeout=4)
            except subprocess.TimeoutExpired:
                process.terminate();process.wait(timeout=2)
        if port is not None:
            try:adb('-s',serial,'forward','--remove','tcp:'+str(port))
            except (OSError,subprocess.SubprocessError):pass
        try:adb('-s',serial,'shell','rm',remote)
        except (OSError,subprocess.SubprocessError):pass


if __name__=='__main__':main()
