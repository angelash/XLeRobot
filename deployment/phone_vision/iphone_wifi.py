"""Record3D local WebRTC receiver. Wi-Fi depth is decoded from lossy hue video.

This is not the USB original sensor float array. Intrinsics follow every received
resolution; unknown camera type and absent confidence remain explicit.
"""
from __future__ import annotations
import asyncio
from collections import deque
import ipaddress
import json
import math
import sys
import time
from urllib.parse import urlsplit
from urllib.request import Request,urlopen


def origin(value):
    parsed=urlsplit(value)
    if parsed.scheme!='http' or parsed.username or parsed.password or parsed.path not in ('','/') or parsed.query or parsed.fragment:
        raise ValueError('需要 Record3D 的局域网 HTTP 地址')
    try:address=ipaddress.IPv4Address(parsed.hostname);port=parsed.port or 80
    except (ValueError,TypeError):raise ValueError('Record3D 地址格式无效') from None
    if not any(address in ipaddress.IPv4Network(n) for n in ('10.0.0.0/8','172.16.0.0/12','192.168.0.0/16')) or not 1<=port<=65535:
        raise ValueError('Record3D 只接受局域网地址')
    return f'http://{address}'+(':'+str(port) if port!=80 else '')


def decode_packed(bgr,metadata):
    import cv2,numpy as np
    if bgr.ndim!=3 or bgr.shape[2]!=3 or bgr.shape[1]%2 or not 2<=bgr.shape[1]<=4096 or not 1<=bgr.shape[0]<=2048:
        raise ValueError('Invalid Record3D packed video shape')
    height,total_width=bgr.shape[:2];width=total_width//2
    k=metadata.get('K');size=metadata.get('originalSize')
    if not isinstance(k,list) or len(k)!=9 or not isinstance(size,list) or len(size)!=2 or not all(isinstance(v,(int,float)) and math.isfinite(v) for v in k+size):
        raise ValueError('Invalid Record3D intrinsics')
    if min(k[0],k[4],*size)<=0 or abs(size[0]/size[1]-width/height)>.02:raise ValueError('Record3D dimensions do not match intrinsics')
    hsv=cv2.cvtColor(bgr[:,:width].astype(np.float32)/255.,cv2.COLOR_BGR2HSV)
    depth=hsv[:,:,0]/360.*3.
    # This rejects hue boundaries and low-saturation pixels; it is not a
    # confidence estimate or a proof that an encoded distance is accurate.
    valid=(hsv[:,:,1]>.25)&(hsv[:,:,2]>.1)&(depth>.1)&(depth<2.8)
    depth=np.where(valid,depth,0).astype('<f4')
    intrinsics={'fx':k[0]*width/size[0],'fy':k[4]*height/size[1],
                'tx':k[6]*width/size[0],'ty':k[7]*height/size[1]}
    return bgr[:,width:].copy(),depth,intrinsics


async def receive(address,stop,publish,status):
    import cv2,numpy as np
    from aiortc import RTCPeerConnection,RTCSessionDescription,RTCConfiguration
    from .android_network import VideoClock
    address=origin(address)
    def diagnostic(message):print('Record3D Wi-Fi: '+message,file=sys.stderr,flush=True)
    def http(path,body=None):
        request=Request(address+path,data=json.dumps(body).encode() if body is not None else None,
                        headers={'Content-Type':'application/json'} if body is not None else {},method='POST' if body is not None else 'GET')
        with urlopen(request,timeout=8) as response:
            raw=response.read(2_000_001)
            if len(raw)>2_000_000:raise ValueError('Record3D signaling response exceeds limit')
            return json.loads(raw) if raw else {}
    metadata=await asyncio.to_thread(http,'/metadata')
    diagnostic('metadata received')
    pc=RTCPeerConnection(RTCConfiguration(iceServers=[]));tasks=[];failure=[];last_frame=time.monotonic()
    sequence=0;last_publish=0.;arrivals=deque(maxlen=90);clock=VideoClock()
    @pc.on('track')
    def track(track):
        if track.kind!='video':return
        async def frames():
            nonlocal sequence,last_publish,last_frame
            try:
                while not stop.is_set():
                    try:frame=await asyncio.wait_for(track.recv(),timeout=12)
                    except asyncio.TimeoutError:raise TimeoutError('Record3D video track delivered no decoded frame for 12 seconds') from None
                    if track.kind!='video':continue
                    now=time.monotonic();last_frame=now;arrivals.append(now)
                    pts_us=round(frame.pts*frame.time_base*1_000_000) if frame.pts is not None else round(now*1_000_000)
                    acquired,lag=clock.observe(pts_us,now)
                    if lag>.5:status('waiting','Wi-Fi 画面积压，正在等待新帧');continue
                    if now-last_publish<.125:continue
                    rgb,depth,intrinsics=decode_packed(frame.to_ndarray(format='bgr24'),metadata)
                    valid=depth>0;scale=np.where(valid,np.clip((depth-.2)/1.3,0,1)*254+1,0).astype(np.uint8)
                    color=cv2.applyColorMap(255-scale,cv2.COLORMAP_TURBO);color[~valid]=0
                    ok,a=cv2.imencode('.jpg',rgb,[cv2.IMWRITE_JPEG_QUALITY,78]);ok2,b=cv2.imencode('.jpg',color,[cv2.IMWRITE_JPEG_QUALITY,78])
                    if not ok or not ok2:raise ValueError('JPEG encoding failed')
                    sequence+=1;last_publish=now
                    meta={'kind':'frame','width':rgb.shape[1],'height':rgb.shape[0],'sequence':sequence,'rgbd':True,
                          'camera_type':None,'transport':'wifi','depth_source':'record3d_hue_video','sensor_raw_depth':False,
                          'confidence_available':False,'calibrated':False,'intrinsics':intrinsics,
                          'host_acquired_monotonic':acquired,'source_pts_us':pts_us,'relative_backlog_ms':round(lag*1000,1),
                          'absolute_latency_verified':False,'positive_depth_fraction':float(valid.mean()),
                          'fps':round((len(arrivals)-1)/(arrivals[-1]-arrivals[0]),2) if len(arrivals)>1 and arrivals[-1]>arrivals[0] else None}
                    publish(meta,a.tobytes(),b.tobytes(),depth.tobytes())
            except asyncio.CancelledError:pass
            except Exception as exc:failure.append(exc)
        tasks.append(asyncio.create_task(frames()))
    @pc.on('connectionstatechange')
    async def connection_state():diagnostic('peer '+pc.connectionState)
    try:
        status('starting','正在连接 Record3D Wi-Fi 视频')
        offer=await asyncio.to_thread(http,'/getOffer')
        diagnostic('offer received')
        await pc.setRemoteDescription(RTCSessionDescription(sdp=offer.get('sdp',offer.get('data')),type=offer['type']))
        for t in pc.getTransceivers():
            if t.kind=='audio':t.direction='inactive'
        await pc.setLocalDescription(await pc.createAnswer())
        diagnostic('local answer gathered')
        await asyncio.to_thread(http,'/answer',{'type':'answer','data':pc.localDescription.sdp})
        diagnostic('answer delivered')
        while not stop.is_set():
            if failure:raise failure[0]
            if pc.connectionState in ('failed','closed') or time.monotonic()-last_frame>16:raise TimeoutError('Record3D Wi-Fi frame timeout')
            await asyncio.sleep(.2)
    finally:
        try:
            stats=await pc.getStats()
            fields=('type','kind','packetsReceived','packetsLost','bytesReceived','dtlsState')
            diagnostic('receive summary '+json.dumps({'published_frames':sequence,'streams':[
                {k:getattr(s,k) for k in fields if hasattr(s,k)} for s in stats.values()
                if s.type in ('inbound-rtp','transport')]}))
        except Exception:pass
        for task in tasks:task.cancel()
        await asyncio.gather(*tasks,return_exceptions=True);await pc.close()
